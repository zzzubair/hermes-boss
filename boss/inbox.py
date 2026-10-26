"""The owner's inbox: every worker event lands on disk first and leaves only once the owner's model saw it.

Append-only $BOSS_HOME/inbox.jsonl (Firstmate model). The wake listener appends
'held' records for worker events, deduped by event key; /away and /back append
captain events (task None). Every append pokes a running `boss watch`.
Delivery appends 'acked' (with the path it took) or 'dropped' (with why).
These paths deliver, all to the owner only:

  watch   boss watch, a tracked background command: its exit is a system notice
  tool    mid-turn: appended to the result of a tool the owner's turn just ran
  inject  inject_message while the owner is idle; off unless wake.inject_when_idle

At delivery, items that are stale (task settled or landed, PR merged or closed,
question answered, worker already replied to or moved on) are dropped, and each
task keeps one line: its newest event. No Hermes imports, model calls or timers.
"""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import json
import os
from pathlib import Path
import socket

from .common import BossError
from .state import append_line, atomic_write, now

FILE = "inbox.jsonl"
LEGACY = "wakes.jsonl"  # pre-inbox wake log: same 'held' records, 'delivered' meant acked
LOCK = "inbox.lock"
SOCKET = "watch.sock"  # a waiting boss watch listens here
TURN = "owner-turn.json"
NOTE = "(boss: handle it; ask the captain only if you need a decision)"
LINE_CHARS = 400
LIMIT = 1500
TURN_STALE = timedelta(hours=2)  # a busy mark nobody refreshed this long is a crashed turn
TURN_REFRESH = timedelta(minutes=5)
_quiet = {}  # inbox path -> (size, mtime) last seen with nothing open: the per-tool-call fast path


class Inbox:
    def __init__(self, home, redact=None):
        self.home = Path(home)
        self.path = self.home / FILE
        self.redact = redact or (lambda text: text)

    @contextmanager
    def locked(self):
        """Held only for one read and one append, so a tool result never waits long."""
        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (self.home / LOCK).open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def records(self):
        legacy = self.home / LEGACY
        if not self.path.exists() and legacy.exists():
            os.replace(legacy, self.path)  # under the lock: one migration, nothing held is lost
        if not self.path.exists():
            return []
        with self.path.open(encoding="utf-8") as stream:
            # A torn final line (crash mid-append) is ignored; every complete line is fsynced.
            return [json.loads(line) for line in stream if line.endswith("\n")]

    def open_items(self, records=None):
        """Held items not yet acked or dropped, oldest first."""
        records = self.records() if records is None else records
        closed = {key for r in records if r["event"] in ("acked", "dropped", "delivered")
                  for key in r.get("keys", [r.get("key")])}
        return [r for r in records if r["event"] == "held" and r["key"] not in closed]

    def hold(self, key, task, kind, line, *, run=None, request_ids=(), pr_urls=()):
        """Record one event once; the same key pushed again is ignored."""
        with self.locked():
            if any(r["event"] == "held" and r["key"] == key for r in self.records()):
                return None
            record = append_line(self.path, {"event": "held", "key": key, "task": task, "kind": kind,
                                             "line": self.redact(line), "run": run, "request_ids": list(request_ids),
                                             "pr_urls": list(pr_urls), "at": now()})
        notify(self.home)
        return record

    def maybe_open(self):
        """Cheap stat check: False only when the file is unchanged since it was last seen empty."""
        try:
            info = self.path.stat()
        except FileNotFoundError:
            return (self.home / LEGACY).exists()
        return _quiet.get(str(self.path)) != (info.st_size, info.st_mtime_ns)

    def take(self, load, via, owner, send=None):
        """Drop stale items, keep one per task, render; ack only once send(text) says it arrived.

        `load(task_id)` returns task metadata. Returns the delivered text, or None.
        """
        with self.locked():
            items = self.open_items()
            keep, drops = triage(load, items)
            for key, reason in drops:
                append_line(self.path, {"event": "dropped", "key": key, "reason": reason, "at": now()})
            if not keep:
                info = self.path.stat() if self.path.exists() else None
                if info is not None:
                    _quiet[str(self.path)] = (info.st_size, info.st_mtime_ns)
                return None
            text = render(keep)
            if send is not None and not send(text):
                return None
            append_line(self.path, {"event": "acked", "keys": [item["key"] for item in keep], "via": via,
                                    "owner": owner, "at": now()})
            return text


def notify(home, message=b"inbox\n"):
    """Wake a waiting boss watch, if one runs; never fails the caller."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(1)
            client.connect(str(Path(home) / SOCKET))
            client.sendall(message)
    except OSError:
        pass


def stale(meta, item):
    """Why an open item no longer needs the owner, from the local ledger only; None if it still does."""
    if meta.get("settled"):
        return "task settled"
    if meta.get("landed"):
        return "PR landed"
    observed = meta.get("observed") or {}
    states = {p["url"]: p.get("state") for p in observed.get("prs", [])}
    finished = [states.get(url) for url in item.get("pr_urls", [])]
    if finished and all(state in ("MERGED", "CLOSED") for state in finished):
        return "PR " + finished[-1].lower()
    if item.get("request_ids") and not set(item["request_ids"]) & set(observed.get("requests", [])):
        return "question already answered"
    if meta.get("handled_at", "") > item["at"]:
        return "already handled: boss replied to or reviewed the worker after this"
    if item.get("run") and observed.get("run_id") and observed["run_id"] != item["run"]:
        return "worker moved on to a newer turn"
    return None


def triage(load, items):
    """(newest live item per task, [(key, reason) to drop]); input is oldest first."""
    newest, drops = {}, []
    for item in items:
        if item.get("task") is None:
            newest[item["key"]] = item  # a captain event: never stale, never merged
            continue
        try:
            meta = load(item["task"])
        except (BossError, OSError, ValueError):
            drops.append((item["key"], "task is not in the ledger"))
            continue
        reason = stale(meta, item)
        if reason:
            drops.append((item["key"], reason))
            continue
        if item["task"] in newest:
            # Pop, so lines stay in the order their kept events arrived.
            drops.append((newest.pop(item["task"])["key"], "superseded by a newer event for the same task"))
        # Pre-inbox records carry bare text; name the task the same way new lines do.
        newest[item["task"]] = item if "line" in item else {
            **item, "line": f"{' '.join(str(meta.get('title') or 'a worker').split())}: {item.get('text', '')}"}
    return list(newest.values()), drops


def render(items):
    """Plain words, task title first, one line per task, and the note to the supervisor."""
    lines = []
    for item in items:
        line = " ".join(str(item["line"]).split())
        lines.append(line if len(line) <= LINE_CHARS else line[:LINE_CHARS - 3] + "...")
    body = "\n".join(lines)
    if len(body) > LIMIT:
        body = body[:LIMIT - 3] + "..."
    return f"[boss inbox]\n{body}\n{NOTE}"


def read_turn(home):
    try:
        record = json.loads((Path(home) / TURN).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def turn_started(home, session_key, session_id):
    """The owner's top-level turn began (pre_llm_call). Its session id is what its tool calls carry."""
    atomic_write(Path(home) / TURN, json.dumps({"session_key": session_key, "session_id": session_id, "busy": True,
                                                "pid": os.getpid(), "at": now()}, sort_keys=True) + "\n")


def turn_touched(home, record):
    """Refresh a long turn's busy mark from its tool calls, at most every few minutes."""
    try:
        if datetime.now(timezone.utc) - datetime.fromisoformat(record["at"]) < TURN_REFRESH:
            return
    except (KeyError, TypeError, ValueError):
        pass
    atomic_write(Path(home) / TURN, json.dumps({**record, "at": now()}, sort_keys=True) + "\n")


def turn_ended(home, *, session_id=None, session_key=None):
    """The owner's turn ended (on_session_end, or the gateway stopped it). True when this was the owner's turn."""
    record = read_turn(home)
    if not record or not record.get("busy"):
        return False
    if (session_id and record.get("session_id") != session_id) or (session_key and record.get("session_key") != session_key):
        return False
    atomic_write(Path(home) / TURN, json.dumps({**record, "busy": False, "at": now()}, sort_keys=True) + "\n")
    return True


def live_turn(home):
    """The owner's running turn record, or None when the owner is idle (or its turn crashed)."""
    record = read_turn(home)
    if not record or not record.get("busy"):
        return None
    try:
        os.kill(int(record["pid"]), 0)
        if datetime.now(timezone.utc) - datetime.fromisoformat(record["at"]) > TURN_STALE:
            return None
    except ProcessLookupError:
        return None
    except PermissionError:
        pass  # alive, another user's process
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    return record


def owner_busy(home, session_key):
    record = live_turn(home)
    return bool(record and session_key and record.get("session_key") == session_key)
