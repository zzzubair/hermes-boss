"""Content-first command line with stable, terse records."""
import argparse
import json
import os
from pathlib import Path
import sys

from . import __version__
from .common import BossError, Redactor, compact, require_text
from .config import Config
from .service import Boss, ci_text
from .state import Store


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
    spawn = commands.add_parser("spawn", help="launch one isolated coding worker")
    spawn.add_argument("project")
    spawn.add_argument("--title", required=True)
    spawn.add_argument("--brief-file", metavar="FILE", help="UTF-8 brief; omitted or '-' reads piped stdin")
    spawn.add_argument("--kind", choices=("ship", "scout"), default="ship")
    spawn.add_argument("--host", help="registry host override; project_id must exist there")
    spawn.add_argument("--model", help="model id or providerInstanceId:model")
    spawn.add_argument("--effort", help="exact effort value supported by that model")
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
    commands.add_parser("poll", help="one deterministic pass; only unresolved actionable items, otherwise silent")
    decide = commands.add_parser("decide", help="record decisions that need the captain")
    decisions = decide.add_subparsers(dest="decision_command", required=True)
    add = decisions.add_parser("add", help="open a durable decision")
    add.add_argument("text")
    add.add_argument("--task", help="related task id")
    listing = decisions.add_parser("list", help="list open captain decisions")
    listing.add_argument("--full", action="store_true", default=argparse.SUPPRESS, help="show complete text")
    close = decisions.add_parser("close", help="record the captain's resolution; repeat is safe")
    close.add_argument("id")
    close.add_argument("text")
    return root


def decision_lines(store, full=False):
    opened = [d for d in store.decisions() if not d.get("closed")]
    lines = [f"{len(opened)} open decisions"]
    for d in opened:
        lines.append(f"{d['id']}: {compact(d['text'], None if full else 180)}" +
                     (f" task={d['task']}" if d.get("task") else ""))
    return lines


def fleet(boss, full=False):
    tasks = boss.store.tasks(active=True)
    executable = str(Path(sys.argv[0]).absolute())
    home = str(Path.home())
    if executable.startswith(home + "/"):
        executable = "~" + executable[len(home):]
    lines = [f"bin: {compact(executable)}", "description: Supervise T3 coding workers.", f"{len(tasks)} active tasks"]
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
        lines.append(f"{meta['id']}: {meta['project']} {state} {compact(meta['title'], None if full else 80)}")
    if counts:
        lines.append("states: " + " ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    lines.extend(decision_lines(boss.store, full))
    if errors:
        for exc in errors:
            lines.append(f"error: {exc.code} {compact(boss.redact(exc.message))}")
        lines.append("next: check host credentials/connectivity; boss status <id>")
    elif tasks:
        lines.append("next: boss status <id>")
    elif any(not d.get("closed") for d in boss.store.decisions()):
        lines.append('next: boss decide close <id> "<captain resolution>"')
    else:
        lines.append('next: boss spawn <project> --title "<task>" --brief-file <file>')
    return lines, max((e.exit_code for e in errors), default=0)


def dispatch(args, boss):
    command = args.command
    full = getattr(args, "full", False)
    if command is None or (command == "status" and not args.id):
        return fleet(boss, full)
    if command == "spawn":
        if args.brief_file and args.brief_file != "-":
            brief = Path(args.brief_file).read_text(encoding="utf-8")
        elif not sys.stdin.isatty():
            brief = sys.stdin.read(115001)
        else:
            raise BossError("usage", "supply --brief-file or pipe a brief on stdin", "boss spawn --help", 1)
        meta = boss.spawn(args.project, args.title, brief, args.kind, args.host, args.model, args.effort)
        return [f"{meta['id']}: spawned {meta['host']} {meta['status']}",
                f"thread: {meta['thread_id']}", f"next: boss status {meta['id']}"], 0
    if command == "status":
        meta = boss.store.load(args.id)
        view = boss.snapshot(meta)
        lines = [f"{meta['id']}: {meta['project']} {view['status']}" + (" settled" if meta.get("settled") else ""),
                 f"thread: {meta['thread_id']}", f"host: {meta['host']}", f"{len(view['requests'])} pending input requests"]
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
        return lines, 0
    if command == "poll":
        lines, code = [], 0
        for meta in boss.store.tasks(active=True):
            try:
                lines.extend(boss.snapshot(meta)["alerts"])
            except BossError as exc:
                # One unavailable host must not hide the rest of the fleet.
                lines.append(f"{meta['id']}: failed {exc.code} {compact(boss.redact(exc.message))}")
                code = max(code, exc.exit_code)
        return lines, code
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
    if command == "decide":
        subcommand = args.decision_command
        if subcommand == "list":
            return decision_lines(boss.store, full) + ['next: boss decide close <id> "<captain resolution>"'], 0
        require_text(args.text, "decision", 20000)
        if subcommand == "add":
            if args.task:
                boss.store.load(args.task)
            decision_id = boss.store.new_id()
            boss.store.decision({"id": decision_id, "text": args.text, "task": args.task, "closed": False})
            return [f"{decision_id}: decision open", "next: boss decide list"], 0
        record = next((d for d in boss.store.decisions() if d["id"] == args.id), None)
        if not record:
            raise BossError("usage", f"unknown decision {args.id}", "boss decide list", 1)
        if not record.get("closed"):
            record.update(closed=True, resolution=args.text)
            boss.store.decision(record)
        return [f"{args.id}: decision closed", "next: boss"], 0
    raise BossError("usage", "unknown command", exit_code=1)


def main(argv=None):
    redact = Redactor()
    for key, value in os.environ.items():
        if any(word in key for word in ("TOKEN", "SECRET", "PASSWORD", "API_KEY")):
            redact.add(value)
    try:
        args = parser().parse_args(argv)
        home = Path(os.environ.get("BOSS_HOME", "~/.boss")).expanduser()
        config = Config(home, redact)
        store = Store(home, redact)
        with store.locked():
            lines, code = dispatch(args, Boss(config, store, redact))
        if lines:
            print(redact("\n".join(lines)))
        return code
    except BossError as exc:
        print(redact(f"error: {exc.code}\nmessage: {compact(redact(exc.message), 600)}\nnext: {exc.hint}"))
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
