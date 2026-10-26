"""Task lifecycle: durable intent first, live evidence before every guard."""
import copy
import json
from pathlib import Path
import re
import uuid

from . import attachments
from . import captain
from .common import BossError, refuse, require_text
from .events import status_line
from .integrations import GitHub, PR_PATTERN, github_repository, inspect_worktree, remove_worktree, resolve_repository
from .mcp import MCP
from .preferences import compose_brief, notes_path, project_name, source_note, steering_sections, steers_path
from .state import FINISHED as TERMINAL, now


SLUG_CHARS = 40
STATUS_FORMS = ("BOSS: done: <PR url or one-line report> / BOSS: needs-decision: <question> / "
                "BOSS: blocked: <why> / BOSS: paused: <what you wait on> until <UTC time>")
MARK = "[boss] "  # Firstmate's from-firstmate marker: the supervisor speaking, never the captain
# A steer written "rule: ..." is a standing rule for every brief; "rule scout: ..." or
# "rule direct-PR: ..." only for briefs of that kind or delivery.
STANDING = re.compile(r"rule(?:\s+(ship|scout|direct-PR|local-only))?:\s*(\S.*)", re.I | re.S)
# The pr-description shape, in order. A no-mistakes appendix supplies the last two.
PR_SECTIONS = ("Intent", "What changed", "Risk Assessment", "Testing")
ATTRIBUTION = re.compile(r"Written by \S.* in \S.* on behalf of @\S+")


def slug(title):
    """'Fix: the Ledger lock!' -> 'fix-the-ledger-lock'; lowercase a-z0-9 and hyphens, whole words when cut."""
    text = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    if len(text) <= SLUG_CHARS:
        return text
    cut = text[:SLUG_CHARS + 1].rpartition("-")[0]
    return cut or text[:SLUG_CHARS]


def standing_rule(steer):
    """'rule scout: Cite files' -> ('scout', 'Cite files'); 'rule: X' -> (None, 'X'); any other steer -> None."""
    match = STANDING.match(steer["text"])
    return match and (match[1] and match[1].lower(), match[2].strip())


def footer(kind, delivery, *, origin=False, test_host=None, standing=(), standing_source=None):
    rules = [
        "### boss worker contract",
        "Work only in this assigned worktree. Never change the primary checkout or another task's worktree.",
        "Stay on the assigned branch. Never rename it or switch to another branch.",
        "Commit as the repository's configured Git user. Never change Git identity or add AI co-author trailers or agent credit.",
        "Never merge. Never deploy. Report through this thread to boss.",
        "Any question, decision or blocker MUST go through your harness's interactive question tool, which raises a T3 input request "
        "(AskUserQuestion in Claude Code, request_user_input in Codex). Never ask in plain text. "
        "After asking, stop: take no further action until the answer arrives, and end the turn if the tool returns without one.",
        "Never end your turn to wait for something your shell can watch (tests, CI, a stack booting). "
        "Wait inside ONE blocking command, such as `until <condition>; do sleep 30; done` or `gh pr checks <pr> --watch`, "
        "with your shell tool's largest timeout, and reissue the same command if it times out. "
        "Never sleep and then check in a separate step, and never background a job to poll it. "
        "Use paused only for a wait you cannot watch yourself, and name until <UTC time>. "
        "Hitting the same obstacle twice is a blocker: ask through the question tool and end with blocked.",
        "End EVERY turn with exactly one status line as the last line of your final message, with nothing after it: "
        f"{STATUS_FORMS}. boss reads only that line to learn how the turn ended; a turn without one is reported as unclear.",
        "Ask a question through the question tool first; the needs-decision line repeats it. "
        "After needs-decision or blocked, end the turn at once.",
        "A message that starts with [boss] is from the boss supervisor, not the captain. "
        "Act on it, then end that turn with a status line as usual.",
    ]
    if kind == "scout":
        rules.append("Scout: investigate and write a report. Do not open a PR. Leave the worktree clean. "
                     "Your final message is the complete report, which boss saves in its ledger, "
                     "ending with BOSS: done: <one-line summary>.")
    elif delivery == "local-only":
        rules.append("Ship, local-only: implement, verify and commit on this branch. Do not push or open a PR. "
                     "Stop with a clean branch; your final message describes the changes and verification "
                     "and ends with BOSS: done: <one-line report>. "
                     "The captain must arrange landing separately; boss never merges local branches.")
    else:
        rules.append("Ship, direct-PR: implement, verify, commit and push this branch. Open exactly ONE PR against the origin default branch, "
                     "link it to this thread with link_pull_request, then stop. Put the verification in your final message "
                     "and end it with BOSS: done: <PR url>.")
    if test_host:
        rules.append(f"Run heavy tests (full suites, browsers, app stacks) on {test_host}, not on this host.")
    if kind != "scout":
        rules.append("If the change touches UI, put before and after screenshots of the real page in the PR or final report.")
    matching = [text for scope, text in standing if scope in (None, kind, delivery.lower())]
    if matching:
        rules.append("Standing rules from the captain. Follow every one:")
        if origin:
            rules.append(source_note(standing_source, True).rstrip())
        rules.extend(f"- {text}" for text in matching)
    if origin:
        rules.insert(1, source_note(Path(__file__), True).rstrip())
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

    def resolve_project(self, name, host=None, overrides=None):
        settings, _ = self.config.effective(name, {**(overrides or {}), **({"host": host} if host else {})})
        host_name = settings["host"]
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
                     "delivery": "direct-PR", "no_mistakes": False, **settings}
        effective["auto_merge"] = effective["merge"] == "auto"
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

    def worker_brief(self, name, brief="<task>", *, worker=None, settings=None, goal=None, steers=None, origin=False):
        if settings is None:
            settings, _ = self.config.effective(name, {"worker": worker})
        steers = self.store.steers(name) if steers is None else steers
        # Standing rules come from the steer store on every brief and move into the contract.
        standing = [rule for rule in map(standing_rule, steers) if rule]
        contract = footer(settings["kind"], settings["delivery"], origin=origin, test_host=settings.get("test_host"),
                          standing=standing, standing_source=steers_path(self.store.home, name))
        return compose_brief(brief, self.store.home, name, contract, self.redact,
                             goal=self.store.goal(name) if goal is None else goal,
                             steers=[s for s in steers if not standing_rule(s)], origin=origin)

    def spawn(self, project_name, title, brief, kind=None, host=None, model=None, effort=None, fits=None, worker=None, override=None,
              images=()):
        require_text(title, "title", 300)
        require_text(brief, "task brief", 115000)
        images = attachments.load_images(images)
        if override is not None:
            require_text(override, "--override", 2000)
        active = len(self.store.tasks(active=True))
        if active >= self.config.max_active:
            refuse(f"{active} workers are active and the cap is workers.max_active = {self.config.max_active}",
                   "settle finished workers first, or raise the cap: boss config set workers.max_active <n>")
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
        overrides = {k: v for k, v in {"kind": kind, "worker": worker, "effort": effort}.items() if v is not None}
        project, info, source = self.resolve_project(project_name, host, overrides)
        kind = project["kind"]
        host_name = project["host"]
        client = self.client(host_name)
        repo = project["repository"]
        delivery = project["delivery"]
        message = self.worker_brief(project_name, brief, settings=project, goal=goal, steers=steers)
        base = self.github.default_branch(repo)
        selection = self.model_selection(client, info, project.get("model_selection"), model or project.get("model"), effort or project.get("effort"))
        _, origins = self.config.effective(project_name, {**overrides, **({"host": host} if host else {})})
        violations = self.config.violations(project, origins, f"{selection['instanceId']}:{selection['model']}" if selection else None)
        if violations and override is None:
            allowed, kinds = project.get("rules", {}).get(kind + "_models"), project.get("kinds")
            fixes = []
            if kinds is not None and kind not in kinds:
                fixes.append(f"--kind {kinds[0]} or another --worker" if kinds else "another --worker")
            if allowed and len(violations) > len(fixes):
                fixes.append(f"--model {allowed[0]}")
            raise BossError("refused", "spawn breaks the captain's worker rules",
                            "retry with " + " / ".join(fixes + ['--override "<captain\'s exact words>"']), 2, context=violations)
        if override is not None and not violations:
            raise BossError("usage", "--override applies only when a worker rule refuses the spawn",
                            "retry boss spawn without --override", 1)
        # Images ride on the launch itself, so the worker's first turn sees them with the brief.
        # Upload before any ledger entry: a failed upload leaves no half-launched task behind.
        uploaded = attachments.upload(client, images) if images else None
        task_id = self.store.new_id()
        branch = self.free_branch(repo, slug(title) or task_id)
        meta = {"id": task_id, "project": project_name, "project_id": project["project_id"],
                "host": host_name, "repository": repo, "title": title, "kind": kind,
                "delivery": delivery, "branch": branch, "base": base,
                "model_selection": selection, "thread_id": None, "settled": False,
                "status": "launching", "merge": project["merge"], "auto_merge": project["auto_merge"],
                "no_mistakes": project["no_mistakes"], "test_host": project.get("test_host"),
                "worker": project.get("worker"), "project_source": source, "fits": fits.strip() if fits is not None else None}
        if violations:
            meta["override"] = {"approved": override, "violations": violations}
        self.store.text(task_id, "brief.md", message)
        self.store.save(meta, "launch_intent", **({"images": attachments.journal(images)} if images else {}))
        try:
            launched = client.call("t3_thread_launch", projectId=project["project_id"],
                                   title=f"[boss:{task_id}] {title}", message=message,
                                   workspaceStrategy={"type": "worktree", "baseRef": base,
                                                      "branch": meta["branch"], "startFromOrigin": True},
                                   **({"modelSelection": selection} if selection else {}),
                                   **({"attachments": uploaded} if uploaded else {}))
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

    def free_branch(self, repository, name):
        """boss/<name>, or boss/<name>-2, -3... when origin or the ledger already has it."""
        taken = {m.get("branch") for m in self.store.tasks()} | self.github.branches(repository, "boss/" + name)
        branch, number = "boss/" + name, 2
        while branch in taken:
            branch, number = f"boss/{name}-{number}", number + 1
        return branch

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
        # T3 lists only question requests, but pendingRequestCount counts every pending
        # runtime request; the rest are permission prompts (approvals).
        approvals = max(0, (thread.get("pendingRequestCount") or 0) - len(requests))
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
        # A report is explicit, complete, terminal, and belongs to the latest run: the final
        # message above its done line (or the one-line report alone). BOSS_REPORT: is the
        # older form, still read for workers briefed before status lines.
        report = ""
        if text.lstrip().startswith("BOSS_REPORT:"):
            report = text.partition("BOSS_REPORT:")[2].strip()
        elif (status_line(text) or ("",))[0] == "done":
            body = text.rstrip().rpartition("\n")[0].strip()
            report = body or status_line(text)[1]
        if status == "completed" and last and last.get("runId") == thread.get("latestRunId") and report:
            self.store.text(meta["id"], "report.md", report + "\n")
            meta["report_run_id"] = thread["latestRunId"]
        alerts = []
        label = {"completed": "finished", "failed": "failed", "interrupted": "interrupted",
                 "cancelled": "interrupted", "rolled_back": "failed"}.get(status)
        if label:
            alerts.append(f"{meta['id']}: {label}")
        if requests:
            alerts.append(f"{meta['id']}: waiting for input requests=" + ",".join(requests))
        if approvals:
            alerts.append(f"{meta['id']}: waiting for {approvals} permission decisions in T3")
        for pr in prs:
            if pr["state"] == "OPEN":
                alerts.append(f"{meta['id']}: PR opened {pr['url']}")
                alerts.append(f"{meta['id']}: CI changed {pr['url']} {ci_text(pr['ci'])}")
        observed = {"status": status, "run_id": thread.get("latestRunId"), "requests": requests, "approvals": approvals,
                    "prs": [{"url": p["url"], "state": p["state"], "head": p["headRefOid"], "ci": p["ci"]} for p in prs],
                    "alerts": alerts}
        if observed != meta.get("observed") or urls != meta.get("pr_urls", []):
            meta.update(observed=observed, status=status, pr_urls=urls)
            self.store.save(meta, "observed")
        return {"thread": thread, "status": status, "requests": requests, "approvals": approvals, "prs": prs,
                "assistant": text, "assistant_run": last.get("runId") if last else None, "alerts": alerts}

    def questions(self, meta, request_ids):
        """The question bodies (question text and options) of pending T3 input requests."""
        client = self.client(meta["host"])
        return [question for request_id in request_ids
                for question in client.call("t3_pending_request_read", threadId=meta["thread_id"],
                                            requestId=request_id)["questions"]]

    def reviewed(self, meta, view):
        """Reading one task acknowledges its finished or failed run, never merge authority."""
        before = (meta.get("reviewed_run_id"), meta.get("pending_inputs"))
        meta["pending_inputs"] = view["requests"]
        if view["status"] in TERMINAL:
            meta["reviewed_run_id"] = view["thread"].get("latestRunId")
        if before != (meta.get("reviewed_run_id"), meta.get("pending_inputs")):
            meta["handled_at"] = now()  # inbox items about what was just reviewed are stale
            self.store.save(meta, "reviewed")

    def active_task(self, task_id):
        meta = self.store.load(task_id)
        if meta.get("settled"):
            refuse(f"task {task_id} is settled", f"boss status {task_id}")
        return meta

    def send(self, task_id, text, images=()):
        require_text(text, "message")
        images = attachments.load_images(images)
        meta = self.active_task(task_id)
        self.recover(meta)
        client = self.client(meta["host"])
        request_id = uuid.uuid4().hex
        message = text if text.startswith(MARK.strip()) else MARK + text
        self.store.save(meta, "send_intent", client_request_id=request_id, text=message,
                        **({"images": attachments.journal(images)} if images else {}))
        if images:
            # t3_thread_send_attachments has no retry key: on a transport error, boss status before resending.
            result = client.call("t3_thread_send_attachments", threadId=meta["thread_id"], message=message,
                                 attachments=attachments.upload(client, images))
        else:
            result = client.call("t3_thread_send", threadId=meta["thread_id"], message=message,
                                 clientRequestId=request_id)
        meta.pop("report_run_id", None)
        meta["handled_at"] = now()
        if meta.get("status") in TERMINAL:
            meta["reviewed_run_id"] = (meta.get("observed") or {}).get("run_id")
        self.store.save(meta, "sent", client_request_id=request_id, result=result)

    def answer(self, task_id, request_id, text):
        require_text(text, "answer")
        meta = self.active_task(task_id)
        self.recover(meta)
        client = self.client(meta["host"])
        pending = client.call("t3_pending_request_list", threadId=meta["thread_id"])["requestIds"]
        request_id, question_id = resolve_request(request_id, pending)
        if request_id is None:
            refuse("request is not pending on this task", f"boss status {task_id}")
        request = client.call("t3_pending_request_read", threadId=meta["thread_id"], requestId=request_id)
        questions = request["questions"]
        if question_id is not None and question_id not in {q["id"] for q in questions}:
            refuse("question is not part of this pending request", f"boss status {task_id}")
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
        if meta.get("observed"):
            meta["observed"]["requests"] = [r for r in meta["observed"].get("requests", []) if r != request_id]
        meta["handled_at"] = now()
        self.store.save(meta, "answered", request_id=request_id, result=result)

    def stop(self, task_id):
        meta = self.active_task(task_id)
        self.recover(meta)
        self.store.save(meta, "interrupt_intent")
        result = self.client(meta["host"]).call("t3_thread_interrupt", threadId=meta["thread_id"])
        self.store.save(meta, "interrupt_requested", result=result)

    def land(self, task_id, approved):
        if not approved or not approved.strip():
            # An approval he already gave, here or in a check-in, is never asked for again.
            stored = captain.approval(self.store, task_id, "merge")
            if stored is None or stored.get("verdict") != "yes":
                refuse("landing requires the captain's words" +
                       (f"; he answered: {captain.plain(stored['resolution'], 200)}" if stored else ""),
                       f'boss land {task_id} --approved "<quote>"')
            approved = stored["resolution"]
        meta = self.active_task(task_id)
        if meta.get("merge", "auto" if meta.get("auto_merge") else "approved") == "never":
            refuse("project merge policy is never", "arrange landing outside boss")
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
        fences = [s for s in self.store.steers(meta["project"]) if s.get("fence") and s.get("task") in (None, task_id)]
        if fences:
            raise BossError("refused", "the captain fenced this until he lifts it",
                            f"boss steer drop {meta['project']} <steer id> only when he lifts it", 2,
                            context=[f"{s['id']}: {s['text']}" for s in fences])
        if (pr["state"] != "OPEN" or pr["isDraft"] or pr["ci"]["state"] != "green" or
                pr["mergeStateStatus"] != "CLEAN" or pr.get("reviewDecision") == "CHANGES_REQUESTED"):
            refuse(f"PR is not ready: state={pr['state']} ci={pr['ci']['state']} merge={pr['mergeStateStatus']}",
                   f"boss status {task_id}")
        project, _ = self.config.effective(meta["project"])
        problems = pr_body_problems(pr.get("body") or "", project["pr_body_forbidden"])
        if problems:
            raise BossError("refused", "PR body does not follow the pr-description format",
                            f"boss send {task_id} asking the worker to fix the PR body with gh pr edit --body-file, then land again",
                            2, context=problems)
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
        """Settle the thread, then remove its worktree if its work is landed or reported."""
        meta = self.store.load(task_id)
        if meta.get("settled"):
            return meta.get("worktree")
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
                    if evidence.get("gone"):
                        self.verify_landed_without_worktree(meta, snapshot["prs"])
                    else:
                        blockers += self.worktree_blockers(meta, evidence, snapshot["prs"])
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
        return self.clean_worktree(meta, snapshot["thread"].get("worktreePath"), snapshot["prs"])

    @staticmethod
    def worktree_blockers(meta, evidence, prs):
        """Reasons the worktree on the host still holds work that has not landed."""
        blockers = []
        merged = next((p for p in prs
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
        return blockers

    def verify_landed_without_worktree(self, meta, prs):
        """A missing worktree proves nothing; only a merged PR on GitHub shows the work landed."""
        for pr in prs:
            if (pr["state"] != "MERGED" or not pr.get("mergeCommit") or pr.get("baseRefName") != meta["base"] or
                    PR_PATTERN.fullmatch(pr["url"])[1].lower() != meta["repository"].lower()):
                continue
            # A branch still on origin must not carry commits past the merged head.
            if all(self.github.branch_head(meta["repository"], b) in (None, pr["headRefOid"])
                   for b in {meta["branch"], pr["headRefName"]}):
                # Keep the captain's approval when boss landed it.
                meta["landed"] = {"url": pr["url"], "head": pr["headRefOid"], "merge": pr["mergeCommit"],
                                  "verified": "worktree already gone; PR merged", **(meta.get("landed") or {})}
                return
        refuse("worktree is gone and no merged PR found",
               f'boss settle {meta["id"]} --discard-approved "<quote>" only with explicit discard authority')

    def clean_worktree(self, meta, path, prs):
        """Remove a settled task's worktree and local branch on its host, never unsaved work."""
        if meta.get("scratch") or not path:
            return None
        finished = [p for p in prs if p["state"] in ("MERGED", "CLOSED") and
                    PR_PATTERN.fullmatch(p["url"])[1].lower() == meta["repository"].lower()]
        if not finished and meta["kind"] != "scout" and meta["delivery"] != "local-only":
            result = {"state": "kept", "reason": "no merged or closed PR"}
        else:
            try:
                result = remove_worktree(self.config.hosts[meta["host"]], path, meta["base"],
                                         {meta["branch"], *(p["headRefName"] for p in finished)},
                                         {p["headRefOid"] for p in finished})
            except BossError as exc:
                result = {"state": "kept", "reason": f"cleanup failed: {exc.message}"}
        result = {"path": path, **result}
        if result != meta.get("worktree"):
            meta["worktree"] = result
            self.store.save(meta, "worktree_" + result["state"])
        return result

    def prune_landed(self):
        """Clean every settled task's leftover worktree, on every host, in one pass."""
        results = []
        for meta in self.store.tasks():
            if not meta.get("settled") or meta.get("scratch") or \
                    (meta.get("worktree") or {}).get("state") in ("removed", "gone"):
                continue
            try:
                path = (meta.get("worktree") or {}).get("path")
                if not path:
                    thread = self.client(meta["host"]).call("t3_thread_read", threadId=meta["thread_id"],
                                                            limit=1, runLimit=1)["thread"]
                    if thread["projectId"] != meta["project_id"]:
                        refuse("thread project differs from the task ledger", f"inspect task {meta['id']}")
                    path = thread.get("worktreePath")
                prs = [self.github.view(url) for url in meta.get("pr_urls", [])]
                result = self.clean_worktree(meta, path, prs)
            except BossError as exc:
                result = {"state": "error", "reason": exc.message}
            if result is not None:
                results.append((meta, result))
        return results


def pr_body_problems(body, forbidden):
    """What keeps a PR body from the pr-description shape; empty when it's fine."""
    headings = [(m.start(), m[1].strip()) for m in re.finditer(r"^#{2,3}[ \t]+(.+?)[ \t#]*$", body, re.M)]
    problems, places = [], []
    for name in PR_SECTIONS:
        found = [at for at, text in headings if text.lower() == name.lower()]
        if len(found) != 1:
            problems.append(f"needs exactly one '## {name}' section, found {len(found)}")
        places.append(found[0] if len(found) == 1 else None)
    known = [at for at in places if at is not None]
    if known != sorted(known):
        problems.append("sections must come in order: " + ", ".join(PR_SECTIONS))
    if places[0] is not None:
        end = min((at for at, _ in headings if at > places[0]), default=len(body))
        lines = [line.strip() for line in body[places[0]:end].splitlines()[1:] if line.strip()]
        if not lines or not ATTRIBUTION.fullmatch(lines[-1]):
            problems.append("Intent must end with 'Written by <model> in <harness> on behalf of @<owner>'")
        elif len(lines) < 2:
            problems.append("Intent needs a sentence on what changed and why before the Written by line")
    # Code spans and blocks may name files and commands; the prose speaks as the owner, about the change.
    prose = re.sub(r"```.*?```|`[^`\n]*`", " ", body, flags=re.S)
    for word in forbidden:
        if re.search(r"\b" + r"\s+".join(map(re.escape, word.split())) + r"\b", prose, re.I):
            problems.append(f"mentions '{word}'; write about the change, not the conversation behind it")
    return problems


def ci_text(ci):
    return f"{ci['state']} {ci['passed']} passed,{ci['failed']} failed,{ci['pending']} pending,{ci['skipped']} skipped"


def normalize_options(options):
    """T3 echoes options as [{"id", "value"}]; boss sends a {id: value} table."""
    if isinstance(options, list):
        return {o["id"]: o["value"] for o in options if isinstance(o, dict) and "id" in o}
    return dict(options or {})


def resolve_request(given, pending):
    """Map what the captain typed to (request id, question id or None).

    status prints `<request id>/<question id>` and T3 question ids can be the
    question text itself, slashes and quotes included, so match against the
    pending ids instead of splitting. A unique prefix of a pending id also works."""
    if given in pending:
        return given, None
    owners = [r for r in pending if given.startswith(f"{r}/")]
    if owners:
        request_id = max(owners, key=len)
        return request_id, given[len(request_id) + 1:]
    prefixed = [r for r in pending if r.startswith(given)] if given else []
    return (prefixed[0], None) if len(prefixed) == 1 else (None, None)
