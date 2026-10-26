"""Event-driven wakes. No Hermes imports, model calls, timers or polling.

T3 pushes every thread change over its orchestration WebSocket (one
`orchestration.subscribeShell` stream per configured host). The listener runs
inside an already-running Hermes host process (gateway, TUI or desktop),
blocked on that socket. Pushes for a boss task's thread that still say it is
running are dropped unread, after noting in memory when the thread last
changed. When its status, run, pending request or PR states change, the push
triggers one Boss.snapshot read and `events.classify` says what it means;
anything but 'working', and any PR that merged or closed, lands once in the
owner's inbox on disk (see inbox.py). Every push and every poke (a claim, the
owner's turn starting or ending) also sweeps for running tasks quiet longer
than wake.stall_minutes and paused tasks past their "until" time, and a poke
re-reads open PRs on GitHub, since T3's copy of a PR's state goes stale when it
merges outside T3. The same sweep holds one line for a host whose stream has
stayed down longer than wake.host_down_minutes, and its reconnect holds one
"back in touch" line. There is no timer: a silent fleet and owner sweep nothing.
The owner's `boss watch` (a tracked background command) or its running turn's
next tool result picks the inbox up. Only with wake.inject_when_idle does the listener
inject it, while the owner is idle; an injected message shows as if the person
boss works for sent it, so it is off by default. With no owner, or an owner that is offline,
items stay in the inbox until the owner's next watch or turn.
Exactly one listener runs per machine: it holds $BOSS_HOME/wake.lock.
"""
from datetime import datetime, timezone
import fcntl
import json
import os
import socket
import sys
import threading
import time
import urllib.parse
import urllib.request

from .common import BossError, Redactor
from .config import Config, boss_home
from .events import FINISHED_PRS, Event, classify, describe, paused_until, pr_events, stall_text
from .inbox import Inbox, owner_busy
from .service import Boss
from .state import Store
from .websocket import WebSocket
from . import owner, panel

CONSENT_KEY = "plugins.entries.boss.allow_gateway_injection"
CONSENT_LINE = CONSENT_KEY + ": true"
LOCK = "wake.lock"
SOCKET = "wake.sock"
HEARTBEAT = "wake-heartbeat"
LAST = "wake-last.json"
TICKET = "/api/auth/websocket-ticket/rpc"
BUSY = {"preparing", "queued", "starting", "running"}
STALL_MINUTES = 15
HOST_DOWN_MINUTES = 10
PR_RECHECK = 300  # seconds: a poke re-reads one task's open PRs on GitHub at most this often
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
    try:
        # Block on the socket indefinitely; the kernel's TCP keepalive notices a dead peer.
        stream.sock.settimeout(None)
        stream.sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        stream.send(json.dumps({"_tag": "Request", "id": "1", "tag": "orchestration.subscribeShell",
                                "payload": {"omitOlderSettled": True, "omitIdleSubagents": True}, "headers": []}))
    except BaseException:
        stream.close()
        raise
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


def shell_prs(thread):
    """(url, STATE) of the PRs a pushed shell carries, for the wake signature."""
    prs = [*(thread.get("pullRequests") or ()), thread.get("linkedPullRequest")]
    states = {(p.get("url"), str((p.get("snapshot") or {}).get("state") or p.get("state") or "").upper())
              for p in prs if isinstance(p, dict) and p.get("url")}
    return tuple(sorted(states))


def epoch(stamp):
    try:
        return datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


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
        self.store = Store(home, config.redact, config.owner_name)
        self.inbox = Inbox(home, config.redact)
        self.stop_event = threading.Event()
        self.deliver_lock = threading.Lock()
        self.descriptor = self.server = None
        self.sockets, self.refused, self.tasks, self.tasks_stamp, self.seen = {}, None, {}, None, {}
        # In memory only. activity: thread id -> when it last changed; paused: thread id -> its "until".
        self.activity, self.paused, self.checked, self.rechecked = {}, {}, set(), {}
        # host -> {"since": when its stream dropped, "told": its lost-touch line was held}; memory only.
        self.down, self.down_lock = {}, threading.Lock()
        self.clock = time.time
        self.sweep_lock = threading.Lock()
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
                        self.sweep(github=True)
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
                self.reconnected(name)
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
                with self.down_lock:
                    self.down.setdefault(name, {"since": self.clock(), "told": False})
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
            held = self.handle(host, thread)
            if self.sweep() or held:
                self.deliver()
        finally:
            self.done("pushes")

    def handle(self, host, thread):
        """True when this push put something new in the inbox."""
        request = thread.get("pendingRuntimeRequest") or {}
        busy = thread.get("status") in BUSY and not request
        self.note(host, thread, busy)
        if busy:
            return False  # most pushes: a running worker's progress, nothing to read
        task = self.task_for(host, thread.get("id"))
        signature = (thread.get("status"), thread.get("latestRunId"), request.get("id"), shell_prs(thread))
        if task is None or self.seen.get(thread["id"]) == signature:
            return False  # T3 re-pushes the same state (and re-snapshots); read each state once
        return self.read(task, thread["id"], signature)

    def note(self, host, thread, busy):
        """Remember when each thread last changed: the stall sweep's clock. Memory only, no I/O."""
        thread_id, mark = thread.get("id"), thread.get("updatedAt") or thread.get("itemCount")
        entry = self.activity.get(thread_id)
        if mark is None or entry is None or entry["mark"] != mark:
            since = self.clock()
            if entry is None and mark is not None:
                # First sight (listener start): T3's own updatedAt, so a hang from before a restart counts.
                since = min(since, epoch(thread.get("updatedAt")) or since)
            entry = {"host": host, "mark": mark, "since": since}
        entry.update(busy=busy, run=thread.get("latestRunId"))
        self.activity[thread_id] = entry
        if busy:
            self.paused.pop(thread_id, None)  # a paused worker that runs again is no longer waiting

    def read(self, task, thread_id, signature=None, quiet=None):
        """One snapshot read of a task; holds what the owner needs. True when something new was held,
        None when the read failed.

        `quiet` is (since, key) for a sweep's read of a thread that looked stalled.
        """
        held = []
        try:
            with self.store.locked(wait=True):
                meta = self.store.load(task)
                if meta.get("settled"):
                    return False
                # One read per state change. A fresh Boss keeps no long-lived MCP session to go
                # stale between events.
                boss = Boss(self.config, self.store, self.config.redact)
                view = boss.snapshot(meta)
                event = classify(view)
                if event.kind == "working" and quiet and view["status"] in BUSY:
                    since, key = quiet
                    minutes = int((self.clock() - since) // 60)
                    event = Event("stalled", thread_id, event.turn_id, key, stall_text(minutes, view["assistant"]))
                if event.kind != "working":
                    # A panel consumes its writers' and judges' finishes and says one line per judged round.
                    consumed, round_line = panel.advance(boss, meta, event) if meta.get("panel") else (False, None)
                    if consumed:
                        if round_line:
                            held.append(self.inbox.hold(f"panel:{event.dedupe_key}", task, "panel", round_line,
                                                        run=event.turn_id))
                    else:
                        questions = boss.questions(meta, event.request_ids) if event.request_ids else []
                        line = describe(meta.get("title"), event, view["prs"], questions)
                        if event.kind == "done" and event.pr_urls and self.docs_only(boss, meta, view["prs"]):
                            line += "; docs only, so boss can land it without asking"
                        if round_line:
                            line += " " + round_line
                        held.append(self.inbox.hold(event.dedupe_key, task, event.kind, line, run=event.turn_id,
                                                    request_ids=event.request_ids, pr_urls=event.pr_urls))
                until = paused_until(event.detail, self.clock()) if event.kind == "paused" else None
                if until is not None:
                    self.paused[thread_id] = (until, task, event.turn_id, event.detail, self.clock())
                elif not quiet:
                    self.paused.pop(thread_id, None)
                if not meta.get("landed"):  # boss landed it itself: nothing to tell
                    for pr in pr_events(view):
                        held.append(self.inbox.hold(pr.dedupe_key, task, pr.kind, describe(meta.get("title"), pr)))
                # A finished run stays in the owner's attention line until someone reviews it.
                self.store.refresh_attention()
                if signature is not None:
                    self.seen[thread_id] = signature
        except BossError as exc:
            # Not marked seen: the next push for this thread reads again.
            log_error(self.home, self.config.redact, f"boss wake read {task} failed: {exc.code} {exc.message}")
            return None
        return any(record is not None for record in held)

    def sweep(self, github=False):
        """Hold one 'stalled' event per stall: a running task quiet longer than wake.stall_minutes, or a
        paused one past its "until". True when something new was held.

        Runs on every push and poke, never on a timer. Memory only until something is due, then one
        T3 read per stall. With github (pokes only), also re-read open PRs on GitHub.
        """
        if not self.sweep_lock.acquire(blocking=github):
            return False  # another push is sweeping right now
        now, held = self.clock(), False
        try:
            limit = self.wake.get("stall_minutes", STALL_MINUTES) * 60
            for thread_id, entry in list(self.activity.items()):
                key = f"{thread_id}:stalled:{entry['run']}:{entry['mark'] or int(entry['since'])}"
                if not limit or not entry["busy"] or now - entry["since"] < limit or key in self.checked:
                    continue
                self.checked.add(key)  # one read per stall, however many pushes follow
                task = self.task_for(entry["host"], thread_id)
                result = None if task is None else self.read(task, thread_id, quiet=(entry["since"], key))
                if result is None and task is not None:
                    self.checked.discard(key)  # the read failed: the next sweep tries again
                held = bool(result) or held
            for thread_id, (until, task, run, detail, since) in list(self.paused.items()):
                if now >= until:
                    self.paused.pop(thread_id, None)
                    held = self.overdue(task, thread_id, run, since, detail) or held
            held = self.out_of_touch(now) or held
            if github:
                held = self.recheck(now) or held
        except Exception as exc:
            # A sweep never takes the push stream down; the next push or poke sweeps again.
            log_error(self.home, self.config.redact, "boss wake sweep failed: " + type(exc).__name__)
        finally:
            self.sweep_lock.release()
        return held

    def out_of_touch(self, now):
        """One line per host whose push stream has stayed down past wake.host_down_minutes: its
        workers may finish or ask with nobody hearing. True when a line was held."""
        limit, held = self.wake.get("host_down_minutes", HOST_DOWN_MINUTES) * 60, False
        with self.down_lock:
            for host, entry in self.down.items():
                if not limit or entry["told"] or now - entry["since"] < limit:
                    continue
                entry["told"] = True
                since = datetime.fromtimestamp(entry["since"]).astimezone().strftime("%H:%M")
                held = self.inbox.hold(f"host-down:{host}:{int(entry['since'])}", None, "host-down",
                                       f"lost touch with {host} since {since}; workers there may be unheard") is not None or held
        return held

    def reconnected(self, host):
        """A stream is back. Say so once, only if its lost-touch line went out; one nobody saw yet
        is dropped instead, since T3's fresh snapshot now covers the gap."""
        with self.down_lock:
            entry = self.down.pop(host, None)
        if not entry or not entry["told"]:
            return
        if self.inbox.drop(f"host-down:{host}:{int(entry['since'])}", "back in touch before anyone saw it"):
            return
        if self.inbox.hold(f"host-back:{host}:{int(entry['since'])}", None, "host-back",
                           f"back in touch with {host}") is not None:
            self.deliver()

    def overdue(self, task, thread_id, run, since, detail):
        """A paused worker whose own "until" passed: one stalled event, no T3 read (the line says it all)."""
        try:
            with self.store.locked(wait=True):
                meta = self.store.load(task)
                if meta.get("settled") or (meta.get("observed") or {}).get("run_id") not in (None, run):
                    return False
                minutes = int((self.clock() - since) // 60)
                event = Event("stalled", thread_id, run, f"{thread_id}:stalled:{run}:paused",
                              stall_text(minutes, "paused, waiting on " + detail))
                return self.inbox.hold(event.dedupe_key, task, event.kind, describe(meta.get("title"), event),
                                       run=run) is not None
        except (BossError, OSError, ValueError) as exc:
            log_error(self.home, self.config.redact, f"boss wake paused check {task} failed: {type(exc).__name__}")
            return False

    def recheck(self, now):
        """T3's copy of a PR's state goes stale when it merges on GitHub, so a poke asks GitHub.

        Only tasks whose ledger shows an open PR, each at most every PR_RECHECK seconds; a merged
        or closed one triggers the task's normal read, which holds its PR event.
        """
        held, boss = False, None
        for meta in self.store.tasks(active=True):
            urls = [p["url"] for p in (meta.get("observed") or {}).get("prs", []) if p.get("state") == "OPEN"]
            last = self.rechecked.get(meta["id"])
            if not urls or meta.get("landed") or (last is not None and now - last < PR_RECHECK):
                continue
            self.rechecked[meta["id"]] = now
            boss = boss or Boss(self.config, self.store, self.config.redact)
            try:
                finished = any(str(boss.github.view(url)["state"]).upper() in FINISHED_PRS for url in urls)
            except BossError as exc:
                log_error(self.home, self.config.redact, f"boss wake PR check {meta['id']} failed: {exc.code} {exc.message}")
                continue
            if finished:
                held = bool(self.read(meta["id"], meta["thread_id"])) or held
        return held

    def docs_only(self, boss, meta, prs):
        """A hint only: a GitHub hiccup here must never cost the wake itself."""
        try:
            return boss.docs_only_pr(meta, prs) is not None
        except BossError as exc:
            log_error(self.home, self.config.redact, f"boss wake docs check {meta['id']} failed: {exc.code} {exc.message}")
            return False

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
        """Inject the inbox into an idle owner; ack only what the owner's host accepted.

        Only with wake.inject_when_idle, and never while the owner's turn runs: that
        turn sees the inbox on its next tool result, and its end pokes this listener.
        """
        with self.deliver_lock:
            if not self.wake.get("inject_when_idle", False):
                return False
            target = self.target()
            if target is None or not self.armed() or self.stop_event.is_set():
                return False
            if owner_busy(self.home, target.get("session_key")):
                return False
            label = target.pop("label")
            sent = []

            def send(text):
                sent.append(self.ctx.inject_message(text, role="user", **target))
                return sent[0]
            self.inbox.take(self.store.load, "inject", label, send)
            if not sent:
                return False  # nothing open, or everything open was stale
            accepted = sent[0]
            self.record(label, accepted)
            if not accepted:
                if self.refused != label:
                    log_error(self.home, self.config.redact,
                              f"wake for {label} refused; held on disk until it is reachable or another session"
                              f" runs /boss. Check {CONSENT_LINE}")
                    self.refused = label
                return False
            self.refused = None
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
