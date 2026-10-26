"""Deterministic worker rules: role kinds and per-kind model allow-lists, refused at spawn."""
import contextlib
import io
import subprocess
from unittest.mock import patch

from boss.cli import main
from tests import test_boss as harness
from tests.support import HermeticTestCase


class ModelRulesTest(HermeticTestCase):
    setUp = harness.CLITest.setUp
    tearDown = harness.CLITest.tearDown
    cli = harness.CLITest.cli
    spawn = harness.CLITest.spawn

    def configure(self, text, project=""):
        path = self.home / "config.toml"
        original = path.read_text()
        # Global keys must precede the first table; the file ends inside [projects.repo].
        path.write_text(text + original + project)
        self.fake.catalog["providers"].append({"providerInstanceId": "claude", "models": [{"id": "opus-test", "options": []}]})

    def cli_err(self, *args, stdin="investigate this"):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors), patch("sys.stdin", io.StringIO(stdin)):
            code = main(list(args))
        return code, output.getvalue(), errors.getvalue()

    def launches(self):
        return [a for n, a in self.fake.calls if n == "t3_thread_launch"]

    def refused(self, *args):
        before = len(self.launches())
        code, output = self.cli("spawn", "repo", "--title", "T", *args)
        self.assertEqual(code, 2, output)
        self.assertIn("error: refused", output)
        self.assertEqual(len(self.launches()), before)
        self.assertFalse((self.home / "tasks").exists() and any((self.home / "tasks").iterdir()))
        return output

    def test_unset_rules_keep_todays_behaviour(self):
        before = {args: self.cli_err(*args) for args in (("config", "show", "--origin", "--project", "repo"),
                                                         ("brief", "repo"))}
        meta = self.spawn("--model", "codex:gpt-test")
        self.assertNotIn("override", meta)
        for args, (code, output, errors) in before.items():
            self.assertEqual(code, 0, output)
            self.assertNotIn("rules", output)
            self.assertNotIn("kinds", output)
            self.assertEqual(errors, "")
        self.assertNotIn("rules", self.cli("project", "show", "repo")[1])
        code, output = self.cli("spawn", "repo", "--title", "T", "--override", "captain said so")
        self.assertEqual(code, 1, output)
        self.assertIn("--override applies only when a worker rule refuses the spawn", output)

    def test_role_kinds_refuse_other_kinds_with_hint(self):
        self.configure('[workers.astra]\nmodel="codex:gpt-test"\nkind="scout"\nkinds=["scout"]\n')
        output = self.refused("--worker", "astra", "--kind", "ship")
        self.assertIn("role astra may run only scout tasks, not ship", output)
        self.assertIn("[workers.astra]", output)
        self.assertIn('next: retry with --kind scout or another --worker / --override "<captain\'s exact words>"', output)
        meta = self.spawn("--worker", "astra")
        self.assertEqual((meta["worker"], meta["kind"]), ("astra", "scout"))
        self.assertNotIn("override", meta)

    def test_project_role_kinds_override_global_role(self):
        self.configure('[workers.astra]\nkinds=["scout"]\n', 'worker="astra"\n')
        self.refused()
        path = self.home / "config.toml"
        path.write_text(path.read_text() + '[projects.repo.workers.astra]\nkinds=["ship", "scout"]\n')
        self.assertEqual(self.spawn()["kind"], "ship")

    def test_allow_list_refuses_explicit_configured_and_default_models(self):
        self.configure('[rules]\nship_models=["claude:opus-test"]\n[workers.sol]\nmodel="codex:gpt-test"\n')
        output = self.refused("--model", "codex:gpt-test")
        self.assertIn('model codex:gpt-test is not allowed for ship tasks; rules.ship_models = ["claude:opus-test"]', output)
        self.assertIn('next: retry with --model claude:opus-test / --override', output)
        self.assertIn("T3 default model is not allowed", self.refused())
        self.refused("--worker", "sol")
        self.refused("--model", "gpt-test")
        self.assertEqual(self.spawn("--model", "claude:opus-test")["model_selection"]["instanceId"], "claude")
        self.assertEqual(self.spawn("--model", "opus-test")["model_selection"]["instanceId"], "claude")
        # The ship list does not restrict scouts.
        self.assertEqual(self.spawn("--kind", "scout", "--model", "codex:gpt-test")["kind"], "scout")

    def test_override_records_captain_words_and_violations(self):
        self.configure('[rules]\nship_models=["claude:opus-test"]\n[workers.astra]\nkinds=["scout"]\n')
        quote = "Let Astra ship this one fix with gpt"
        meta = self.spawn("--worker", "astra", "--model", "codex:gpt-test", "--override", quote)
        self.assertEqual(meta["override"]["approved"], quote)
        self.assertEqual(len(meta["override"]["violations"]), 2)
        self.assertIn("role astra may run only scout", meta["override"]["violations"][0])
        self.assertIn("model codex:gpt-test is not allowed", meta["override"]["violations"][1])
        self.assertEqual(self.store.load(meta["id"])["override"], meta["override"])
        self.assertIn(f"override: {quote}", self.cli("status", meta["id"])[1])
        code, output = self.cli("spawn", "repo", "--title", "T", "--worker", "astra", "--override", " ")
        self.assertEqual(code, 1, output)

    def test_rules_layer_per_kind_with_provenance_everywhere(self):
        self.configure('[rules]\nship_models=["codex:gpt-test"]\nscout_models=["codex:gpt-test"]\n'
                       '[workers.astra]\nkinds=["scout"]\n',
                       '[projects.repo.rules]\nship_models=["claude:opus-test"]\n')
        code, output = self.cli("config", "show", "--origin")
        self.assertEqual(code, 0, output)
        self.assertIn(f'{self.home / "config.toml"} [rules]\trules.ship_models = ["codex:gpt-test"]', output)
        code, output = self.cli("config", "show", "--origin", "--project", "repo", "--worker", "astra")
        self.assertIn(f'{self.home / "config.toml"} [projects.repo.rules]\trules.ship_models = ["claude:opus-test"]', output)
        self.assertIn(f'{self.home / "config.toml"} [rules]\trules.scout_models = ["codex:gpt-test"]', output)
        self.assertIn(f'{self.home / "config.toml"} [workers.astra]\tkinds = ["scout"]', output)
        self.assertEqual(self.cli("config", "get", "rules.scout_models")[1].strip(), '["codex:gpt-test"]')
        shown = self.cli("project", "show", "repo")[1]
        self.assertIn('rules.ship_models: ["claude:opus-test"] origin=', shown)
        self.assertIn('rules.scout_models: ["codex:gpt-test"] origin=', shown)
        code, preview, errors = self.cli_err("brief", "repo", "--worker", "astra")
        self.assertEqual(code, 0, preview)
        self.assertNotIn("rules", preview)
        self.assertIn("supervisor rules (enforced at spawn; not sent to workers):", errors)
        self.assertIn('kinds: ["scout"] origin=', errors)
        self.assertIn('rules.ship_models: ["claude:opus-test"] origin=', errors)
        self.refused("--model", "codex:gpt-test")
        meta = self.spawn("--model", "claude:opus-test")
        sent = self.launches()[-1]["message"]
        self.assertEqual(sent, self.cli_err("brief", "repo")[1].replace("<task>", "investigate this", 1))
        for absent in ("rules", "opus-test", "kinds"):
            self.assertNotIn(absent, sent)
        self.assertNotIn("override", meta)

    def test_invalid_rules_fail_with_full_key_path(self):
        path = self.home / "config.toml"
        original = path.read_text()
        for text, key in (('[rules]\nship_models=["bare"]\n', "rules.ship_models"),
                          ('[rules]\nreview_models=[]\n', "rules.review_models"),
                          ('[rules]\nship_models="codex:gpt-test"\n', "rules.ship_models"),
                          ('[workers.astra]\nkinds=["deploy"]\n', "workers.astra.kinds"),
                          ('kinds=["scout"]\n', "kinds")):
            path.write_text(text + original)
            code, output = self.cli("config", "show")
            self.assertEqual(code, 1, output)
            self.assertIn(key, output)
        path.write_text(original + '[projects.repo.rules]\nscout_models=["x:"]\n')
        self.assertIn("projects.repo.rules.scout_models", self.cli("config", "show")[1])
        path.write_text(original)
        self.assertEqual(self.cli("config", "set", "workers.astra.kinds", '["scout"]')[0], 0)
        self.assertEqual(self.cli("config", "set", "projects.repo.rules.ship_models", '["claude:opus-test"]')[0], 0)
        self.assertEqual(self.cli("config", "get", "projects.repo.rules.ship_models")[1].strip(), '["claude:opus-test"]')

    def doctor(self):
        paths = {key: self.home / key for key in ("plugins", "skills")}
        for key, filename in (("plugins", "plugin.yaml"), ("skills", "SKILL.md")):
            (paths[key] / "boss").mkdir(parents=True, exist_ok=True)
            (paths[key] / "boss" / filename).touch()

        def command(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, '[{"name":"boss","status":"enabled"}]', "")
        with patch("boss.setup.install_paths", return_value=paths), patch("boss.setup.subprocess.run", side_effect=command), \
                patch("boss.setup.wake_cron", return_value=False):
            return self.cli("doctor")

    def test_doctor_lints_rules(self):
        self.base = (self.home / "config.toml").read_text()
        code, output = self.doctor()
        self.assertEqual(code, 0, output)
        self.assertNotIn("rules", output)
        self.configure('default_host="test"\n[rules]\nship_models=["claude:opus-test", "claude:missing"]\nscout_models=[]\n'
                       '[workers.sol]\nmodel="codex:gpt-test"\n[workers.astra]\nkind="ship"\nkinds=["scout"]\n')
        code, output = self.doctor()
        self.assertEqual(code, 1, output)
        self.assertIn("fail: rules.ship_models: claude:missing is not in host test's live T3 catalog", output)
        self.assertIn('fail: role sol: model codex:gpt-test breaks rules.ship_models ["claude:opus-test", "claude:missing"]', output)
        self.assertEqual(output.count("role sol:"), 1)
        self.assertIn('fail: role astra: default kind ship is not in kinds ["scout"]', output)
        self.assertIn("warn: rules.scout_models is empty; every scout spawn needs --override", output)
        self.assertNotIn("ok: worker rules", output)
        path = self.home / "config.toml"
        path.write_text(self.base)
        self.configure('default_host="test"\n[rules]\nship_models=["claude:opus-test"]\n'
                       '[workers.astra]\nkind="scout"\nkinds=["scout"]\nmodel="codex:gpt-test"\n')
        code, output = self.doctor()
        self.assertEqual(code, 0, output)
        self.assertIn("ok: worker rules", output)
