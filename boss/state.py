"""Atomic journal commits, recoverable materialized metadata and one writer."""
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import tempfile
import uuid

from .captain import presence
from .common import BossError, refuse, require_text
from .preferences import BRIEF_LIMIT, goal_path, layer_text, project_name, steers_path


def atomic_write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def now():
    return datetime.now(timezone.utc).isoformat()


def append_line(path, record):
    """Append one fsynced JSON line without replacing or changing earlier records."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return record


def identifier(value):
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", value):
        raise BossError("usage", "invalid id", "boss status", 1)
    return value


class Store:
    def __init__(self, home, redact):
        self.home, self.redact = Path(home), redact

    @contextmanager
    def locked(self, wait=False):
        """One writer. CLI commands refuse when busy; the wake listener waits its turn."""
        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (self.home / ".lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
            except BlockingIOError:
                refuse("another boss command holds the ledger", "retry after that command finishes")
            yield

    def directory(self, task_id):
        return self.home / "tasks" / identifier(task_id)

    def new_id(self):
        return uuid.uuid4().hex[:12]

    def load(self, task_id):
        directory = self.directory(task_id)
        journal = directory / "events.log"
        if not journal.exists():
            raise BossError("usage", f"unknown task {task_id}", "boss status", 1)
        try:
            # The last atomically committed journal record owns the metadata.
            meta = json.loads(journal.read_text().splitlines()[-1])["meta"]
            if meta["id"] != task_id:
                raise ValueError("task id mismatch")
            return meta
        except (ValueError, KeyError, IndexError) as exc:
            raise BossError("ledger", f"invalid journal for {task_id}: {exc}", "inspect tasks/<id>/events.log") from None

    def tasks(self, active=False):
        root = self.home / "tasks"
        tasks = [self.load(p.name) for p in sorted(root.iterdir()) if p.is_dir() and not p.name.startswith(".")] if root.exists() else []
        return [t for t in tasks if not active or not t.get("settled")]

    def save(self, meta, event, **details):
        directory = self.directory(meta["id"])
        meta["revision"] = meta.get("revision", 0) + 1
        meta.update(self.redact.data(meta))
        record = {"event": event, "at": now(), "details": details, "meta": meta}
        journal = directory / "events.log"
        prior = journal.read_text() if journal.exists() else ""
        atomic_write(journal, prior + json.dumps(self.redact.data(record), sort_keys=True) + "\n")
        atomic_write(directory / "meta.json", json.dumps(meta, indent=2, sort_keys=True) + "\n")

    def text(self, task_id, name, text):
        atomic_write(self.directory(task_id) / name, self.redact(text))

    def decisions(self):
        root = self.home / "decisions"
        return [json.loads(p.read_text()) for p in sorted(root.glob("*.json"))] if root.exists() else []

    def decision(self, record):
        atomic_write(self.home / "decisions" / (identifier(record["id"]) + ".json"),
                     json.dumps(self.redact.data(record), sort_keys=True) + "\n")

    def goal(self, name):
        return layer_text(goal_path(self.home, name), "project goal", self.redact)

    def set_goal(self, name, text):
        path = goal_path(self.home, name)
        require_text(text, "project goal", BRIEF_LIMIT)
        if self.redact(text) != text:
            refuse("project goal layer contains a token or secret", "remove credentials from the goal")
        atomic_write(path, text.strip())

    def steers(self, name, all=False):
        path = steers_path(self.home, name)
        if not path.exists():
            return []
        records, seen, replaced, dropped = [], {}, set(), set()
        try:
            with path.open(encoding="utf-8") as stream:
                for line in stream:
                    if not line.endswith("\n"):
                        raise ValueError("incomplete append")
                    record = json.loads(line)
                    steer_id = identifier(record["id"])
                    if steer_id in seen:
                        raise ValueError("duplicate steer id")
                    if not datetime.fromisoformat(record["created_at"]).tzinfo:
                        raise ValueError("timestamp has no timezone")
                    if "drops" in record:
                        if set(record) != {"id", "drops", "created_at"}:
                            raise ValueError("tombstone must contain only id, drops and created_at")
                        target = identifier(record["drops"])
                        if target not in seen:
                            raise ValueError("unknown dropped steer")
                        if "drops" in seen[target]:
                            raise ValueError("cannot drop a tombstone")
                        if target in dropped:
                            raise ValueError("duplicate drop")
                        if target in replaced:
                            raise ValueError("cannot drop a superseded steer")
                        dropped.add(target)
                    else:
                        require_text(record["text"], "captain steer", BRIEF_LIMIT)
                        if record.get("task"):
                            identifier(record["task"])
                    if record.get("replaces"):
                        target = record["replaces"]
                        if target not in seen:
                            raise ValueError("unknown replaced steer")
                        if "drops" in seen[target] or target in dropped:
                            raise ValueError("cannot replace a dropped steer or tombstone")
                        replaced.add(target)
                    seen[steer_id] = record
                    records.append(record)
        except (ValueError, KeyError, TypeError, BossError) as exc:
            raise BossError("ledger", f"invalid steer log for {name}: {exc}",
                            f"inspect {path}; preserve existing history") from None
        return [{**r, "superseded": r["id"] in replaced, "dropped": r["id"] in dropped}
                for r in records if all or ("drops" not in r and r["id"] not in replaced and r["id"] not in dropped)]

    def add_steer(self, name, text, task=None, replaces=None):
        project_name(name)
        require_text(text, "captain steer", BRIEF_LIMIT)
        if self.redact(text) != text:
            refuse("captain steers layer contains a token or secret", "remove credentials from the steer")
        records = self.steers(name, all=True)
        if task is not None and self.load(task)["project"] != name:
            refuse("related task belongs to another project", "use a task from this project")
        if replaces is not None:
            target = next((r for r in records if r["id"] == replaces), None)
            if target is None:
                raise BossError("usage", "unknown steer to replace in this project", f"boss steer list {name} --all", 1)
            if "drops" in target or target["dropped"]:
                refuse("cannot replace a dropped steer or tombstone", f"boss steer list {name} --all")
        record = {"id": self.new_id(), "text": text.strip(), "task": task, "replaces": replaces,
                  "created_at": now()}
        return self._append_steer(name, record)

    def drop_steer(self, name, steer_id):
        identifier(steer_id)
        records = self.steers(name, all=True)
        target = next((r for r in records if r["id"] == steer_id), None)
        hint = f"boss steer list {name} --all"
        if target is None:
            raise BossError("usage", "unknown steer to drop in this project", hint, 1)
        if "drops" in target:
            refuse("cannot drop a tombstone", hint)
        if target["dropped"]:
            refuse("steer is already dropped", hint)
        if target["superseded"]:
            refuse("steer is already superseded", hint)
        record = {"id": self.new_id(), "drops": steer_id,
                  "created_at": now()}
        return self._append_steer(name, record)

    def _append_steer(self, name, record):
        # The CLI holds the ledger lock. Never rewrite history, even on supersession.
        return append_line(steers_path(self.home, name), record)

    def journal(self, task_id):
        """Every committed record of one task, oldest first."""
        path = self.directory(task_id) / "events.log"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def captain_path(self):
        return self.home / "captain.jsonl"

    def captain_events(self):
        """Append-only captain journal: away, back, check-in logs, ask rounds, unclear marks, ownership."""
        path = self.captain_path()
        if not path.exists():
            return []
        records = []
        try:
            with path.open(encoding="utf-8") as stream:
                for line in stream:
                    if not line.endswith("\n"):
                        raise ValueError("incomplete append")
                    record = json.loads(line)
                    if record["event"] not in ("away", "back", "log", "ask", "unclear", "own"):
                        raise ValueError("unknown event " + str(record["event"]))
                    if not datetime.fromisoformat(record["at"]).tzinfo:
                        raise ValueError("timestamp has no timezone")
                    records.append(record)
        except (ValueError, KeyError, TypeError) as exc:
            raise BossError("ledger", f"invalid captain journal: {exc}",
                            f"inspect {path}; preserve existing history") from None
        return records

    def captain(self, event, **fields):
        return append_line(self.captain_path(), self.redact.data({"event": event, "at": now(), **fields}))

    def wakes_path(self):
        return self.home / "wakes.jsonl"

    def wakes(self):
        """Append-only wake log: 'held' when a pushed event arrives, 'delivered' once the owner got it."""
        path = self.wakes_path()
        if not path.exists():
            return []
        with path.open(encoding="utf-8") as stream:
            # A torn final line (crash mid-append) is ignored; every complete line is fsynced.
            return [json.loads(line) for line in stream if line.endswith("\n")]

    def held_wakes(self):
        records = self.wakes()
        delivered = {key for r in records if r["event"] == "delivered" for key in r["keys"]}
        return [r for r in records if r["event"] == "held" and r["key"] not in delivered]

    def hold_wake(self, key, task, kind, text):
        """Record one pushed event once; a repeated key (same event pushed again) is ignored."""
        if any(r["event"] == "held" and r["key"] == key for r in self.wakes()):
            return None
        return append_line(self.wakes_path(), self.redact.data(
            {"event": "held", "key": key, "task": task, "kind": kind, "text": text, "at": now()}))

    def wakes_delivered(self, keys, label):
        return append_line(self.wakes_path(), {"event": "delivered", "keys": list(keys), "owner": label, "at": now()})

    def refresh_attention(self):
        """Materialize a tiny local hook view once per CLI command, under its lock.

        The journal remains authoritative. No text, question bodies or secrets
        enter this view: only validated IDs, kinds and a precomputed total.
        """
        items = []
        for path in sorted((self.home / "decisions").glob("*.json")):
            try:
                record = json.loads(path.read_text())
                record_id = identifier(record["id"])
                if not record.get("closed"):
                    items.append({"id": record_id, "kind": "decision"})
            except (OSError, ValueError, KeyError, TypeError, BossError):
                continue
        for directory in sorted((self.home / "tasks").glob("*")):
            if not directory.is_dir() or directory.name.startswith("."):
                continue
            try:
                meta = self.load(directory.name)
                if meta.get("settled"):
                    continue
                if meta.get("pending_inputs"):
                    items.append({"id": meta["id"], "kind": "input"})
            except (OSError, ValueError, KeyError, TypeError, AttributeError, BossError):
                continue
        try:
            # Pushed events the owner has not received yet (owner offline, or nobody owns boss).
            seen = {item["id"] for item in items}
            for wake in self.held_wakes():
                task = identifier(wake["task"])
                if task not in seen and not self.load(task).get("settled"):
                    seen.add(task)
                    items.append({"id": task, "kind": "input" if wake["kind"] in ("input", "approval") else "finished"})
        except (OSError, ValueError, KeyError, TypeError, BossError):
            pass
        attention = {"version": 1, "count": len(items), "items": items[:6]}
        try:
            away = presence(self.captain_events())
            if away:
                # The captain's own short note; one line, bounded for the hook.
                attention["away"] = {"since": away["at"], "note": " ".join(str(away.get("note") or "").split())[:80]}
        except BossError:
            pass
        atomic_write(self.home / "attention.json", json.dumps(attention, sort_keys=True) + "\n")
