"""Deterministic worker rules: role kinds and per-role/kind model+effort allow-lists, refused at spawn."""
import contextlib
import io
import json
import shlex
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
        before, tasks = len(self.launches()), len(self.store.tasks())
        code, output, _ = self.cli_err("spawn", "repo", "--title", "T", *args)
        self.assertEqual(code, 2, output)
        self.assertIn("error: refused", output)
        self.assertEqual(len(self.launches()), before)
        self.assertEqual(len(self.store.tasks()), tasks)
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
        code, output = self.cli("spawn", "repo", "--title", "T", "--allow-model", "captain said so")
        self.assertEqual(code, 1, output)
        self.assertIn("--allow-model applies only when a worker rule refuses the spawn", output)

    def test_role_kinds_refuse_other_kinds_with_hint(self):
        self.configure('[workers.astra]\nmodel="codex:gpt-test"\nkind="scout"\nkinds=["scout"]\n')
        output = self.refused("--worker", "astra", "--kind", "ship")
        self.assertIn("role astra may run only scout tasks, not ship", output)
        self.assertIn("[workers.astra]", output)
        self.assertIn('next: retry with --kind scout or another --worker / --allow-model "<captain\'s exact words>"', output)
        self.assertIn("to allow it from now on: boss config set workers.astra.kinds '[\"scout\", \"ship\"]'", output)
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
        self.configure('[rules.models]\nship=["claude:opus-test"]\n[workers.sol]\nmodel="codex:gpt-test"\n')
        output = self.refused("--model", "codex:gpt-test")
        self.assertIn('model codex:gpt-test is not allowed for ship tasks; rules.models.ship = ["claude:opus-test"]', output)
        self.assertIn('next: retry with --model claude:opus-test / --allow-model', output)
        self.assertIn("T3 default model is not allowed", self.refused())
        self.refused("--worker", "sol")
        self.refused("--model", "gpt-test")
        self.assertEqual(self.spawn("--model", "claude:opus-test")["model_selection"]["instanceId"], "claude")
        self.assertEqual(self.spawn("--model", "opus-test")["model_selection"]["instanceId"], "claude")
        # The ship list does not restrict scouts.
        self.assertEqual(self.spawn("--kind", "scout", "--model", "codex:gpt-test")["kind"], "scout")

    def test_override_records_captain_words_and_violations(self):
        self.configure('[rules.models]\nship=["claude:opus-test"]\n[workers.astra]\nkinds=["scout"]\n')
        quote = "Let Astra ship this one fix with gpt"
        meta = self.spawn("--worker", "astra", "--model", "codex:gpt-test", "--allow-model", quote)
        self.assertEqual(meta["override"]["approved"], quote)
        self.assertEqual(len(meta["override"]["violations"]), 2)
        self.assertIn("role astra may run only scout", meta["override"]["violations"][0])
        self.assertIn("model codex:gpt-test is not allowed", meta["override"]["violations"][1])
        self.assertEqual(self.store.load(meta["id"])["override"], meta["override"])
        journal = [json.loads(line) for line in (self.home / "tasks" / meta["id"] / "events.log").read_text().splitlines()]
        self.assertEqual(journal[0]["event"], "launch_intent")
        self.assertEqual(journal[0]["meta"]["override"]["approved"], quote)
        status = self.cli("status", meta["id"])[1]
        self.assertIn(f"allowed by captain: {quote}", status)
        self.assertIn("  despite: model codex:gpt-test is not allowed for ship tasks", status)
        code, output = self.cli("spawn", "repo", "--title", "T", "--worker", "astra", "--allow-model", " ")
        self.assertEqual(code, 1, output)
        # The older flag name still works.
        self.assertIn("override", self.spawn("--worker", "astra", "--model", "claude:opus-test", "--override", quote))

    def test_rules_layer_per_kind_with_provenance_everywhere(self):
        self.configure('[rules.models]\nship=["codex:gpt-test"]\nscout=["codex:gpt-test"]\n'
                       '[workers.astra]\nkinds=["scout"]\n',
                       '[projects.repo.rules.models]\nship=["claude:opus-test"]\n')
        code, output = self.cli("config", "show", "--origin")
        self.assertEqual(code, 0, output)
        self.assertIn(f'{self.home / "config.toml"} [rules]\trules.models.ship = ["codex:gpt-test"]', output)
        code, output = self.cli("config", "show", "--origin", "--project", "repo", "--worker", "astra")
        self.assertIn(f'{self.home / "config.toml"} [projects.repo.rules]\trules.models.ship = ["claude:opus-test"]', output)
        self.assertIn(f'{self.home / "config.toml"} [rules]\trules.models.scout = ["codex:gpt-test"]', output)
        self.assertIn(f'{self.home / "config.toml"} [workers.astra]\tkinds = ["scout"]', output)
        self.assertEqual(self.cli("config", "get", "rules.models.scout")[1].strip(), '["codex:gpt-test"]')
        shown = self.cli("project", "show", "repo")[1]
        self.assertIn('rules.models.ship: ["claude:opus-test"] origin=', shown)
        self.assertIn('rules.models.scout: ["codex:gpt-test"] origin=', shown)
        code, preview, errors = self.cli_err("brief", "repo", "--worker", "astra")
        self.assertEqual(code, 0, preview)
        self.assertNotIn("rules", preview)
        self.assertIn("supervisor rules (enforced at spawn; not sent to workers):", errors)
        self.assertIn('kinds: ["scout"] origin=', errors)
        self.assertIn('rules.models.ship: ["claude:opus-test"] origin=', errors)
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
                          ('[rules.models]\nship=["bare"]\n', "rules.models.ship"),
                          ('[rules.models]\nship=["codex:gpt-test high extra"]\n', "rules.models.ship"),
                          ('[rules.models]\nship=["codex:gpt-test  high"]\n', "rules.models.ship"),
                          ('[rules.models]\nship="codex:gpt-test"\n', "rules.models.ship"),
                          ('[rules.models]\nreviewer=["codex:gpt-test"]\n', "rules.models.reviewer"),
                          ('[workers]\nmax_active=3\n[rules.models]\nmax_active=["codex:gpt-test"]\n', "rules.models.max_active"),
                          ('[rules]\nmodels=["codex:gpt-test"]\n', "rules.models"),
                          ('[workers.astra]\nkinds=["deploy"]\n', "workers.astra.kinds"),
                          ('kinds=["scout"]\n', "kinds")):
            path.write_text(text + original)
            code, output = self.cli("config", "show")
            self.assertEqual(code, 1, output)
            self.assertIn(key, output)
        path.write_text(original + '[projects.repo.rules]\nscout_models=["x:"]\n')
        self.assertIn("projects.repo.rules.scout_models", self.cli("config", "show")[1])
        path.write_text(original + '[projects.repo.rules.models]\nscout=["x:"]\n')
        self.assertIn("projects.repo.rules.models.scout", self.cli("config", "show")[1])
        path.write_text(original)
        self.assertEqual(self.cli("config", "set", "workers.astra.kinds", '["scout"]')[0], 0)
        self.assertEqual(self.cli("config", "set", "projects.repo.rules.models.astra", '["claude:opus-test high"]')[0], 0)
        self.assertIn('rules.models.astra = ["claude:opus-test high"]', self.cli("config", "show", "--project", "repo")[1])

    def doctor(self):
        paths = {key: self.home / key for key in ("plugins", "skills")}
        for key, filename in (("plugins", "plugin.yaml"), ("skills", "SKILL.md")):
            (paths[key] / "boss").mkdir(parents=True, exist_ok=True)
            (paths[key] / "boss" / filename).touch()

        def command(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, '[{"name":"boss","status":"enabled"}]', "")
        with patch("boss.setup.install_paths", return_value=paths), patch("boss.setup.subprocess.run", side_effect=command):
            return self.cli("doctor")

    def test_doctor_lints_rules(self):
        self.base = (self.home / "config.toml").read_text()
        code, output = self.doctor()
        self.assertEqual(code, 0, output)
        self.assertNotIn("rules", output)
        self.configure('default_host="test"\n[rules.models]\nship=["claude:opus-test", "claude:missing"]\nscout=[]\n'
                       'reviewer=["codex:gpt-test xhigh", "codex:gpt-test high"]\n'
                       '[workers.sol]\nmodel="codex:gpt-test"\n[workers.astra]\nkind="ship"\nkinds=["scout"]\n'
                       '[workers.reviewer]\nmodel="codex:gpt-test"\neffort="low"\n')
        code, output = self.doctor()
        self.assertEqual(code, 1, output)
        self.assertIn("fail: rules.models.ship: claude:missing is not in host test's live T3 catalog", output)
        self.assertIn("fail: rules.models.reviewer: codex:gpt-test has no effort xhigh on host test", output)
        self.assertNotIn("no effort high", output)
        self.assertIn('fail: role sol: model codex:gpt-test breaks rules.models.ship ["claude:opus-test", "claude:missing"]', output)
        self.assertIn('fail: role reviewer: model codex:gpt-test low breaks rules.models.reviewer', output)
        self.assertEqual(output.count("role sol:"), 1)
        self.assertIn('fail: role astra: default kind ship is not in kinds ["scout"]', output)
        self.assertIn("warn: rules.models.scout is empty; every scout spawn needs --allow-model", output)
        self.assertNotIn("ok: worker rules", output)
        path = self.home / "config.toml"
        path.write_text(self.base)
        self.configure('default_host="test"\n[rules.models]\nship=["claude:opus-test"]\nastra=["codex:gpt-test high"]\n'
                       '[workers.astra]\nkind="scout"\nkinds=["scout"]\nmodel="codex:gpt-test"\neffort="high"\n')
        code, output = self.doctor()
        self.assertEqual(code, 0, output)
        self.assertIn("ok: worker rules", output)

    def test_effort_rules_refuse_wrong_or_missing_effort(self):
        self.configure('[rules.models]\nship=["claude:opus-test", "codex:gpt-test max"]\n')
        output = self.refused("--model", "codex:gpt-test", "--effort", "high")
        self.assertIn('model codex:gpt-test high is not allowed for ship tasks; '
                      'rules.models.ship = ["claude:opus-test", "codex:gpt-test max"]', output)
        self.assertIn("next: retry with --model claude:opus-test / --allow-model", output)
        self.assertIn("model codex:gpt-test is not allowed", self.refused("--model", "codex:gpt-test"))
        meta = self.spawn("--model", "codex:gpt-test", "--effort", "max")
        self.assertEqual(meta["model_selection"]["options"], {"reasoningEffort": "max"})
        # An entry without an effort allows every effort of that model.
        self.assertEqual(self.spawn("--model", "claude:opus-test")["model_selection"]["model"], "opus-test")
        # boss status names model and effort, in the table and for one task.
        self.assertIn("| gpt-test max |", self.cli("status")[1])
        self.assertIn("model: gpt-test max", self.cli("status", meta["id"])[1])

    def test_role_rules_replace_kind_rules_and_effort_hint(self):
        self.configure('[rules.models]\nship=["claude:opus-test"]\nreviewer=["codex:gpt-test max"]\n'
                       '[workers.reviewer]\nmodel="codex:gpt-test"\n[workers.builder]\nmodel="claude:opus-test"\n')
        output = self.refused("--worker", "reviewer")
        self.assertIn('model codex:gpt-test is not allowed for role reviewer; rules.models.reviewer = ["codex:gpt-test max"]', output)
        self.assertIn("next: retry with --model codex:gpt-test --effort max / --allow-model", output)
        self.refused("--worker", "reviewer", "--model", "claude:opus-test")
        meta = self.spawn("--worker", "reviewer", "--effort", "max")
        self.assertEqual((meta["worker"], meta["model_selection"]["options"]), ("reviewer", {"reasoningEffort": "max"}))
        # Roles without their own list fall back to the kind's list: Codex only when asked.
        self.assertEqual(self.spawn("--worker", "builder")["worker"], "builder")
        self.assertIn("is not allowed for ship tasks", self.refused("--worker", "builder", "--model", "codex:gpt-test"))

    def test_refusal_names_the_one_config_change_that_allows_it(self):
        self.configure('[rules.models]\nship=["claude:opus-test"]\n',
                       '[projects.repo.rules.models]\nscout=["claude:opus-test"]\n')
        output = self.refused("--model", "codex:gpt-test", "--effort", "high")
        prefix = "to allow it from now on: "
        line = next(l for l in output.splitlines() if l.startswith(prefix))
        self.assertEqual(line, prefix + "boss config set rules.models.ship '[\"claude:opus-test\", \"codex:gpt-test high\"]'")
        self.assertEqual(self.cli(*shlex.split(line[len(prefix):])[1:])[0], 0)
        self.assertEqual(self.spawn("--model", "codex:gpt-test", "--effort", "high")["kind"], "ship")
        # A project-level rule names the project key; T3's default model has no config fix.
        output = self.refused("--kind", "scout", "--model", "codex:gpt-test")
        self.assertIn(prefix + "boss config set projects.repo.rules.models.scout '[\"claude:opus-test\", \"codex:gpt-test\"]'", output)
        output = self.refused("--kind", "scout")
        self.assertIn("T3 default model is not allowed for scout tasks", output)
        self.assertNotIn(prefix, output)

    def test_deprecated_kind_lists_still_enforce_with_a_warning(self):
        self.configure('[rules]\nship_models=["claude:opus-test"]\n')
        code, output, errors = self.cli_err("config", "show")
        self.assertEqual(code, 0, output)
        self.assertIn("warning: rules.ship_models is deprecated; use rules.models.ship", errors)
        self.assertIn('rules.models.ship = ["claude:opus-test"]', output)
        self.assertIn("rules.models.ship = [", self.refused("--model", "codex:gpt-test"))
        path = self.home / "config.toml"
        path.write_text(path.read_text().replace('ship_models=["claude:opus-test"]\n',
                                                 'ship_models=["claude:opus-test"]\nmodels={ship=["codex:gpt-test"]}\n'))
        # The new spelling wins over the old one in the same table.
        self.refused("--model", "claude:opus-test")
        self.assertEqual(self.spawn("--model", "codex:gpt-test")["kind"], "ship")
