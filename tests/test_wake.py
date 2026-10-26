"""Pushed T3 events reach the owner session: a fake T3 WebSocket server, real listener, real ledger."""
import base64
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import queue
import re
import struct
import subprocess
import sys
import threading
from types import SimpleNamespace
from unittest.mock import patch

from boss.common import Redactor
from boss.config import Config
from boss.wake import LOCK, Listener, _listeners, endpoints, start_wake
from boss.websocket import GUID
import plugin
from tests import test_boss as harness
from tests.support import HermeticTestCase


EVENTS_TOKEN = "events-secret-token-never-print"
WAIT = 5


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
        push = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
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
                push.subscriptions.append(json.loads(subscribe))
                channel = queue.Queue()
                push.channels.append(channel)
                push.connections.append(self.path)
                threading.Thread(target=self.drain, daemon=True).start()
                while True:
                    item = channel.get()
                    if item is None:
                        self.wfile.write(bytes([0x88, 0]))
                        self.wfile.flush()
                        return
                    self.wfile.write(frame(json.dumps({"_tag": "Chunk", "requestId": "1", "values": item})))
                    self.wfile.flush()

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

    def send(self, *values):
        self.channels[-1].put(list(values))

    def drop(self):
        self.channels[-1].put(None)

    def close(self):
        for channel in self.channels:
            channel.put(None)
        self.server.shutdown()
        self.server.server_close()


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
        self.injected = threading.Condition()

    def inject_message(self, content, **target):
        with self.injected:
            self.messages.append((content, target))
            self.injected.notify_all()
        return self.accepted

    def wait_for(self, count):
        with self.injected:
            return self.injected.wait_for(lambda: len(self.messages) >= count, WAIT)

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

    def setUp(self):
        self.setUp_cli()
        self.addCleanup(self.tearDown_cli)
        self.push = FakePush()
        self.addCleanup(self.push.close)
        os.environ["T3_EVENTS_TOKEN"] = EVENTS_TOKEN
        # The fake host serves MCP (spawn) and the push stream from two ports; point the stream at push.
        self.task = self.spawn()
        self.listeners = []
        self.addCleanup(self.stop_all)

    def stop_all(self):
        for listener in self.listeners:
            listener.stop()
        _listeners.clear()

    def config(self):
        config = Config(self.home, Redactor(), hermes=self.home / "profile")
        config.hosts["test"] = {**config.hosts["test"], "url": self.push.url}
        return config

    def listen(self, ctx, connections=1):
        listener = Listener(ctx, self.config(), self.home)
        self.listeners.append(listener)
        listener.thread.start()
        self.wait(lambda: len(self.push.connections) >= connections)
        return listener

    def wait(self, condition):
        done = threading.Event()
        for _ in range(WAIT * 50):
            if condition():
                return True
            done.wait(0.02)  # test-side wait for the listener thread; product code never does this
        self.fail("condition not reached")

    def own(self, key, label, *extra):
        with patch.dict(os.environ, {"HERMES_SESSION_KEY": key}):
            code, output = self.cli("own", "--label", label, *extra)
        self.assertEqual(code, 0, output)
        return output

    def held(self):
        return self.store.held_wakes()

    def test_pushed_event_reaches_owner_session_once(self):
        self.own("agent:captain", "Discord DM")
        ctx = Context(self.home / "profile")
        self.listen(ctx)
        self.assertEqual(self.push.subscriptions[0]["tag"], "orchestration.subscribeShell")
        thread = self.task["thread_id"]
        self.push.send(snapshot(shell(thread)), updated(shell("someone-elses-thread", "completed")))
        self.push.send(updated(shell(thread, "completed")))
        self.assertTrue(ctx.wait_for(1))
        content, target = ctx.messages[0]
        self.assertEqual(target, {"role": "user", "session_key": "agent:captain"})
        self.assertIn(f"[boss wake] {self.task['id']} (Fix thing): finished", content)
        self.assertIn("boss status", content)
        # T3 pushes the same completion several times and re-snapshots; still one wake.
        self.push.send(updated(shell(thread, "completed"), 2), updated(shell(thread, "completed"), 3))
        self.push.send(snapshot(shell(thread, "completed")))
        self.push.send(updated(shell(thread, "waiting", request={"id": "req-1", "kind": "user-input", "blocking": True})))
        self.assertTrue(ctx.wait_for(2))
        self.assertEqual(len(ctx.messages), 2)
        self.assertIn("needs input", ctx.messages[1][0])
        self.wait(lambda: self.held() == [])
        self.assertTrue((self.home / "wakes.jsonl").read_text().count('"held"') == 2)

    def test_no_owner_holds_on_disk_then_claim_delivers(self):
        ctx = Context(self.home / "profile")
        self.listen(ctx)
        self.push.send(updated(shell(self.task["thread_id"], "failed")))
        self.wait(lambda: len(self.held()) == 1)
        self.assertEqual(ctx.messages, [])
        attention = json.loads((self.home / "attention.json").read_text())
        self.assertEqual(attention["items"], [{"id": self.task["id"], "kind": "finished"}])
        self.own("agent:captain", "Hermes app")  # claiming pokes the listener
        self.assertTrue(ctx.wait_for(1))
        self.assertEqual(ctx.messages[0][1]["session_key"], "agent:captain")
        self.assertIn("failed", ctx.messages[0][0])
        self.wait(lambda: self.held() == [])

    def test_refused_injection_is_held_then_delivered_when_owner_returns_or_new_owner_claims(self):
        self.own("agent:captain", "Discord DM")
        ctx = Context(self.home / "profile", accepted=False)
        self.listen(ctx)
        thread = self.task["thread_id"]
        self.push.send(updated(shell(thread, "completed")))
        self.assertTrue(ctx.wait_for(1))
        self.wait(lambda: (self.home / "wake-errors.log").exists())
        self.assertIn("refused", (self.home / "wake-errors.log").read_text())
        self.assertEqual(len(self.held()), 1)  # refused: not consumed
        self.push.send(updated(shell(thread, "completed"), 2))  # same event again: no new record
        # A second, new event retries delivery of everything held; still refused, still held.
        self.push.send(updated(shell(thread, "interrupted", run="run-2"), 3))
        self.assertTrue(ctx.wait_for(2))
        self.assertEqual(len(self.held()), 2)
        # Another session takes over: one injection with both held events, then nothing is held.
        ctx.accepted = True
        self.own("agent:phone", "Telegram DM", "--take")
        self.assertTrue(ctx.wait_for(3))
        content, target = ctx.messages[2]
        self.assertEqual(target["session_key"], "agent:phone")
        self.assertIn("finished", content)
        self.assertIn("interrupted", content)
        self.wait(lambda: self.held() == [])
        last = json.loads((self.home / "wake-last.json").read_text())
        self.assertEqual((last["owner"], last["result"]), ("Telegram DM", "delivered"))

    def test_owner_turn_delivers_held_wakes(self):
        # Owner offline when the event arrives; the owner's next turn pokes the listener.
        self.own("agent:captain", "Hermes CLI")
        ctx = Context(self.home / "profile", accepted=False)
        listener = self.listen(ctx)
        self.push.send(updated(shell(self.task["thread_id"], "completed")))
        self.assertTrue(ctx.wait_for(1))
        ctx.accepted = True
        hook = plugin.turn_hook(Context(self.home / "profile"))
        with patch("boss.wake.start_wake", return_value=listener):
            result = hook(session_id="agent:captain", user_message="hi")
        self.assertIn("need you", result["context"])
        self.assertTrue(ctx.wait_for(2))
        self.wait(lambda: self.held() == [])

    def test_cli_process_never_injects(self):
        self.own("agent:captain", "Discord DM")
        ctx = Context(self.home / "profile", gateway=False, cli=object())
        self.listen(ctx)
        self.push.send(updated(shell(self.task["thread_id"], "completed")))
        self.wait(lambda: len(self.held()) == 1)
        self.assertEqual(ctx.messages, [])

    def test_settled_task_is_ignored(self):
        self.own("agent:captain", "Discord DM")
        meta = self.store.load(self.task["id"])
        meta["settled"] = True
        self.store.save(meta, "settled")
        ctx = Context(self.home / "profile")
        self.listen(ctx)
        self.push.send(updated(shell(self.task["thread_id"], "completed")))
        self.push.send(updated(shell(self.task["thread_id"], "failed", run="run-2")))
        self.push.send(snapshot())
        self.wait(lambda: len(self.push.connections) == 1)
        self.assertFalse((self.home / "wakes.jsonl").exists())

    def test_one_listener_machine_wide_and_takeover(self):
        first = self.listen(Context(self.home / "profile"))
        second = Listener(Context(self.home / "profile"), self.config(), self.home)
        self.listeners.append(second)
        second.thread.start()
        heartbeat = (self.home / "wake-heartbeat").read_text().split()
        self.assertEqual(heartbeat[1], str(os.getpid()))
        self.assertEqual(len(self.push.connections), 1)  # the second blocks on wake.lock
        first.stop()
        self.wait(lambda: len(self.push.connections) == 2)

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
        threading.Event().wait(0.3)
        self.assertEqual(self.push.connections, [])
        holder.stdin.close()
        holder.wait(WAIT)
        self.wait(lambda: len(self.push.connections) == 1)

    def test_dropped_stream_reconnects_and_snapshot_covers_the_gap(self):
        self.own("agent:captain", "Discord DM")
        ctx = Context(self.home / "profile")
        with patch("boss.wake.backoff", return_value=0.01):
            self.listen(ctx)
            self.push.drop()
            self.wait(lambda: len(self.push.connections) == 2)
        self.push.send(snapshot(shell(self.task["thread_id"], "completed")))
        self.assertTrue(ctx.wait_for(1))
        self.assertIn("dropped", (self.home / "wake-errors.log").read_text())

    def test_bad_events_token_never_connects_and_is_redacted(self):
        os.environ["T3_EVENTS_TOKEN"] = "wrong-but-long-token-value"
        with patch("boss.wake.backoff", return_value=60):
            listener = Listener(Context(self.home / "profile"), self.config(), self.home)
            self.listeners.append(listener)
            listener.thread.start()
            self.wait(lambda: (self.home / "wake-errors.log").exists())
        self.assertEqual(self.push.connections, [])
        self.assertNotIn("wrong-but-long-token-value", (self.home / "wake-errors.log").read_text())

    def test_register_starts_listener_without_gateway_marker(self):
        # Hermes loads plugins before gateway.run sets _HERMES_GATEWAY (the original dead-loop bug).
        os.environ.pop("_HERMES_GATEWAY", None)
        ctx = Context(self.home / "profile", gateway=False)
        with patch.object(sys, "argv", ["hermes", "gateway", "run"]), \
                patch("boss.wake.Config", side_effect=lambda home, redact, hermes: self.config()):
            plugin.register(ctx)
        self.addCleanup(lambda: [cleanup() for cleanup in ctx.cleanups])
        self.assertEqual([name for name, _ in ctx.hooks], ["pre_llm_call"])
        self.assertEqual(len(ctx.cleanups), 1)
        self.wait(lambda: len(self.push.connections) == 1)
        listener = next(iter(_listeners.values()))
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
        self.wait(lambda: len(self.push.connections) == 1)

    def test_disabled_wake_starts_nothing(self):
        Config(self.home, Redactor()).change("wake.enabled", False)
        with patch.object(sys, "argv", ["hermes", "gateway", "run"]):
            self.assertIsNone(start_wake(Context(self.home / "profile")))
        self.assertEqual(_listeners, {})

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
            'boss/config.py: fields(wake, {"enabled": bool, "interval_seconds": int, "origin": dict, "session_key": str}, "wake")',
            'boss/setup.py: if "interval_seconds" in wake:',
            'boss/setup.py: lines.append("info: wake.interval_seconds is unused now that wakes are pushed; boss config unset wake.interval_seconds")',
            'boss/wake.py: self.stop_event.wait(backoff(failures))',
        ])
