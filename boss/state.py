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


def identifier(value):
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", value):
        raise BossError("usage", "invalid id", "boss status", 1)
    return value


class Store:
    def __init__(self, home, redact):
        self.home, self.redact = Path(home), redact

    @contextmanager
    def locked(self):
        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (self.home / ".lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
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
        record = {"event": event, "details": details, "meta": meta}
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
        records, seen, replaced = [], set(), set()
        try:
            with path.open(encoding="utf-8") as stream:
                for line in stream:
                    if not line.endswith("\n"):
                        raise ValueError("incomplete append")
                    record = json.loads(line)
                    steer_id = identifier(record["id"])
                    require_text(record["text"], "captain steer", BRIEF_LIMIT)
                    if steer_id in seen:
                        raise ValueError("duplicate steer id")
                    if not datetime.fromisoformat(record["created_at"]).tzinfo:
                        raise ValueError("timestamp has no timezone")
                    if record.get("task"):
                        identifier(record["task"])
                    if record.get("replaces"):
                        if record["replaces"] not in seen:
                            raise ValueError("unknown replaced steer")
                        replaced.add(record["replaces"])
                    seen.add(steer_id)
                    records.append(record)
        except (ValueError, KeyError, TypeError, BossError) as exc:
            raise BossError("ledger", f"invalid steer log for {name}: {exc}",
                            f"inspect {path}; preserve existing history") from None
        return [{**r, "superseded": r["id"] in replaced} for r in records if all or r["id"] not in replaced]

    def add_steer(self, name, text, task=None, replaces=None):
        project_name(name)
        require_text(text, "captain steer", BRIEF_LIMIT)
        if self.redact(text) != text:
            refuse("captain steers layer contains a token or secret", "remove credentials from the steer")
        records = self.steers(name, all=True)
        if task is not None and self.load(task)["project"] != name:
            refuse("related task belongs to another project", "use a task from this project")
        if replaces is not None and not any(r["id"] == replaces for r in records):
            raise BossError("usage", "unknown steer to replace in this project", f"boss steer list {name} --all", 1)
        record = {"id": self.new_id(), "text": text.strip(), "task": task, "replaces": replaces,
                  "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        path = steers_path(self.home, name)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # The CLI holds the ledger lock. Append one record and fsync it without
        # replacing the file or changing any previous record, even on supersession.
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
                polled = meta.get("poll_observed") or {}
                if meta.get("pending_inputs"):
                    items.append({"id": meta["id"], "kind": "input"})
                elif (meta.get("status") == "completed" and polled.get("status") == "completed" and
                      polled.get("run_id") and polled["run_id"] != meta.get("reviewed_run_id") and
                      polled["run_id"] == (meta.get("observed") or {}).get("run_id")):
                    items.append({"id": meta["id"], "kind": "finished"})
            except (OSError, ValueError, KeyError, TypeError, AttributeError, BossError):
                continue
        attention = {"version": 1, "count": len(items), "items": items[:6]}
        atomic_write(self.home / "attention.json", json.dumps(attention, sort_keys=True) + "\n")
