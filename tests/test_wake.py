"""Pushed T3 events reach the owner session: a fake T3 WebSocket server, real listener, real ledger."""
import base64
import fcntl
from datetime import datetime, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import queue
import re
import socket
import struct
import subprocess
import sys
import threading
from types import SimpleNamespace
from unittest.mock import patch

from boss.common import BossError, Redactor
from boss.config import Config
from boss.inbox import NOTE, Inbox, live_turn
from boss.service import Boss
from boss.wake import LOCK, TICKET, Listener, _listeners, endpoints, start_wake
from boss.websocket import GUID, WebSocket
import plugin
from tests import test_boss as harness
from tests.support import HermeticTestCase


EVENTS_TOKEN = "events-secret-token-never-print"
WAIT = 5
T0 = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc).timestamp()


def stamp(seconds):
    """T3's updatedAt for an epoch time."""
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat().replace("+00:00", "Z")


def linked(state):
    """A pullRequests entry as T3's pushed shell carries it (seen live: snapshot.state is lowercase)."""
    return {"url": harness.URL, "source": "agent", "snapshot": {"state": state, "title": "Fix the thing"}}


def frame(text):
    payload = text.encode()
    size = len(payload)
    header = bytes([0x81]) + (bytes([size]) if size < 126 else bytes([126]) + struct.pack("!H", size)
                              if size < 1 << 16 else bytes([127]) + struct.pack("!Q", size))
    return header + payload


def read_frame(stream):
    first, second = stream.read(2)
    size = second & 0x7F
    if size == 126:
        size = struct.unpack("!H", stream.read(2))[0]
    elif size == 127:
        size = struct.unpack("!Q", stream.read(8))[0]
    mask = stream.read(4)
    payload = bytes(b ^ mask[i % 4] for i, b in enumerate(stream.read(size)))
    return first & 0x0F, payload.decode()


class FakePush:
    """T3's ticket endpoint and orchestration WebSocket; tests push shell items through `send`."""

    def __init__(self):
        self.connections, self.subscriptions = [], []
        self.channels = []
        self.down = False  # True: the host is unreachable, every reconnect fails
        self.registered = threading.Condition()  # notified once a subscriber can receive pushes
        push = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                if push.down:
                    self.send_response(503)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if self.path != "/api/auth/websocket-ticket/rpc" or self.headers.get("Authorization") != "Bearer " + EVENTS_TOKEN:
                    self.send_response(401)
                    self.end_headers()
                    return
                body = json.dumps({"ticket": "ticket-1", "expiresAt": "2026-10-26T13:07:50.799Z"}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if not re.match(r"^/ws\?wsTicket=ticket-1&orchestrationProtocol=2", self.path):
                    self.send_response(403)
                    self.end_headers()
                    return
                accept = base64.b64encode(hashlib.sha1((self.headers["Sec-WebSocket-Key"] + GUID).encode()).digest()).decode()
                self.wfile.write(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                                  f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode())
                self.wfile.flush()
                _, subscribe = read_frame(self.rfile)
                channel = queue.Queue()
                with push.registered:
                    push.subscriptions.append(json.loads(subscribe))
                    push.channels.append(channel)
                    push.connections.append(self.path)
                    push.registered.notify_all()
                threading.Thread(target=self.drain, daemon=True).start()
                try:
                    while True:
                        item = channel.get()
                        if item is None:
                            self.wfile.write(bytes([0x88, 0]))
                            self.wfile.flush()
                            return
                        self.wfile.write(frame(json.dumps({"_tag": "Chunk", "requestId": "1", "values": item})))
                        self.wfile.flush()
                except OSError:
                    pass  # the listener closed its end

            def drain(self):
                try:
                    while True:
                        read_frame(self.rfile)  # client Acks
                except Exception:
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/mcp"

    def connected(self, count):
        """Wait until `count` subscribers are registered server-side (the client may be ahead of us)."""
        with self.registered:
            return self.registered.wait_for(lambda: len(self.channels) >= count, WAIT)

    def send(self, *values):
        self.channels[-1].put(list(values))

    def drop(self):
        self.channels[-1].put(None)

    def close(self):
        for channel in self.channels:
            channel.put(None)
        self.server.shutdown()
        self.server.server_close()


def plugin_busy(home):
    return live_turn(home) is not None


def shell(thread_id, status="running", run="run-1", request=None, **extra):
    return {"id": thread_id, "title": "worker", "status": status, "latestRunId": run,
            "pendingRuntimeRequest": request, "settledAt": None, **extra}


def updated(thread, sequence=1):
    return {"kind": "thread.updated", "sequence": sequence, "location": "active", "thread": thread}


def snapshot(*threads):
    return {"kind": "snapshot", "snapshot": {"schemaVersion": 2, "snapshotSequence": 1, "projects": [],
                                             "threads": list(threads), "archivedThreads": []}}


class Context:
    def __init__(self, home, *, gateway=True, tui=False, cli=None, accepted=True):
        self._manager = SimpleNamespace(home_path=home, _cli_ref=cli, has_gateway_message_injector=gateway,
                                        has_tui_message_injector=tui)
        self.accepted, self.messages, self.hooks, self.commands, self.cleanups = accepted, [], [], {}, []

    def inject_message(self, content, **target):
        self.messages.append((content, target))
        return self.accepted

    def register_hook(self, event, callback):
        self.hooks.append((event, callback))

    def register_command(self, name, handler, description="", args_hint=""):
        self.commands[name] = handler

    def on_unload(self, callback):
        self.cleanups.append(callback)


class WakeTest(HermeticTestCase):
    setUp_cli = harness.CLITest.setUp
    tearDown_cli = harness.CLITest.tearDown
    cli = harness.CLITest.cli
    spawn = harness.CLITest.spawn
    completed = harness.CLITest.completed

    def setUp(self):
        self.setUp_cli()
        self.addCleanup(self.tearDown_cli)
        self.push = FakePush()
        self.addCleanup(self.push.close)
        os.environ["T3_EVENTS_TOKEN"] = EVENTS_TOKEN
        # MCP (spawn and the listener's snapshot reads) is FakeT3; the push stream is FakePush.
        streams = patch("boss.wake.endpoints", return_value=(self.push.url.replace("http", "ws", 1).replace("/mcp", "/ws"),
                                                             self.push.url.replace("/mcp", TICKET)))
        streams.start()
        self.addCleanup(streams.stop)
        self.task = self.spawn()
        self.thread = self.fake.threads[self.task["thread_id"]]
        self.listeners, self.listener = [], None
        # Most tests here drive the opt-in idle injection; test_idle_injection_is_off_by_default covers the default.
        self.inject = True
        self.addCleanup(self.stop_all)

    def stop_all(self):
        for listener in self.listeners:
            listener.stop()
        _listeners.clear()

    def config(self):
        config = Config(self.home, Redactor(), hermes=self.home / "profile")
        if getattr(self, "inject", False):
            config.data.setdefault("wake", {})["inject_when_idle"] = True  # as if set in config.toml
        return config

    def reads(self):
        return sum(1 for name, _ in self.fake.calls if name == "t3_thread_read")

    def state(self, status, run="run-1", **extra):
        """Set the fake T3 thread and return the shell T3 would push for it."""
        self.thread.update(status=status, latestRunId=run)
        return shell(self.task["thread_id"], status, run, **extra)

    def listen(self, ctx):
        listener = Listener(ctx, self.config(), self.home)
        self.listeners.append(listener)
        listener.thread.start()
        self.assertTrue(listener.reached("streams", 1, WAIT))
        self.assertTrue(self.push.connected(1))
        self.listener, self.sent, self.poked = listener, 0, 0
        return listener

    def send(self, *values):
        """Push shells, then wait for the listener's own signal that each one is fully handled
        (ledger, attention, delivery, logs); never on files appearing or on sleeps."""
        self.push.send(*values)
        self.sent += sum(len(v["snapshot"]["threads"]) if v["kind"] == "snapshot" else 1 for v in values)
        self.assertTrue(self.listener.reached("pushes", self.sent, WAIT))

    def own(self, key, label, *extra):
        with patch.dict(os.environ, {"HERMES_SESSION_KEY": key}):
            code, output = self.cli("own", "--label", label, *extra)
        self.assertEqual(code, 0, output)
        if self.listener is not None:
            # boss own pokes the running listener; wait until that delivery attempt is done.
            self.poked += 1
            self.assertTrue(self.listener.reached("pokes", self.poked, WAIT))
        return output

    def held(self):
        return Inbox(self.home).open_items()

    def inbox_log(self, event):
        return [r for r in Inbox(self.home).records() if r["event"] == event]

    def test_pushed_event_reaches_owner_session_once(self):
        self.own("agent:owner", "Discord DM")
        ctx = Context(self.home / "profile")
        self.listen(ctx)
        self.assertEqual(self.push.subscriptions[0]["tag"], "orchestration.subscribeShell")
        reads = self.reads()
        # Running progress and other people's threads cost nothing: no T3 read, no wake.
        self.send(snapshot(shell(self.task["thread_id"])), updated(shell("someone-elses-thread", "completed")))
        self.send(*[updated(shell(self.task["thread_id"]), n) for n in range(2, 6)])
        self.assertEqual((self.reads(), ctx.messages), (reads, []))
        self.completed(self.task, "Three callers, all in cli.py.\nBOSS: done: three callers found")
        self.send(updated(self.state("completed"), 6))
        self.assertEqual(self.reads(), reads + 1)
        self.assertEqual(len(ctx.messages), 1)
        content, target = ctx.messages[0]
        self.assertEqual(target, {"role": "user", "session_key": "agent:owner"})
        self.assertEqual(content, f"[boss inbox]\nFix thing: finished: three callers found\n{NOTE}")
        self.assertNotIn(self.task["id"], content)
        # T3 pushes the same completion several times and re-snapshots; still one wake, no re-read.
        self.send(updated(self.state("completed"), 7), updated(self.state("completed"), 8))
        self.send(snapshot(self.state("completed")))
        self.assertEqual((len(ctx.messages), self.reads()), (1, reads + 1))
        self.fake.pending = {"req-1": [{"id": "q1", "question": "Which API?", "options": [{"label": "REST"}, {"label": "GraphQL"}]}]}
        self.send(updated(self.state("waiting", request={"id": "req-1", "kind": "user-input", "blocking": True}), 9))
        self.assertEqual(len(ctx.messages), 2)
        self.assertEqual(ctx.messages[1][0],
                         f'[boss inbox]\nFix thing: needs a decision: "Which API?" (options: REST / GraphQL)\n{NOTE}')
        self.assertEqual(self.reads(), reads + 2)
        self.assertEqual(self.held(), [])
        self.assertEqual([r["via"] for r in self.inbox_log("acked")], ["inject", "inject"])

    def test_failed_read_is_logged_and_the_next_push_reads_again(self):
        self.own("agent:owner", "Discord DM")
        ctx = Context(self.home / "profile")
        self.listen(ctx)
        self.fake.failure = "t3_thread_read"
        self.send(updated(self.state("failed")))
        log = (self.home / "wake-errors.log").read_text()
        self.assertIn("read", log)
        self.assertNotIn(harness.TOKEN, log)
        self.assertEqual(ctx.messages, [])
        self.fake.failure = None
        self.send(updated(self.state("failed"), 2))
        self.assertEqual(len(ctx.messages), 1)
        self.assertIn("Fix thing: stopped, the thread failed", ctx.messages[0][0])

    def test_no_owner_holds_on_disk_then_claim_delivers(self):
        ctx = Context(self.home / "profile")
        self.listen(ctx)
        self.send(updated(self.state("failed")))
        self.assertEqual(len(self.held()), 1)
        self.assertEqual(ctx.messages, [])
        self.own("agent:owner", "Hermes app")  # claiming pokes the listener
        self.assertEqual(len(ctx.messages), 1)
        self.assertEqual(ctx.messages[0][1]["session_key"], "agent:owner")
        self.assertIn("failed", ctx.messages[0][0])
        self.assertEqual(self.held(), [])

    def test_refused_injection_is_held_then_delivered_when_owner_returns_or_new_owner_claims(self):
        self.own("agent:owner", "Discord DM")
        ctx = Context(self.home / "profile", accepted=False)
        self.listen(ctx)
        self.send(updated(self.state("completed")))
        self.assertEqual(len(ctx.messages), 1)
        self.assertIn("refused", (self.home / "wake-errors.log").read_text())
        self.assertEqual(len(self.held()), 1)  # refused: not consumed
        self.send(updated(self.state("completed"), 2))  # same event again: no new record, no retry
        self.assertEqual((len(ctx.messages), len(self.held())), (1, 1))
        # A second, new event retries delivery; the first is now stale (the worker moved on), still refused.
        self.send(updated(self.state("interrupted", run="run-2"), 3))
        self.assertEqual((len(ctx.messages), len(self.held())), (2, 1))
        self.assertEqual([r["reason"] for r in self.inbox_log("dropped")], ["worker moved on to a newer turn"])
        # Another session takes over: one injection with the live event, then nothing is held.
        ctx.accepted = True
        self.own("agent:phone", "Telegram DM", "--take")
        self.assertEqual(len(ctx.messages), 3)
        content, target = ctx.messages[2]
        self.assertEqual(target["session_key"], "agent:phone")
        self.assertEqual(content, f"[boss inbox]\nFix thing: stopped, the thread interrupted\n{NOTE}")
        self.assertEqual(self.held(), [])
        last = json.loads((self.home / "wake-last.json").read_text())
        self.assertEqual((last["owner"], last["result"]), ("Telegram DM", "delivered"))

    def test_owner_turn_start_adds_nothing_to_the_persons_message(self):
        # Hermes shows turn-start context inside the person's own bubble: the inbox waits for boss watch.
        self.own("agent:owner", "Hermes CLI")
        ctx = Context(self.home / "profile", accepted=False)
        listener = self.listen(ctx)
        self.send(updated(self.state("completed")))
        self.assertEqual(len(ctx.messages), 1)
        hook = plugin.turn_hook(Context(self.home / "profile"))
        with patch("boss.wake.start_wake", return_value=listener):
            self.assertIsNone(hook(session_id="agent:owner", user_message="hi"))
            self.assertTrue(listener.reached("pokes", self.poked + 1, WAIT))  # turn start still sweeps
            self.assertIsNone(hook(session_id="agent:owner", user_message="again"))
            self.assertTrue(listener.reached("pokes", self.poked + 2, WAIT))
            self.assertTrue(plugin_busy(self.home))  # the owner-turn bookkeeping still runs
            with patch.dict(os.environ, {"HERMES_SESSION_KEY": "agent:owner"}):
                self.assertIsNone(hook(session_id="child", parent_session_id="agent:owner", user_message="sub"))
        self.assertEqual(len(ctx.messages), 1)  # the owner's turn is running: the listener never injects into it
        self.assertEqual(self.inbox_log("acked"), [])
        self.assertEqual(len(self.held()), 1)

    def test_idle_injection_is_off_by_default(self):
        # An injected message shows as if you sent it, so only boss watch delivers by default.
        self.inject = False
        self.own("agent:owner", "Hermes app")
        ctx = Context(self.home / "profile")
        self.listen(ctx)
        self.completed(self.task, "BOSS: done: shipped")
        self.send(updated(self.state("completed")))
        with patch.dict(os.environ, {"HERMES_SESSION_KEY": "agent:owner"}):
            self.cli("away", "lunch")  # pokes the listener too
        self.assertEqual(ctx.messages, [])
        self.assertEqual(self.inbox_log("acked"), [])
        with patch.dict(os.environ, {"HERMES_SESSION_KEY": "agent:owner"}):
            code, output = self.cli("watch")
        self.assertEqual(code, 0, output)
        self.assertIn("Fix thing: finished: shipped\nAway mode on at ", output)
        self.assertEqual([r["via"] for r in self.inbox_log("acked")], ["watch"])

    def watching(self):
        """The owner keeps a boss watch running: hold watch.lock as its process would."""
        descriptor = os.open(self.home / "watch.lock", os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, descriptor)
        fcntl.flock(descriptor, fcntl.LOCK_EX)

    def owner_turn(self, listener, session="sess-1"):
        """Start the owner's top-level turn through the real pre_llm_call hook; returns its context result."""
        hook = plugin.turn_hook(Context(self.home / "profile"))
        with patch("boss.wake.start_wake", return_value=listener), \
                patch.dict(os.environ, {"HERMES_SESSION_KEY": "agent:owner"}):
            result = hook(session_id=session, task_id="t", turn_id="turn-1", user_message="hi", parent_session_id="")
        # The owner's turn start pokes the listener to sweep; wait until that is done.
        self.poked += 1
        self.assertTrue(listener.reached("pokes", self.poked, WAIT))
        return result

    def tool(self, session="sess-1", result='{"output": "ok"}'):
        """One tool result through transform_tool_result, with the keywords Hermes passes."""
        with patch.dict(os.environ, {"HERMES_SESSION_KEY": "agent:owner"}):
            return plugin.on_tool_result(tool_name="terminal", args={}, result=result, session_id=session, task_id="t",
                                         tool_call_id="call-1", turn_id="turn-1", api_request_id="", duration_ms=3,
                                         status="ok", error_type=None, error_message=None)

    def turn_end(self, **kwargs):
        plugin.turn_end_hook(**kwargs)
        self.poked += 1
        self.assertTrue(self.listener.reached("pokes", self.poked, WAIT))

    def test_worker_finishing_mid_turn_is_seen_in_the_same_turn(self):
        self.own("agent:owner", "Hermes app")
        self.watching()
        ctx = Context(self.home / "profile")
        listener = self.listen(ctx)
        self.assertIsNone(self.owner_turn(listener))
        self.assertIsNone(self.tool())  # nothing waiting: the result is untouched
        self.gh.pr["title"] = "Fix the thing"
        self.fake.prs = [harness.URL]
        self.completed(self.task, f"All 40 tests pass.\nBOSS: done: {harness.URL}")
        self.send(updated(self.state("completed")))
        self.assertEqual(ctx.messages, [])  # never injected while the owner's turn runs
        # Another session's tool, or a subagent's (same session key, own session id), is untouched.
        self.assertIsNone(self.tool(session="sess-other"))
        self.assertEqual(self.tool(), '{"output": "ok"}\n\n[boss inbox]\n'
                                      f'Fix thing: finished, PR "Fix the thing" {harness.URL}\n{NOTE}')
        self.assertIsNone(self.tool())  # acked once
        self.turn_end(session_id="sess-1", completed=True)
        self.assertEqual(ctx.messages, [])  # nothing left for an idle injection
        self.assertEqual([r["via"] for r in self.inbox_log("acked")], ["tool"])

    def test_worker_finishing_after_the_last_tool_call_is_injected_once_the_turn_ends(self):
        self.own("agent:owner", "Hermes app")
        ctx = Context(self.home / "profile")
        listener = self.listen(ctx)
        self.owner_turn(listener)
        self.completed(self.task, "BOSS: blocked: no GitHub token on this host")
        self.send(updated(self.state("completed")))
        self.assertEqual(ctx.messages, [])
        # Another session's turn ending changes nothing; the owner's own end makes it idle.
        plugin.turn_end_hook(session_id="sess-other")
        self.assertTrue(plugin_busy(self.home))
        self.turn_end(session_id="sess-1", completed=True)
        self.assertEqual(ctx.messages, [(f"[boss inbox]\nFix thing: blocked: no GitHub token on this host\n{NOTE}",
                                         {"role": "user", "session_key": "agent:owner"})])
        self.assertEqual([r["via"] for r in self.inbox_log("acked")], ["inject"])
        # A gateway /stop ends the turn the same way (agent_loop_stopped names the session key).
        self.owner_turn(listener)
        self.assertTrue(plugin_busy(self.home))
        self.turn_end(session_key="agent:owner", platform="discord", reason="stop")
        self.assertFalse(plugin_busy(self.home))

    def test_replied_answered_and_merged_items_are_dropped_not_delivered(self):
        self.own("agent:owner", "Hermes app")
        ctx = Context(self.home / "profile", accepted=False)
        self.listen(ctx)
        # Already replied to: boss send after the event arrived handles it. Sends are marked [boss].
        self.completed(self.task, "Should I also rename the flag?")
        self.send(updated(self.state("completed")))
        self.assertEqual(self.cli("send", self.task["id"], "yes, rename it")[0], 0)
        sent = [a["message"] for n, a in self.fake.calls if n == "t3_thread_send"]
        self.assertEqual(sent, ["[boss] yes, rename it"])
        # Already answered: the question is no longer pending.
        self.fake.pending = {"req-1": [{"id": "q1", "question": "Which API?", "options": []}]}
        self.send(updated(self.state("waiting", run="run-2", request={"id": "req-1", "kind": "user-input"}), 2))
        self.assertEqual(self.cli("answer", self.task["id"], "req-1", "REST")[0], 0)
        ctx.accepted = True
        self.own("agent:owner", "Hermes app")  # re-opening pokes the listener
        self.assertEqual(len(ctx.messages), 2)  # only the two refused attempts; nothing stale went out
        self.assertEqual(sorted(r["reason"] for r in self.inbox_log("dropped")),
                         ["already handled: boss replied to or reviewed the worker after this", "question already answered"])
        # Merged: a PR the worker reported is merged before the owner sees it.
        self.fake.prs = [harness.URL]
        self.completed(self.task, f"BOSS: done: {harness.URL}")
        self.thread["items"][-1]["runId"] = "run-3"  # this turn's own message
        ctx.accepted = False
        self.send(updated(self.state("completed", run="run-3"), 3))
        self.gh.pr["state"] = "MERGED"
        Boss(self.config(), self.store, Redactor(), github=self.gh).snapshot(self.store.load(self.task["id"]))
        ctx.accepted = True
        self.own("agent:owner", "Hermes app")
        self.assertEqual(len(ctx.messages), 3)
        self.assertEqual(self.inbox_log("dropped")[-1]["reason"], "PR merged")
        self.assertEqual(self.held(), [])

    def test_cli_process_never_injects(self):
        self.own("agent:owner", "Discord DM")
        ctx = Context(self.home / "profile", gateway=False, cli=object())
        self.listen(ctx)
        self.send(updated(self.state("completed")))
        self.assertEqual(len(self.held()), 1)
        self.assertEqual(ctx.messages, [])

    def test_settled_task_is_ignored(self):
        self.own("agent:owner", "Discord DM")
        meta = self.store.load(self.task["id"])
        meta["settled"] = True
        self.store.save(meta, "settled")
        ctx = Context(self.home / "profile")
        self.listen(ctx)
        reads = self.reads()
        self.send(updated(self.state("completed")), updated(self.state("failed", run="run-2")), snapshot())
        self.assertEqual((self.reads(), ctx.messages), (reads, []))
        self.assertFalse((self.home / "inbox.jsonl").exists())

    def test_one_listener_machine_wide_and_takeover(self):
        first = self.listen(Context(self.home / "profile"))
        second = Listener(Context(self.home / "profile"), self.config(), self.home)
        self.listeners.append(second)
        second.thread.start()
        heartbeat = (self.home / "wake-heartbeat").read_text().split()
        self.assertEqual(heartbeat[1], str(os.getpid()))
        # The second blocks on wake.lock: it never connects while the first holds it.
        self.assertFalse(second.reached("streams", 1, 0.3))
        self.assertEqual(len(self.push.connections), 1)
        first.stop()
        self.assertTrue(second.reached("streams", 1, WAIT))
        self.assertTrue(self.push.connected(2))

    def test_lock_held_by_another_process_blocks_until_it_exits(self):
        holder = subprocess.Popen([sys.executable, "-c", (
            "import fcntl, os, sys; d = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600); "
            "fcntl.flock(d, fcntl.LOCK_EX); print('locked', flush=True); sys.stdin.read()"), str(self.home / LOCK)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.addCleanup(holder.kill)
        self.assertEqual(holder.stdout.readline().strip(), "locked")
        listener = Listener(Context(self.home / "profile"), self.config(), self.home)
        self.listeners.append(listener)
        listener.thread.start()
        self.assertFalse(listener.reached("streams", 1, 0.3))
        self.assertEqual(self.push.connections, [])
        holder.stdin.close()
        holder.wait(WAIT)
        self.assertTrue(listener.reached("streams", 1, WAIT))

    def test_dropped_stream_reconnects_and_snapshot_covers_the_gap(self):
        self.own("agent:owner", "Discord DM")
        ctx = Context(self.home / "profile")
        with patch("boss.wake.backoff", return_value=0.01):
            listener = self.listen(ctx)
            self.push.drop()
            self.assertTrue(listener.reached("drops", 1, WAIT))
            self.assertTrue(listener.reached("streams", 2, WAIT))
            self.assertTrue(self.push.connected(2))
        self.assertIn("dropped", (self.home / "wake-errors.log").read_text())
        self.send(snapshot(self.state("completed")))
        self.assertEqual(len(ctx.messages), 1)

    def test_bad_events_token_never_connects_and_is_redacted(self):
        os.environ["T3_EVENTS_TOKEN"] = "wrong-but-long-token-value"
        with patch("boss.wake.backoff", return_value=60):
            listener = Listener(Context(self.home / "profile"), self.config(), self.home)
            self.listeners.append(listener)
            listener.thread.start()
            self.assertTrue(listener.reached("drops", 1, WAIT))
        self.assertEqual(self.push.connections, [])
        log = (self.home / "wake-errors.log").read_text()
        self.assertIn("stream dropped", log)
        self.assertNotIn("wrong-but-long-token-value", log)

    def test_register_starts_listener_without_gateway_marker(self):
        # Hermes loads plugins before gateway.run sets _HERMES_GATEWAY (the original dead-loop bug).
        os.environ.pop("_HERMES_GATEWAY", None)
        ctx = Context(self.home / "profile", gateway=False)
        with patch.object(sys, "argv", ["hermes", "gateway", "run"]), \
                patch("boss.wake.Config", side_effect=lambda home, redact, hermes: self.config()):
            plugin.register(ctx)
        self.addCleanup(lambda: [cleanup() for cleanup in ctx.cleanups])
        self.assertEqual([name for name, _ in ctx.hooks],
                         ["pre_llm_call", "transform_tool_result", "on_session_end", "agent_loop_stopped"])
        self.assertEqual(len(ctx.cleanups), 1)
        listener = next(iter(_listeners.values()))
        self.assertTrue(listener.reached("streams", 1, WAIT))
        self.assertTrue(listener.thread.daemon and listener.thread.is_alive())
        plugin.register(ctx)  # reload in the same process: no second listener
        self.assertEqual(len(ctx.cleanups), 1)

    def test_cli_child_starts_nothing_until_it_can_inject(self):
        ctx = Context(self.home / "profile", gateway=False)
        with patch.object(sys, "argv", ["hermes", "chat"]), patch.dict(os.environ, {}, clear=False):
            os.environ.pop("_HERMES_GATEWAY", None)
            plugin.register(ctx)
            self.assertEqual(_listeners, {})
            # The desktop backend publishes its TUI injector later; its first turn starts the listener.
            ctx._manager.has_tui_message_injector = True
            with patch("boss.wake.Config", side_effect=lambda home, redact, hermes: self.config()):
                ctx.hooks[0][1](session_id="s", user_message="hello")
        self.addCleanup(lambda: [cleanup() for cleanup in ctx.cleanups])
        self.assertTrue(next(iter(_listeners.values())).reached("streams", 1, WAIT))

    def test_disabled_wake_starts_nothing(self):
        Config(self.home, Redactor()).change("wake.enabled", False)
        with patch.object(sys, "argv", ["hermes", "gateway", "run"]):
            self.assertIsNone(start_wake(Context(self.home / "profile")))
        self.assertEqual(_listeners, {})

    def clocked(self, ctx):
        """A listener on a test clock (self.now), so quiet time passes without waiting."""
        self.now = T0
        listener = self.listen(ctx)
        listener.clock = lambda: self.now
        return listener

    def other(self, sequence):
        """Another thread's progress: a push that triggers a sweep but is nothing to read."""
        return updated(shell("someone-elses-thread", updatedAt=stamp(self.now)), sequence)

    def test_running_worker_quiet_too_long_wakes_once_per_stall(self):
        self.own("agent:owner", "Discord DM")
        ctx = Context(self.home / "profile")
        self.clocked(ctx)
        words = "Running the full suite in one blocking wait, then I open the PR. " * 3
        self.thread["items"] = [{"type": "assistant_message", "createdBy": "agent", "text": words, "itemId": "i1",
                                 "runId": "run-1", "textTruncated": False}]
        reads = self.reads()
        self.send(updated(self.state("running", updatedAt=stamp(T0)), 1))
        self.now = T0 + 14 * 60
        self.send(self.other(2))
        self.assertEqual((self.reads(), ctx.messages), (reads, []))  # quiet, not yet too long: memory only
        self.now = T0 + 25 * 60
        self.send(self.other(3))
        self.assertEqual(self.reads(), reads + 1)
        tail = "..." + " ".join(words.split())[-117:]
        self.assertEqual(ctx.messages, [(f"[boss inbox]\nFix thing: no progress for 25 min (last: {tail})\n{NOTE}",
                                         {"role": "user", "session_key": "agent:owner"})])
        # One wake per stall: more pushes, a poke and a re-snapshot neither read nor wake again.
        self.now = T0 + 40 * 60
        self.send(self.other(4), snapshot(self.state("running", updatedAt=stamp(T0))))
        self.own("agent:owner", "Discord DM")
        self.assertEqual((self.reads(), len(ctx.messages)), (reads + 1, 1))
        # Progress resets the clock; a new stall later is a new wake.
        self.send(updated(self.state("running", updatedAt=stamp(self.now)), 5))
        self.now += 15 * 60
        self.send(self.other(6))
        self.assertEqual((self.reads(), len(ctx.messages)), (reads + 2, 2))
        self.assertIn("Fix thing: no progress for 15 min", ctx.messages[1][0])
        self.assertNotIn(self.task["id"], ctx.messages[1][0])

    def test_a_restart_counts_quiet_time_from_t3_and_stall_minutes_is_configurable(self):
        Config(self.home, Redactor()).change("wake.stall_minutes", 5)
        self.own("agent:owner", "Discord DM")
        ctx = Context(self.home / "profile")
        self.clocked(ctx)
        # The listener's first sight of a thread (a fresh start's snapshot): T3's own updatedAt says it went quiet.
        self.send(snapshot(self.state("running", updatedAt=stamp(T0 - 6 * 60))))
        self.assertEqual(ctx.messages[0][0], f"[boss inbox]\nFix thing: no progress for 6 min\n{NOTE}")
        with self.assertRaises(BossError):
            Config(self.home, Redactor()).change("wake.stall_minutes", -1)

    def test_zero_stall_minutes_turns_the_check_off_and_a_waiting_worker_is_never_stalled(self):
        Config(self.home, Redactor()).change("wake.stall_minutes", 0)
        self.own("agent:owner", "Discord DM")
        ctx = Context(self.home / "profile")
        self.clocked(ctx)
        self.send(updated(self.state("running", updatedAt=stamp(T0)), 1))
        self.now = T0 + 120 * 60
        self.send(self.other(2))
        self.assertEqual(ctx.messages, [])

    def test_a_stall_read_that_finds_the_turn_finished_reports_that_instead(self):
        self.own("agent:owner", "Discord DM")
        ctx = Context(self.home / "profile")
        self.clocked(ctx)
        self.send(updated(self.state("running", updatedAt=stamp(T0)), 1))
        self.completed(self.task, "BOSS: done: three callers found")  # its completion push never came
        self.now = T0 + 20 * 60
        self.send(self.other(2))
        self.assertEqual(ctx.messages[0][0], f"[boss inbox]\nFix thing: finished: three callers found\n{NOTE}")

    def test_paused_worker_past_its_until_time_wakes_once_as_stalled(self):
        self.own("agent:owner", "Discord DM")
        ctx = Context(self.home / "profile")
        self.clocked(ctx)
        self.completed(self.task, "Deploy queued.\nBOSS: paused: the staging deploy until 12:30 UTC")
        self.send(updated(self.state("completed", updatedAt=stamp(T0)), 1))
        self.assertEqual(ctx.messages[0][0],
                         f"[boss inbox]\nFix thing: paused, waiting on the staging deploy until 12:30 UTC\n{NOTE}")
        reads = self.reads()
        self.now = T0 + 29 * 60
        self.send(self.other(2))
        self.assertEqual(len(ctx.messages), 1)
        self.now = T0 + 40 * 60
        self.send(self.other(3))
        self.assertEqual(ctx.messages[1][0], "[boss inbox]\nFix thing: no progress for 40 min "
                                             f"(last: paused, waiting on the staging deploy until 12:30 UTC)\n{NOTE}")
        self.send(self.other(4))
        self.own("agent:owner", "Discord DM")
        self.assertEqual((len(ctx.messages), self.reads()), (2, reads))  # once, and the line needed no T3 read

    def test_paused_worker_that_resumes_is_not_reported_when_its_until_passes(self):
        self.own("agent:owner", "Discord DM")
        ctx = Context(self.home / "profile")
        self.clocked(ctx)
        self.completed(self.task, "BOSS: paused: CI until 12:10 UTC")
        self.send(updated(self.state("completed", updatedAt=stamp(T0)), 1))
        self.send(updated(self.state("running", run="run-2", updatedAt=stamp(T0 + 60)), 2))
        self.now = T0 + 11 * 60
        self.send(updated(self.state("running", run="run-2", updatedAt=stamp(self.now)), 3))
        self.assertEqual(len(ctx.messages), 1)

    def test_pr_merged_in_a_pushed_shell_wakes_once(self):
        self.own("agent:owner", "Discord DM")
        ctx = Context(self.home / "profile")
        self.listen(ctx)
        self.gh.pr["title"] = "Fix the thing"
        self.fake.prs = [harness.URL]
        self.completed(self.task, f"BOSS: done: {harness.URL}")
        self.send(updated(self.state("completed", pullRequests=[linked("open")]), 1))
        self.assertIn(f'finished, PR "Fix the thing" {harness.URL}', ctx.messages[0][0])
        # You merge on GitHub; T3 syncs its copy and pushes the same run with a new PR state.
        self.gh.pr["state"] = "MERGED"
        self.send(updated(self.state("completed", pullRequests=[linked("merged")]), 2))
        self.assertEqual(ctx.messages[1][0], f"[boss inbox]\nFix thing: PR merged {harness.URL}\n{NOTE}")
        reads = self.reads()
        self.send(updated(self.state("completed", pullRequests=[linked("merged")]), 3),
                  snapshot(self.state("completed", pullRequests=[linked("merged")])))
        self.assertEqual((len(ctx.messages), self.reads()), (2, reads))

    def test_pr_closed_without_merging_wakes_once(self):
        self.own("agent:owner", "Discord DM")
        ctx = Context(self.home / "profile")
        self.listen(ctx)
        self.fake.prs = [harness.URL]
        self.completed(self.task, f"BOSS: done: {harness.URL}")
        self.send(updated(self.state("completed", pullRequests=[linked("open")]), 1))
        self.gh.pr["state"] = "CLOSED"
        self.send(updated(self.state("completed", pullRequests=[linked("closed")]), 2))
        self.assertEqual(ctx.messages[1][0], f"[boss inbox]\nFix thing: PR closed {harness.URL}\n{NOTE}")

    def test_pr_landed_by_boss_itself_is_not_announced(self):
        self.own("agent:owner", "Discord DM")
        ctx = Context(self.home / "profile")
        self.listen(ctx)
        self.fake.prs = [harness.URL]
        meta = self.store.load(self.task["id"])
        meta["landed"] = True
        self.store.save(meta, "landed")
        self.gh.pr["state"] = "MERGED"
        self.completed(self.task, f"BOSS: done: {harness.URL}")
        self.send(updated(self.state("completed", pullRequests=[linked("merged")]), 1))
        self.assertEqual(ctx.messages, [])
        self.assertFalse(any(r.get("kind", "").startswith("pr-") for r in Inbox(self.home).records()))

    def test_pr_merged_on_github_is_found_at_the_owner_turn_start_when_t3s_copy_is_stale(self):
        # Seen live: T3 often keeps a PR "open" after you merge it on GitHub.
        self.own("agent:owner", "Hermes app")
        self.watching()
        ctx = Context(self.home / "profile")
        listener = self.clocked(ctx)
        views, view = [], self.gh.view
        self.gh.view = lambda url: views.append(url) or view(url)
        self.fake.prs = [harness.URL]
        self.completed(self.task, f"BOSS: done: {harness.URL}")
        self.send(updated(self.state("completed", pullRequests=[linked("open")]), 1))
        self.assertEqual(len(ctx.messages), 1)
        before = len(views)
        # The owner's turn start asks GitHub about open PRs; its end (another poke) is within the
        # recheck window, so GitHub is not asked again.
        self.owner_turn(listener)
        self.turn_end(session_id="sess-1", completed=True)
        self.assertEqual(len(views), before + 1)
        self.gh.pr["state"] = "MERGED"  # merged on GitHub; T3 never re-pushes
        self.now = T0 + 5 * 60
        self.owner_turn(listener)
        self.assertEqual(self.tool(), '{"output": "ok"}\n\n[boss inbox]\n'
                                      f"Fix thing: PR merged {harness.URL}\n{NOTE}")
        self.turn_end(session_id="sess-1", completed=True)
        self.assertEqual(len(ctx.messages), 1)  # seen mid-turn; nothing left to inject
        # The ledger now says merged: later turns never ask GitHub about it again.
        asked = len(views)
        self.now = T0 + 60 * 60
        self.owner_turn(listener)
        self.assertEqual(len(views), asked)

    def test_a_refused_stream_upgrade_closes_its_socket_at_once(self):
        # Seen while T3 restarts: the upgrade is refused. The socket must close then, not whenever
        # the garbage collector frees an error that something (a logger, a traceback) still holds.
        server = socket.create_server(("127.0.0.1", 0))
        self.addCleanup(server.close)
        ended = queue.Queue()

        def refuse():
            peer, _ = server.accept()
            with peer:
                peer.recv(65536)
                peer.sendall(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\n\r\n")
                peer.settimeout(WAIT)
                try:
                    ended.put(peer.recv(1))  # b"" once the client closes its end
                except OSError as exc:
                    ended.put(exc)
        threading.Thread(target=refuse, daemon=True).start()
        kept = None
        try:
            WebSocket(f"ws://127.0.0.1:{server.getsockname()[1]}/ws")
        except ConnectionError as exc:
            kept = exc  # its traceback keeps the failed handshake's frame alive, as a logger would
        self.assertIn("503", str(kept))
        self.assertEqual(ended.get(timeout=WAIT + 1), b"")  # closed while the error is still alive

    def lose_touch(self, ctx):
        """A clocked listener whose only host goes away at T0 and keeps refusing reconnects."""
        listener = self.clocked(ctx)
        self.push.down = True
        self.push.drop()
        self.assertTrue(listener.reached("drops", 2, WAIT))  # the drop, then a refused reconnect
        return listener

    def come_back(self, listener):
        self.push.down = False
        self.assertTrue(listener.reached("streams", 2, WAIT))
        self.assertTrue(self.push.connected(2))

    def test_a_host_out_of_touch_for_ten_minutes_says_so_once_and_once_more_when_back(self):
        self.own("agent:owner", "Hermes app")
        self.watching()
        ctx = Context(self.home / "profile")
        since = datetime.fromtimestamp(T0).astimezone().strftime("%H:%M")
        with patch("boss.wake.backoff", return_value=0.01):
            listener = self.lose_touch(ctx)
            self.now = T0 + 9 * 60  # a blip: T3 restarting takes seconds
            self.owner_turn(listener)
            self.assertIsNone(self.tool())
            self.now = T0 + 11 * 60  # no timer: Zubair's next turn is what notices
            self.owner_turn(listener)
            self.assertEqual(self.tool(), '{"output": "ok"}\n\n[boss inbox]\n'
                                          f"lost touch with test since {since}; workers there may be unheard\n{NOTE}")
            self.now = T0 + 60 * 60
            self.owner_turn(listener)
            self.assertIsNone(self.tool())  # said once, however long it stays down
            self.come_back(listener)
        self.assertEqual([i["line"] for i in self.held()], ["back in touch with test"])
        self.assertEqual(self.tool(), '{"output": "ok"}\n\n[boss inbox]\nback in touch with test\n' + NOTE)
        self.assertNotIn("agent:owner", (self.home / "inbox.jsonl").read_text())

    def test_a_host_back_before_anyone_saw_it_was_lost_leaves_nothing_to_read(self):
        self.inject = False  # the default: an idle owner sees the inbox at its next turn
        self.own("agent:owner", "Hermes app")
        self.watching()
        ctx = Context(self.home / "profile")
        with patch("boss.wake.backoff", return_value=0.01):
            listener = self.lose_touch(ctx)
            self.now = T0 + 11 * 60
            listener.pushed("elsewhere", self.other(1)["thread"])  # a push from another host sweeps too
            self.assertEqual([i["kind"] for i in self.held()], ["host-down"])
            self.come_back(listener)
        self.assertEqual(self.held(), [])
        self.assertEqual([r["reason"] for r in self.inbox_log("dropped")], ["back in touch before anyone saw it"])

    def test_zero_host_down_minutes_turns_the_lost_touch_line_off(self):
        Config(self.home, Redactor()).change("wake.host_down_minutes", 0)
        with self.assertRaises(BossError):
            Config(self.home, Redactor()).change("wake.host_down_minutes", -1)
        self.own("agent:owner", "Hermes app")
        ctx = Context(self.home / "profile")
        with patch("boss.wake.backoff", return_value=0.01):
            listener = self.lose_touch(ctx)
            self.now = T0 + 24 * 60 * 60
            self.owner_turn(listener)
            self.come_back(listener)
        self.assertEqual(Inbox(self.home).records(), [])

    def test_endpoints_follow_the_mcp_origin(self):
        self.assertEqual(endpoints("https://t3.example.ts.net/mcp"),
                         ("wss://t3.example.ts.net/ws", "https://t3.example.ts.net/api/auth/websocket-ticket/rpc"))
        self.assertEqual(endpoints("http://127.0.0.1:3773/mcp"),
                         ("ws://127.0.0.1:3773/ws", "http://127.0.0.1:3773/api/auth/websocket-ticket/rpc"))


class NoPollingTest(HermeticTestCase):
    def test_no_timer_or_poll_code_remains(self):
        root = Path(__file__).resolve().parent.parent
        files = [*sorted((root / "boss").glob("*.py")), root / "plugin/__init__.py", root / "bin/boss", root / "install.sh"]
        found = []
        pattern = re.compile(r"time\.sleep|threading\.Timer|\bsched\b|\bcron\b|interval_seconds|--hermes-gate|"
                             r"add_parser\(\"poll\"|\.wait\([^)]")
        for path in files:
            for number, line in enumerate(path.read_text().splitlines(), 1):
                if pattern.search(line):
                    found.append(f"{path.relative_to(root)}: {line.strip()}")
        # Only: old configs still load (never read), doctor says to drop the key, and the reconnect
        # backoff after a dropped T3 stream. A healthy listener blocks on its socket.
        self.assertEqual(found, [
            'boss/config.py: # interval_seconds is a leftover from timed polling; tolerated so old configs load, never read.',
            'boss/config.py: fields(wake, {"enabled": bool, "host_down_minutes": int, "inject_when_idle": bool, "interval_seconds": int, "origin": dict, "session_key": str, "stall_minutes": int}, "wake")',
            'boss/setup.py: if "interval_seconds" in wake:',
            'boss/setup.py: lines.append("info: wake.interval_seconds is unused now that wakes are pushed; boss config unset wake.interval_seconds")',
            'boss/wake.py: self.stop_event.wait(backoff(failures))',
        ])
