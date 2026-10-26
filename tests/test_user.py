from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import unittest

from boss import user
from boss.inbox import Inbox, LINE_CHARS, NOTE
import plugin
from plugin import on_pre_llm_call, register
from tests import test_boss as harness


class UserTest(unittest.TestCase):
    setUp = harness.CLITest.setUp
    tearDown = harness.CLITest.tearDown
    cli = harness.CLITest.cli
    spawn = harness.CLITest.spawn
    completed = harness.CLITest.completed
    as_owner = harness.CLITest.as_owner

    def ok(self, *args):
        code, output = self.cli(*args)
        self.assertEqual(code, 0, output)
        return output

    def events(self):
        path = self.home / "user.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def presence(self):
        return [(e["event"], e["at"]) for e in self.events() if e["event"] in ("away", "back")]

    def decision(self, text):
        return self.ok("decide", "add", text).split(":", 1)[0]

    def visible_part(self, output):
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
        self.assertIn("away mode: on since", self.ok())
        self.ok("back")
        self.assertEqual([e for e, _ in self.presence()], ["away", "back"])
        self.assertIn("away mode: off", self.ok())

    def test_away_is_idempotent_and_keeps_first_since_and_note(self):
        first = self.ok("away", "going to bed")
        self.assertIn("(going to bed)", first)
        second = plugin.on_away("different note")  # the real /away handler, via a subprocess
        self.assertIn("Got it. Away mode is already on", second)
        self.assertIn("(going to bed)", second)
        self.assertNotIn("next:", second)
        away = [e for e in self.events() if e["event"] == "away"]
        self.assertEqual(len(away), 1)
        self.assertEqual(away[0]["note"], "going to bed")
        self.assertIn("Welcome back", plugin.on_back(""))
        output = self.ok("back")
        self.assertIn("Welcome back. You weren't marked away", output)
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
        self.assertEqual(sorted(context.commands), ["afk", "ahoy", "away", "back", "boss"])
        self.assertEqual(context.commands["boss"][1], "[take|release|status]")
        self.assertIs(context.commands["afk"][0], context.commands["away"][0])
        self.assertIs(context.commands["ahoy"][0], context.commands["back"][0])
        self.assertEqual(context.commands["away"][1], "[note]")

    def test_ask_round_numbers_in_order_and_close_by_number(self):
        first, second, third = (self.decision(t) for t in ("Pick storage", "Rename CLI", "Drop Python 3.10"))
        output = self.ok("decide", "ask")
        self.assertIn('3 open decisions (round 1; reply like "1 yes 2 no"):', output)
        self.assertRegex(output, r"1\. Pick storage\n2\. Rename CLI\n3\. Drop Python 3\.10\n")
        self.assertNotIn(first, self.visible_part(output))
        self.assertEqual(self.events()[-1]["items"], [first, second, third])
        self.ok("decide", "close", "3", "no")
        self.ok("decide", "close", "1", "yes", "--round", "1")
        closed = {d["id"]: d for d in self.store.decisions()}
        self.assertEqual((closed[first]["resolution"], closed[third]["resolution"]), ("yes", "no"))
        self.assertFalse(closed[second]["closed"])
        # Repeating the answer is safe and keeps the first one.
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
        # An unclear answer goes back to him only with more detail.
        code, output = self.cli("decide", "ask")
        self.assertEqual(code, 2)
        self.assertIn("1 unclear answer needs more detail", output)
        self.ok("decide", "detail", "2", "Options: boss, t3boss")
        output = self.ok("decide", "ask")
        self.assertIn("1. Rename CLI (asked again; unclear last time: he said 'sure' but which name?)\n"
                      "   more: Options: boss, t3boss\n", output)
        output = self.ok("decide", "ask")
        self.assertIn("1. Rename CLI (asked again)\n", output)
        self.assertNotIn("unclear", output)
        self.ok("decide", "close", "1", "first answer")
        code, output = self.cli("decide", "unclear", "1", "late")
        self.assertEqual(code, 2)
        self.assertIn("never asked again", output)

    def test_supervisor_cannot_ask_while_away_unless_he_asks(self):
        self.decision("Pick storage")
        self.ok("away")
        code, output = self.cli("decide", "ask")
        self.assertEqual(code, 2)
        self.assertIn("away mode is on; don't ask", output)
        self.assertIn("1. Pick storage", self.ok("decide", "ask", "--requested", "what's open?"))

    def test_back_timeline_is_chronological_with_checkins_and_no_ids(self):
        self.as_owner()
        self.ok("away", "going to bed")
        meta = self.spawn("--title", "Fix login")
        self.ok("log", "Checked in from phone: keep going, no rush")
        decision = self.decision("Pick storage")
        self.ok("steer", "repo", "Prefer SQLite everywhere")
        self.completed(meta)
        self.ok("status", meta["id"])
        self.ok("decide", "close", decision, "SQLite, decided from phone")
        self.ok("log", "Asked how the login fix went; said fine")
        self.ok("back")
        visible_text = self.visible_part(self.ok("back", "--full"))
        expected = ["started worker: Fix login (repo)", "check-in: Checked in from phone: keep going, no rush",
                    "decision opened: Pick storage", "steer (repo): Prefer SQLite everywhere",
                    "finished: Fix login (repo)", "decided: Pick storage -> SQLite, decided from phone",
                    "check-in: Asked how the login fix went; said fine"]
        positions = [visible_text.find(text) for text in expected]
        self.assertNotIn(-1, positions, visible_text)
        self.assertEqual(positions, sorted(positions))
        self.assertIn("While away (7 events):", visible_text)
        self.assertIn("Tasks: 1 finished, 0 landed, 0 failed.", visible_text)
        self.assertNotIn(meta["id"], visible_text)
        self.assertNotIn(decision, visible_text)
        self.assertIsNone(re.search(r"\b[0-9a-f]{12,40}\b", visible_text))
        self.assertIn("0 open decisions", visible_text)

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
        self.ok("back")
        output = self.visible_part(self.ok("back", "--full"))
        self.assertIn("decided: Pick storage -> SQLite", output)
        self.assertIn("landed: Ship docs (repo); approved: land the docs PR", output)
        self.assertIn("Tasks: 1 finished, 1 landed, 0 failed.", output)
        # The supervisor numbers the round once he is back; his answered one is not asked again.
        decisions_part = self.ok("decide", "ask")
        self.assertIn("1. Rename CLI (asked again)", decisions_part)
        self.assertNotIn("Pick storage", decisions_part)
        self.assertEqual(self.events()[-1]["items"], [rename])
        self.assertTrue(next(d for d in self.store.decisions() if d["id"] == storage)["closed"])

    def test_away_and_back_reach_the_supervisor_through_the_inbox_not_his_message(self):
        self.assertIsNone(on_pre_llm_call())
        self.assertTrue(self.ok("away", "going to bed").startswith("Got it. Away mode on since "))
        self.assertIsNone(on_pre_llm_call())  # nothing rides on the person's own message
        self.decision("Pick storage")
        self.assertIsNone(on_pre_llm_call())
        self.ok("away", "again")  # nothing changed: nothing new to tell
        self.assertTrue(self.ok("back").startswith("Welcome back. Catching you up."))
        self.ok("back")
        box = Inbox(self.home)
        items = box.open_items()
        self.assertEqual([(i["task"], i["kind"]) for i in items], [(None, "user"), (None, "user")])
        away, back = (i["line"] for i in items)
        self.assertRegex(away, r"^Away mode on at \d\d:\d\d \(going to bed\)\. Ask nothing until /back")
        for text in ("queue decisions for /back", "Anything said or decided meanwhile counts in full"):
            self.assertIn(text, away)
        self.assertRegex(back, r"^Away mode off at \d\d:\d\d after under a minute away \(since \d\d:\d\d \(going to bed\)\): "
                               r"1 event, tasks 0 finished, 0 landed, 0 failed, 1 open decision\. Write the catch-up now")
        self.assertIn("read boss back --full", back)
        # Both survive delivery: away and back events are never stale and never merged into one line.
        text = box.take(self.store.load, "watch", "Hermes app")
        self.assertEqual(text, f"[boss inbox]\n{away}\n{back}\n{NOTE}")
        self.assertEqual(box.open_items(), [])
        attention = (self.home / "attention.json").read_text()
        self.assertLess(len(attention), 4096)

    def test_slash_away_and_back_reply_with_one_line_only(self):
        self.assertEqual(plugin.on_away("going to bed"), f"Got it. Away mode on {user.since_text(self.away())}.")
        self.assertNotIn("\n", plugin.on_away("again"))
        meta = self.spawn("--title", "Fix login")
        for i in range(60):
            self.ok("log", f"check-in number {i}: keep going")
        self.decision("Pick storage")
        self.completed(meta)
        self.ok("status", meta["id"])
        # However much happened, his /back reply is the ack and nothing else: no event list, no decisions.
        self.assertEqual(plugin.on_back(""), "Welcome back. Catching you up.")
        self.assertEqual([e["event"] for e in self.events() if e["event"] in ("away", "back", "ask")], ["away", "back"])

    def test_slash_ahoy_is_back(self):
        context = type("Context", (), {"register_hook": lambda *a: None, "commands": {},
                                       "register_command": lambda self, name, handler, **k: self.commands.update({name: handler})})()
        register(context)
        self.assertEqual(plugin.on_away("lunch"), f"Got it. Away mode on {user.since_text(self.away())}.")
        self.assertEqual(context.commands["ahoy"](""), "Welcome back. Catching you up.")
        self.assertEqual([e["event"] for e in self.events() if e["event"] in ("away", "back")], ["away", "back"])

    def test_back_hands_the_catch_up_to_the_supervisor_through_the_inbox(self):
        self.as_owner()
        self.ok("away", "going to bed")
        meta = self.spawn("--title", "Fix login")
        for i in range(60):
            self.ok("log", f"check-in number {i}: keep going")
        self.decision("Pick storage")
        self.completed(meta)
        self.ok("status", meta["id"])
        self.ok("back")
        items = [i for i in Inbox(self.home).open_items() if i["key"].startswith("user:back:")]
        self.assertEqual(len(items), 1)
        line = items[0]["line"]
        self.assertRegex(line, r"^Away mode off at \d\d:\d\d after under a minute away \(since \d\d:\d\d "
                               r"\(going to bed\)\): 63 events, tasks 1 finished, 0 landed, 0 failed, 1 open decision\.")
        for text in ("read boss back --full", "short catch-up", "Never paste the timeline"):
            self.assertIn(text, line)
        self.assertNotIn("check-in number", line)
        self.assertNotIn(meta["id"], line)
        # Delivered whole: the inbox never cuts it short.
        self.assertLessEqual(len(line), LINE_CHARS)

    def test_back_full_is_read_only_and_shows_the_latest_window(self):
        self.assertIn("Away mode has not run yet", self.ok("back", "--full"))
        self.ok("away", "lunch")
        self.ok("log", "first trip: all good")
        self.ok("back")
        self.ok("away", "going to bed")
        self.ok("log", "second trip: merge it")
        self.decision("Pick storage")
        before = self.events()
        output = self.ok("back", "--full")
        self.assertRegex(output, r"^Away under a minute, since \d\d:\d\d \(going to bed\); still away\.\n")
        self.assertIn("check-in: second trip: merge it", output)
        self.assertNotIn("first trip", output)
        self.assertIn("1 open decisions\n- Pick storage (not asked yet)", output)
        self.assertEqual(self.events(), before)  # read-only: no presence change, no round
        self.assertIn("away mode: on", self.ok())
        self.ok("back")
        output = self.ok("back", "--full")
        self.assertRegex(output, r"^Away under a minute, since \d\d:\d\d \(going to bed\) until \d\d:\d\d\.\n")
        self.assertIn("next: boss decide ask", output)

    def test_skill_tells_the_supervisor_to_write_a_short_catch_up(self):
        skill = (Path(plugin.__file__).resolve().parent.parent / "skill" / "SKILL.md").read_text()
        section = skill[skill.index("### Writing the catch-up"):skill.index("When they answer a numbered list")]
        for text in ("boss back --full", "short catch-up in 3-5 plain bullets", "what shipped, what broke, what",
                     "No PR lists, no ids", "test counts unless something", "with your recommendation",
                     "raw timeline only when they ask"):
            self.assertIn(text, " ".join(section.split()))

    def away(self):
        return user.presence(self.store.user_events())

    def test_long_timelines_group_by_hour_without_dropping_events(self):
        start = datetime(2026, 10, 24, 22, 50, tzinfo=timezone.utc)
        rows = [(start + timedelta(minutes=9 * i), "log", f"check-in: event {i}") for i in range(20)]
        lines = user.timeline_lines(rows)
        self.assertEqual(sum(1 for line in lines if "check-in: event" in line), 20)
        headers = [line for line in lines if not line.startswith("  ")]
        self.assertEqual(len(headers), len({r[0].astimezone().replace(minute=0) for r in rows}))
        self.assertEqual(user.timeline_lines(rows[:3])[0], f"{rows[0][0].astimezone():%H:%M} check-in: event 0")


if __name__ == "__main__":
    unittest.main()
