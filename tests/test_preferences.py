import os
import shlex
import subprocess
import tempfile
import tomllib
import unittest
from unittest.mock import patch

from boss.common import BossError
from boss.config import Config
from boss.integrations import repository_from_origin, resolve_repository
from boss.preferences import BRIEF_LIMIT, compose_brief
from boss.service import footer
from tests import test_boss as harness
from tests.support import HermeticTestCase


TOKEN = harness.TOKEN


class PreferenceTest(unittest.TestCase):
    # Reuse the fake HTTP server harness without inheriting its test methods.
    setUp = harness.CLITest.setUp
    tearDown = harness.CLITest.tearDown
    cli = harness.CLITest.cli
    spawn = harness.CLITest.spawn
    completed = harness.CLITest.completed

    def unlisted(self):
        config = self.home / "config.toml"
        config.write_text('default_host = "test"\n' + config.read_text().split("[projects.repo]")[0])
        self.fake.projects[0]["repositoryIdentity"] = None

    def test_unlisted_exact_title_match_spawns_without_registration(self):
        self.unlisted()
        with patch("boss.service.resolve_repository", return_value="example/repo") as resolve:
            meta = self.spawn()
        resolve.assert_called_once()
        self.assertEqual(resolve.call_args.args[1], "/fake/repo")
        self.assertEqual(meta["project_source"], "T3 lookup")
        self.assertEqual(meta["host"], "test")
        self.assertEqual(meta["project_id"], "p1")
        self.assertFalse(meta["auto_merge"])
        self.assertNotIn("[projects.", (self.home / "config.toml").read_text())

    def test_zero_title_matches_refuses_with_candidates(self):
        self.unlisted()
        self.fake.projects[0]["title"] = "different"
        code, output = self.cli("spawn", "repo", "--title", "T")
        self.assertEqual(code, 2, output)
        self.assertIn("0 exact title matches", output)
        self.assertIn("different (p1)", output)
        self.assertFalse(self.fake.threads)

    def test_empty_catalog_is_definitive(self):
        self.unlisted()
        self.fake.projects = []
        code, output = self.cli("spawn", "repo", "--title", "T")
        self.assertEqual(code, 2, output)
        self.assertIn("candidates: 0 projects", output)
        self.assertIn("0 projects host=test", self.cli("project", "list")[1])

    def test_many_title_matches_refuses_with_ids(self):
        self.unlisted()
        self.fake.projects.append({**self.fake.projects[0], "id": "p2"})
        code, output = self.cli("spawn", "repo", "--title", "T")
        self.assertEqual(code, 2, output)
        self.assertIn("2 exact title matches", output)
        self.assertIn("repo (p1)", output)
        self.assertIn("repo (p2)", output)
        self.assertFalse(self.fake.threads)

    def test_title_match_is_case_sensitive_and_exact(self):
        self.unlisted()
        for title in ("Repo", "repo-extra", " repo "):
            self.fake.projects[0]["title"] = title
            self.assertEqual(self.cli("spawn", "repo", "--title", "T")[0], 2)

    def test_lookup_checks_all_pages_before_selecting(self):
        self.unlisted()
        original = self.fake.tool
        first = self.fake.projects[0]
        second = {**first, "id": "p2"}

        def paginated(name, args):
            if name == "t3_project_list":
                return {"projects": [second] if args.get("cursor") else [first],
                        "nextCursor": None if args.get("cursor") else 1}
            return original(name, args)

        with patch.object(self.fake, "tool", side_effect=paginated):
            self.assertIn("2 exact title matches", self.cli("spawn", "repo", "--title", "T")[1])
            first["title"] = "other"
            with patch("boss.service.resolve_repository", return_value="example/repo"):
                meta = self.spawn()
        self.assertEqual(meta["project_id"], "p2")

    def test_project_catalog_refuses_repeated_cursor(self):
        self.unlisted()
        with patch.object(self.fake, "tool", return_value={"projects": [], "nextCursor": 1}):
            code, output = self.cli("project", "list")
        self.assertEqual(code, 3, output)
        self.assertIn("repeated its cursor", output)

    def test_no_default_host_and_explicit_host_override(self):
        self.unlisted()
        config = self.home / "config.toml"
        config.write_text(config.read_text().replace('default_host = "test"\n', ""))
        self.assertEqual(Config(self.home, self.redact).default_host, "")
        with patch("boss.service.resolve_repository", return_value="example/repo"):
            meta = self.spawn("--host", "test")
        self.assertEqual(meta["host"], "test")

    def test_registry_overrides_lookup_repository_and_preferences(self):
        self.fake.projects[0]["title"] = "T3 renamed the project"
        self.fake.projects[0]["repositoryIdentity"] = None
        config = self.home / "config.toml"
        config.write_text(config.read_text() + 'auto_merge = true\nno_mistakes = true\ntest_host = "wsl"\n')
        with patch("boss.service.resolve_repository", side_effect=AssertionError("registry repository must win")):
            meta = self.spawn()
            code, shown = self.cli("project", "show", "repo")
        self.assertEqual(code, 0, shown)
        self.assertIn("source: registry", shown)
        self.assertIn("auto_merge: true", shown)
        self.assertIn("no_mistakes: true", shown)
        self.assertIn("test_host: wsl", shown)
        self.assertTrue(meta["auto_merge"])
        self.assertTrue(meta["no_mistakes"])
        self.assertEqual(meta["test_host"], "wsl")
        status = self.cli("status", meta["id"])[1]
        self.assertIn("auto_merge: true", status)
        message = (self.store.directory(meta["id"]) / "brief.md").read_text()
        self.assertNotIn("auto_merge", message)
        self.assertIn("Never merge. Never deploy.", message)

    def test_settings_only_entry_uses_title_lookup_and_remains_optional(self):
        self.unlisted()
        with (self.home / "config.toml").open("a") as f:
            f.write('[projects.repo]\nno_mistakes = false\n')
        with patch("boss.service.resolve_repository", return_value="example/repo"):
            meta = self.spawn()
        self.assertEqual(meta["project_source"], "registry + T3 lookup")
        message = (self.store.directory(meta["id"]) / "brief.md").read_text()
        self.assertFalse(meta["no_mistakes"])
        self.assertNotIn("no_mistakes:", message)

    def test_composed_brief_order_and_sent_saved_identity(self):
        (self.home / "rules.md").write_text("## General\nGLOBAL_ONLY")
        (self.home / "projects").mkdir()
        (self.home / "projects/repo.md").write_text("NOTES_ONLY")
        with (self.home / "config.toml").open("a") as f:
            f.write('test_host = "wsl"\nno_mistakes = true\n')
        meta = self.spawn()
        message = (self.store.directory(meta["id"]) / "brief.md").read_text()
        sent = next(a["message"] for n, a in self.fake.calls if n == "t3_thread_launch")
        self.assertEqual(message, sent)
        sections = ["investigate this", "### boss worker contract"]
        offsets = [message.index(s) for s in sections]
        self.assertEqual(offsets, sorted(offsets))
        for absent in ("GLOBAL_ONLY", "NOTES_ONLY", "## Rules", "Execution settings", "test_host:", "no_mistakes:"):
            self.assertNotIn(absent, message)
        self.assertNotIn("win over", message)
        self.assertLessEqual(len(message), BRIEF_LIMIT)

    def test_empty_notes_do_not_change_the_brief(self):
        (self.home / "rules.md").write_text("  \n")
        (self.home / "projects").mkdir()
        (self.home / "projects/repo.md").write_text("\n")
        meta = self.spawn()
        message = (self.store.directory(meta["id"]) / "brief.md").read_text()
        self.assertEqual(message, "investigate this" + footer("ship", "direct-PR"))
        self.assertNotIn("## Project settings", message)
        self.assertNotIn("## Project notes", message)

    def test_composed_limit_refuses_before_any_launch_or_ledger(self):
        code, output = self.cli("spawn", "repo", "--title", "T", stdin="x" * BRIEF_LIMIT)
        self.assertEqual(code, 1, output)
        self.assertIn("worker contract layer", output)
        self.assertEqual(self.store.tasks(), [])
        self.assertFalse(self.fake.threads)

    def test_exact_composed_limit_and_one_over(self):
        contract = footer("ship", "direct-PR")
        brief = "x" * (BRIEF_LIMIT - len(contract))
        composed = compose_brief(brief, self.home, "repo", contract, self.redact)
        self.assertEqual(len(composed), BRIEF_LIMIT)
        with self.assertRaises(BossError):
            compose_brief(brief + "x", self.home, "repo", contract, self.redact)

    def test_limit_is_applied_after_redaction_expands_task_text(self):
        self.redact.add("short-secret")
        # A short credential can be smaller than its replacement marker.
        self.redact.add("xy")
        with self.assertRaises(BossError) as caught:
            compose_brief("xy" * 20000, self.home, "repo", footer("ship", "direct-PR"), self.redact)
        self.assertIn("task brief layer", caught.exception.message)

    def test_show_sources_and_list_markers(self):
        (self.home / "rules.md").write_text("global")
        (self.home / "projects").mkdir()
        notes = "## Testing\nSupervisor context\n```\nUnclosed fence is fine here.\n" + TOKEN
        (self.home / "projects/repo.md").write_text(notes)
        self.fake.projects.append({**self.fake.projects[0], "id": "other", "title": "other"})
        code, output = self.cli("project", "list", "--host", "test")
        self.assertEqual(code, 0, output)
        self.assertIn("2 projects host=test", output)
        self.assertIn("p1: repo registry=true notes=true", output)
        self.assertIn("other: other registry=false notes=false", output)
        shown = self.cli("project", "show", "repo")[1]
        self.assertIn("registry: " + str(self.home / "config.toml"), shown)
        self.assertNotIn("rules:", shown)
        self.assertIn("notes: " + str(self.home / "projects/repo.md"), shown)
        self.assertIn("Supervisor notes (not sent to workers)\n" + notes.replace(TOKEN, "[REDACTED]"), shown)
        self.assertNotIn(TOKEN, shown)

    def test_show_unlisted_project_and_defaults(self):
        self.unlisted()
        with patch("boss.service.resolve_repository", return_value="example/repo"):
            code, shown = self.cli("project", "show", "repo")
        self.assertEqual(code, 0, shown)
        for text in ("source: T3 lookup", "auto_merge: false", "no_mistakes: false", "test_host: unset",
                     "registry: absent", "notes: absent"):
            self.assertIn(text, shown)

    def test_project_add_appends_exact_prefix_and_refuses_duplicate(self):
        self.unlisted()
        config = self.home / "config.toml"
        original = config.read_bytes() + b"\r\n# Preserve this comment and CRLF.\r\n"
        config.write_bytes(original)
        with patch("boss.service.resolve_repository", return_value="example/repo") as resolve:
            code, output = self.cli("project", "add", "repo", "--delivery", "local-only", "--auto-merge",
                                    "--test-host", "wsl", "--no-mistakes")
            self.assertEqual(code, 0, output)
            appended = config.read_bytes()
            self.assertTrue(appended.startswith(original))
            entry = tomllib.loads(appended.decode())["projects"]["repo"]
            self.assertEqual(entry, {"host": "test", "project_id": "p1", "repository": "example/repo",
                                     "delivery": "local-only", "auto_merge": True, "test_host": "wsl", "no_mistakes": True})
            code, output = self.cli("project", "add", "repo")
            self.assertEqual(code, 2, output)
            self.assertIn("already has a registry entry", output)
            self.assertEqual(config.read_bytes(), appended)
            self.assertEqual(resolve.call_count, 1)

    def test_add_quotes_project_name_and_preserves_existing_nested_tables(self):
        self.unlisted()
        self.fake.projects[0]["title"] = 'Project.with "quotes"'
        config = self.home / "config.toml"
        config.write_text(config.read_text() + '[projects.existing.model_selection]\nmodel = "gpt-test"\n')
        original = config.read_bytes()
        with patch("boss.service.resolve_repository", return_value="example/repo"):
            code, output = self.cli("project", "add", 'Project.with "quotes"')
        self.assertEqual(code, 0, output)
        self.assertTrue(config.read_bytes().startswith(original))
        projects = tomllib.loads(config.read_text())["projects"]
        self.assertIn('Project.with "quotes"', projects)
        self.assertEqual(projects["existing"]["model_selection"]["model"], "gpt-test")

    def test_invalid_preferences_and_unsafe_name_fail_without_launch(self):
        config = self.home / "config.toml"
        original = config.read_text()
        for setting in ('auto_merge = "yes"', 'no_mistakes = 1', 'test_host = false'):
            config.write_text(original + setting + "\n")
            self.assertEqual(self.cli("spawn", "repo", "--title", "T")[0], 1)
        config.write_text(original)
        self.assertEqual(self.cli("project", "show", "../outside")[0], 1)
        self.assertFalse(self.fake.threads)


class RepositoryResolutionTest(HermeticTestCase):
    def test_github_remote_forms(self):
        for url in ("https://github.com/example/repo.git", "git@github.com:example/repo.git",
                    "ssh://git@github.com/example/repo.git", "https://github.com/example/repo"):
            self.assertEqual(repository_from_origin(url), "example/repo", url)
        for url in ("https://notgithub.com/example/repo.git", "https://github.com.evil/example/repo.git",
                    "https://gitlab.com/example/repo.git", "/tmp/repo", "example/repo",
                    "https://github.com/example/repo/tree/main", "https://github.com/example/../",
                    "https://github.com/example/repo?token=secret"):
            self.assertIsNone(repository_from_origin(url), url)

    def test_local_resolution_reads_real_origin(self):
        with tempfile.TemporaryDirectory(prefix="boss origin ") as directory:
            subprocess.run(["git", "init", directory], capture_output=True, check=True)
            subprocess.run(["git", "-C", directory, "remote", "add", "origin", "git@github.com:example/repo.git"], check=True)
            self.assertEqual(resolve_repository({}, directory), "example/repo")

    def test_remote_resolution_uses_selected_host_and_quotes_workspace(self):
        host = {"ssh": "oracle"}
        path = "/a path/it's $(not-a-command)"
        with patch("boss.integrations.run", return_value="git@github.com:example/repo.git\n") as run:
            self.assertEqual(resolve_repository(host, path), "example/repo")
        argv = run.call_args.args[0]
        self.assertEqual(argv[:4], ["ssh", "-oBatchMode=yes", "-oConnectTimeout=10", "oracle"])
        self.assertEqual(shlex.split(argv[4]), ["git", "-C", path, "remote", "get-url", "origin"])

    def test_non_github_remote_refuses_without_echoing_credentials(self):
        with patch("boss.integrations.run", return_value="https://secret@gitlab.com/example/repo.git"):
            with self.assertRaises(BossError) as raised:
                resolve_repository({}, "/repo")
        self.assertEqual(raised.exception.exit_code, 2)
        self.assertNotIn("secret", raised.exception.message)


if __name__ == "__main__":
    unittest.main()
