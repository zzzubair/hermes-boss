from datetime import datetime, timedelta, timezone
import json
import re
import unittest

from boss import captain
import plugin
from plugin import on_pre_llm_call, register
from tests import test_boss as harness


class CaptainTest(unittest.TestCase):
    setUp = harness.CLITest.setUp
    tearDown = harness.CLITest.tearDown
    cli = harness.CLITest.cli
    spawn = harness.CLITest.spawn
    completed = harness.CLITest.completed

    def ok(self, *args):
        code, output = self.cli(*args)
        self.assertEqual(code, 0, output)
        return output

    def events(self):
        path = self.home / "captain.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def presence(self):
        return [(e["event"], e["at"]) for e in self.events() if e["event"] in ("away", "back")]

    def decision(self, text):
        return self.ok("decide", "add", text).split(":", 1)[0]

    def captain_part(self, output):
        return "\n".join(line for line in output.splitlines() if not line.startswith("next: "))

    def test_only_away_and_back_change_presence(self):
        self.ok("away", "going", "to", "bed")
        before = self.presence()
        meta = self.spawn("--fits", "unrelated")  # supervisor work while away
        decision = self.decision("Pick storage")
        for argv in [("log", "he said: back is fine, I'm still away though"), ("steer", "repo", "Prefer SQLite"),
                     ("decide", "close", decision, "SQLite"), ("status", meta["id"]), ()]:
            self.ok(*argv)
        for message in ("I'm back", "/back", "ok im here now"):
            on_pre_llm_call(user_message=message, conversation_history=[])
        self.assertEqual(self.presence(), before)
        self.assertIn("captain: away since", self.ok())
        self.ok("back")
        self.assertEqual([e for e, _ in self.presence()], ["away", "back"])
        self.assertIn("captain: here", self.ok())

    def test_away_is_idempotent_and_keeps_first_since_and_note(self):
        first = self.ok("away", "going to bed")
        self.assertIn("(going to bed)", first)
        second = plugin.on_away("different note")  # the real /away handler, via a subprocess
        self.assertIn("Already away", second)
        self.assertIn("(going to bed)", second)
        self.assertNotIn("next:", second)
        away = [e for e in self.events() if e["event"] == "away"]
        self.assertEqual(len(away), 1)
        self.assertEqual(away[0]["note"], "going to bed")
        self.assertIn("Welcome back", plugin.on_back(""))
        output = self.ok("back")
        self.assertIn("weren't marked away", output)
        self.assertEqual([e["event"] for e in self.events() if e["event"] in ("away", "back")], ["away", "back"])

    def test_slash_commands_registered_from_plugin(self):
        class Context:
            def __init__(self):
                self.hooks, self.commands = [], {}

            def register_hook(self, event, callback):
                self.hooks.append(event)

            def register_command(self, name, handler, description="", args_hint=""):
                self.commands[name] = (handler, args_hint)

        context = Context()
        register(context)
        self.assertEqual(sorted(context.commands), ["afk", "away", "back", "boss"])
        self.assertEqual(context.commands["boss"][1], "[take|release|status]")
        self.assertIs(context.commands["afk"][0], context.commands["away"][0])
        self.assertEqual(context.commands["away"][1], "[note]")

    def test_ask_round_numbers_in_order_and_close_by_number(self):
        first, second, third = (self.decision(t) for t in ("Pick storage", "Rename CLI", "Drop Python 3.10"))
        output = self.ok("decide", "ask")
        self.assertIn('3 open decisions (round 1; reply like "1 yes 2 no"):', output)
        self.assertRegex(output, r"1\. Pick storage\n2\. Rename CLI\n3\. Drop Python 3\.10\n")
        self.assertNotIn(first, self.captain_part(output))
        self.assertEqual(self.events()[-1]["items"], [first, second, third])
        self.ok("decide", "close", "3", "no")
        self.ok("decide", "close", "1", "yes", "--round", "1")
        closed = {d["id"]: d for d in self.store.decisions()}
        self.assertEqual((closed[first]["resolution"], closed[third]["resolution"]), ("yes", "no"))
        self.assertFalse(closed[second]["closed"])
        # Repeating the captain's answer is safe and keeps the first one.
        self.assertIn("already closed", self.ok("decide", "close", "1", "changed"))
        self.assertEqual(self.store.decisions()[[d["id"] for d in self.store.decisions()].index(first)]["resolution"], "yes")
        fleet = self.ok()
        self.assertIn("1 open decisions (round 1)\n2. Rename CLI\n", fleet)

    def test_numbers_from_old_rounds_or_without_a_round_are_refused(self):
        decision = self.decision("Pick storage")
        code, output = self.cli("decide", "close", "1", "yes")
        self.assertEqual(code, 2)
        self.assertIn("next: boss decide ask", output)
        self.ok("decide", "ask")
        self.ok("decide", "ask")
        code, output = self.cli("decide", "close", "1", "yes", "--round", "1")
        self.assertEqual(code, 2)
        self.assertIn("round 1 is old; the latest is round 2", output)
        self.assertIn("next: boss decide ask", output)
        code, output = self.cli("decide", "close", "2", "yes")
        self.assertEqual(code, 2)
        self.assertIn("not in round 2", output)
        self.assertFalse(self.store.decisions()[0]["closed"])
        self.ok("decide", "close", decision, "yes")  # an id always works

    def test_unanswered_decisions_are_asked_again_answered_never(self):
        storage, rename = self.decision("Pick storage"), self.decision("Rename CLI")
        self.ok("decide", "ask")
        self.ok("decide", "close", "1", "SQLite")
        output = self.ok("decide", "ask")
        self.assertIn("1 open decision (round 2", output)
        self.assertIn("1. Rename CLI (asked again)", output)
        self.assertNotIn("Pick storage", output)
        self.assertIn(f"{rename}: Rename CLI asked=2", self.ok("decide", "list"))
        new = self.decision("Add Windows support")
        output = self.ok("decide", "ask")
        self.assertIn("1. Rename CLI (asked again)\n2. Add Windows support\n", output)
        self.assertNotIn("Pick storage", output)
        self.assertNotIn(new, output)
        self.assertNotIn(storage, output)

    def test_unclear_answer_is_reasked_with_the_note_once(self):
        self.decision("Pick storage")
        self.decision("Rename CLI")
        self.ok("decide", "ask")
        self.ok("decide", "unclear", "2", "he said 'sure' but which name?")
        self.ok("decide", "close", "1", "SQLite")
        output = self.ok("decide", "ask")
        self.assertIn("1. Rename CLI (asked again; unclear last time: he said 'sure' but which name?)", output)
        output = self.ok("decide", "ask")
        self.assertIn("1. Rename CLI (asked again)\n", output)
        self.assertNotIn("unclear", output)
        self.ok("decide", "close", "1", "captain")
        code, output = self.cli("decide", "unclear", "1", "late")
        self.assertEqual(code, 2)
        self.assertIn("never asked again", output)

    def test_supervisor_cannot_ask_while_away_unless_he_asks(self):
        self.decision("Pick storage")
        self.ok("away")
        code, output = self.cli("decide", "ask")
        self.assertEqual(code, 2)
        self.assertIn("do not ask him", output)
        self.assertIn("1. Pick storage", self.ok("decide", "ask", "--requested", "what's open?"))

    def test_back_timeline_is_chronological_with_checkins_and_no_ids(self):
        self.ok("away", "going to bed")
        meta = self.spawn("--title", "Fix login")
        self.ok("log", "Checked in from phone: keep going, no rush")
        decision = self.decision("Pick storage")
        self.ok("steer", "repo", "Prefer SQLite everywhere")
        self.completed(meta)
        self.ok("status", meta["id"])
        self.ok("decide", "close", decision, "SQLite, decided from phone")
        self.ok("log", "Asked how the login fix went; said fine")
        output = self.ok("back")
        captain_text = self.captain_part(output)
        expected = ["started worker: Fix login (repo)", "check-in: Checked in from phone: keep going, no rush",
                    "decision opened: Pick storage", "steer (repo): Prefer SQLite everywhere",
                    "finished: Fix login (repo)", "decided: Pick storage -> SQLite, decided from phone",
                    "check-in: Asked how the login fix went; said fine"]
        positions = [captain_text.find(text) for text in expected]
        self.assertNotIn(-1, positions, captain_text)
        self.assertEqual(positions, sorted(positions))
        self.assertIn("While you were away (7 events):", captain_text)
        self.assertIn("Tasks: 1 finished, 0 landed, 0 failed.", captain_text)
        self.assertNotIn(meta["id"], captain_text)
        self.assertNotIn(decision, captain_text)
        self.assertIsNone(re.search(r"\b[0-9a-f]{12,40}\b", captain_text))
        self.assertIn("No open decisions.", captain_text)

    def test_decisions_made_while_away_count_in_full_and_are_not_reasked(self):
        storage, rename = self.decision("Pick storage"), self.decision("Rename CLI")
        self.ok("decide", "ask")
        meta = self.spawn("--title", "Ship docs")
        self.completed(meta)
        self.fake.prs = [harness.URL]
        self.ok("away", "travelling")
        # His check-in words have full authority: closing by number, landing, steering.
        self.ok("decide", "close", "1", "SQLite")
        self.ok("land", meta["id"], "--approved", "land the docs PR")
        self.ok("steer", "repo", "Docs first")
        self.assertEqual(len(self.gh.merges), 1)
        self.assertEqual(self.store.load(meta["id"])["landed"]["approved"], "land the docs PR")
        output = self.captain_part(self.ok("back"))
        self.assertIn("decided: Pick storage -> SQLite", output)
        self.assertIn("landed: Ship docs (repo); approved: land the docs PR", output)
        self.assertIn("Tasks: 1 finished, 1 landed, 0 failed.", output)
        decisions_part = output[output.index("open decision"):]
        self.assertIn("1. Rename CLI (asked again)", decisions_part)
        self.assertNotIn("Pick storage", decisions_part)
        self.assertEqual(self.events()[-1]["items"], [rename])
        self.assertTrue(next(d for d in self.store.decisions() if d["id"] == storage)["closed"])

    def test_hook_line_while_away_says_do_not_ask_and_decisions_count(self):
        self.assertIsNone(on_pre_llm_call())
        self.ok("away", "going to bed")
        context = on_pre_llm_call()["context"]
        self.assertTrue(context.startswith("boss: captain away since "), context)
        for text in ("(going to bed)", "don't ask him anything", "queue decisions for /back",
                     "anything he says or decides in a check-in counts in full"):
            self.assertIn(text, context)
        self.assertNotIn("\n", context)
        self.decision("Pick storage")
        context = on_pre_llm_call()["context"]
        self.assertIn("captain away", context)
        self.assertIn("1 need you", context)
        self.ok("back")
        self.assertNotIn("away", on_pre_llm_call()["context"])
        attention = (self.home / "attention.json").read_text()
        self.assertLess(len(attention), 4096)

    def test_long_timelines_group_by_hour_without_dropping_events(self):
        start = datetime(2026, 10, 24, 22, 50, tzinfo=timezone.utc)
        rows = [(start + timedelta(minutes=9 * i), "log", f"check-in: event {i}") for i in range(20)]
        lines = captain.timeline_lines(rows)
        self.assertEqual(sum(1 for line in lines if "check-in: event" in line), 20)
        headers = [line for line in lines if not line.startswith("  ")]
        self.assertEqual(len(headers), len({r[0].astimezone().replace(minute=0) for r in rows}))
        self.assertEqual(captain.timeline_lines(rows[:3])[0], f"{rows[0][0].astimezone():%H:%M} check-in: event 0")


if __name__ == "__main__":
    unittest.main()
