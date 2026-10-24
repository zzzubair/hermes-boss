"""Compiled rules and preview behavior through the same boundary as spawn."""
from pathlib import Path
import subprocess
from unittest.mock import patch

from boss.common import BossError, Redactor
from boss.config import Config
from boss.preferences import merged_rules
from tests import test_boss as harness
from tests.support import HermeticTestCase


class MergeTest(HermeticTestCase):
    def rules(self, global_text, project_text):
        (self.home / "rules.md").write_text(global_text)
        (self.home / "projects").mkdir(exist_ok=True)
        (self.home / "projects/repo.md").write_text(project_text)
        return merged_rules(self.home, "repo", Redactor())

    def test_replacement_keeps_global_order_then_project_only_sections(self):
        sections = self.rules("## First\nkeep\n## Testing\nold\n### Unit tests\nobsolete\n## Last\nkeep too",
                              "## New\nadded\n## Testing\nnew\n### Integration\nreplacement")
        self.assertEqual([s.heading for s in sections], ["First", "Testing", "Last", "New"])
        self.assertEqual(sections[1].text, "### Testing\nnew\n#### Integration\nreplacement")
        self.assertNotIn("obsolete", "\n".join(s.text for s in sections))
        self.assertEqual(sections[0].source, self.home / "rules.md")
        self.assertEqual(sections[1].source, self.home / "projects/repo.md")

    def test_preamble_replaces_as_a_whole_and_stays_distinct_from_named_heading(self):
        sections = self.rules("old preamble\n## preamble\nnamed\n## Keep\nglobal",
                              "new preamble\n## Extra\nproject")
        self.assertEqual([s.heading for s in sections], [None, "preamble", "Keep", "Extra"])
        self.assertEqual(sections[0].text, "new preamble")
        self.assertNotIn("old preamble", "\n".join(s.text for s in sections))

    def test_one_file_only_missing_preamble_and_empty_files_pass_through(self):
        for global_text, project_text, expected in (
            ("## Only\nglobal", "", ["Only"]),
            ("", "## Only\nproject", ["Only"]),
            ("global preamble\n## A\nbody", "## A\nnew", [None, "A"]),
            ("\n", "", []),
        ):
            with self.subTest(global_text=global_text, project_text=project_text):
                self.assertEqual([s.heading for s in self.rules(global_text, project_text)], expected)

    def test_fenced_headings_stay_literal_and_do_not_replace_sections(self):
        text = "## Example\n```md\n## Literal\n```\n~~~\n## Other literal\n~~~\n## Keep\nbody"
        sections = self.rules(text, "## Literal\nreal section")
        self.assertEqual([s.heading for s in sections], ["Example", "Keep", "Literal"])
        self.assertIn("```md\n## Literal\n```", sections[0].text)
        self.assertIn("~~~\n## Other literal\n~~~", sections[0].text)

    def test_setext_closing_hashes_and_case_sensitive_names(self):
        sections = self.rules("Testing\n-------\nold\n## testing\nkeep", "## Testing ##\nnew")
        self.assertEqual([s.heading for s in sections], ["Testing", "testing"])
        self.assertEqual(sections[0].text, "### Testing\nnew")

    def test_empty_override_removes_the_inherited_body(self):
        sections = self.rules("## Testing\nold instructions", "## Testing")
        self.assertEqual(sections[0].text, "### Testing")


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
                                 ["## Rules", "## Project goal", "## Captain steers"])
                for absent in ("old tests", "obsolete", "Project notes", "Project settings", "source:", "precedence", "win over", str(self.home)):
                    self.assertNotIn(absent, sent)
                for present in ("new tests", "format", "stdlib", "test_host: workstation", "no_mistakes: true"):
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
        self.assertIn("## Rules", output)
        self.assertFalse(empty.exists())

    def test_origin_annotates_only_effective_sections_and_never_changes_spawn(self):
        self.context()
        with (self.home / "config.toml").open("a") as stream:
            stream.write('no_mistakes=true\n')
        code, output = self.cli("brief", "repo", "--origin")
        self.assertEqual(code, 0, output)
        for source in (self.home / "rules.md", self.home / "projects/repo.md", self.home / "config.toml",
                       self.home / "projects/repo.goal", self.home / "projects/repo.steers.jsonl",
                       Path(harness.__file__).parents[1] / "boss/service.py"):
            self.assertIn(f"<!-- source: {source} -->", output)
        self.assertNotIn("old tests", output)
        self.assertNotIn("source:", self.cli("brief", "repo")[1])

    def test_invalid_role_and_secret_in_replaced_rules_are_refused(self):
        self.assertEqual(self.cli("brief", "repo", "--worker", "missing")[0], 1)
        (self.home / "rules.md").write_text("## Testing\n" + harness.TOKEN)
        (self.home / "projects").mkdir()
        (self.home / "projects/repo.md").write_text("## Testing\nsafe replacement")
        for args in (("brief", "repo"), ("brief", "repo", "--origin"), ("spawn", "repo", "--title", "T")):
            code, output = self.cli(*args)
            self.assertEqual(code, 2, output)
            self.assertNotIn(harness.TOKEN, output)
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
        for legacy, target in ((f'extra_rules=["{harness.TOKEN}"]\n', "<p>"),
                               ('[projects."dotted.name"]\nextra_rules=[]\n', "dotted.name")):
            path.write_text(legacy + original)
            with self.assertRaises(BossError) as caught:
                Config(self.home, self.redact)
            self.assertIn("extra_rules was removed", caught.exception.message)
            for args in (("doctor",), ("config", "show"), ("brief", "repo"), ("spawn", "repo", "--title", "T")):
                code, output = self.cli(*args)
                self.assertEqual(code, 1, output)
                self.assertIn("extra_rules was removed", output)
                self.assertIn(f"next: move the lines into {self.home}/projects/{target}.md", output)
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

    def test_lint_warnings_do_not_fail_doctor_and_include_unregistered_files(self):
        (self.home / "rules.md").write_text("## Same\nbody\n## Duplicate\nfirst\n## Duplicate\nlast\n##\nunnamed\n## Empty")
        (self.home / "projects").mkdir()
        (self.home / "projects/unregistered.md").write_text("## Same\nbody")
        code, output = self.doctor()
        self.assertEqual(code, 0, output)
        for warning in ("empty heading", "empty heading 'Empty' (no content)", "duplicate heading 'Duplicate'", "identical to global (pointless override)"):
            self.assertIn(warning, output)
        self.assertIn("warning: " + str(self.home / "projects/unregistered.md"), output)

    def test_unparsable_file_fails_doctor_and_dispatch(self):
        (self.home / "projects").mkdir()
        path = self.home / "projects/repo.md"
        for content in (b"## Heading\n```\nunclosed", b"invalid\xff", b"bad\0text"):
            with self.subTest(content=content):
                path.write_bytes(content)
                code, output = self.doctor()
                self.assertEqual(code, 1, output)
                self.assertIn("fail: rules " + str(path), output)
                self.assertNotEqual(self.cli("brief", "repo")[0], 0)
                self.assertNotEqual(self.cli("spawn", "repo", "--title", "T")[0], 0)
        self.assertFalse(self.fake.threads)
