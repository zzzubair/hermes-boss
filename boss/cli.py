"""Content-first command line with stable, terse records."""
import argparse
import copy
import json
import os
from pathlib import Path
import sys
import tomllib

from .setup import initialize, doctor

from . import __version__
from . import captain
from . import owner
from .common import BossError, Redactor, compact, refuse, require_text
from .config import Config, boss_home, flatten, display_key, key_parts, literal
from .preferences import goal_path, notes_path
from .service import Boss, ci_text
from .state import Store, now


class Parser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("allow_abbrev", False)
        super().__init__(*args, **kwargs)

    def error(self, message):
        raise BossError("usage", message, f"{self.prog} --help", 1)


def parser():
    root = Parser(prog="boss", description="Run and supervise T3 coding workers. Bare boss shows the live fleet.",
                  epilog="Exit: 0 ok; 1 usage/config; 2 guard refusal; 3 transport/T3/external error.")
    root.add_argument("--version", action="version", version="boss " + __version__)
    root.add_argument("--full", action="store_true", help="show complete text")
    commands = root.add_subparsers(dest="command")
    init = commands.add_parser("init", help="detect local T3 and save a minimal config; non-interactive")
    init.add_argument("--url", help="MCP URL; default http://127.0.0.1:3773/mcp")
    init.add_argument("--name", default="local", help="host name to save")
    init.add_argument("--token-env", default="T3_MCP_TOKEN")
    init.add_argument("--force", action="store_true", help="update the named host and default in an existing config")
    commands.add_parser("doctor", help="check installation, host access, owner and the wake listener")
    host = commands.add_parser("host", help="manage explicit MCP endpoints")
    hosts = host.add_subparsers(dest="host_command", required=True)
    host_add = hosts.add_parser("add")
    host_add.add_argument("name")
    host_add.add_argument("--url", required=True)
    host_add.add_argument("--token-env", default="T3_MCP_TOKEN")
    host_add.add_argument("--ssh")
    hosts.add_parser("list")
    for action in ("remove", "test"):
        hosts.add_parser(action).add_argument("name")
    config = commands.add_parser("config", help="inspect or edit strict TOML settings")
    configs = config.add_subparsers(dest="config_command", required=True)
    show = configs.add_parser("show")
    show.add_argument("--origin", action="store_true")
    show.add_argument("--project", help="apply this project's layer")
    for flag in ("host", "worker", "model", "effort", "kind", "merge", "delivery"):
        choices = {"kind": ("ship", "scout"), "merge": ("never", "approved", "auto"), "delivery": ("direct-PR", "local-only")}
        show.add_argument("--" + flag, choices=choices.get(flag))
    for action in ("get", "set", "unset"):
        item = configs.add_parser(action)
        item.add_argument("key")
        if action == "set":
            item.add_argument("value", help="TOML literal, or bare text for a string")
    spawn = commands.add_parser("spawn", help="launch one isolated coding worker")
    spawn.add_argument("project")
    spawn.add_argument("--title", required=True)
    spawn.add_argument("--brief-file", metavar="FILE", help="UTF-8 brief; omitted or '-' reads piped stdin")
    spawn.add_argument("--kind", choices=("ship", "scout"))
    spawn.add_argument("--host", help="registry host override; project_id must exist there")
    spawn.add_argument("--worker", help="named worker preset")
    spawn.add_argument("--model", help="model id or providerInstanceId:model")
    spawn.add_argument("--effort", help="exact effort value supported by that model")
    spawn.add_argument("--fits", metavar="TEXT", help="one line reconciling this task with the project goal and active steers")
    spawn.add_argument("--override", metavar="QUOTE", help="captain's exact words letting a rule-refused spawn through; saved on the task")
    brief = commands.add_parser("brief", help="preview worker context and contract with a <task> placeholder; read-only")
    brief.add_argument("project")
    brief.add_argument("--worker", help="named worker preset")
    brief.add_argument("--origin", action="store_true", help="annotate sections with their source files")
    status = commands.add_parser("status", help="live fleet or one task, pending questions and PR checks")
    status.add_argument("id", nargs="?")
    status.add_argument("--full", action="store_true", default=argparse.SUPPRESS, help="show complete text")
    send = commands.add_parser("send", help="send a durable message to a worker")
    send.add_argument("id")
    send.add_argument("text")
    answer = commands.add_parser("answer", help="answer a pending input request; cannot approve permissions")
    answer.add_argument("id")
    answer.add_argument("request")
    answer.add_argument("text", help="answer text; multi-question requests take a JSON object keyed by question id")
    stop = commands.add_parser("stop", help="interrupt a worker without removing work")
    stop.add_argument("id")
    land = commands.add_parser("land", help="merge one green PR with explicit captain approval")
    land.add_argument("id")
    land.add_argument("--approved", metavar="QUOTE", help="captain's exact words; required by guard")
    settle = commands.add_parser("settle", help="unwatch PRs and settle a terminal thread; preserve its worktree")
    settle.add_argument("id")
    settle.add_argument("--discard-approved", metavar="QUOTE", help="captain's explicit authority to leave work unlanded")
    project = commands.add_parser("project", help="discover T3 projects and inspect or save preferences")
    projects = project.add_subparsers(dest="project_command", required=True)
    project_list = projects.add_parser("list", help="list T3 projects with registry/notes markers")
    project_list.add_argument("--host", help="override default_host")
    project_add = projects.add_parser("add", help="append a discovered project to config.toml")
    project_add.add_argument("name")
    project_add.add_argument("--host", help="override default_host")
    project_add.add_argument("--delivery", choices=("direct-PR", "local-only"))
    project_add.add_argument("--merge", choices=("never", "approved", "auto"))
    project_add.add_argument("--worker")
    project_add.add_argument("--auto-merge", action="store_true", default=None, help="supervisor preference only; workers never merge")
    project_add.add_argument("--test-host", help="preferred host for tests; supervisor reference")
    project_add.add_argument("--no-mistakes", action="store_true", default=None, help="save no_mistakes: true for the supervisor")
    project_show = projects.add_parser("show", help="show effective settings and preference sources")
    project_show.add_argument("name")
    goal = commands.add_parser("goal", help="show or set a project's goal")
    goal.add_argument("project")
    goal.add_argument("text", nargs="?", help="new goal; omit to show the current goal")
    steer = commands.add_parser("steer", help="append, list or drop a project's captain steers")
    steer.add_argument("project", help="project name, or 'list' / 'drop'")
    steer.add_argument("text", help="steer text, or the project name after 'list' / 'drop'")
    steer.add_argument("steer_id", nargs="?", help="with 'drop', the active steer to retract")
    steer.add_argument("--task", help="related task from this project")
    steer.add_argument("--replaces", metavar="STEER_ID", help="supersede an earlier steer in this project")
    steer.add_argument("--all", action="store_true", help="with 'list', include superseded/dropped steers and tombstones")
    away = commands.add_parser("away", help="mark the captain away; only away/back change presence")
    away.add_argument("note", nargs="*", help="optional note, e.g. going to bed")
    commands.add_parser("back", help="mark the captain back; plain catch-up timeline and numbered decisions")
    own = commands.add_parser("own", help="open boss in this Hermes session (HERMES_SESSION_KEY); it gets wakes")
    own.add_argument("status", nargs="?", choices=("status",), help="show the owner; read-only")
    own.add_argument("--take", action="store_true", help="move ownership here from another session")
    own.add_argument("--label", help="human name for this session, e.g. 'Discord DM'")
    commands.add_parser("release", help="leave boss with no owner; wakes are held on disk")
    log = commands.add_parser("log", help="record what the captain said or decided in a check-in")
    log.add_argument("text")
    decide = commands.add_parser("decide", help="record decisions that need the captain")
    decisions = decide.add_subparsers(dest="decision_command", required=True)
    add = decisions.add_parser("add", help="open a durable decision")
    add.add_argument("text")
    add.add_argument("--task", help="related task id")
    listing = decisions.add_parser("list", help="list open captain decisions with ids")
    listing.add_argument("--full", action="store_true", default=argparse.SUPPRESS, help="show complete text")
    ask = decisions.add_parser("ask", help="number open decisions 1..N for the captain and record the round")
    ask.add_argument("--requested", metavar="QUOTE", help="while away, only when the captain asked for the list")
    close = decisions.add_parser("close", help="record the captain's resolution; repeat is safe")
    close.add_argument("id", help="decision id, or number from the latest ask round")
    close.add_argument("text")
    close.add_argument("--round", type=int, help="ask round the captain answered; older rounds are refused")
    unclear = decisions.add_parser("unclear", help="re-ask a decision next round with what was unclear")
    unclear.add_argument("id", help="decision id, or number from the latest ask round")
    unclear.add_argument("text", help="what was unclear about his answer")
    unclear.add_argument("--round", type=int, help="ask round the captain answered; older rounds are refused")
    return root


def rule_lines(values, origins):
    """Effective deterministic rules, for the supervisor only."""
    lines = [f"kinds: {literal(values['kinds'])} origin={origins[('kinds',)]}"] if "kinds" in values else []
    return lines + [f"rules.{key}: {literal(allowed)} origin={origins[('rules', key)]}"
                    for key, allowed in sorted(values.get("rules", {}).items())]


def decision_lines(store, full=False):
    opened = captain.open_decisions(store)
    counts = captain.asked_counts(store.captain_events())
    lines = [f"{len(opened)} open decisions"]
    for d in opened:
        lines.append(f"{d['id']}: {compact(d['text'], None if full else 180)}" +
                     (f" task={d['task']}" if d.get("task") else "") +
                     (f" asked={counts[d['id']]}" if counts.get(d["id"]) else ""))
    return lines


def fleet(boss, full=False):
    tasks = boss.store.tasks(active=True)
    executable = str(Path(sys.argv[0]).absolute())
    home = str(Path.home())
    if executable.startswith(home + "/"):
        executable = "~" + executable[len(home):]
    events = boss.store.captain_events()
    away = captain.presence(events)
    lines = [f"bin: {compact(executable)}", "description: Supervise T3 coding workers.",
             "captain: " + (f"away {captain.since_text(away)}; don't ask him, anything he decides counts" if away else "here"),
             f"{len(tasks)} active tasks"]
    errors = []
    counts = {}
    for meta in tasks:
        try:
            view = boss.snapshot(meta)
            state = view["status"]
            counts[state] = counts.get(state, 0) + 1
        except BossError as exc:
            state = "unavailable"
            errors.append(exc)
        lines.append(f"{meta['id']}: {meta['project']} {state} {compact(meta['title'], None if full else 80)} auto_merge={str(meta.get('auto_merge', False)).lower()}")
    if counts:
        lines.append("states: " + " ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    view = captain.numbered_view(boss.store, events, full)
    lines.extend(view)
    if errors:
        for exc in errors:
            lines.append(f"error: {exc.code} {compact(boss.redact(exc.message))}")
        lines.append("next: check host credentials/connectivity; boss status <id>")
    elif tasks:
        lines.append("next: boss status <id>")
    elif len(view) > 1:
        lines.append("next: queue decisions for /back; record his check-in words with boss log" if away else
                     "next: boss decide ask" if any(line.endswith("(not asked yet)") for line in view) else
                     'next: boss decide close <n> "<captain words>"')
    elif not boss.config.hosts:
        lines.append("next: boss init")
    else:
        lines.append('next: boss spawn <project> --title "<task>" --brief-file <file>')
    return lines, max((e.exit_code for e in errors), default=0)


def mutating(args):
    """Commands that change the ledger or config; read-only ones always work."""
    command = args.command
    return (command in ("init", "spawn", "send", "answer", "stop", "land", "settle", "away", "back", "log", "release") or
            (command == "steer" and args.project != "list") or (command == "goal" and args.text is not None) or
            (command == "decide" and args.decision_command != "list") or
            (command == "config" and args.config_command in ("set", "unset")) or
            (command == "host" and args.host_command in ("add", "remove")) or
            (command == "project" and args.project_command == "add"))


def dispatch(args, boss):
    command = args.command
    full = getattr(args, "full", False)
    if command == "init":
        return initialize(boss, args)
    if command == "doctor":
        return doctor(boss)
    if command == "host":
        config = boss.config
        if args.host_command == "list":
            return ([f"{name}: {host['url']} token_env={host.get('token_env', 'T3_MCP_TOKEN')}" +
                     (f" ssh={host['ssh']}" if host.get("ssh") else "") for name, host in sorted(config.hosts.items())]
                    or ["no hosts configured; next: boss init"]), 0
        if args.host_command == "test":
            rows = boss.project_catalog(args.name)
            return [f"{args.name}: MCP handshake ok; {len(rows)} projects", *[f"{p['id']}: {p['title']}" for p in rows]], 0
        if args.host_command == "add":
            if args.name in config.hosts:
                raise BossError("config", f"host {args.name} already exists", "use boss config set hosts.<name>.url <url>", 1)
            config.change("hosts." + literal(args.name), {"url": args.url, "token_env": args.token_env,
                          **({"ssh": args.ssh} if args.ssh else {})})
        else:
            if args.name not in config.hosts:
                raise BossError("config", f"unknown host {args.name}", "boss host list", 1)
            data = copy.deepcopy(config.data)
            del data["hosts"][args.name]
            if data.get("default_host") == args.name:
                data.pop("default_host")
            config.write(data)
        return [f"{args.name}: {'added' if args.host_command == 'add' else 'removed'}"], 0
    if command == "config":
        config = boss.config
        if args.config_command == "show":
            overrides = {k: getattr(args, k) for k in ("host", "worker", "model", "effort", "kind", "merge", "delivery") if getattr(args, k) is not None}
            values, origins = config.effective(args.project, overrides)
            rows = [(path, value, origins[path]) for path, value in flatten(values)]
            for section in ("hosts", "workers", "projects", "wake"):
                rows.extend((path, value, str(config.path)) for path, value in flatten({section: config.data.get(section, {})}))
            return [(f"{origin}\t" if args.origin else "") + f"{display_key(path)} = {literal(value)}"
                    for path, value, origin in sorted(rows)], 0
        if args.config_command == "get":
            values, _ = config.effective()
            node = {**values, **{k: config.data.get(k, {}) for k in ("hosts", "workers", "projects", "wake")}}
            for part in key_parts(args.key):
                if not isinstance(node, dict) or part not in node:
                    raise BossError("config", f"unset key {args.key}", "boss config show --origin", 1)
                node = node[part]
            return [literal(node)], 0
        value = None
        if args.config_command == "set":
            try:
                value = tomllib.loads("value = " + args.value)["value"]
            except ValueError:
                value = args.value
        config.change(args.key, value, unset=args.config_command == "unset")
        return [f"{args.key}: {'unset' if args.config_command == 'unset' else 'set'}"], 0
    if command is None or (command == "status" and not args.id):
        return fleet(boss, full)
    if command == "goal":
        if args.text is not None:
            boss.store.set_goal(args.project, args.text)
        goal = boss.store.goal(args.project)
        return [f"project: {compact(args.project)}", "goal: " + (goal or "unset")], 0
    if command == "steer":
        if args.project == "drop":
            if args.task is not None or args.replaces is not None or args.all or args.steer_id is None:
                raise BossError("usage", "drop requires a project and steer id, without --task, --replaces or --all",
                                "boss steer drop <project> <steer_id>", 1)
            record = boss.store.drop_steer(args.text, args.steer_id)
            return [f"{record['id']}: steer dropped project={compact(args.text)} drops={record['drops']}",
                    f"next: boss steer list {compact(args.text)} --all"], 0
        if args.steer_id is not None:
            raise BossError("usage", "a third argument applies only to boss steer drop", "boss steer --help", 1)
        if args.project == "list":
            if args.task is not None or args.replaces is not None:
                raise BossError("usage", "--task and --replaces apply only when adding a steer", "boss steer --help", 1)
            records = boss.store.steers(args.text, all=args.all)
            lines = [f"{len(records)} {'total' if args.all else 'active'} steers project={compact(args.text)}"]
            for record in records:
                if "drops" in record:
                    lines.append(f"{record['id']}: tombstone {record['created_at']} drops={record['drops']}")
                    continue
                state = "dropped" if record["dropped"] else "superseded" if record["superseded"] else "active"
                lines.append(f"{record['id']}: {state} {record['created_at']} {record['text']}" +
                             (f" task={record['task']}" if record.get("task") else "") +
                             (f" replaces={record['replaces']}" if record.get("replaces") else ""))
            return lines, 0
        if args.all:
            raise BossError("usage", "--all applies only to boss steer list", "boss steer --help", 1)
        record = boss.store.add_steer(args.project, args.text, args.task, args.replaces)
        return [f"{record['id']}: steer recorded project={compact(args.project)}",
                f"next: boss steer list {compact(args.project)}"], 0
    if command == "project":
        if args.project_command == "list":
            host_name, rows = boss.project_list(args.host)
            lines = [f"{len(rows)} projects host={host_name}"]
            for row in rows:
                lines.append(f"{row['id']}: {compact(row['name'], 120)} registry={str(row['registry']).lower()} notes={str(row['notes']).lower()}")
            hint = f"boss project add <name> --host {args.host}" if args.host else "boss project show <name>"
            return lines + [f"next: {hint}"], 0
        if args.project_command == "add":
            boss.project_add(args.name, args.host, delivery=args.delivery, auto_merge=args.auto_merge, merge=args.merge, worker=args.worker,
                             test_host=args.test_host, no_mistakes=args.no_mistakes)
            return [f"{compact(args.name)}: registered", f"next: boss project show {compact(args.name)}"], 0
        settings, _, source = boss.resolve_project(args.name)
        notes = notes_path(boss.store.home, args.name)
        lines = [f"project: {compact(args.name)}", f"source: {source}",
                 f"host: {settings['host']}", f"project_id: {settings['project_id']}",
                 f"repository: {settings['repository']}", f"delivery: {settings['delivery']}",
                 f"merge: {settings['merge']}",
                 f"auto_merge: {str(settings['auto_merge']).lower()}",
                 f"test_host: {compact(settings.get('test_host', 'unset'))}",
                 f"no_mistakes: {str(settings['no_mistakes']).lower()}",
                 "model_selection: " + json.dumps(settings['model_selection'], sort_keys=True, separators=(',', ':'))]
        effective, origins = boss.config.effective(args.name)
        for key in ("worker", "model", "effort", "host", "kind"):
            if key in effective:
                lines.append(f"{key}: {literal(effective[key])} origin={origins[(key,)]}")
        lines.extend(rule_lines(effective, origins))
        lines.append(f"registry: {boss.config.path}" if args.name in boss.config.projects else "registry: absent")
        lines.append(f"notes: {notes}" if notes.is_file() else "notes: absent")
        goal = boss.store.goal(args.name)
        steers = boss.store.steers(args.name)
        lines.append(f"goal: {goal_path(boss.store.home, args.name)}" if goal else "goal: absent")
        lines.append(f"active_steers: {len(steers)}")
        if notes.is_file():
            lines.extend(["", "Supervisor notes (not sent to workers)", notes.read_text(encoding="utf-8"), ""])
        return lines + [f'next: boss spawn {compact(args.name)} --title "<task>" --brief-file <file>'], 0
    if command == "spawn":
        if args.brief_file and args.brief_file != "-":
            brief = Path(args.brief_file).read_text(encoding="utf-8")
        elif not sys.stdin.isatty():
            brief = sys.stdin.read(115001)
        else:
            raise BossError("usage", "supply --brief-file or pipe a brief on stdin", "boss spawn --help", 1)
        meta = boss.spawn(args.project, args.title, brief, args.kind, args.host, args.model, args.effort, args.fits, args.worker,
                          args.override)
        return [f"{meta['id']}: spawned {meta['host']} {meta['status']}",
                f"thread: {meta['thread_id']}", f"next: boss status {meta['id']}"], 0
    if command == "status":
        meta = boss.store.load(args.id)
        view = boss.snapshot(meta)
        lines = [f"{meta['id']}: {meta['project']} {view['status']}" + (" settled" if meta.get("settled") else ""),
                 f"thread: {meta['thread_id']}", f"host: {meta['host']}",
                 f"auto_merge: {str(meta.get('auto_merge', False)).lower()}",
                 f"no_mistakes: {str(meta.get('no_mistakes', False)).lower()}"]
        if meta.get("test_host"):
            lines.append(f"test_host: {compact(meta['test_host'])}")
        if meta.get("override"):
            lines.append(f"override: {compact(meta['override']['approved'])}")
        lines.append(f"{len(view['requests'])} pending input requests")
        client = boss.client(meta["host"])
        for request_id in view["requests"]:
            request = client.call("t3_pending_request_read", threadId=meta["thread_id"], requestId=request_id)
            for q in request["questions"]:
                lines.append(f"{request_id}/{q['id']}: {compact(q['question'], None if full else 240)}")
                if q.get("options"):
                    lines.append("options: " + compact(" | ".join(o["label"] for o in q["options"]), None if full else 240))
        lines.append(f"{len(view['prs'])} linked PRs")
        for pr in view["prs"]:
            lines.append(f"{pr['url']}: {pr['state'].lower()} ci={ci_text(pr['ci'])}")
        lines.append("last: " + (json.dumps(view["assistant"], ensure_ascii=False) if full else compact(view["assistant"] or "0 assistant messages", 360)))
        lines.append(f"next: boss answer {args.id} <request> \"<answer>\"" if view["requests"] else
                     f"next: open T3 thread {meta['thread_id']} to review pending permissions" if view["status"] == "waiting" else
                     f'next: boss land {args.id} --approved "<captain quote>"' if view["status"] == "completed" and any(p["state"] == "OPEN" and p["ci"]["state"] == "green" for p in view["prs"]) else
                     f"next: boss settle {args.id}" if view["status"] == "completed" and not any(p["state"] == "OPEN" for p in view["prs"]) else
                     f"next: boss send {args.id} \"<message>\"")
        boss.reviewed(meta, view)
        return lines, 0
    if command == "own":
        if args.status:
            return owner.status_lines(boss.store.home), 0
        surface = os.environ.get("HERMES_SESSION_PLATFORM") or os.environ.get("HERMES_SESSION_SOURCE") or ""
        lines = owner.own(boss.store, os.environ.get("HERMES_SESSION_KEY", ""), surface, args.label, args.take)
        from .wake import poke
        poke(boss.store.home)  # held wakes go to the new owner now, not on some later push
        return lines, 0
    if command == "release":
        return owner.release(boss.store), 0
    if command == "send":
        boss.send(args.id, args.text)
        return [f"{args.id}: sent", f"next: boss status {args.id}"], 0
    if command == "answer":
        boss.answer(args.id, args.request, args.text)
        return [f"{args.id}: answered {args.request}", f"next: boss status {args.id}"], 0
    if command == "stop":
        boss.stop(args.id)
        return [f"{args.id}: interruption requested", f"next: boss status {args.id}"], 0
    if command == "land":
        boss.land(args.id, args.approved)
        return [f"{args.id}: landed", f"next: boss settle {args.id}"], 0
    if command == "settle":
        boss.settle(args.id, args.discard_approved)
        return [f"{args.id}: settled", "next: boss"], 0
    if command == "away":
        note = " ".join(args.note)
        if note:
            require_text(note, "away note", 300)
        return captain.away(boss.store, note), 0
    if command == "back":
        return captain.back(boss.store), 0
    if command == "log":
        require_text(args.text, "check-in log", 20000)
        boss.store.captain("log", text=args.text.strip())
        return ["check-in logged", "next: boss"], 0
    if command == "decide":
        subcommand = args.decision_command
        if subcommand == "list":
            return decision_lines(boss.store, full) + ['next: boss decide close <id> "<captain resolution>"'], 0
        if subcommand == "ask":
            lines, record = captain.ask(boss.store, args.requested)
            return lines + (['next: boss decide close <n> "<captain words>"'] if record else ["next: boss"]), 0
        require_text(args.text, "decision", 20000)
        if subcommand == "add":
            if args.task:
                boss.store.load(args.task)
            decision_id = boss.store.new_id()
            boss.store.decision({"id": decision_id, "text": args.text, "task": args.task, "closed": False,
                                 "created_at": now()})
            return [f"{decision_id}: decision open", "next: boss decide ask"], 0
        record = captain.resolve(boss.store, args.id, args.round)
        if subcommand == "unclear":
            if record.get("closed"):
                refuse("that decision is already answered; it is never asked again", "boss decide ask")
            boss.store.captain("unclear", decision=record["id"], note=args.text.strip())
            return [f"{record['id']}: marked unclear; re-asked next round", "next: boss decide ask"], 0
        if not record.get("closed"):
            # Full authority whether or not the captain is away.
            record.update(closed=True, resolution=args.text, closed_at=now())
            boss.store.decision(record)
            return [f"{record['id']}: decision closed", "next: boss"], 0
        return [f"{record['id']}: decision already closed; kept the first answer", "next: boss"], 0
    raise BossError("usage", "unknown command", exit_code=1)


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    redact = Redactor()
    redact.discover(os.environ)
    try:
        args = parser().parse_args(argv)
        home = boss_home()
        config = Config(home, redact)
        store = Store(home, redact)
        boss = Boss(config, store, redact)
        if args.command == "brief":
            print(boss.worker_brief(args.project, worker=args.worker, origin=args.origin), end="")
            rules = rule_lines(*config.effective(args.project, {"worker": args.worker}))
            if rules:
                # stdout stays byte-identical to the spawned brief; workers never see rules.
                print(redact("\n".join(["supervisor rules (enforced at spawn; not sent to workers):", *rules])), file=sys.stderr)
            return 0
        with store.locked(wait=os.environ.get("BOSS_WAIT_FOR_LEDGER") == "1"):
            if mutating(args):
                # Only the owning session may change boss; a human shell is warned, not stopped.
                warning = owner.guard(home, os.environ.get("HERMES_SESSION_KEY", ""))
                if warning:
                    print(warning, file=sys.stderr)
            try:
                lines, code = dispatch(args, boss)
            finally:
                store.refresh_attention()
        if lines:
            print(redact("\n".join(lines)))
        return code
    except BossError as exc:
        lines = [f"error: {exc.code}", f"message: {compact(redact(exc.message), 600)}", *exc.context, f"next: {exc.hint}"]
        print(redact("\n".join(lines)))
        return exc.exit_code
    except (OSError, UnicodeError) as exc:
        print(redact(f"error: io\nmessage: {compact(redact(exc), 400)}\nnext: check file paths and permissions"))
        return 3
    except (KeyError, TypeError, ValueError, IndexError) as exc:
        print(redact(f"error: data\nmessage: {compact(redact(exc), 400)}\nnext: check config, ledger and T3 response schemas"))
        return 3
    except KeyboardInterrupt:
        print("error: interrupted\nnext: boss status; reconcile before retrying mutations")
        return 3
