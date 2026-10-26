"""boss watch: the owner's background command that waits for the inbox, prints it, acks it and exits."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from unittest.mock import patch

from boss import watch
from boss.inbox import NOTE, SOCKET, Inbox
import plugin
from tests import test_boss as harness
from tests.support import HermeticTestCase
from tests.test_wake import Context

OWNER = "agent:owner"
WAIT = 5
BOSS = str(Path(__file__).resolve().parent.parent / "bin/boss")


class WatchTest(HermeticTestCase):
    setUp = harness.CLITest.setUp
    tearDown = harness.CLITest.tearDown
    spawn = harness.CLITest.spawn

    def cli(self, *args, key=OWNER, stdin=""):
        output = io.StringIO()
        with patch.dict(os.environ, {"HERMES_SESSION_KEY": key} if key else {}), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()), \
                patch("sys.stdin", io.StringIO(stdin)):
            if not key:
                os.environ.pop("HERMES_SESSION_KEY", None)
            from boss.cli import main
            code = main(list(args))
        return code, output.getvalue()

    def own(self, key=OWNER, label="Hermes app", *extra):
        code, output = self.cli("own", "--label", label, *extra, key=key)
        self.assertEqual(code, 0, output)

    def acks(self):
        return [r for r in Inbox(self.home).records() if r["event"] == "acked"]

    def start(self, key=OWNER):
        """Run watch.run in a thread, as the CLI does; returns (thread, printed texts, [exit code])."""
        printed, codes = [], []
        thread = threading.Thread(target=lambda: codes.append(watch.run(self.home, key, self.store.load, printed.append)),
                                  daemon=True)
        thread.start()
        # Waiting means its socket is bound: an append from now on wakes it.
        for _ in range(500):
            if (self.home / SOCKET).exists() or not thread.is_alive():
                break
            thread.join(0.01)
        self.assertTrue((self.home / SOCKET).exists())
        return thread, printed, codes

    def user_event(self, line, key="user:test"):
        Inbox(self.home).hold(key, None, "user", line)

    def test_waiting_items_are_printed_and_acked_at_once(self):
        self.own()
        self.user_event("Away mode on at 16:40 (bed).")
        self.cli("decide", "add", "Pick storage")
        code, output = self.cli("watch")
        self.assertEqual(code, 0, output)
        self.assertEqual(output, f"[boss inbox]\nAway mode on at 16:40 (bed).\n{NOTE}\n"
                                 "Still open: 1 open decision. Run boss status.\n")
        self.assertEqual([(r["via"], r["owner"]) for r in self.acks()], [("watch", "Hermes app")])
        self.assertFalse((self.home / SOCKET).exists())  # cleaned up on exit

    def test_blocks_until_an_item_arrives_then_exits(self):
        self.own()
        thread, printed, codes = self.start()
        thread.join(0.3)
        self.assertTrue(thread.is_alive())  # nothing open: it waits
        self.assertEqual(printed, [])
        self.user_event("Away mode off at 17:10.")
        thread.join(WAIT)
        self.assertFalse(thread.is_alive())
        self.assertEqual((codes, printed), ([0], [f"[boss inbox]\nAway mode off at 17:10.\n{NOTE}"]))
        self.assertEqual([r["via"] for r in self.acks()], ["watch"])

    def test_stale_items_are_dropped_and_it_keeps_waiting(self):
        self.own()
        thread, printed, codes = self.start()
        Inbox(self.home).hold("gone", "no-such-task", "finished", "Gone: finished")
        thread.join(0.3)
        self.assertTrue(thread.is_alive())
        self.assertEqual([r["reason"] for r in Inbox(self.home).records() if r["event"] == "dropped"],
                         ["task is not in the ledger"])
        self.user_event("Away mode off at 17:10.")
        thread.join(WAIT)
        self.assertEqual(codes, [0])

    def test_refused_for_other_sessions_and_plain_shells(self):
        self.own()
        self.user_event("Away mode off at 17:10.")
        code, output = self.cli("watch", key="agent:tui")
        self.assertEqual(code, 2)
        self.assertIn("boss is owned by Hermes app", output)
        code, output = self.cli("watch", key=None)
        self.assertEqual(code, 2)
        self.assertIn("only in the Hermes session that owns boss", output)
        self.assertEqual(self.acks(), [])
        self.assertEqual(len(Inbox(self.home).open_items()), 1)

    def test_a_newer_watch_replaces_the_older_one_which_takes_nothing(self):
        self.own()
        first, first_printed, first_codes = self.start()
        second_printed, second_codes = [], []

        def run_second():
            second_codes.append(watch.run(self.home, OWNER, self.store.load, second_printed.append))
        second = threading.Thread(target=run_second, daemon=True)
        second.start()
        first.join(WAIT)
        self.assertFalse(first.is_alive())
        self.assertEqual((first_codes, first_printed), ([0], ["A newer boss watch took over; this one took nothing."]))
        self.assertTrue(second.is_alive())
        # Bound yet or not, the second sees this: on its first look or through its socket.
        self.user_event("Away mode off at 17:10.")
        second.join(WAIT)
        self.assertEqual((second_codes, len(second_printed)), ([0], 1))
        self.assertEqual(len(self.acks()), 1)

    def test_a_watch_from_a_session_that_lost_boss_takes_nothing(self):
        self.own()
        thread, printed, codes = self.start()
        self.own("agent:phone", "Telegram DM", "--take")
        self.user_event("Away mode off at 17:10.")
        thread.join(WAIT)
        self.assertEqual((codes, printed), ([0], ["boss moved to another session; this watch stopped and took nothing."]))
        self.assertEqual(self.acks(), [])
        self.assertEqual(len(Inbox(self.home).open_items()), 1)

    def test_away_and_back_slash_commands_wake_a_background_watch(self):
        # The real path: boss watch as its own process, /away and /back through the plugin's handlers.
        self.own()
        env = {**os.environ, "HERMES_SESSION_KEY": OWNER}
        for command, words, expected in ((plugin.on_away, "bed", "Got it. Away mode on since "),
                                         (plugin.on_back, "", "Welcome back. Catching you up.")):
            process = subprocess.Popen([sys.executable, BOSS, "watch"], env=env, stdout=subprocess.PIPE, text=True)
            self.addCleanup(process.kill)
            # Whether the watch is already waiting or starts after, the event reaches it exactly once.
            reply = command(words, session_key=OWNER, platform="desktop")
            self.assertTrue(reply.startswith(expected), reply)
            output, _ = process.communicate(timeout=WAIT)
            self.assertEqual(process.returncode, 0)
            lines = output.splitlines()
            self.assertEqual((lines[0], lines[2]), ("[boss inbox]", NOTE))
            if command is plugin.on_away:
                self.assertRegex(lines[1], r"^Away mode on at \d\d:\d\d \(bed\)\. Ask nothing until /back")
            else:
                self.assertRegex(lines[1], r"^Away mode off at \d\d:\d\d after under a minute away \(since \d\d:\d\d \(bed\)\)")
                self.assertIn("boss back --full", lines[1])
        self.assertEqual([r["via"] for r in self.acks()], ["watch", "watch"])
        self.assertEqual(Inbox(self.home).open_items(), [])
        journal = [json.loads(line)["event"] for line in (self.home / "user.jsonl").read_text().splitlines()]
        self.assertEqual([e for e in journal if e in ("away", "back")], ["away", "back"])


class WatchReminderTest(HermeticTestCase):
    """Without a boss watch an idle owner hears nothing, so its tool results and boss own say to start one."""
    setUp = harness.CLITest.setUp
    tearDown = harness.CLITest.tearDown
    spawn = harness.CLITest.spawn
    own = WatchTest.own
    start = WatchTest.start
    user_event = WatchTest.user_event
    RESULT = '{"output": "ok"}'

    def cli(self, *args, key=OWNER, stdin="investigate this"):
        return WatchTest.cli(self, *args, key=key, stdin=stdin)

    def turn(self, session="sess-1", key=OWNER, parent=""):
        """One top-level turn starting, through the plugin's pre_llm_call bookkeeping."""
        with patch("plugin.current_session_key", return_value=key):
            plugin.turn_start({"session_id": session, "parent_session_id": parent, "user_message": "hi"})

    def tool(self, session="sess-1"):
        return plugin.on_tool_result(tool_name="terminal", args={}, result=self.RESULT, session_id=session,
                                     task_id="t", tool_call_id="call-1", turn_id="turn-1")

    def active(self):
        return json.loads((self.home / "attention.json").read_text())["active"]

    def test_owner_turn_without_a_watch_is_reminded_once_per_turn(self):
        self.own()
        self.spawn()
        self.assertEqual(self.active(), 1)
        self.turn()
        self.assertEqual(self.tool(), f"{self.RESULT}\n\n{watch.REMIND}")
        self.assertEqual(watch.REMIND, "boss: no boss watch running; start it: boss watch (background, notify on exit)")
        self.assertIsNone(self.tool())  # once per turn
        self.turn()
        self.assertEqual(self.tool(), f"{self.RESULT}\n\n{watch.REMIND}")  # a new turn is told again
        thread, _, codes = self.start()
        self.turn()
        self.assertIsNone(self.tool())  # a watch is waiting: nothing to say
        self.user_event("Away mode off at 17:10.")
        thread.join(WAIT)
        self.assertEqual(codes, [0])
        self.turn()
        self.assertEqual(self.tool(), f"{self.RESULT}\n\n{watch.REMIND}")  # it delivered and exited, not re-armed

    def test_no_reminder_while_no_task_is_active(self):
        self.own()
        self.cli("status")
        self.assertEqual(self.active(), 0)
        self.turn()
        self.assertIsNone(self.tool())

    def test_reminder_follows_the_inbox_block_on_one_tool_result(self):
        self.own()
        self.spawn()
        self.turn()
        self.user_event("Away mode off at 17:10.")
        self.assertEqual(self.tool(), f"{self.RESULT}\n\n[boss inbox]\nAway mode off at 17:10.\n{NOTE}\n{watch.REMIND}")
        self.assertIsNone(self.tool())

    def test_other_sessions_never_get_the_reminder(self):
        self.own()
        self.spawn()
        # Another session's turn records nothing, and its tool results are untouched, with or without an owner turn.
        self.turn(session="sess-tui", key="agent:tui")
        self.assertIsNone(self.tool(session="sess-tui"))
        self.turn()
        self.assertIsNone(self.tool(session="sess-tui"))
        # A subagent of the owner shares its session key but neither its turn nor its session id.
        self.turn(session="sess-child", parent="sess-1")
        self.assertIsNone(self.tool(session="sess-child"))
        # Boss moved mid-turn: the old owner's running turn is no longer the owner's.
        self.own("agent:phone", "Telegram DM", "--take")
        self.assertIsNone(self.tool())
        # Never in turn-start context: the app shows that inside the person's own message.
        for key in (OWNER, "agent:phone", "agent:tui"):
            with patch("plugin.current_session_key", return_value=key):
                context = (plugin.on_pre_llm_call(session_id="sess-1", user_message="boss?") or {}).get("context", "")
            self.assertNotIn("boss watch", context)

    def test_own_and_take_end_with_the_watch_instruction(self):
        code, output = self.cli("own", "--label", "Hermes app")
        self.assertEqual(code, 0, output)
        self.assertEqual(output.splitlines()[-2:], [watch.REMIND, "next: boss"])
        thread, _, codes = self.start()
        # Re-opening while its own watch waits: nothing to start.
        code, output = self.cli("own")
        self.assertEqual(code, 0, output)
        self.assertNotIn(watch.REMIND, output)
        # A take moves boss to a session with no watch of its own, whatever still holds the lock.
        code, output = self.cli("own", "--take", "--label", "Telegram DM", key="agent:phone")
        self.assertEqual(code, 0, output)
        self.assertEqual(output.splitlines()[-2:], [watch.REMIND, "next: boss"])
        self.user_event("Away mode off at 17:10.")
        thread.join(WAIT)
        self.assertEqual(codes, [0])
        # /boss and /boss take in chat end with it too (the plugin hides next: hints).
        handler = plugin.boss_command(Context(self.home / "profile"))
        self.assertTrue(handler("take", session_key="agent:tui", platform="tui").endswith(f"\n{watch.REMIND}"))
        self.assertIn("Released boss", handler("release", session_key="agent:tui", platform="tui"))
        self.assertTrue(handler("", session_key="agent:discord", platform="discord").endswith(f"\n{watch.REMIND}"))
