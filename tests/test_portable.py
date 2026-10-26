"""Portable configuration and role behavior through the public CLI."""
import contextlib
import io
import os
from pathlib import Path
import tomllib
from unittest.mock import patch

from boss.config import Config
from boss.common import Redactor
from boss.cli import main
from tests import test_boss as harness
from tests.support import HermeticTestCase


class ConfigTest(HermeticTestCase):
    def cli(self, *args):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = main(list(args))
        return code, output.getvalue(), errors.getvalue()

    def test_config_location_precedence_and_legacy_ledger(self):
        modern = self.user_home / ".config/boss/config.toml"
        legacy = self.user_home / ".boss/config.toml"
        legacy.parent.mkdir()
        legacy.write_text('delivery = "local-only"\n')
        self.assertEqual(Config(legacy.parent, Redactor()).path, legacy)
        modern.parent.mkdir(parents=True)
        modern.write_text('merge = "never"\n')
        self.assertEqual(Config(legacy.parent, Redactor()).path, modern)
        xdg = self.user_home / "xdg"
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(xdg)}):
            self.assertEqual(Config(legacy.parent, Redactor()).path, legacy)
            (xdg / "boss").mkdir(parents=True)
            (xdg / "boss/config.toml").write_text("")
            self.assertEqual(Config(legacy.parent, Redactor()).path, xdg / "boss/config.toml")
        with patch.dict(os.environ, {"BOSS_HOME": str(self.home)}):
            self.assertEqual(Config(self.home, Redactor()).path, self.home / "config.toml")
        self.assertEqual(self.cli("decide", "add", "retain ledger")[0], 0)
        self.assertEqual(len(list((legacy.parent / "decisions").glob("*.json"))), 1)

    def test_empty_hosts_point_to_init_without_inventing_a_machine(self):
        code, output, _ = self.cli("project", "list")
        self.assertEqual(code, 1, output)
        self.assertIn("no hosts configured", output)
        self.assertIn("boss init", output)
        self.assertEqual(Config(self.home, Redactor()).hosts, {})

    def test_config_commands_validate_and_report_origins(self):
        os.environ["BOSS_HOME"] = str(self.home)
        for args in (("set", "delivery", "local-only"), ("set", "projects.demo.merge", '"never"'),
                     ("set", "projects.demo.test_host", '"workstation"')):
            self.assertEqual(self.cli("config", *args)[0], 0)
        code, output, _ = self.cli("config", "show", "--origin", "--project", "demo", "--delivery", "direct-PR")
        self.assertEqual(code, 0, output)
        self.assertIn('CLI\tdelivery = "direct-PR"', output)
        self.assertIn('[projects.demo]\tmerge = "never"', output)
        self.assertIn('built-in\tkind = "ship"', output)
        self.assertEqual(self.cli("config", "get", "projects.demo.test_host")[1].strip(), '"workstation"')
        self.assertEqual(self.cli("config", "unset", "projects.demo.merge")[0], 0)
        self.assertEqual(self.cli("config", "get", "merge")[1].strip(), '"approved"')
        before = (self.home / "config.toml").read_bytes()
        for key, value in (("projects.demo.no_mistakes", "1"), ("projects.demo.no_mistaks", "true"),
                           ("workers.ui.model", '"missing-instance"')):
            code, output, _ = self.cli("config", "set", key, value)
            self.assertEqual(code, 1, output)
            self.assertIn(key, output)
            self.assertEqual((self.home / "config.toml").read_bytes(), before)

    def test_unknown_and_bad_types_fail_with_full_key_path(self):
        os.environ["BOSS_HOME"] = str(self.home)
        for text, path in (('[hosts.local]\nurl="http://127.0.0.1/mcp"\ntokn="x"', 'hosts.local.tokn'),
                           ('[projects.demo.workers.ui]\neffort=7', 'projects.demo.workers.ui.effort'),
                           ('[workers.ui]\nunknown=[]', 'workers.ui.unknown'),
                           ('[workers.model_selection]\nmodel="bare"', 'workers.model_selection.model'),
                           ('merge=true', 'merge'), ('hosts=[]', 'hosts')):
            (self.home / "config.toml").write_text(text)
            code, output, _ = self.cli("config", "show")
            self.assertEqual(code, 1, output)
            self.assertIn(path, output)

    def test_hermes_home_credentials_are_literal_and_environment_wins(self):
        os.environ.update(BOSS_HOME=str(self.home), HERMES_HOME=str(self.home / "custom hermes"))
        directory = Path(os.environ["HERMES_HOME"])
        directory.mkdir()
        (directory / ".env").write_text("CUSTOM='tiny'\nLITERAL=$(echo unsafe)\n")
        (self.home / "config.toml").write_text('[hosts.local]\nurl="http://127.0.0.1:3773/mcp"\ntoken_env="CUSTOM"\n')
        os.environ["CUSTOM"] = "env-value"
        redact = Redactor()
        config = Config(self.home, redact)
        self.assertEqual(config.host("local")[1], "env-value")
        self.assertEqual(config.credentials["LITERAL"], "$(echo unsafe)")
        self.assertEqual(redact("tiny env-value"), "[REDACTED] [REDACTED]")

class RoleTest(HermeticTestCase):
    setUp = harness.CLITest.setUp
    tearDown = harness.CLITest.tearDown
    cli = harness.CLITest.cli
    spawn = harness.CLITest.spawn
    completed = harness.CLITest.completed

    def configure(self, text):
        path = self.home / "config.toml"
        path.write_text(path.read_text() + text)

    def test_global_role_and_default_worker(self):
        path = self.home / "config.toml"
        path.write_text('default_worker="builder"\n' + path.read_text())
        self.configure('[workers.builder]\nmodel="codex:gpt-test"\neffort="high"\n')
        meta = self.spawn()
        self.assertEqual(meta["worker"], "builder")
        self.assertEqual(meta["model_selection"], {"instanceId": "codex", "model": "gpt-test", "options": {"reasoningEffort": "high"}})

    def test_project_override_merges_fields_and_shows_field_origins(self):
        self.configure('worker="ui"\n[workers.ui]\nmodel="codex:gpt-test"\neffort="low"\nhost="test"\n'
                       '[projects.repo.workers.ui]\neffort="max"\n')
        meta = self.spawn()
        self.assertEqual(meta["model_selection"]["model"], "gpt-test")
        self.assertEqual(meta["model_selection"]["options"], {"reasoningEffort": "max"})
        self.assertEqual(meta["worker"], "ui")
        code, output = self.cli("config", "show", "--origin", "--project", "repo")
        self.assertEqual(code, 0, output)
        self.assertIn('[workers.ui]\tmodel = "codex:gpt-test"', output)
        self.assertIn('[projects.repo.workers.ui]\teffort = "max"', output)
        shown = self.cli("project", "show", "repo")[1]
        self.assertIn('model: "codex:gpt-test" origin=', shown)
        self.assertIn('[projects.repo.workers.ui]', shown)

    def test_project_only_role_and_project_default_choice(self):
        path = self.home / "config.toml"
        path.write_text('default_worker="ui"\n' + path.read_text())
        self.configure('worker="builder"\n[projects.repo.workers.builder]\nmodel="codex:gpt-test"\nkind="scout"\n[workers.ui]\nkind="ship"\n')
        meta = self.spawn()
        self.assertEqual((meta["worker"], meta["kind"]), ("builder", "scout"))

    def test_flags_beat_role_layers_and_explicit_role_beats_project_default(self):
        self.fake.catalog["providers"][0]["models"].append({"id": "gpt-override", "options": [
            {"id": "reasoningEffort", "values": ["max"]}]})
        for name in ("global-host", "project-host"):
            self.assertEqual(self.cli("host", "add", name, "--url", self.fake.url, "--token-env", "T3_TEST_MCP_TOKEN")[0], 0)
        self.configure('[workers.builder]\nkind="ship"\n[workers.ui]\n'
                       'model="codex:gpt-test"\neffort="low"\nhost="global-host"\nkind="ship"\n'
                       '[projects.repo.workers.ui]\neffort="high"\nhost="project-host"\n')
        self.assertEqual(self.cli("config", "set", "projects.repo.worker", "builder")[0], 0)
        meta = self.spawn("--worker", "ui", "--model", "codex:gpt-override", "--effort", "max", "--host", "test", "--kind", "scout")
        self.assertEqual(meta["worker"], "ui")
        self.assertEqual(meta["kind"], "scout")
        self.assertEqual(meta["host"], "test")
        self.assertEqual(meta["model_selection"]["model"], "gpt-override")
        self.assertEqual(meta["model_selection"]["options"], {"reasoningEffort": "max"})

    def test_kind_scout_auto_selects_role(self):
        self.configure('worker="builder"\n[workers.builder]\nkind="ship"\n[workers.scout]\nmodel="codex:gpt-test"\neffort="low"\n')
        meta = self.spawn("--kind", "scout")
        self.assertEqual((meta["worker"], meta["kind"]), ("scout", "scout"))

    def test_unknown_role_lists_global_and_project_roles(self):
        self.configure('[workers.ui]\neffort="low"\n[projects.repo.workers.builder]\nkind="ship"\n')
        code, output = self.cli("spawn", "repo", "--title", "T", "--worker", "missing")
        self.assertEqual(code, 1, output)
        self.assertIn("available for repo: builder, ui", output)
        self.assertFalse(self.fake.threads)

    def test_merged_effort_is_checked_against_live_catalog_before_launch(self):
        self.configure('[workers.ui]\nmodel="codex:gpt-test"\neffort="low"\n[projects.repo.workers.ui]\neffort="xhigh"\n')
        code, output = self.cli("spawn", "repo", "--title", "T", "--worker", "ui")
        self.assertEqual(code, 1, output)
        self.assertIn("unsupported reasoningEffort=xhigh", output)
        self.assertFalse(self.fake.threads)

    def test_merge_policy_preserves_guards_and_notes_stay_with_supervisor(self):
        self.configure('merge="never"\n')
        (self.home / "projects").mkdir()
        (self.home / "projects/repo.md").write_text("Run the formatter")
        meta = self.spawn()
        self.assertEqual(meta["merge"], "never")
        message = self.store.directory(meta["id"]).joinpath("brief.md").read_text()
        self.assertIn("Never merge. Never deploy.", message)
        self.assertNotIn("Run the formatter", message)
        self.assertIn("Run the formatter", self.cli("project", "show", "repo")[1])
        code, output = self.cli("land", meta["id"], "--approved", "merge it")
        self.assertEqual(code, 2, output)
        self.assertIn("merge policy is never", output)
        self.assertFalse(self.gh.merges)

    def test_auto_merge_alias_warns_and_cannot_bypass_approval(self):
        self.configure('auto_merge=true\n')
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            meta = self.spawn()
        self.assertEqual(meta["merge"], "auto")
        self.assertIn("projects.repo.auto_merge is deprecated", errors.getvalue())
        self.assertEqual(self.cli("land", meta["id"])[0], 2)

    def test_answer_accepts_exact_id_printed_by_status(self):
        meta = self.spawn()
        self.fake.pending["request-123"] = [{"id": "0", "question": "Which route?", "options": []}]
        shown = self.cli("status", meta["id"])[1]
        printed_id = next(line.split(":", 1)[0] for line in shown.splitlines() if "Which route?" in line)
        self.assertEqual(printed_id, "request-123/0")
        code, output = self.cli("answer", meta["id"], printed_id, "Use route A")
        self.assertEqual(code, 0, output)
        sent = [args for name, args in self.fake.calls if name == "t3_pending_request_respond"][-1]
        self.assertEqual(sent["requestId"], "request-123")
        self.assertEqual(sent["answers"], {"0": "Use route A"})
        self.fake.pending["request-123"] = [{"id": "0", "question": "Again?", "options": []}]
        self.assertEqual(self.cli("answer", meta["id"], "request-123/99", "A")[0], 2)

    def test_answer_accepts_question_id_with_slashes_and_quotes(self):
        meta = self.spawn()
        question_id = '"/" was removed on purpose, keep it out? a/b'
        self.fake.pending["request-9"] = [{"id": question_id, "question": "Keep it out?", "options": []}]
        shown = self.cli("status", meta["id"], "--full")[1]
        self.assertIn(f"request-9/{question_id}: Keep it out?", shown)
        code, output = self.cli("answer", meta["id"], f"request-9/{question_id}", "yes")
        self.assertEqual(code, 0, output)
        sent = [args for name, args in self.fake.calls if name == "t3_pending_request_respond"][-1]
        self.assertEqual(sent["requestId"], "request-9")
        self.assertEqual(sent["answers"], {question_id: "yes"})
        # A unique prefix of a pending id is enough; an ambiguous one is refused.
        self.fake.pending["request-9"] = [{"id": "0", "question": "Again?", "options": []}]
        self.assertEqual(self.cli("answer", meta["id"], "request-", "A")[0], 0)
        self.fake.pending["request-1"] = [{"id": "0", "question": "One?", "options": []}]
        self.fake.pending["request-2"] = [{"id": "0", "question": "Two?", "options": []}]
        self.assertEqual(self.cli("answer", meta["id"], "request-", "A")[0], 2)
        self.assertEqual(self.cli("answer", meta["id"], "request-2", "A")[0], 0)

    def test_host_commands_and_init_are_idempotent(self):
        self.assertIn("MCP handshake ok", self.cli("host", "test", "test")[1])
        self.assertEqual(self.cli("host", "add", "other", "--url", self.fake.url, "--token-env", "T3_TEST_MCP_TOKEN")[0], 0)
        self.assertIn("other:", self.cli("host", "list")[1])
        self.assertEqual(self.cli("host", "remove", "other")[0], 0)
        path = self.home / "config.toml"
        original = path.read_bytes()
        self.assertEqual(self.cli("init", "--url", self.fake.url, "--token-env", "T3_TEST_MCP_TOKEN")[0], 0)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(self.cli("init", "--force", "--url", self.fake.url, "--token-env", "T3_TEST_MCP_TOKEN")[0], 0)
        self.assertEqual(tomllib.loads(path.read_text())["default_host"], "local")
        self.assertIn("repo", tomllib.loads(path.read_text())["projects"])

    def test_init_probes_default_and_fails_without_writing_on_bad_token(self):
        path = self.home / "config.toml"
        path.unlink()
        with patch("boss.setup.DEFAULT_URL", self.fake.url):
            code, output = self.cli("init", "--token-env", "T3_TEST_MCP_TOKEN")
        self.assertEqual(code, 0, output)
        self.assertEqual(tomllib.loads(path.read_text())["hosts"]["local"]["url"], self.fake.url)
        original = path.read_bytes()
        code, output = self.cli("init", "--force", "--url", self.fake.url, "--token-env", "MISSING")
        self.assertEqual(code, 3, output)
        self.assertEqual(path.read_bytes(), original)

    def test_doctor_checks_ssh_resolution_and_required_auth(self):
        import socket
        import subprocess
        self.assertEqual(self.cli("config", "set", "hosts.test.ssh", "build-alias")[0], 0)

        def command(argv, **kwargs):
            if argv[0] == "ssh":
                return subprocess.CompletedProcess(argv, 0, "hostname build.example\n", "")
            if argv[0] == "gh":
                return subprocess.CompletedProcess(argv, 1, "", "not logged in")
            return subprocess.CompletedProcess(argv, 0, '[{"name":"boss","status":"disabled"}]', "")
        with patch("boss.setup.subprocess.run", side_effect=command), patch("boss.setup.socket.getaddrinfo", side_effect=socket.gaierror("unresolved")):
            code, output = self.cli("doctor")
        self.assertEqual(code, 1, output)
        self.assertIn("fail: gh auth:", output)
        self.assertIn("fail: host test SSH:", output)
        self.assertIn("fix: configure build-alias", output)
        self.assertIn("fail: plugin enabled:", output)
