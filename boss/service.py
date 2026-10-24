"""Task lifecycle: durable intent first, live evidence before every guard."""
import copy
import json
import uuid

from .common import BossError, compact, refuse, require_text
from .integrations import GitHub, PR_PATTERN, github_repository, inspect_worktree, resolve_repository
from .mcp import MCP
from .preferences import compose_brief, notes_path, project_name, steering_sections


TERMINAL = {"completed", "failed", "interrupted", "cancelled", "rolled_back", "idle"}


def footer(kind, delivery):
    rules = [
        "## boss worker contract",
        "Work only in this assigned worktree. Never change the primary checkout or another task's worktree.",
        "Stay on the assigned branch. Never rename it or switch to another branch.",
        "Commit as the repository's configured Git user. Never change Git identity or add AI co-author trailers or agent credit.",
        "Never merge. Never deploy. Report through this thread to boss.",
    ]
    if kind == "scout":
        rules.append("Scout: investigate and write a report. Do not open a PR. Leave the worktree clean. "
                     "Your final assistant message must start with BOSS_REPORT: followed by the complete report; boss saves it in its ledger.")
    elif delivery == "local-only":
        rules.append("Ship, local-only: implement, verify and commit on this branch. Do not push or open a PR. "
                     "Stop with a clean branch and a final BOSS_REPORT: message describing changes and verification. "
                     "The captain must arrange landing separately; boss never merges local branches.")
    else:
        rules.append("Ship, direct-PR: implement, verify, commit and push this branch. Open exactly ONE PR against the origin default branch, "
                     "link it to this thread with link_pull_request, then stop. Include the PR URL and verification in your final reply.")
    return "\n\n" + "\n".join(rules) + "\n"


class Boss:
    def __init__(self, config, store, redact, github=None):
        self.config, self.store, self.redact = config, store, redact
        self.github = github or GitHub()
        self.clients = {}

    def client(self, host_name):
        if host_name not in self.clients:
            host, token = self.config.host(host_name)
            self.clients[host_name] = MCP(host["url"], token, self.redact)
        return self.clients[host_name]

    def model_selection(self, client, project, configured, model, effort):
        selection = copy.deepcopy(configured or project.get("defaultModelSelection") or {})
        if model:
            if ":" in model:
                instance, model = model.split(":", 1)
                selection = {"instanceId": instance, "model": model}
            else:
                if selection.get("model") != model:
                    selection.pop("options", None)
                selection["model"] = model
        if not selection:
            if effort:
                raise BossError("usage", "--effort requires a model or a configured default selection", exit_code=1)
            return None
        catalog = client.call("orchestrator_capabilities", search=selection["model"])
        matches = [(p, m) for p in catalog["providers"] for m in p["models"]
                   if m["id"] == selection["model"] and
                   (not selection.get("instanceId") or p["providerInstanceId"] == selection["instanceId"])]
        if len(matches) != 1:
            raise BossError("model", "model is unavailable or ambiguous", "use --model providerInstanceId:model")
        provider, descriptor = matches[0]
        selection["instanceId"] = provider["providerInstanceId"]
        options = selection.setdefault("options", {})
        if options is None or options == []:
            options = selection["options"] = {}
        if not isinstance(options, dict):
            raise BossError("model", "model options must be a table", exit_code=1)
        descriptors = {o["id"]: o for o in descriptor.get("options", [])}
        if effort:
            key = next((k for k in ("reasoningEffort", "effort") if k in descriptors), None)
            if not key:
                raise BossError("model", "selected model has no effort option", "omit --effort or choose another model", 1)
            options[key] = effort
        for key, value in options.items():
            if key not in descriptors or value not in descriptors[key]["values"]:
                raise BossError("model", f"unsupported {key}={value}", "choose an option from the live T3 catalog", 1)
        return selection

    def project_catalog(self, host_name):
        client = self.client(host_name)
        projects, seen, cursor = [], set(), None
        while True:
            page = client.call("t3_project_list", limit=100, **({"cursor": cursor} if cursor is not None else {}))
            projects.extend(page["projects"])
            next_cursor = page.get("nextCursor")
            if next_cursor is None:
                return sorted(projects, key=lambda p: (p["title"], p["id"]))
            if next_cursor in seen:
                raise BossError("protocol", "t3_project_list repeated its cursor")
            seen.add(next_cursor)
            cursor = next_cursor

    def lookup_title(self, name, host_name):
        projects = self.project_catalog(host_name)
        matches = [p for p in projects if p["title"] == name]
        if len(matches) != 1:
            candidates = "; ".join(f"{p['title']} ({p['id']})" for p in (matches or projects)) or "0 projects"
            refuse(f"{len(matches)} exact title matches for {name} on {host_name}; candidates: {candidates}",
                   f"boss project list --host {host_name}")
        return matches[0]

    def resolve_project(self, name, host=None):
        settings = self.config.project(name)
        host_name = host or settings.get("host") or self.config.default_host
        if settings.get("project_id"):
            info = next((p for p in self.project_catalog(host_name) if p["id"] == settings["project_id"]), None)
            if info is None:
                refuse("configured project_id does not exist on selected host", f"boss project list --host {host_name}")
            source = "registry"
        else:
            info = self.lookup_title(name, host_name)
            source = "registry + T3 lookup" if name in self.config.projects else "T3 lookup"
        # Registry values are authoritative. T3's nullable repositoryIdentity
        # is not needed: discovery reads origin on the actual project host.
        repository = settings.get("repository") or resolve_repository(self.config.hosts[host_name], info.get("workspaceRoot"))
        effective = {"host": host_name, "project_id": info["id"], "repository": repository,
                     "delivery": "direct-PR", "auto_merge": False, "no_mistakes": False, **settings}
        effective["host"] = host_name
        effective["model_selection"] = settings.get("model_selection") or info.get("defaultModelSelection")
        return effective, info, source

    def project_list(self, host=None):
        host_name = host or self.config.default_host
        rows = []
        registry = {name: self.config.project(name) for name in self.config.projects}
        for info in self.project_catalog(host_name):
            registered = []
            for name, settings in registry.items():
                if (settings.get("host") or self.config.default_host) != host_name:
                    continue
                matches = settings["project_id"] == info["id"] if settings.get("project_id") else name == info["title"]
                if matches:
                    registered.append(name)
            names = set(registered + [info["title"]])
            has_notes = False
            for name in names:
                try:
                    has_notes |= notes_path(self.store.home, name).is_file()
                except BossError:
                    # A title containing '/' can be listed, but cannot name a notes file.
                    continue
            rows.append({"id": info["id"], "name": info["title"], "registry": bool(registered), "notes": has_notes})
        return host_name, rows

    def project_add(self, name, host=None, **preferences):
        project_name(name)
        if name in self.config.projects:
            refuse(f"project {name} already has a registry entry", f"boss project show {name}")
        host_name = host or self.config.default_host
        info = self.lookup_title(name, host_name)
        repository = resolve_repository(self.config.hosts[host_name], info.get("workspaceRoot"))
        settings = {"host": host_name, "project_id": info["id"], "repository": repository}
        settings.update({k: v for k, v in preferences.items() if v is not None})
        if "test_host" in settings:
            require_text(settings["test_host"], "test_host", 200)
        self.config.add_project(name, settings)
        return settings

    def spawn(self, project_name, title, brief, kind="ship", host=None, model=None, effort=None, fits=None):
        require_text(title, "title", 300)
        require_text(brief, "task brief", 115000)
        goal = self.store.goal(project_name)
        steers = self.store.steers(project_name)
        context = steering_sections(goal, steers, self.redact)
        if context and (fits is None or not fits.strip()):
            raise BossError("refused", "reconcile this task against the project goal and active captain steers",
                            'retry boss spawn with --fits "<one line explaining how the task fits>"', 2,
                            context=[text.strip() for _, text in context])
        if fits is not None:
            require_text(fits, "--fits", 2000)
            if len(fits.splitlines()) != 1:
                raise BossError("usage", "--fits must be one line", "boss spawn --help", 1)
        project, info, source = self.resolve_project(project_name, host)
        host_name = project["host"]
        client = self.client(host_name)
        repo = project["repository"]
        delivery = project["delivery"]
        # Render only settings actually supplied, rather than injecting defaults
        # into the worker's preference layer. auto_merge stays supervisor-only.
        configured = self.config.project(project_name)
        message = compose_brief(brief, self.store.home, project_name, configured, footer(kind, delivery), self.redact,
                                goal=goal, steers=steers)
        base = self.github.default_branch(repo)
        selection = self.model_selection(client, info, project.get("model_selection"), model, effort)
        task_id = self.store.new_id()
        meta = {"id": task_id, "project": project_name, "project_id": project["project_id"],
                "host": host_name, "repository": repo, "title": title, "kind": kind,
                "delivery": delivery, "branch": "boss/" + task_id, "base": base,
                "model_selection": selection, "thread_id": None, "settled": False,
                "status": "launching", "auto_merge": project["auto_merge"],
                "no_mistakes": project["no_mistakes"], "test_host": project.get("test_host"),
                "project_source": source, "fits": fits.strip() if fits is not None else None}
        self.store.text(task_id, "brief.md", message)
        self.store.save(meta, "launch_intent")
        try:
            launched = client.call("t3_thread_launch", projectId=project["project_id"],
                                   title=f"[boss:{task_id}] {title}", message=message,
                                   workspaceStrategy={"type": "worktree", "baseRef": base,
                                                      "branch": meta["branch"], "startFromOrigin": True},
                                   **({"modelSelection": selection} if selection else {}))
        except BossError as exc:
            meta["status"] = "launch_unknown"
            self.store.save(meta, "launch_unknown")
            raise BossError(exc.code, f"task {task_id}: {exc.message}",
                            f"boss status {task_id}; do not respawn until reconciled", exc.exit_code) from None
        meta.update(thread_id=launched["threadId"], status=launched.get("status") or "preparing")
        self.store.save(meta, "launched")
        if selection:
            actual = launched.get("modelSelection") or {}
            if any(actual.get(k) != selection.get(k) for k in ("model", "instanceId")) or \
                    normalize_options(actual.get("options")) != normalize_options(selection.get("options")):
                client.call("t3_thread_interrupt", threadId=meta["thread_id"])
                raise BossError("model", f"task {task_id}: T3 returned different model settings; interruption requested",
                                f"boss status {task_id}")
        return meta

    def recover(self, meta):
        if meta.get("thread_id"):
            return
        page = self.client(meta["host"]).call("t3_thread_list", projectId=meta["project_id"],
                                             titleContains=f"[boss:{meta['id']}]", limit=100)
        matches = [t for t in page["threads"] if f"[boss:{meta['id']}]" in t["title"]]
        if len(matches) != 1 or page.get("nextCursor") is not None:
            refuse(f"task {meta['id']} has an unresolved launch; {len(matches)} matching threads",
                   f"inspect T3 project {meta['project_id']}; never retry launch blindly")
        meta["thread_id"] = matches[0]["threadId"]
        self.store.save(meta, "launch_recovered")

    def read_messages(self, client, thread_id):
        items, cursor = [], None
        while True:
            page = client.call("t3_thread_read", threadId=thread_id, limit=100, maxCharsPerItem=50000,
                               runLimit=1, **({"afterPosition": cursor} if cursor is not None else {}))
            items.extend(page["items"])
            if not page.get("hasMore"):
                break
            next_cursor = page.get("nextPosition")
            if next_cursor is None or next_cursor == cursor:
                raise BossError("protocol", "t3_thread_read did not advance")
            cursor = next_cursor
        return page, items

    def snapshot(self, meta):
        self.recover(meta)
        client = self.client(meta["host"])
        page, items = self.read_messages(client, meta["thread_id"])
        thread = page["thread"]
        if thread["projectId"] != meta["project_id"]:
            refuse("thread project differs from the task ledger", f"inspect task {meta['id']}")
        status = thread["status"]
        requests = sorted(client.call("t3_pending_request_list", threadId=meta["thread_id"])["requestIds"])
        linked = client.call("list_thread_pull_requests", threadId=meta["thread_id"])["pullRequests"]
        # Retain prior links so unlinking cannot make an open PR disappear from guards.
        urls = sorted(set(p["url"] for p in linked) | set(meta.get("pr_urls", [])))
        prs = [self.github.view(url) for url in urls]
        assistant = [i for i in items if i.get("type") in ("assistant_message", "assistant") and i.get("text")]
        # T3 timeline type is usually assistant_message; createdBy disambiguates older versions.
        if not assistant:
            assistant = [i for i in items if i.get("createdBy") == "agent" and i.get("type") == "message" and i.get("text")]
        last = assistant[-1] if assistant else None
        text = last["text"] if last else ""
        if last and last.get("textTruncated"):
            offset = last.get("nextTextOffset")
            while offset is not None:
                chunk = client.call("t3_thread_read", threadId=meta["thread_id"], itemId=last["itemId"],
                                    textOffset=offset, maxCharsPerItem=50000, runLimit=1)
                item = chunk["items"][0]
                text += item.get("text") or ""
                next_offset = item.get("nextTextOffset")
                if next_offset == offset:
                    raise BossError("protocol", "assistant text pagination did not advance")
                offset = next_offset
        # A report is explicit, complete, terminal, and belongs to the latest run.
        report = text.partition("BOSS_REPORT:")[2].strip() if text.lstrip().startswith("BOSS_REPORT:") else ""
        if status == "completed" and last and last.get("runId") == thread.get("latestRunId") and report:
            self.store.text(meta["id"], "report.md", report + "\n")
            meta["report_run_id"] = thread["latestRunId"]
        alerts = []
        label = {"completed": "finished", "failed": "failed", "interrupted": "interrupted",
                 "cancelled": "interrupted", "rolled_back": "failed"}.get(status)
        if label:
            alerts.append(f"{meta['id']}: {label}")
        if requests or status == "waiting":
            alerts.append(f"{meta['id']}: waiting for input" + (" requests=" + ",".join(requests) if requests else ""))
        for pr in prs:
            if pr["state"] == "OPEN":
                alerts.append(f"{meta['id']}: PR opened {pr['url']}")
                alerts.append(f"{meta['id']}: CI changed {pr['url']} {ci_text(pr['ci'])}")
        observed = {"status": status, "run_id": thread.get("latestRunId"), "requests": requests,
                    "prs": [{"url": p["url"], "state": p["state"], "head": p["headRefOid"], "ci": p["ci"]} for p in prs],
                    "alerts": alerts}
        if observed != meta.get("observed") or urls != meta.get("pr_urls", []):
            meta.update(observed=observed, status=status, pr_urls=urls)
            self.store.save(meta, "observed")
        return {"thread": thread, "status": status, "requests": requests, "prs": prs,
                "assistant": text, "alerts": alerts}

    def poll(self, meta):
        view = self.snapshot(meta)
        observed = meta["observed"]
        changed = observed != meta.get("poll_observed")
        if changed or meta.get("pending_inputs") != view["requests"]:
            meta.update(poll_observed=observed, pending_inputs=view["requests"])
            self.store.save(meta, "polled")
        if not changed:
            return []
        return [line for line in view["alerts"]
                if not (line == f"{meta['id']}: finished" and meta.get("reviewed_run_id") == observed.get("run_id"))]

    def reviewed(self, meta, view):
        """Reading one task acknowledges its completed run, never merge authority."""
        before = (meta.get("reviewed_run_id"), meta.get("pending_inputs"))
        meta["pending_inputs"] = view["requests"]
        if view["status"] == "completed":
            meta["reviewed_run_id"] = view["thread"].get("latestRunId")
        if before != (meta.get("reviewed_run_id"), meta.get("pending_inputs")):
            self.store.save(meta, "reviewed")

    def active_task(self, task_id):
        meta = self.store.load(task_id)
        if meta.get("settled"):
            refuse(f"task {task_id} is settled", f"boss status {task_id}")
        return meta

    def send(self, task_id, text):
        require_text(text, "message")
        meta = self.active_task(task_id)
        self.recover(meta)
        client = self.client(meta["host"])
        request_id = uuid.uuid4().hex
        self.store.save(meta, "send_intent", client_request_id=request_id, text=text)
        result = client.call("t3_thread_send", threadId=meta["thread_id"], message=text,
                             clientRequestId=request_id)
        meta.pop("report_run_id", None)
        if meta.get("status") == "completed":
            meta["reviewed_run_id"] = (meta.get("observed") or {}).get("run_id")
        self.store.save(meta, "sent", client_request_id=request_id, result=result)

    def answer(self, task_id, request_id, text):
        require_text(text, "answer")
        meta = self.active_task(task_id)
        self.recover(meta)
        client = self.client(meta["host"])
        pending = client.call("t3_pending_request_list", threadId=meta["thread_id"])["requestIds"]
        if request_id not in pending:
            refuse("request is not pending on this task", f"boss status {task_id}")
        request = client.call("t3_pending_request_read", threadId=meta["thread_id"], requestId=request_id)
        questions = request["questions"]
        if len(questions) == 1:
            answers = {questions[0]["id"]: text}
        else:
            try:
                answers = json.loads(text)
            except ValueError:
                raise BossError("usage", "multi-question requests need a JSON object keyed by question id", exit_code=1) from None
            if not isinstance(answers, dict) or set(answers) != {q["id"] for q in questions}:
                raise BossError("usage", "answer keys must match all question ids", exit_code=1)
        self.store.save(meta, "answer_intent", request_id=request_id, answers=answers)
        result = client.call("t3_pending_request_respond", threadId=meta["thread_id"], requestId=request_id, answers=answers)
        meta["pending_inputs"] = [r for r in meta.get("pending_inputs", []) if r != request_id]
        self.store.save(meta, "answered", request_id=request_id, result=result)

    def stop(self, task_id):
        meta = self.active_task(task_id)
        self.recover(meta)
        self.store.save(meta, "interrupt_intent")
        result = self.client(meta["host"]).call("t3_thread_interrupt", threadId=meta["thread_id"])
        self.store.save(meta, "interrupt_requested", result=result)

    def land(self, task_id, approved):
        if not approved or not approved.strip():
            refuse("landing requires the captain's words", f'boss land {task_id} --approved "<quote>"')
        meta = self.active_task(task_id)
        snapshot = self.snapshot(meta)
        if meta["kind"] != "ship" or meta["delivery"] != "direct-PR":
            refuse("only direct-PR ship tasks can land with boss", "arrange an explicitly approved local landing separately")
        if snapshot["status"] not in TERMINAL or snapshot["requests"]:
            refuse("worker must stop before landing", f"boss stop {task_id}")
        if len(snapshot["prs"]) != 1:
            refuse("landing requires exactly one linked PR", f"boss status {task_id}")
        pr = snapshot["prs"][0]
        self.check_pr_identity(meta, pr)
        if pr["state"] == "MERGED":
            intent = meta.get("land_intent", {})
            if intent.get("head") == pr["headRefOid"]:
                meta["landed"] = {"url": pr["url"], "head": pr["headRefOid"], "merge": pr.get("mergeCommit"), "approved": intent["approved"]}
                meta["reviewed_run_id"] = snapshot["thread"].get("latestRunId")
                self.store.save(meta, "land_reconciled")
                return
            refuse("PR already merged outside this landing", f"boss settle {task_id}")
        if (pr["state"] != "OPEN" or pr["isDraft"] or pr["ci"]["state"] != "green" or
                pr["mergeStateStatus"] != "CLEAN" or pr.get("reviewDecision") == "CHANGES_REQUESTED"):
            refuse(f"PR is not ready: state={pr['state']} ci={pr['ci']['state']} merge={pr['mergeStateStatus']}",
                   f"boss status {task_id}")
        meta["land_intent"] = {"url": pr["url"], "head": pr["headRefOid"], "approved": approved}
        self.store.save(meta, "land_intent")
        self.github.merge(pr)
        after = self.github.view(pr["url"])
        if after["state"] != "MERGED" or after["headRefOid"] != pr["headRefOid"] or not after.get("mergeCommit"):
            raise BossError("github", "merge not confirmed for the approved head", f"boss status {task_id}")
        meta["landed"] = {**meta["land_intent"], "merge": after["mergeCommit"]}
        meta["reviewed_run_id"] = snapshot["thread"].get("latestRunId")
        self.store.save(meta, "landed")

    @staticmethod
    def check_pr_identity(meta, pr):
        match = PR_PATTERN.fullmatch(pr["url"])
        if (not match or match[1].lower() != meta["repository"].lower() or
                pr["headRefName"] != meta["branch"] or pr.get("baseRefName") != meta["base"]):
            refuse("PR repository or branches differ from the task", f"boss status {meta['id']}")

    def settle(self, task_id, discard=None):
        meta = self.store.load(task_id)
        if meta.get("settled"):
            return
        snapshot = self.snapshot(meta)
        if snapshot["status"] not in TERMINAL or snapshot["requests"]:
            refuse("worker must stop and resolve inputs before settlement", f"boss stop {task_id}")
        blockers = []
        if any(p["state"] == "OPEN" for p in snapshot["prs"]):
            blockers.append("open PR")
        needs_report = meta["kind"] == "scout" or meta["delivery"] == "local-only"
        report_path = self.store.directory(task_id) / "report.md"
        if needs_report and (not report_path.exists() or not report_path.read_text().strip() or
                             meta.get("report_run_id") != snapshot["thread"].get("latestRunId")):
            blockers.append("no report for latest run")
        if not discard or not discard.strip():
            if not meta.get("scratch"):
                path = snapshot["thread"].get("worktreePath")
                if not path:
                    blockers.append("no verified worktree")
                else:
                    try:
                        evidence = inspect_worktree(self.config.hosts[meta["host"]], path, meta["branch"], meta["base"])
                    except BossError as exc:
                        refuse(f"cannot verify unlanded work: {exc.message}",
                               f'boss settle {task_id} --discard-approved "<quote>" only with explicit discard authority')
                    merged = next((p for p in snapshot["prs"]
                                   if p["state"] == "MERGED" and p["headRefOid"] == evidence["head"] and
                                   PR_PATTERN.fullmatch(p["url"])[1].lower() == meta["repository"].lower() and
                                   p.get("baseRefName") == meta["base"]), None)
                    branch = merged["headRefName"] if merged else meta["branch"]
                    if evidence["branch"] != branch:
                        blockers.append("worktree branch mismatch")
                    actual_repo = github_repository({"repositoryIdentity": {"locator": {"remoteUrl": evidence["origin"]}}})
                    if not actual_repo or actual_repo.lower() != meta["repository"].lower():
                        blockers.append("worktree origin differs from the task")
                    if evidence["dirty"]:
                        blockers.append("uncommitted work")
                    if not evidence["in_default"] and not merged:
                        if not evidence["pushed"]:
                            blockers.append("unpushed branch")
                        blockers.append("branch is not landed")
            if blockers:
                refuse("unlanded work: " + ", ".join(blockers),
                       f'boss settle {task_id} --discard-approved "<quote>" only with explicit discard authority')
        self.store.save(meta, "settle_intent", discard_approved=discard, blockers=blockers)
        client = self.client(meta["host"])
        for pr in snapshot["prs"]:
            client.call("unwatch_pull_request", threadId=meta["thread_id"], url=pr["url"])
        result = client.call("t3_thread_organize", threadId=meta["thread_id"], action="settle")
        if result.get("settlesWhenTurnEnds"):
            raise BossError("t3", "T3 deferred settlement until turn end", f"boss status {task_id}")
        verify = client.call("t3_thread_read", threadId=meta["thread_id"], limit=1, runLimit=1)
        if not verify["thread"].get("settled"):
            raise BossError("t3", "T3 did not confirm settlement", f"boss status {task_id}")
        meta.update(settled=True, discard_approved=discard)
        self.store.save(meta, "settled")


def ci_text(ci):
    return f"{ci['state']} {ci['passed']} passed,{ci['failed']} failed,{ci['pending']} pending,{ci['skipped']} skipped"


def normalize_options(options):
    """T3 echoes options as [{"id", "value"}]; boss sends a {id: value} table."""
    if isinstance(options, list):
        return {o["id"]: o["value"] for o in options if isinstance(o, dict) and "id" in o}
    return dict(options or {})
