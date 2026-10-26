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
    fake_command = harness.HermeticTestCase.fake_command

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

    def test_drop_appends_tombstone_and_lists_distinct_history(self):
        superseded = self.steer("Old direction")
        dropped = self.steer("Forget this", "--replaces", superseded)
        active = self.steer("Keep this")
        path = steers_path(self.home, "repo")
        original, inode = path.read_bytes(), path.stat().st_ino
        code, output = self.cli("steer", "drop", "repo", dropped)
        self.assertEqual(code, 0, output)
        tombstone = output.split(":", 1)[0]
        self.assertIn(f"steer dropped project=repo drops={dropped}", output)
        self.assertIn("next: boss steer list repo --all", output)
        self.assertTrue(path.read_bytes().startswith(original))
        self.assertEqual(path.stat().st_ino, inode)
        records = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual(len(records), 4)
        self.assertEqual(set(records[-1]), {"id", "drops", "created_at"})
        self.assertEqual(records[-1]["drops"], dropped)
        self.assertIsNotNone(datetime.fromisoformat(records[-1]["created_at"]).utcoffset())
        self.assertEqual([r["id"] for r in self.store.steers("repo")], [active])
        listed = self.cli("steer", "list", "repo")[1]
        self.assertIn("1 active steers", listed)
        self.assertNotIn("Forget this", listed)
        self.assertNotIn(tombstone, listed)
        history = self.cli("steer", "list", "repo", "--all")[1]
        self.assertIn(superseded + ": superseded", history)
        self.assertIn(dropped + ": dropped", history)
        self.assertIn(active + ": active", history)
        self.assertIn(tombstone + ": tombstone", history)
        self.assertIn("drops=" + dropped, history)
        self.assertFalse(self.fake.calls)

    def test_drop_refusals_have_exact_errors_and_leave_history_unchanged(self):
        superseded = self.steer("Old steer")
        dropped = self.steer("Retract this", "--replaces", superseded)
        code, output = self.cli("steer", "drop", "repo", dropped)
        self.assertEqual(code, 0, output)
        tombstone = output.split(":", 1)[0]
        foreign = self.steer("Other project steer", project="other")
        paths = [steers_path(self.home, name) for name in ("repo", "other")]
        original = [path.read_bytes() for path in paths]
        cases = [("unknown", 1, "usage", "unknown steer to drop in this project"),
                 (foreign, 1, "usage", "unknown steer to drop in this project"),
                 (dropped, 2, "refused", "steer is already dropped"),
                 (superseded, 2, "refused", "steer is already superseded"),
                 (tombstone, 2, "refused", "cannot drop a tombstone")]
        for steer_id, expected, error, message in cases:
            with self.subTest(target=steer_id):
                code, output = self.cli("steer", "drop", "repo", steer_id)
                self.assertEqual((code, output), (expected,
                    f"error: {error}\nmessage: {message}\nnext: boss steer list repo --all\n"))
                self.assertEqual([path.read_bytes() for path in paths], original)
        code, output = self.cli("steer", "drop", "missing", dropped)
        self.assertEqual(code, 1, output)
        self.assertFalse(steers_path(self.home, "missing").exists())
        self.assertFalse(self.fake.calls)

    def test_legacy_log_without_tombstones_still_loads_without_migration(self):
        path = steers_path(self.home, "repo")
        path.parent.mkdir()
        records = [{"id": "old1", "text": "Original", "task": None, "replaces": None,
                    "created_at": "2026-01-01T00:00:00+00:00"},
                   {"id": "old2", "text": "Replacement", "task": None, "replaces": "old1",
                    "created_at": "2026-01-02T00:00:00+00:00"}]
        original = "".join(json.dumps(r) + "\n" for r in records).encode()
        path.write_bytes(original)
        self.assertEqual([r["id"] for r in self.store.steers("repo")], ["old2"])
        self.assertEqual([r["superseded"] for r in self.store.steers("repo", all=True)], [True, False])
        self.assertEqual(path.read_bytes(), original)

    def test_malformed_tombstones_refuse_reads_and_writes_without_changing_bytes(self):
        first = self.steer("Original")
        active = self.steer("Replacement", "--replaces", first)
        path = steers_path(self.home, "repo")
        original = path.read_text()
        drop = {"id": "tombstone", "drops": active, "created_at": "2026-01-01T00:00:00+00:00"}
        cases = [([drop], ""),
                 ([{**drop, "drops": "unknown"}], "unknown dropped steer"),
                 ([drop, {**drop, "id": "duplicate"}], "duplicate drop"),
                 ([drop, {**drop, "id": "nested", "drops": "tombstone"}], "cannot drop a tombstone"),
                 ([{**drop, "drops": first}], "cannot drop a superseded steer"),
                 ([{**drop, "text": "ambiguous"}], "tombstone must contain only"),
                 ([{**drop, "replaces": first}], "tombstone must contain only"),
                 ([{**drop, "task": None}], "tombstone must contain only"),
                 ([{**drop, "drops": None}], "invalid steer log"),
                 ([{**drop, "drops": []}], "invalid steer log"),
                 ([{**drop, "id": active}], "duplicate steer id"),
                 ([{**drop, "created_at": "2026-01-01T00:00:00"}], "timestamp has no timezone"),
                 ([{k: v for k, v in drop.items() if k != "created_at"}], "invalid steer log"),
                 ([drop, {"id": "new", "text": "Revive", "replaces": active,
                          "created_at": drop["created_at"]}], "cannot replace a dropped steer or tombstone")]
        # A valid tombstone is accepted first, then every malformed log is rejected.
        for records, message in cases:
            with self.subTest(records=records):
                damaged = original + "".join(json.dumps(r) + "\n" for r in records)
                path.write_text(damaged)
                if not message:
                    self.assertEqual(self.store.steers("repo"), [])
                    continue
                for all_records in (False, True):
                    with self.assertRaises(BossError) as caught:
                        self.store.steers("repo", all=all_records)
                    self.assertEqual(caught.exception.code, "ledger")
                    self.assertIn(message, caught.exception.message)
                for args in (("list", "repo"), ("repo", "New steer"), ("drop", "repo", active)):
                    code, output = self.cli("steer", *args)
                    self.assertEqual(code, 3, output)
                    self.assertIn("error: ledger", output)
                self.assertEqual(path.read_text(), damaged)

    def test_replacing_dropped_steer_or_tombstone_is_refused(self):
        dropped = self.steer("Retract this")
        tombstone = self.cli("steer", "drop", "repo", dropped)[1].split(":", 1)[0]
        path = steers_path(self.home, "repo")
        original = path.read_bytes()
        for target in (dropped, tombstone):
            code, output = self.cli("steer", "repo", "Replacement", "--replaces", target)
            self.assertEqual(code, 2, output)
            self.assertIn("cannot replace a dropped steer or tombstone", output)
            self.assertEqual(path.read_bytes(), original)

    def test_drop_parsing_preserves_normal_text_and_refuses_extra_arguments(self):
        for text in ("list", "drop", "drop this instruction"):
            self.steer(text)
        path = steers_path(self.home, "repo")
        original = path.read_bytes()
        for args in (("drop",), ("drop", "repo"), ("drop", "repo", "unknown", "extra"),
                     ("drop", "repo", "unknown", "--all"),
                     ("drop", "repo", "unknown", "--task", "unknown"),
                     ("drop", "repo", "unknown", "--replaces", "unknown"),
                     ("repo", "Text", "extra"), ("list", "repo", "extra"),
                     ("drop", "../outside", "unknown"), ("drop", "repo", "../outside")):
            with self.subTest(args=args):
                code, output = self.cli("steer", *args)
                self.assertEqual(code, 1, output)
                self.assertIn("error: usage", output)
                self.assertIn("next:", output)
                self.assertEqual(path.read_bytes(), original)

    def test_reserved_t3_project_titles_work_with_a_configured_local_alias(self):
        for key, value in (("host", "test"), ("project_id", "p1"), ("repository", "example/repo")):
            self.assertEqual(self.cli("config", "set", "projects.alias." + key, value)[0], 0)
        for title in ("list", "drop"):
            with self.subTest(title=title):
                self.fake.projects[0]["title"] = title
                steer_id = self.steer("Alias steer", project="alias")
                self.assertIn(steer_id, self.cli("steer", "list", "alias")[1])
                self.assertEqual(self.cli("steer", "drop", "alias", steer_id)[0], 0)
                code, output = self.cli("spawn", "alias", "--title", "Use alias")
                self.assertEqual(code, 0, output)
                task = self.store.load(output.split(":", 1)[0])
                self.assertEqual((task["project"], task["project_id"]), ("alias", "p1"))

    def test_drop_removes_context_from_gate_and_dispatched_worker_briefs(self):
        dropped = self.steer("Forget this instruction")
        active = self.steer("Keep this instruction")
        self.assertEqual(self.cli("steer", "drop", "repo", dropped)[0], 0)
        code, output = self.cli("spawn", "repo", "--title", "T")
        self.assertEqual(code, 2, output)
        self.assertIn(active + ": Keep this instruction", output)
        self.assertNotIn("Forget this instruction", output)
        self.assertFalse(self.fake.calls)
        task = self.spawn("--fits", "Follow the remaining steer")
        message = (self.store.directory(task["id"]) / "brief.md").read_text()
        sent = next(a["message"] for n, a in self.fake.calls if n == "t3_thread_launch")
        self.assertEqual(message, sent)
        self.assertIn("Keep this instruction", sent)
        self.assertNotIn("Forget this instruction", sent)
        self.assertEqual(self.cli("steer", "drop", "repo", active)[0], 0)
        self.assertIn("active_steers: 0", self.cli("project", "show", "repo")[1])
        task = self.spawn()
        message = (self.store.directory(task["id"]) / "brief.md").read_text()
        self.assertNotIn("## Captain steers", message)
        # A project goal still requires reconciliation when every steer is dropped.
        self.cli("goal", "repo", "Keep the goal")
        code, output = self.cli("spawn", "repo", "--title", "T")
        self.assertEqual(code, 2, output)
        self.assertIn("Keep the goal", output)
        self.assertNotIn("## Captain steers", output)

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
        self.fake_command("no-mistakes")
        fits = "Improve mobile navigation without analytics"
        task = self.spawn("--fits", fits)
        self.assertEqual(self.store.load(task["id"])["fits"], fits)
        directory = self.store.directory(task["id"])
        intent = json.loads((directory / "events.log").read_text().splitlines()[0])
        self.assertEqual(intent["meta"]["fits"], fits)
        message = (directory / "brief.md").read_text()
        sent = next(a["message"] for n, a in self.fake.calls if n == "t3_thread_launch")
        self.assertEqual(message, sent)
        sections = ["investigate this", "## Project goal\nMobile-first app", "## Captain steers",
                    active + ": No PostHog", "### boss worker contract"]
        offsets = [message.index(section) for section in sections]
        self.assertEqual(offsets, sorted(offsets))
        for absent in ("GLOBAL_ONLY", "NOTES_ONLY", "## Rules", "Execution settings", "test_host:", "no_mistakes:"):
            self.assertNotIn(absent, message)
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
        original = compose_brief("x", self.home, "repo", contract, self.redact, **kwargs)
        brief = "x" * (BRIEF_LIMIT - len(original) + 1)
        message = compose_brief(brief, self.home, "repo", contract, self.redact, **kwargs)
        self.assertEqual(len(message), BRIEF_LIMIT)
        with self.assertRaises(BossError):
            compose_brief(brief + "x", self.home, "repo", contract, self.redact, **kwargs)

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
