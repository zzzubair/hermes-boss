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

OWNER = "agent:captain"
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

    def captain_event(self, line, key="captain:test"):
        Inbox(self.home).hold(key, None, "captain", line)

    def test_waiting_items_are_printed_and_acked_at_once(self):
        self.own()
        self.captain_event("Captain went away at 16:40 (bed).")
        self.cli("decide", "add", "Pick storage")
        code, output = self.cli("watch")
        self.assertEqual(code, 0, output)
        self.assertEqual(output, f"[boss inbox]\nCaptain went away at 16:40 (bed).\n{NOTE}\n"
                                 "Still open: 1 open decision. Run boss status.\n")
        self.assertEqual([(r["via"], r["owner"]) for r in self.acks()], [("watch", "Hermes app")])
        self.assertFalse((self.home / SOCKET).exists())  # cleaned up on exit

    def test_blocks_until_an_item_arrives_then_exits(self):
        self.own()
        thread, printed, codes = self.start()
        thread.join(0.3)
        self.assertTrue(thread.is_alive())  # nothing open: it waits
        self.assertEqual(printed, [])
        self.captain_event("Captain is back at 17:10.")
        thread.join(WAIT)
        self.assertFalse(thread.is_alive())
        self.assertEqual((codes, printed), ([0], [f"[boss inbox]\nCaptain is back at 17:10.\n{NOTE}"]))
        self.assertEqual([r["via"] for r in self.acks()], ["watch"])

    def test_stale_items_are_dropped_and_it_keeps_waiting(self):
        self.own()
        thread, printed, codes = self.start()
        Inbox(self.home).hold("gone", "no-such-task", "finished", "Gone: finished")
        thread.join(0.3)
        self.assertTrue(thread.is_alive())
        self.assertEqual([r["reason"] for r in Inbox(self.home).records() if r["event"] == "dropped"],
                         ["task is not in the ledger"])
        self.captain_event("Captain is back at 17:10.")
        thread.join(WAIT)
        self.assertEqual(codes, [0])

    def test_refused_for_other_sessions_and_plain_shells(self):
        self.own()
        self.captain_event("Captain is back at 17:10.")
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
        self.captain_event("Captain is back at 17:10.")
        second.join(WAIT)
        self.assertEqual((second_codes, len(second_printed)), ([0], 1))
        self.assertEqual(len(self.acks()), 1)

    def test_a_watch_from_a_session_that_lost_boss_takes_nothing(self):
        self.own()
        thread, printed, codes = self.start()
        self.own("agent:phone", "Telegram DM", "--take")
        self.captain_event("Captain is back at 17:10.")
        thread.join(WAIT)
        self.assertEqual((codes, printed), ([0], ["boss moved to another session; this watch stopped and took nothing."]))
        self.assertEqual(self.acks(), [])
        self.assertEqual(len(Inbox(self.home).open_items()), 1)

    def test_away_and_back_slash_commands_wake_a_background_watch(self):
        # The real path: boss watch as its own process, /away and /back through the plugin's handlers.
        self.own()
        env = {**os.environ, "HERMES_SESSION_KEY": OWNER}
        for command, words, expected in ((plugin.on_away, "bed", "Got it. Away mode on since "),
                                         (plugin.on_back, "", "Welcome back. You were away")):
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
                self.assertRegex(lines[1], r"^Captain went away at \d\d:\d\d \(bed\)\. Don't ask him anything until /back")
            else:
                self.assertRegex(lines[1], r"^Captain is back at \d\d:\d\d\. Away mode is off")
        self.assertEqual([r["via"] for r in self.acks()], ["watch", "watch"])
        self.assertEqual(Inbox(self.home).open_items(), [])
        captain = [json.loads(line)["event"] for line in (self.home / "captain.jsonl").read_text().splitlines()]
        self.assertEqual([e for e in captain if e in ("away", "back")], ["away", "back"])
