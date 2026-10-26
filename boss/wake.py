"""Event-driven wakes. No Hermes imports, model calls, timers or polling.

T3 pushes every thread change over its orchestration WebSocket (one
`orchestration.subscribeShell` stream per configured host). The listener runs
inside an already-running Hermes host process (gateway, TUI or desktop),
blocked on that socket. Pushes for a boss task's thread that still say it is
running are dropped unread. When its status, run or pending request changes,
the push triggers one Boss.snapshot read and `events.classify` says what it
means; anything but 'working' is held once on disk (deduped by the event's
key) and injected as one line into the session that owns boss.
With no owner, or an owner that is offline, the wake stays held and goes out
on the next push, when a session claims boss, or on the owner's next turn.
Exactly one listener runs per machine: it holds $BOSS_HOME/wake.lock.
"""
from datetime import datetime, timezone
import fcntl
import json
import os
import socket
import sys
import threading
import urllib.parse
import urllib.request

from .common import BossError, Redactor
from .config import Config, boss_home
from .events import classify
from .service import Boss
from .state import Store
from .websocket import WebSocket
from . import owner

CONSENT_KEY = "plugins.entries.boss.allow_gateway_injection"
CONSENT_LINE = CONSENT_KEY + ": true"
LOCK = "wake.lock"
SOCKET = "wake.sock"
HEARTBEAT = "wake-heartbeat"
LAST = "wake-last.json"
TICKET = "/api/auth/websocket-ticket/rpc"
BUSY = {"preparing", "queued", "starting", "running"}
WORDS = {"input": "needs input", "approval": "needs a permission decision", "finished_pr": "finished with a PR",
         "finished_report": "finished with a report", "ended_unclear": "turn ended unclear", "error": "error"}
_lock = threading.Lock()
_listeners = {}


def legacy_target(wake):
    """Pre-ownership [wake] target, read only while no session owns boss."""
    target = {key: wake[key] for key in ("origin", "session_key") if key in wake}
    return {**target, "label": "legacy [wake] target"} if target else None


def log_error(home, redact, message):
    """Redact before shortening; never let an unwritable log break Hermes."""
    try:
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
        text = " ".join(redact(message).split())[:2000]
        descriptor = os.open(home / "wake-errors.log", os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(descriptor, "a") as stream:
            stream.write(datetime.now(timezone.utc).isoformat() + " " + text + "\n")
    except (OSError, UnicodeError):
        pass


def endpoints(mcp_url):
    """The T3 server behind an MCP URL: its WebSocket and ticket endpoints."""
    parts = urllib.parse.urlsplit(mcp_url)
    origin = f"{parts.scheme}://{parts.netloc}"
    return ("wss" if parts.scheme == "https" else "ws") + f"://{parts.netloc}/ws", origin + TICKET


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def open_stream(mcp_url, token):
    """Exchange the bearer token for a one-use ticket, then subscribe to every thread's shell."""
    ws_url, ticket_url = endpoints(mcp_url)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    request = urllib.request.Request(ticket_url, data=b"", method="POST", headers={"Authorization": "Bearer " + token})
    with opener.open(request, timeout=30) as response:
        ticket = json.load(response)["ticket"]
    query = urllib.parse.urlencode({"wsTicket": ticket, "orchestrationProtocol": 2, "clientSurface": "boss"})
    stream = WebSocket(ws_url + "?" + query)
    # Block on the socket indefinitely; the kernel's TCP keepalive notices a dead peer.
    stream.sock.settimeout(None)
    stream.sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    stream.send(json.dumps({"_tag": "Request", "id": "1", "tag": "orchestration.subscribeShell",
                            "payload": {"omitOlderSettled": True, "omitIdleSubagents": True}, "headers": []}))
    return stream


def shell_threads(message):
    """Thread shells carried by one RPC frame: snapshot rows and live thread.updated items."""
    if message.get("_tag") == "Exit":
        raise ConnectionError("T3 ended the subscription")
    if message.get("_tag") != "Chunk":
        return []
    threads = []
    for item in message.get("values", []):
        if item.get("kind") == "snapshot":
            threads.extend(item["snapshot"].get("threads", []))
        elif item.get("kind") == "thread.updated":
            threads.append(item["thread"])
    return threads


def backoff(failures):
    """Seconds before reconnecting a dropped stream (T3 restarts, network blips)."""
    return min(2 ** failures, 300)


def poke(home):
    """Ask the running listener to deliver held wakes now (after a claim, or on the owner's turn)."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(1)
            client.connect(str(home / SOCKET))
            client.sendall(b"flush\n")
    except OSError:
        pass


class Listener:
    def __init__(self, ctx, config, home):
        self.ctx, self.config, self.home = ctx, config, home
        self.wake = config.data.get("wake", {})
        self.store = Store(home, config.redact)
        self.stop_event = threading.Event()
        self.deliver_lock = threading.Lock()
        self.descriptor = self.server = None
        self.sockets, self.refused, self.tasks, self.tasks_stamp, self.seen = {}, None, {}, None, {}
        # Completion counters, bumped only after everything a step writes is on disk.
        self.progress = threading.Condition()
        self.counts = {"streams": 0, "drops": 0, "pushes": 0, "pokes": 0}
        self.thread = threading.Thread(target=self.run, name="boss-wake", daemon=True)

    def done(self, step):
        with self.progress:
            self.counts[step] += 1
            self.progress.notify_all()

    def reached(self, step, count, timeout):
        """True once `count` of `step` have finished (doctor-style observability; tests wait on it)."""
        with self.progress:
            return self.progress.wait_for(lambda: self.counts[step] >= count, timeout)

    def armed(self):
        """This process can reach a session; the classic CLI queue is never a wake target."""
        manager = self.ctx._manager
        return manager._cli_ref is None and bool(
            getattr(manager, "has_gateway_message_injector", False) or getattr(manager, "has_tui_message_injector", False))

    def beat(self, state):
        """Holder pid for boss doctor; written on start and on stream changes, never on a timer."""
        try:
            (self.home / HEARTBEAT).write_text(f"{datetime.now(timezone.utc).isoformat()} {os.getpid()} {state}\n")
        except OSError:
            pass

    def run(self):
        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(self.home / LOCK, os.O_RDWR | os.O_CREAT, 0o600)
        # Blocks (zero CPU) while another process listens; takes over when that process exits.
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        self.descriptor = descriptor
        if self.stop_event.is_set():
            return self.release()
        self.beat("listening")
        self.serve()
        for name in self.config.hosts:
            threading.Thread(target=self.follow, args=(name,), name=f"boss-wake-{name}", daemon=True).start()
        self.deliver()
        self.stop_event.wait()
        self.release()

    def serve(self):
        path = self.home / SOCKET
        try:
            path.unlink()  # we hold wake.lock, so any socket file is a dead listener's
        except FileNotFoundError:
            pass
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(str(path))
        os.chmod(path, 0o600)
        self.server.listen(4)
        threading.Thread(target=self.accept, name="boss-wake-poke", daemon=True).start()

    def accept(self):
        while not self.stop_event.is_set():
            try:
                connection, _ = self.server.accept()
            except OSError:
                return
            with connection:
                connection.settimeout(1)
                try:
                    if connection.recv(16).startswith(b"flush"):
                        self.deliver()
                except Exception as exc:
                    log_error(self.home, self.config.redact, "boss wake poke failed: " + type(exc).__name__)
                finally:
                    self.done("pokes")

    def follow(self, name):
        """One push stream per host. Reconnects after a drop; T3's snapshot covers the gap."""
        failures = 0
        while not self.stop_event.is_set():
            stream = None
            try:
                token = self.config.events_token(name)
                stream = open_stream(self.config.hosts[name]["url"], token)
                self.sockets[name] = stream
                self.beat(f"listening host={name}")
                self.done("streams")
                failures = 0
                while not self.stop_event.is_set():
                    message = json.loads(stream.receive())
                    for thread in shell_threads(message):
                        self.pushed(name, thread)
                    if message.get("_tag") == "Chunk":
                        stream.send(json.dumps({"_tag": "Ack", "requestId": message["requestId"]}))
            except BossError as exc:
                log_error(self.home, self.config.redact, f"boss wake {name}: {exc.message}")
                self.done("drops")
                return  # no token for this host: doctor says how to add one
            except Exception as exc:
                if self.stop_event.is_set():
                    return
                failures += 1
                log_error(self.home, self.config.redact, f"boss wake {name} stream dropped: {type(exc).__name__}")
                self.done("drops")
            finally:
                if stream is not None:
                    stream.close()
            # Reconnect backoff after a failure only; a healthy stream never wakes this thread.
            self.stop_event.wait(backoff(failures))

    def task_for(self, host, thread_id):
        """Active boss task for a pushed thread; the map reloads only when a task is added."""
        try:
            stamp = (self.home / "tasks").stat().st_mtime_ns
        except FileNotFoundError:
            return None
        if stamp != self.tasks_stamp:
            self.tasks = {(m["host"], m["thread_id"]): m["id"] for m in self.store.tasks() if m.get("thread_id")}
            self.tasks_stamp = stamp
        return self.tasks.get((host, thread_id))

    def pushed(self, host, thread):
        """Handle one pushed thread shell, including any delivery it triggers."""
        try:
            self.handle(host, thread)
        finally:
            self.done("pushes")

    def handle(self, host, thread):
        request = thread.get("pendingRuntimeRequest") or {}
        if thread.get("status") in BUSY and not request:
            return  # most pushes: a running worker's progress, nothing to read
        task = self.task_for(host, thread.get("id"))
        signature = (thread.get("status"), thread.get("latestRunId"), request.get("id"))
        if task is None or self.seen.get(thread["id"]) == signature:
            return  # T3 re-pushes the same state (and re-snapshots); read each state once
        try:
            with self.store.locked(wait=True):
                meta = self.store.load(task)
                if meta.get("settled"):
                    return
                # One read per state change, triggered by this push. A fresh Boss keeps no
                # long-lived MCP session to go stale between events.
                event = classify(Boss(self.config, self.store, self.config.redact).snapshot(meta))
                self.seen[thread["id"]] = signature
                if event.kind == "working":
                    return
                text = WORDS[event.kind] + (f": {event.detail}" if event.detail else "")
                record = self.store.hold_wake(event.dedupe_key, task, event.kind, text)
                self.store.refresh_attention()
        except BossError as exc:
            # Not marked seen: the next push for this thread reads again.
            log_error(self.home, self.config.redact, f"boss wake read {task} failed: {exc.code} {exc.message}")
            return
        if record is not None:
            self.deliver()

    def target(self):
        try:
            record = owner.read(self.home)
        except BossError as exc:
            log_error(self.home, self.config.redact, "boss wake: " + exc.message)
            return None
        if record is not None:
            return {"session_key": record["session_key"], "label": record["label"]}
        return legacy_target(self.wake)

    def deliver(self):
        """Inject every held wake as one message to the owner; mark them delivered only if accepted."""
        with self.deliver_lock:
            target = self.target()
            if target is None or not self.armed() or self.stop_event.is_set():
                return False
            with self.store.locked(wait=True):
                held = self.store.held_wakes()
            if not held:
                return False
            titles = {}
            for wake in held:
                if wake["task"] not in titles:
                    try:
                        titles[wake["task"]] = self.store.load(wake["task"]).get("title") or wake["task"]
                    except BossError:
                        titles[wake["task"]] = wake["task"]
            lines = [f"{wake['task']} ({' '.join(titles[wake['task']].split())[:60]}): {wake['text']}" for wake in held]
            label = target.pop("label")
            content = self.config.redact("[boss wake] " + "; ".join(lines))[:1500]
            accepted = self.ctx.inject_message(content + ". Run boss status <id> to review.", role="user", **target)
            self.record(label, accepted)
            if not accepted:
                if self.refused != label:
                    log_error(self.home, self.config.redact,
                              f"wake for {label} refused; held on disk until it is reachable or another session"
                              f" runs /boss. Check {CONSENT_LINE}")
                    self.refused = label
                return False
            self.refused = None
            with self.store.locked(wait=True):
                self.store.wakes_delivered([wake["key"] for wake in held], label)
                self.store.refresh_attention()
            return True

    def record(self, label, accepted):
        try:
            text = json.dumps({"at": datetime.now(timezone.utc).isoformat(), "pid": os.getpid(), "owner": label,
                               "result": "delivered" if accepted else "refused"}, sort_keys=True)
            (self.home / LAST).write_text(text + "\n")
        except OSError:
            pass

    def release(self):
        if self.server is not None:
            try:
                self.server.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.server.close()
            self.server = None
        if self.descriptor is not None:
            os.close(self.descriptor)
            self.descriptor = None

    def stop(self):
        self.stop_event.set()
        for stream in list(self.sockets.values()):
            try:
                stream.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        # A thread still blocked on another process's lock exits as soon as it gets it.
        self.thread.join(5)


def host_process():
    """A long-lived Hermes host (gateway, Ink TUI backend, desktop/web app). CLI children never listen."""
    spec = getattr(getattr(sys.modules.get("__main__"), "__spec__", None), "name", "") or ""
    argv = [str(arg) for arg in sys.argv[1:]]
    gateway = "gateway" in argv and "run" in argv[argv.index("gateway"):]
    return (os.environ.get("_HERMES_GATEWAY") == "1" or spec in ("gateway.run", "tui_gateway.entry") or
            gateway or "dashboard" in argv)


def start_wake(ctx, force=False):
    """From register() (host processes) and from hooks once this process can inject. Cheap and idempotent."""
    home, redact = boss_home(), Redactor()
    try:
        config = Config(home, redact, hermes=ctx._manager.home_path)
        if not config.data.get("wake", {}).get("enabled", True):
            return None
        key = (str(config.hermes.resolve()), str(home.resolve()))
        with _lock:
            current = _listeners.get(key)
            if current is not None and current.thread.is_alive():
                return current
            listener = Listener(ctx, config, home)
            if not (force or host_process() or listener.armed()):
                return None
            ctx.on_unload(listener.stop)
            _listeners[key] = listener
            listener.thread.start()
            return listener
    except Exception as exc:
        log_error(home, redact, "boss wake setup failed: " + type(exc).__name__)
        return None
