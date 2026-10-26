"""boss watch: block until the owner's inbox has something, print it, ack it, exit.

The owner session runs it as a Hermes tracked background command; its exit reaches
the supervisor as a system notice, never as a message the captain seems to have sent.
No polling: it blocks on $BOSS_HOME/watch.sock, which every inbox append pokes
(inbox.notify). One watch runs at a time (watch.lock); a newer one replaces it,
and the replaced one exits without taking anything.
"""
import fcntl
import json
import os
import socket

from . import owner
from .common import refuse
from .inbox import Inbox, SOCKET, notify

LOCK = "watch.lock"
KINDS = (("decision", "open decision"), ("input", "unanswered worker question"), ("finished", "finished run to review"))


def still_open(home):
    """One plain line for what still waits after this delivery; no ids. None when nothing does."""
    try:
        data = json.loads((home / "attention.json").read_text(encoding="utf-8"))
        items, count = data["items"], int(data["count"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if count <= 0:
        return None
    counts = {kind: sum(1 for item in items if isinstance(item, dict) and item.get("kind") == kind) for kind, _ in KINDS}
    parts = [f"{counts[kind]} {word}" + "s" * (counts[kind] > 1) for kind, word in KINDS if counts[kind]]
    more = count - sum(counts.values())  # the view lists at most six items
    text = ", ".join(parts) or f"{count} items"
    if parts and more > 0:
        text += f" and {more} more"
    return f"Still open: {text}. Run boss status."


def waiting(home):
    """True while some process holds watch.lock (a boss watch is waiting); the probe never keeps it."""
    try:
        descriptor = os.open(home / LOCK, os.O_RDWR)
    except FileNotFoundError:
        return False
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        return False
    except BlockingIOError:
        return True
    finally:
        os.close(descriptor)


def run(home, session_key, load, out):
    """Exit code. `out(text)` prints; items are acked only after it returns."""
    if not session_key:
        refuse("boss watch runs only in the Hermes session that owns boss", "run it from the owner session")
    owner.guard(home, session_key)
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(home / LOCK, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            notify(home, b"replace\n")  # the older watch exits without taking anything
            fcntl.flock(descriptor, fcntl.LOCK_EX)
        path = home / SOCKET
        try:
            path.unlink()  # we hold watch.lock, so any socket file is a dead watch's
        except FileNotFoundError:
            pass
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            # Bound before the first look: an append after that look queues a poke we accept next.
            server.bind(str(path))
            os.chmod(path, 0o600)
            server.listen(8)
            try:
                return wait(home, session_key, load, out, server)
            finally:
                path.unlink(missing_ok=True)
    finally:
        os.close(descriptor)


def wait(home, session_key, load, out, server):
    box = Inbox(home)
    while True:
        record = owner.read(home)
        if record is None or record["session_key"] != session_key:
            out("boss moved to another session; this watch stopped and took nothing.")
            return 0

        def send(text):
            footer = still_open(home)
            out(text + (f"\n{footer}" if footer else ""))
            return True
        if box.take(load, "watch", record["label"], send):
            return 0
        connection, _ = server.accept()
        with connection:
            connection.settimeout(1)
            try:
                message = connection.recv(16)
            except OSError:
                message = b""
        if message.startswith(b"replace"):
            out("A newer boss watch took over; this one took nothing.")
            return 0
