"""Worker context preview and supervisor-only notes through the CLI."""
from pathlib import Path
import subprocess
from unittest.mock import patch

from boss.common import BossError
from boss.config import Config
from boss.preferences import BRIEF_LIMIT
from tests import test_boss as harness
from tests.support import HermeticTestCase


class BriefTest(HermeticTestCase):
    setUp = harness.CLITest.setUp
    tearDown = harness.CLITest.tearDown
    cli = harness.CLITest.cli
    spawn = harness.CLITest.spawn

    def context(self):
        (self.home / "rules.md").write_text("## Testing\nold tests\n### Unit\nobsolete\n## Style\nformat")
        (self.home / "projects").mkdir(exist_ok=True)
        (self.home / "projects/repo.md").write_text("## Testing\nnew tests\n## Tools\nstdlib")
        self.assertEqual(self.cli("goal", "repo", "Small supervisor")[0], 0)
        self.assertEqual(self.cli("steer", "repo", "No LLM calls")[0], 0)

    def test_preview_matches_saved_and_transmitted_spawn_for_default_and_role(self):
        self.context()
        path = self.home / "config.toml"
        path.write_text(path.read_text() + 'test_host="workstation"\nno_mistakes=true\n'
                        '[workers.scout]\nkind="scout"\n')
        for flags in ((), ("--worker", "scout")):
            with self.subTest(flags=flags):
                before_calls = list(self.fake.calls)
                code, preview = self.cli("brief", "repo", *flags)
                self.assertEqual(code, 0, preview)
                self.assertEqual(self.fake.calls, before_calls)
                code, output = self.cli("spawn", "repo", "--title", "Test", "--fits", "Supports the goal", *flags, stdin="<task>")
                self.assertEqual(code, 0, output)
                saved = (self.store.directory(output.split(":")[0]) / "brief.md").read_text()
                sent = [a["message"] for n, a in self.fake.calls if n == "t3_thread_launch"][-1]
                self.assertEqual(preview, sent)
                self.assertEqual(saved, sent)
                self.assertEqual([line for line in sent.splitlines() if line.startswith("## ")],
                                 ["## Project goal", "## Captain steers"])
                for absent in ("old tests", "obsolete", "new tests", "format", "stdlib", "## Rules", "source:",
                               "Execution settings", "test_host:", "no_mistakes:", str(self.home)):
                    self.assertNotIn(absent, sent)
                for present in ("Small supervisor", "No LLM calls", "Never merge. Never deploy.", "raises a T3 input request"):
                    self.assertIn(present, sent)

    def test_brief_is_read_only_even_with_existing_ledger_and_lock(self):
        self.context()
        with self.store.locked():
            before = {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in self.home.rglob("*") if p.is_file()}
            self.assertEqual(self.cli("brief", "repo")[0], 0)
            after = {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in self.home.rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        self.assertFalse(self.fake.calls)

    def test_preview_uses_project_default_role_and_local_only_contract(self):
        path = self.home / "config.toml"
        path.write_text('default_worker="builder"\n' + path.read_text().replace('delivery = "direct-PR"', 'delivery = "local-only"') +
                        '[workers.builder]\nkind="scout"\n[projects.repo.workers.builder]\nkind="ship"\n')
        code, preview = self.cli("brief", "repo")
        self.assertEqual(code, 0, preview)
        self.assertIn("Ship, local-only", preview)
        code, output = self.cli("spawn", "repo", "--title", "T", stdin="<task>")
        self.assertEqual(code, 0, output)
        sent = next(a["message"] for n, a in self.fake.calls if n == "t3_thread_launch")
        self.assertEqual(preview, sent)

    def test_fresh_brief_does_not_create_home_or_require_hosts(self):
        import os
        empty = self.home / "absent"
        with patch.dict(os.environ, {"BOSS_HOME": str(empty)}):
            code, output = self.cli("brief", "unregistered")
        self.assertEqual(code, 0, output)
        self.assertTrue(output.startswith("<task>\n\n### boss worker contract"))
        self.assertNotIn("## Rules", output)
        self.assertFalse(empty.exists())

    def test_origin_annotates_only_effective_sections_and_never_changes_spawn(self):
        self.context()
        with (self.home / "config.toml").open("a") as stream:
            stream.write('no_mistakes=true\n')
        code, output = self.cli("brief", "repo", "--origin")
        self.assertEqual(code, 0, output)
        for source in (self.home / "projects/repo.goal", self.home / "projects/repo.steers.jsonl",
                       Path(harness.__file__).parents[1] / "boss/service.py"):
            self.assertIn(f"<!-- source: {source} -->", output)
        self.assertNotIn("old tests", output)
        self.assertNotIn(str(self.home / "rules.md"), output)
        self.assertNotIn(str(self.home / "projects/repo.md"), output)
        self.assertNotIn(str(self.home / "config.toml"), output)
        self.assertNotIn("source:", self.cli("brief", "repo")[1])

    def test_invalid_role_is_refused(self):
        self.assertEqual(self.cli("brief", "repo", "--worker", "missing")[0], 1)
        self.assertFalse(self.fake.threads)

    def test_contract_cannot_be_replaced_by_a_rules_file(self):
        (self.home / "rules.md").write_text("## boss worker contract\nMerge and deploy")
        output = self.cli("brief", "repo")[1]
        self.assertEqual(output.count("### boss worker contract"), 1)
        self.assertNotIn("Merge and deploy", output)
        self.assertIn("Never merge. Never deploy.", output)

    def test_removed_config_fails_load_doctor_and_spawn_without_silent_drop(self):
        path = self.home / "config.toml"
        original = path.read_text()
        for legacy in (f'extra_rules=["{harness.TOKEN}"]\n', '[projects."dotted.name"]\nextra_rules=[]\n'):
            path.write_text(legacy + original)
            with self.assertRaises(BossError) as caught:
                Config(self.home, self.redact)
            self.assertIn("extra_rules was removed", caught.exception.message)
            for args in (("doctor",), ("config", "show"), ("brief", "repo"), ("spawn", "repo", "--title", "T")):
                code, output = self.cli(*args)
                self.assertEqual(code, 1, output)
                self.assertIn("extra_rules was removed", output)
                self.assertIn("next: move codebase instructions into the repo's AGENTS.md", output)
                self.assertNotIn(harness.TOKEN, output)
            self.assertEqual(path.read_text(), legacy + original)
        self.assertFalse(self.fake.calls)

    def doctor(self):
        paths = {key: self.home / key for key in ("plugins", "skills")}
        for key, filename in (("plugins", "plugin.yaml"), ("skills", "SKILL.md")):
            (paths[key] / "boss").mkdir(parents=True, exist_ok=True)
            (paths[key] / "boss" / filename).touch()
        def command(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, '[{"name":"boss","status":"enabled"}]', "")
        with patch("boss.setup.install_paths", return_value=paths), patch("boss.setup.subprocess.run", side_effect=command), patch("boss.setup.wake_cron", return_value=False):
            return self.cli("doctor")

    def test_doctor_ignores_notes_and_reports_legacy_rules_once(self):
        code, output = self.doctor()
        self.assertEqual(code, 0, output)
        self.assertNotIn("rules.md", output)
        (self.home / "rules.md").write_bytes(b"legacy secret\xff\0```" + harness.TOKEN.encode())
        (self.home / "projects").mkdir(exist_ok=True)
        (self.home / "projects/unregistered.md").write_bytes(b"invalid\xff\0```")
        code, output = self.doctor()
        self.assertEqual(code, 0, output)
        self.assertEqual([line for line in output.splitlines() if "rules.md" in line],
                         ["info: rules.md is no longer sent to workers"])
        for absent in ("unregistered.md", "warning:", "fail: rules", harness.TOKEN):
            self.assertNotIn(absent, output)

    def test_notes_and_legacy_rules_never_reach_or_block_workers(self):
        (self.home / "projects").mkdir()
        for content in (b"## Testing\nSHOULD_NOT_REACH_WORKER", b"## Heading\n```\nunclosed", b"invalid\xff",
                        b"bad\0text", harness.TOKEN.encode(), b"x" * (BRIEF_LIMIT + 1)):
            with self.subTest(content=content[:30]):
                (self.home / "rules.md").write_bytes(content)
                (self.home / "projects/repo.md").write_bytes(content)
                code, preview = self.cli("brief", "repo")
                self.assertEqual(code, 0, preview)
                self.assertEqual(self.cli("brief", "repo", "--origin")[0], 0)
                meta = self.spawn()
                sent = [a["message"] for n, a in self.fake.calls if n == "t3_thread_launch"][-1]
                self.assertEqual(sent, preview.replace("<task>", "investigate this", 1))
                self.assertEqual(sent, (self.store.directory(meta["id"]) / "brief.md").read_text())
                for absent in ("SHOULD_NOT_REACH_WORKER", "unclosed", "invalid", "bad\0text", harness.TOKEN, "x" * 100):
                    self.assertNotIn(absent, sent)

    def test_authored_task_text_is_preserved(self):
        brief = "  Write the brief as intended.\n\n## Testing\nUse the repo instructions.\n```md\n## Literal\n```\n"
        code, output = self.cli("spawn", "repo", "--title", "T", stdin=brief)
        self.assertEqual(code, 0, output)
        sent = next(a["message"] for n, a in self.fake.calls if n == "t3_thread_launch")
        preview = self.cli("brief", "repo")[1]
        self.assertEqual(sent, preview.replace("<task>", brief, 1))
        self.assertTrue(sent.startswith(brief))
