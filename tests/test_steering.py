from datetime import datetime
import json
import unittest
from unittest.mock import patch

from boss.common import BossError
from boss.preferences import BRIEF_LIMIT, compose_brief, goal_path, steers_path
from boss.service import footer
from tests import test_boss as harness


class SteeringTest(unittest.TestCase):
    setUp = harness.CLITest.setUp
    tearDown = harness.CLITest.tearDown
    cli = harness.CLITest.cli
    spawn = harness.CLITest.spawn

    def steer(self, text, *options, project="repo"):
        code, output = self.cli("steer", project, text, *options)
        self.assertEqual(code, 0, output)
        return output.split(":", 1)[0]

    def test_goal_unset_set_replace_and_restart_without_remote_calls(self):
        self.assertIn("goal: unset", self.cli("goal", "repo")[1])
        self.assertEqual(self.cli("goal", "repo", "Make it mobile-first")[0], 0)
        path = goal_path(self.home, "repo")
        self.assertEqual(path.read_text(), "Make it mobile-first")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.cli("goal", "repo", "Ship a small mobile app")[0], 0)
        self.assertIn("goal: Ship a small mobile app", self.cli("goal", "repo")[1])
        self.assertFalse(self.fake.calls)

    def test_goal_atomic_failure_preserves_previous_value(self):
        self.cli("goal", "repo", "Original goal")
        with patch("boss.state.os.replace", side_effect=OSError("simulated rename failure")):
            self.assertEqual(self.cli("goal", "repo", "New goal")[0], 3)
        self.assertEqual(self.store.goal("repo"), "Original goal")
        self.assertFalse(list((self.home / "projects").glob(".tmp-*")))

    def test_steers_append_and_replacements_preserve_history(self):
        first = self.steer("Use PostHog")
        path = steers_path(self.home, "repo")
        original, inode = path.read_bytes(), path.stat().st_ino
        second = self.steer("No PostHog", "--replaces", first)
        third = self.steer("Use local analytics", "--replaces", second)
        self.assertTrue(path.read_bytes().startswith(original))
        self.assertEqual(path.stat().st_ino, inode)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        records = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual([r["id"] for r in records], [first, second, third])
        self.assertEqual(len({r["id"] for r in records}), 3)
        self.assertTrue(all(datetime.fromisoformat(r["created_at"]).utcoffset() is not None for r in records))
        self.assertEqual(records[1]["replaces"], first)
        self.assertEqual(self.cli("steer", "list", "repo")[0], 0)
        active = self.cli("steer", "list", "repo")[1]
        self.assertIn("1 active steers", active)
        self.assertIn(third + ": active", active)
        self.assertNotIn("Use PostHog", active)
        history = self.cli("steer", "list", "repo", "--all")[1]
        self.assertIn("3 total steers", history)
        self.assertIn(first + ": superseded", history)
        self.assertIn(second + ": superseded", history)
        self.assertIn(third + ": active", history)
        self.assertFalse(self.fake.calls)

    def test_empty_and_separate_project_ledgers_need_no_registry(self):
        self.assertIn("0 active steers", self.cli("steer", "list", "repo")[1])
        self.cli("goal", "unregistered", "Independent goal")
        self.steer("Independent steer", project="unregistered")
        self.assertIn("goal: unset", self.cli("goal", "repo")[1])
        self.assertIn("0 total steers", self.cli("steer", "list", "repo", "--all")[1])
        self.assertIn("Independent steer", self.cli("steer", "list", "unregistered")[1])
        self.assertFalse(self.fake.calls)

    def test_related_task_must_exist_and_belong_to_the_project(self):
        task = self.spawn()
        steer_id = self.steer("Keep scope small", "--task", task["id"])
        self.assertEqual(self.store.steers("repo")[0]["task"], task["id"])
        self.assertIn("task=" + task["id"], self.cli("steer", "list", "repo")[1])
        self.assertEqual(self.cli("steer", "other", "Wrong project", "--task", task["id"])[0], 2)
        self.assertEqual(self.cli("steer", "repo", "Missing task", "--task", "unknown")[0], 1)
        self.assertEqual([r["id"] for r in self.store.steers("repo")], [steer_id])
        self.assertFalse(steers_path(self.home, "other").exists())

    def test_unknown_or_other_project_replacement_does_not_append(self):
        steer_id = self.steer("Existing steer")
        prior = steers_path(self.home, "repo").read_bytes()
        self.assertEqual(self.cli("steer", "repo", "Replacement", "--replaces", "unknown")[0], 1)
        self.assertEqual(steers_path(self.home, "repo").read_bytes(), prior)
        self.assertEqual(self.cli("steer", "other", "Replacement", "--replaces", steer_id)[0], 1)
        self.assertFalse(steers_path(self.home, "other").exists())

    def test_invalid_text_and_unsafe_names_never_write_project_files(self):
        for name in ("../outside", ".", "..", "nested/project", "bad\nname"):
            self.assertEqual(self.cli("goal", name, "Goal")[0], 1)
            self.assertEqual(self.cli("steer", name, "Steer")[0], 1)
            self.assertEqual(self.cli("steer", "list", name)[0], 1)
        for text in ("", " \n", "x" * (BRIEF_LIMIT + 1)):
            self.assertEqual(self.cli("goal", "repo", text)[0], 1)
            self.assertEqual(self.cli("steer", "repo", text)[0], 1)
        self.assertFalse((self.home / "projects").exists())

    def test_secrets_are_refused_without_echo_or_project_files(self):
        for args in (("goal", "repo", harness.TOKEN), ("steer", "repo", harness.TOKEN)):
            code, output = self.cli(*args)
            self.assertEqual(code, 2, output)
            self.assertNotIn(harness.TOKEN, output)
            self.assertIn("contains a token or secret", output)
        self.assertFalse((self.home / "projects").exists())

    def test_corrupt_or_incomplete_log_refuses_append_without_changing_bytes(self):
        self.steer("Original steer")
        path = steers_path(self.home, "repo")
        original = path.read_bytes()
        for damaged in (original + b'{"id":', original.rstrip(b"\n"), original + original):
            path.write_bytes(damaged)
            self.assertEqual(self.cli("steer", "list", "repo")[0], 3)
            self.assertEqual(self.cli("steer", "repo", "New steer")[0], 3)
            self.assertEqual(path.read_bytes(), damaged)

    def test_list_and_append_flags_cannot_be_silently_ignored(self):
        for args in (("list", "repo", "--task", "unknown"),
                     ("list", "repo", "--replaces", "unknown"), ("repo", "Steer", "--all")):
            self.assertEqual(self.cli("steer", *args)[0], 1)
        self.assertFalse(steers_path(self.home, "repo").exists())

    def test_goal_alone_requires_fits_before_remote_calls_or_task_creation(self):
        self.cli("goal", "repo", "Make it mobile-first")
        code, output = self.cli("spawn", "repo", "--title", "T")
        self.assertEqual(code, 2, output)
        self.assertIn("## Project goal\nMake it mobile-first", output)
        self.assertIn("--fits", output)
        self.assertFalse(self.fake.calls)
        self.assertEqual(self.store.tasks(), [])

    def test_steer_alone_requires_fits_and_displays_only_active_steers(self):
        first = self.steer("Use PostHog")
        active = self.steer("No PostHog", "--replaces", first)
        code, output = self.cli("spawn", "repo", "--title", "T")
        self.assertEqual(code, 2, output)
        self.assertIn("## Captain steers", output)
        self.assertIn(active + ": No PostHog", output)
        self.assertNotIn("Use PostHog", output)
        self.assertFalse(self.fake.calls)
        self.assertEqual(self.store.tasks(), [])

    def test_refusal_never_truncates_goal_or_any_active_steer(self):
        goal = "Long goal " + "g" * 1100 + " GOAL_END"
        first = "First steer " + "s" * 1100 + " FIRST_END"
        last = "Last steer " + "s" * 1100 + " LAST_END"
        self.cli("goal", "repo", goal)
        self.steer(first)
        self.steer(last)
        code, output = self.cli("spawn", "repo", "--title", "T")
        self.assertEqual(code, 2, output)
        for text in (goal, first, last):
            self.assertIn(text, output)
        self.assertNotIn("chars; --full", output)

    def test_fits_and_composed_context_are_durable_and_match_sent_brief(self):
        self.cli("goal", "repo", "Mobile-first app")
        first = self.steer("Use PostHog")
        active = self.steer("No PostHog", "--replaces", first)
        (self.home / "rules.md").write_text("GLOBAL_ONLY")
        (self.home / "projects/repo.md").write_text("NOTES_ONLY")
        with (self.home / "config.toml").open("a") as stream:
            stream.write('test_host = "wsl"\nno_mistakes = true\n')
        fits = "Improve mobile navigation without analytics"
        task = self.spawn("--fits", fits)
        self.assertEqual(self.store.load(task["id"])["fits"], fits)
        directory = self.store.directory(task["id"])
        intent = json.loads((directory / "events.log").read_text().splitlines()[0])
        self.assertEqual(intent["meta"]["fits"], fits)
        message = (directory / "brief.md").read_text()
        sent = next(a["message"] for n, a in self.fake.calls if n == "t3_thread_launch")
        self.assertEqual(message, sent)
        sections = ["investigate this", "## Rules", "## Project settings", "## Project notes",
                    "## Project goal\nMobile-first app", "## Captain steers", active + ": No PostHog",
                    "## boss worker contract"]
        offsets = [message.index(section) for section in sections]
        self.assertEqual(offsets, sorted(offsets))
        self.assertNotIn("Use PostHog", message)
        self.assertIn("Never merge. Never deploy.", message)
        # Later edits affect future workers, leaving the dispatched brief intact.
        self.cli("goal", "repo", "Changed goal")
        self.steer("Changed steer", "--replaces", active)
        self.assertEqual((directory / "brief.md").read_text(), sent)

    def test_task_related_steer_still_applies_to_future_project_tasks(self):
        previous = self.spawn()
        self.steer("No new dependency", "--task", previous["id"])
        task = self.spawn("--fits", "Use the standard library")
        message = (self.store.directory(task["id"]) / "brief.md").read_text()
        self.assertIn("No new dependency [task " + previous["id"] + "]", message)

    def test_empty_fits_refuses_and_multiline_or_oversized_fits_is_usage_error(self):
        self.cli("goal", "repo", "Mobile app")
        for fits in ("", " \t "):
            self.assertEqual(self.cli("spawn", "repo", "--title", "T", "--fits", fits)[0], 2)
        for fits in ("First\nSecond", "First\rSecond", "First\u2028Second", "x" * 2001):
            self.assertEqual(self.cli("spawn", "repo", "--title", "T", "--fits", fits)[0], 1)
        self.assertFalse(self.fake.calls)
        self.assertEqual(self.store.tasks(), [])

    def test_fits_is_optional_without_context_and_redacted_when_saved(self):
        task = self.spawn()
        self.assertIsNone(task["fits"])
        task = self.spawn("--fits", "Fits " + harness.TOKEN)
        self.assertEqual(task["fits"], "Fits [REDACTED]")
        for path in self.store.directory(task["id"]).iterdir():
            self.assertNotIn(harness.TOKEN, path.read_text())

    def test_goal_and_steers_share_the_existing_composed_size_limit(self):
        self.assertEqual(self.cli("goal", "repo", "g" * BRIEF_LIMIT)[0], 0)
        code, output = self.cli("spawn", "repo", "--title", "T", "--fits", "Fits goal")
        self.assertEqual(code, 1, output)
        self.assertIn("project goal layer", output)
        self.cli("goal", "repo", "g" * 60000)
        self.steer("s" * 60000)
        code, output = self.cli("spawn", "repo", "--title", "T", "--fits", "Fits both")
        self.assertEqual(code, 1, output)
        self.assertIn("captain steers layer", output)
        self.assertEqual(self.store.tasks(), [])
        self.assertFalse(self.fake.threads)

    def test_exact_composed_limit_includes_steering_sections(self):
        kwargs = {"goal": "Goal", "steers": [{"id": "s1", "text": "Steer"}]}
        contract = footer("ship", "direct-PR")
        original = compose_brief("x", self.home, "repo", {}, contract, self.redact, **kwargs)
        brief = "x" * (BRIEF_LIMIT - len(original) + 1)
        message = compose_brief(brief, self.home, "repo", {}, contract, self.redact, **kwargs)
        self.assertEqual(len(message), BRIEF_LIMIT)
        with self.assertRaises(BossError):
            compose_brief(brief + "x", self.home, "repo", {}, contract, self.redact, **kwargs)

    def test_hand_edited_context_secrets_refuse_with_or_without_fits(self):
        self.cli("goal", "repo", "Safe goal")
        self.steer("Safe steer")
        goal_file = goal_path(self.home, "repo")
        log = steers_path(self.home, "repo")
        record = json.loads(log.read_text())
        for layer in ("project goal", "captain steers"):
            goal_file.write_text(harness.TOKEN if layer == "project goal" else "Safe goal")
            log.write_text(json.dumps({**record, "text": harness.TOKEN if layer == "captain steers" else "Safe steer"}) + "\n")
            for options in ((), ("--fits", "Fits")):
                code, output = self.cli("spawn", "repo", "--title", "T", *options)
                self.assertEqual(code, 2, output)
                self.assertIn(layer + " layer contains a token", output)
                self.assertNotIn(harness.TOKEN, output)
        self.assertFalse(self.fake.calls)
        self.assertEqual(self.store.tasks(), [])

    def test_corrupt_log_cannot_bypass_reconciliation(self):
        self.steer("No analytics")
        with steers_path(self.home, "repo").open("a") as stream:
            stream.write('{"id":')
        self.assertEqual(self.cli("spawn", "repo", "--title", "T", "--fits", "Fits")[0], 3)
        self.assertFalse(self.fake.calls)
        self.assertEqual(self.store.tasks(), [])

    def test_project_show_reports_goal_presence_and_active_count(self):
        output = self.cli("project", "show", "repo")[1]
        self.assertIn("goal: absent", output)
        self.assertIn("active_steers: 0", output)
        self.cli("goal", "repo", "Mobile app")
        first = self.steer("Use PostHog")
        self.steer("No PostHog", "--replaces", first)
        self.steer("Use standard library")
        code, output = self.cli("project", "show", "repo")
        self.assertEqual(code, 0, output)
        self.assertIn("goal: " + str(goal_path(self.home, "repo")), output)
        self.assertIn("active_steers: 2", output)


if __name__ == "__main__":
    unittest.main()
