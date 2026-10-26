"""boss speaks in its own plain voice: "you" or the configured owner_name, never ship words."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re

from boss.inbox import Inbox, NOTE
from boss.watch import still_open
from tests import test_boss as harness
from tests.support import HermeticTestCase

ROOT = Path(__file__).resolve().parent.parent
URL = harness.URL
# Words from another tool's voice. "the user" is out too: boss says "you" or the person's name.
SHIP_WORDS = re.compile(r"\b(?:captain|firstmate|first mate|ahoy|crew|matey|aye aye|the user)\b", re.I)


class VoiceTest(HermeticTestCase):
    setUp_cli = harness.CLITest.setUp
    tearDown_cli = harness.CLITest.tearDown
    cli = harness.CLITest.cli
    spawn = harness.CLITest.spawn
    completed = harness.CLITest.completed
    as_owner = harness.CLITest.as_owner

    def setUp(self):
        self.setUp_cli()
        self.addCleanup(self.tearDown_cli)

    def ok(self, *args):
        code, output = self.cli(*args)
        self.assertEqual(code, 0, output)
        return output

    def name_owner(self, name="Sam"):
        config = self.home / "config.toml"
        config.write_text(f'owner_name = "{name}"\n' + config.read_text())

    def finished_with_pr(self, *extra):
        meta = self.spawn(*extra)
        self.fake.prs = [URL]
        self.gh.pr["headRefName"] = meta["branch"]
        self.completed(meta, f"BOSS: done: {URL}")
        return meta

    # Old names keep working ---------------------------------------------------

    def test_history_in_the_old_captain_journal_still_loads_and_new_records_go_to_the_new_file(self):
        at = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        legacy = self.home / "captain.jsonl"
        legacy.write_text(json.dumps({"event": "away", "at": at, "note": "going to bed"}) + "\n" +
                          json.dumps({"event": "log", "at": at, "text": "merge the docs fix"}) + "\n")
        before = legacy.read_bytes()
        self.assertIn("away mode: on since", self.ok())
        self.assertIn("Got it. Away mode is already on", self.ok("away", "again"))
        timeline = self.ok("back", "--full")
        self.assertIn("(going to bed)", timeline)
        self.assertIn("check-in: merge the docs fix", timeline)
        self.ok("back")
        self.assertIn("away mode: off", self.ok())
        self.assertEqual(legacy.read_bytes(), before)  # old history is read, never rewritten
        written = [json.loads(line)["event"] for line in (self.home / "user.jsonl").read_text().splitlines()]
        self.assertEqual(written, ["back"])

    def test_an_inbox_line_held_under_the_old_captain_kind_is_still_delivered(self):
        box = Inbox(self.home)
        box.hold("captain:away:2026-10-26T20:00:00+00:00", None, "captain", "Away mode on at 20:00.")
        self.assertEqual(box.take(self.store.load, "watch", "Hermes app"), f"[boss inbox]\nAway mode on at 20:00.\n{NOTE}")

    # Parked tasks are not open items ------------------------------------------

    def test_a_parked_task_never_counts_in_the_still_open_footer_or_the_inbox_count(self):
        self.as_owner()
        meta = self.finished_with_pr()
        self.ok()  # a bare look from the owner leaves the finished run unreviewed
        attention = json.loads((self.home / "attention.json").read_text())
        self.assertEqual(attention["count"], 1)
        self.assertEqual(still_open(self.home), "Still open: 1 finished run to review. Run boss status.")
        self.ok("park", meta["id"], "wait until after the demo")
        attention = json.loads((self.home / "attention.json").read_text())
        self.assertEqual((attention["count"], attention["items"]), (0, []))
        self.assertIsNone(still_open(self.home))
        # A question from its worker waits with the park too.
        meta = self.store.load(meta["id"])
        meta["observed"]["requests"] = ["request-1"]
        self.store.save(meta, "observed")
        self.store.refresh_attention()
        self.assertEqual(json.loads((self.home / "attention.json").read_text())["count"], 0)
        self.ok("unpark", meta["id"])
        self.assertEqual(json.loads((self.home / "attention.json").read_text())["items"],
                         [{"id": meta["id"], "kind": "input"}])

    # owner_name ---------------------------------------------------------------

    def test_without_owner_name_boss_says_you_or_nothing(self):
        self.as_owner()
        self.ok("away", "lunch")
        self.assertIn("away mode: on since", self.ok())
        self.ok("back")
        away, back = (item["line"] for item in Inbox(self.home).open_items())
        self.assertIn("Ask nothing until /back", away)
        self.assertIn("Write the catch-up now", back)
        brief = self.ok("brief", "repo")
        self.assertIn("A message that starts with [boss] is from the boss supervisor. Act on it", brief)
        self.assertIn("Any question, decision or blocker MUST go through", brief)

    def test_owner_name_names_the_person_in_supervisor_text_and_worker_briefs(self):
        self.name_owner()
        self.as_owner()
        self.ok("steer", "repo", "rule: Test on a phone")
        brief = self.ok("brief", "repo")
        for text in ("Any question, decision or blocker for Sam MUST go through",
                     "A message that starts with [boss] is from the boss supervisor, not Sam.",
                     "Standing rules from Sam. Follow every one:"):
            self.assertIn(text, brief)
        self.ok("away", "lunch")
        self.assertIn("Sam: away since", self.ok())
        self.ok("back")
        self.assertIn("Sam: here", self.ok())
        away, back = (item["line"] for item in Inbox(self.home).open_items())
        self.assertIn("Don't ask Sam anything until /back", away)
        self.assertIn("Anything Sam says or decides meanwhile counts in full", away)
        self.assertIn("Catch Sam up now", back)
        meta = self.finished_with_pr("--fits", "a fix that follows the rule")
        self.assertIn("landing needs Sam's words", self.cli("land", meta["id"])[1])
        self.ok("park", meta["id"], "after the demo")
        status = self.ok("status", meta["id"])
        self.assertIn("parked by Sam: after the demo", status)
        self.assertIn(f"next: wait; boss unpark {meta['id']} only when Sam says so", status)
        self.assertIn("Sam parked this: after the demo", self.cli("land", meta["id"], "--approved", "merge it")[1])

    def test_owner_name_must_be_one_short_line(self):
        for bad in ("x" * 61, "Sam  Smith", " Sam"):
            self.name_owner(bad)
            code, output = self.cli()
            self.assertEqual(code, 1, output)
            self.assertIn("owner_name: must be one line of at most 60 characters", output)
            config = self.home / "config.toml"
            config.write_text(config.read_text().split("\n", 1)[1])
        self.ok("config", "set", "owner_name", "Sam")
        self.assertIn("Sam: here", self.ok())

    # No ship words ------------------------------------------------------------

    def test_nothing_people_or_the_supervisor_read_uses_ship_words(self):
        # Allowed: the PR-body guard that refuses those words, and the old names kept working.
        allowed = ("PR_BODY_FORBIDDEN", "LEGACY_USER_JOURNAL", "captain.jsonl", 'command("ahoy"', "pr_body_forbidden")
        files = [*sorted((ROOT / "boss").glob("*.py")), *sorted((ROOT / "plugin").iterdir()),
                 ROOT / "skill/SKILL.md", ROOT / "README.md"]
        found = [f"{path.relative_to(ROOT)}:{number}: {line.strip()}"
                 for path in files if path.is_file()
                 for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
                 if SHIP_WORDS.search(line) and not any(word in line for word in allowed)]
        self.assertEqual(found, [])
