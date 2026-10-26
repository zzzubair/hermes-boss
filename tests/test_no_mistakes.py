"""delivery = "no-mistakes": the ship contract, the host check at spawn and in doctor, and the evidence check at land."""
import os
import shutil
import subprocess
import unittest
from unittest.mock import patch

from boss.config import Config
from boss.common import BossError, Redactor
from tests import test_boss as harness
from tests.support import HermeticTestCase


URL = harness.URL
# The pipeline's appendix supplies Risk Assessment and Testing, between its two markers.
EVIDENCE_BODY = """## Intent

Fixes the thing so it works.

Written by Claude Opus 5.5 in Claude Code on behalf of @owner

## What changed

- `fix.py`: does the thing

<!-- no-mistakes-pr-appendix:v1 sha256=0123abcd -->
## Risk Assessment

Low: one function.

## Testing

- `python3 -m unittest`: 1 passed

Updates from [git push no-mistakes](https://github.com/kunchenguid/no-mistakes)
<!-- /no-mistakes-pr-appendix:v1 -->
"""
SYSTEM_PATH = "/usr/bin:/bin"


class NoMistakesTest(HermeticTestCase):
    setUp = harness.CLITest.setUp
    tearDown = harness.CLITest.tearDown
    cli = harness.CLITest.cli
    spawn = harness.CLITest.spawn
    completed = harness.CLITest.completed

    def delivery(self, line='delivery = "no-mistakes"'):
        path = self.home / "config.toml"
        path.write_text(path.read_text().replace('delivery = "direct-PR"', line))

    def without_no_mistakes(self):
        """A PATH where no real no-mistakes install can answer for the host."""
        if shutil.which("no-mistakes", path=SYSTEM_PATH):
            self.skipTest("no-mistakes is installed system-wide on this machine")
        os.environ["PATH"] = SYSTEM_PATH

    def brief(self):
        code, output = self.cli("brief", "repo")
        self.assertEqual(code, 0, output)
        return output

    def launches(self):
        return [a for n, a in self.fake.calls if n == "t3_thread_launch"]

    # Config: the new value and the old flag as its alias.

    def settings(self, text):
        (self.home / "config.toml").write_text(text)
        return Config(self.home, Redactor()).effective("demo")[0]

    def test_no_mistakes_flag_is_an_alias_for_the_delivery(self):
        cases = [('delivery = "no-mistakes"\n', "no-mistakes"),
                 ("no_mistakes = true\n", "no-mistakes"),
                 ('delivery = "direct-PR"\nno_mistakes = true\n', "no-mistakes"),
                 ("no_mistakes = true\n[projects.demo]\nno_mistakes = false\n", "direct-PR"),
                 ('no_mistakes = true\n[projects.demo]\ndelivery = "local-only"\n', "local-only"),
                 ('[projects.demo]\ndelivery = "no-mistakes"\n', "no-mistakes"),
                 ("", "direct-PR")]
        for text, expected in cases:
            with self.subTest(text=text):
                settings = self.settings(text)
                self.assertEqual(settings["delivery"], expected)
                self.assertEqual(settings["no_mistakes"], expected == "no-mistakes")

    def test_a_flag_that_contradicts_the_delivery_in_one_table_is_refused(self):
        for text in ('delivery = "local-only"\nno_mistakes = true\n', '[projects.demo]\ndelivery = "no-mistakes"\nno_mistakes = false\n'):
            with self.subTest(text=text), self.assertRaises(BossError) as caught:
                self.settings(text)
            self.assertIn("contradicts", caught.exception.message)

    def test_cli_accepts_the_new_delivery(self):
        code, output = self.cli("config", "show", "--project", "repo", "--delivery", "no-mistakes")
        self.assertEqual(code, 0, output)
        self.assertIn('delivery = "no-mistakes"', output)
        self.assertIn("no_mistakes = true", output)
        self.fake_command("no-mistakes")
        self.delivery()
        code, output = self.cli("project", "show", "repo")
        self.assertIn("delivery: no-mistakes", output)
        self.assertIn("no_mistakes: true", output)

    # The ship contract.

    def test_contract_sends_the_work_through_no_mistakes_before_the_pr(self):
        direct = self.brief()
        self.delivery()
        brief = self.brief()
        for text in ("Ship, no-mistakes", "validate it through /no-mistakes", "no-mistakes axi run --intent",
                     "before any PR exists", "ask it through the question tool and end the turn",
                     "keep the pipeline's evidence appendix", "boss refuses to land a PR without it"):
            self.assertIn(text, brief)
            self.assertNotIn(text, direct)
        for text in ("link_pull_request", "end it with BOSS: done: <PR url>"):
            self.assertIn(text, brief)
        self.assertNotIn("Ship, direct-PR", brief)

    def test_no_mistakes_standing_rules_reach_only_no_mistakes_briefs(self):
        code, output = self.cli("steer", "repo", "rule no-mistakes: Skip the lint step for docs")
        self.assertEqual(code, 0, output)
        self.assertNotIn("Skip the lint step", self.brief())
        self.delivery()
        self.assertIn("- Skip the lint step for docs", self.brief())

    # boss spawn checks the host.

    def test_spawn_refuses_a_local_host_without_no_mistakes_and_names_it(self):
        self.delivery()
        self.without_no_mistakes()
        code, output = self.cli("spawn", "repo", "--title", "Fix thing")
        self.assertEqual(code, 2, output)
        self.assertIn("host test has no no-mistakes command", output)
        self.assertIn("install no-mistakes on test", output)
        self.assertEqual(self.launches(), [])
        self.assertEqual(self.store.tasks(), [])

    def test_spawn_finds_no_mistakes_on_path_or_in_a_login_shell(self):
        self.delivery()
        self.without_no_mistakes()
        # ~/.local/bin is often on PATH only in a login shell, as on a fresh Ubuntu ssh session.
        local_bin = self.user_home / ".local/bin"
        local_bin.mkdir(parents=True)
        (local_bin / "no-mistakes").write_text("#!/bin/sh\nexit 0\n")
        (local_bin / "no-mistakes").chmod(0o755)
        (self.user_home / ".profile").write_text(f'PATH="{local_bin}:$PATH"\n')
        meta = self.spawn()
        self.assertEqual(meta["delivery"], "no-mistakes")
        self.assertIn("Ship, no-mistakes", self.launches()[0]["message"])

    def test_spawn_checks_a_remote_host_over_ssh(self):
        self.delivery()
        self.without_no_mistakes()
        log = self.home / "ssh.log"
        # Fake ssh: record the target, then run the remote command here.
        self.fake_command("ssh", f'echo "$3" >> {log}\nshift 3\nexec sh -c "$1"')
        path = self.home / "config.toml"
        path.write_text(path.read_text().replace('ssh = ""', 'ssh = "devbox"'))
        code, output = self.cli("spawn", "repo", "--title", "Fix thing")
        self.assertEqual(code, 2, output)
        self.assertIn("host test has no no-mistakes command", output)
        self.assertEqual(log.read_text().split(), ["devbox"])
        self.fake_command("no-mistakes")
        self.spawn()
        self.assertEqual(len(self.launches()), 1)
        self.assertEqual(log.read_text().split(), ["devbox", "devbox"])

    def test_spawn_reports_ssh_failure_instead_of_a_missing_command(self):
        self.delivery()
        self.fake_command("ssh", "exit 255")
        path = self.home / "config.toml"
        path.write_text(path.read_text().replace('ssh = ""', 'ssh = "devbox"'))
        code, output = self.cli("spawn", "repo", "--title", "Fix thing")
        self.assertEqual(code, 3, output)
        self.assertIn("ssh devbox failed", output)
        self.assertEqual(self.launches(), [])

    def test_scouts_and_direct_pr_spawns_need_no_no_mistakes(self):
        self.without_no_mistakes()
        self.spawn()
        self.delivery()
        self.spawn("--kind", "scout")
        self.assertEqual(len(self.launches()), 2)

    # boss doctor checks every host a no-mistakes project uses.

    def doctor(self):
        paths = {key: self.home / key for key in ("plugins", "skills")}
        for key, filename in (("plugins", "plugin.yaml"), ("skills", "SKILL.md")):
            (paths[key] / "boss").mkdir(parents=True, exist_ok=True)
            (paths[key] / "boss" / filename).touch()
        real = subprocess.run

        def command(argv, **kwargs):
            if argv[0] == "sh":  # the no-mistakes host check runs for real
                return real(argv, **kwargs)
            return subprocess.CompletedProcess(argv, 0, '[{"name":"boss","status":"enabled"}]', "")
        with patch("boss.setup.install_paths", return_value=paths), patch("boss.setup.subprocess.run", side_effect=command):
            return self.cli("doctor")

    def test_doctor_checks_no_mistakes_for_each_project_that_uses_it(self):
        self.without_no_mistakes()
        code, output = self.doctor()
        self.assertNotIn("no-mistakes", output)
        self.delivery()
        code, output = self.doctor()
        self.assertEqual(code, 1, output)
        self.assertIn("fail: host test no-mistakes (for repo): no no-mistakes command; fix: install no-mistakes on test", output)
        installed = self.fake_command("no-mistakes")
        code, output = self.doctor()
        self.assertIn(f"ok: host test no-mistakes (for repo) {installed}", output)

    # boss land wants the pipeline's evidence.

    def landable(self, body):
        self.delivery()
        self.fake_command("no-mistakes")
        meta = self.spawn()
        self.completed(meta)
        self.fake.prs = [URL]
        self.gh.pr["body"] = body
        return meta

    def test_land_refuses_a_no_mistakes_pr_without_evidence(self):
        unclosed = EVIDENCE_BODY.replace("<!-- /no-mistakes-pr-appendix:v1 -->", "")
        for body in (harness.BODY, unclosed):
            with self.subTest(body=body[-60:]):
                meta = self.landable(body)
                code, output = self.cli("land", meta["id"], "--approved", "merge it")
                self.assertEqual(code, 2, output)
                self.assertIn("the PR body has no no-mistakes evidence", output)
                self.assertIn("validate through /no-mistakes", output)
        self.assertEqual(self.gh.merges, [])

    def test_land_merges_a_no_mistakes_pr_with_its_evidence(self):
        meta = self.landable(EVIDENCE_BODY)
        code, output = self.cli("land", meta["id"], "--approved", "merge it")
        self.assertEqual(code, 0, output)
        self.assertEqual(len(self.gh.merges), 1)

    def test_direct_pr_land_needs_no_evidence(self):
        meta = self.spawn()
        self.completed(meta)
        self.fake.prs = [URL]
        self.assertEqual(self.cli("land", meta["id"], "--approved", "merge it")[0], 0)

    def test_docs_only_no_mistakes_pr_lands_alone_only_with_evidence(self):
        meta = self.landable(harness.BODY)
        (self.home / "config.toml").write_text((self.home / "config.toml").read_text() + "fast_land_docs = true\n")
        self.gh.paths = ["README.md"]
        code, output = self.cli("land", meta["id"])
        self.assertEqual(code, 2, output)
        self.assertIn("landing needs approving words", output)
        self.gh.pr["body"] = EVIDENCE_BODY
        code, output = self.cli("land", meta["id"])
        self.assertEqual(code, 0, output)
        self.assertEqual(len(self.gh.merges), 1)


if __name__ == "__main__":
    unittest.main()
