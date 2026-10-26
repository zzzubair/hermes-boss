"""boss deploy: guards, the blocking check, rollback and scrubbed remote output, with a fake ssh."""
import contextlib
from datetime import datetime, timedelta, timezone
import io
import json
import os
from unittest.mock import patch

from boss.deploy import inside, scrub
from boss.common import Redactor
from tests import test_boss as harness
from tests.support import HermeticTestCase


OWNER = "agent:discord"
SECRET = "hermes-env-secret-value-1234"
# Runs the "remote" command locally in the test home and logs every argv line.
FAKE_SSH = """#!/bin/sh
for arg in "$@"; do printf '%s\\n' "$arg"; done >> "$BOSS_HOME/ssh.log"
printf -- '--\\n' >> "$BOSS_HOME/ssh.log"
for last in "$@"; do :; done
cd "$BOSS_HOME" && exec sh -c "$last"
"""


class DeployTest(HermeticTestCase):
    setUp_cli = harness.CLITest.setUp
    tearDown = harness.CLITest.tearDown
    spawn = harness.CLITest.spawn

    def setUp(self):
        self.setUp_cli()
        bin_dir = self.home / "bin"
        bin_dir.mkdir()
        (bin_dir / "ssh").write_text(FAKE_SSH)
        (bin_dir / "ssh").chmod(0o755)
        os.environ["PATH"] = f"{bin_dir}:{os.environ['PATH']}"
        os.environ["DEPLOY_API_TOKEN"] = SECRET
        self.sleeps = []
        sleeper = patch("boss.deploy.sleep", side_effect=self.slept)
        sleeper.start()
        self.addCleanup(sleeper.stop)
        self.on_sleep = None
        self.base = (self.home / "config.toml").read_text()
        self.assertEqual(self.cli("own", "--label", "Discord DM", key=OWNER)[0], 0)

    def slept(self, seconds):
        self.sleeps.append(seconds)
        if self.on_sleep:
            self.on_sleep()

    def configure(self, **deploy):
        """Replace the project's deploy table."""
        deploy = {"host": "prod", "command": "echo deployed > deployed; echo deploy done", **deploy}
        with (self.home / "config.toml").open("w") as stream:
            stream.write(self.base + "[projects.repo.deploy]\n" + "".join(f"{k} = {json.dumps(v)}\n" for k, v in deploy.items()))

    def cli(self, *args, key=OWNER):
        output = io.StringIO()
        with patch.dict(os.environ, {"HERMES_SESSION_KEY": key} if key else {}), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()), \
                patch("sys.stdin", io.StringIO("brief")):
            if not key:
                os.environ.pop("HERMES_SESSION_KEY", None)
            from boss.cli import main
            code = main(list(args))
        return code, output.getvalue()

    def ssh_runs(self):
        path = self.home / "ssh.log"
        return [run.strip("\n").split("\n") for run in path.read_text().split("--\n") if run] if path.exists() else []

    def deploys(self):
        path = self.home / "captain.jsonl"
        return [r for r in map(json.loads, path.read_text().splitlines()) if r["event"] == "deploy"] if path.exists() else []

    def now_window(self, hours=0):
        at = datetime.now(timezone.utc) + timedelta(hours=hours)
        return f"{(at - timedelta(minutes=30)):%H:%M}-{(at + timedelta(minutes=30)):%H:%M}"

    def test_happy_path_checks_now_and_after_the_wait_and_journals_his_words(self):
        self.configure(check="test -f deployed && echo healthy", check_minutes=2, rollback="echo undo",
                       no_touch=[self.now_window(hours=12)])
        self.assertEqual(self.cli("away", "going to bed")[0], 0)
        code, output = self.cli("deploy", "repo", "--approved", "yes deploy it")
        self.assertEqual(code, 0, output)
        lines = output.splitlines()
        self.assertEqual(lines[0], "deploying repo on prod...")
        for text in ("  deploy done", "checking repo right away...", "  healthy", "check passed right away.",
                     "waiting 2 more minutes before the second check...", "waiting 1 more minute before the second check...",
                     "check passed after 2 minutes.", "repo: deployed; check passed right away and after 2 minutes.",
                     "next: tell the captain it is live"):
            self.assertIn(text, lines)
        self.assertNotIn("rollback", output)
        self.assertEqual(self.sleeps, [60, 60])
        runs = self.ssh_runs()
        self.assertEqual(len(runs), 3)
        self.assertEqual(runs[0][:2], ["-oBatchMode=yes", "-oConnectTimeout=10"])
        self.assertEqual(runs[0][-2:], ["prod", "echo deployed > deployed; echo deploy done"])
        start, done = self.deploys()
        self.assertEqual((start["phase"], start["approved"], start["host"]), ("start", "yes deploy it", "prod"))
        self.assertEqual((done["phase"], done["ok"]), ("done", True))
        # /back shows both, in plain words.
        self.assertEqual(self.cli("back")[0], 0)
        timeline = self.cli("back", "--full")[1]
        self.assertIn("deploy of repo started on your words: yes deploy it", timeline)
        self.assertIn("repo deploy: deployed; check passed right away and after 2 minutes", timeline)

    def test_remote_output_never_shows_env_or_secrets(self):
        self.configure(command=f"echo starting; env; echo HOME=/x; echo 'api_key: abc123'; "
                               f"echo 'Authorization: Basic dXNlcjpwYXNz'; echo ghp_{'a' * 36}; echo $DEPLOY_API_TOKEN; echo done")
        code, output = self.cli("deploy", "repo", "--approved", "ship it")
        self.assertEqual(code, 0, output)
        for leaked in (SECRET, "abc123", "dXNlcjpwYXNz", "ghp_", "T3_TEST_MCP_TOKEN", harness.TOKEN, "PATH="):
            self.assertNotIn(leaked, output)
        self.assertIn("  [environment line hidden]", output)
        self.assertIn("  api_key: [REDACTED]", output)
        self.assertIn("  starting", output)
        self.assertIn("  done", output)
        self.assertNotIn(SECRET, (self.home / "captain.jsonl").read_text())

    def test_scrub_keeps_only_the_tail(self):
        lines = scrub("\n".join(f"line {n}" for n in range(30)), Redactor())
        self.assertEqual(lines[0], "...")
        self.assertEqual(lines[1:], [f"line {n}" for n in range(10, 30)])

    def test_failed_check_prints_rollback_and_does_not_run_it(self):
        self.configure(check="echo unhealthy; exit 4", rollback="echo rolling back > rolled", check_minutes=10)
        code, output = self.cli("deploy", "repo", "--approved", "go ahead")
        self.assertEqual(code, 3, output)
        self.assertIn("check failed right away (exit 4).", output)
        self.assertIn("rollback (not run): ssh prod 'echo rolling back > rolled'", output)
        self.assertIn("repo: deployed, but the check failed right away.", output)
        self.assertIn("ask whether to run the rollback above", output)
        self.assertEqual(self.sleeps, [])  # a failed first check never waits
        self.assertEqual(len(self.ssh_runs()), 2)
        self.assertFalse((self.home / "rolled").exists())
        self.assertFalse(self.deploys()[-1]["ok"])

    def test_late_failure_auto_rolls_back_when_configured(self):
        self.configure(check="test -f deployed", check_minutes=1, rollback="echo undone",
                       auto_rollback=True)
        self.on_sleep = lambda: (self.home / "deployed").unlink()
        code, output = self.cli("deploy", "repo", "--approved", "yes")
        self.assertEqual(code, 3, output)
        self.assertIn("check passed right away.", output)
        self.assertIn("check failed after 1 minute (exit 1).", output)
        self.assertIn("rolling back repo...", output)
        self.assertIn("  undone", output)
        self.assertIn("repo: deployed, but the check failed after 1 minute; rolled back.", output)
        self.assertEqual(self.deploys()[-1]["rolled_back"], True)

    def test_failed_deploy_skips_the_check(self):
        self.configure(command="echo boom; exit 7", check="echo should-not-run", rollback="echo undo")
        code, output = self.cli("deploy", "repo", "--approved", "yes")
        self.assertEqual(code, 3, output)
        self.assertIn("repo: the deploy command failed (exit 7).", output)
        self.assertNotIn("should-not-run", output.replace("ssh prod 'echo undo'", ""))
        self.assertEqual(len(self.ssh_runs()), 1)

    def test_no_check_and_zero_minutes_never_wait(self):
        self.configure()
        self.assertIn("repo: deployed; no check is configured.", self.cli("deploy", "repo", "--approved", "yes")[1])
        self.configure(check="true", check_minutes=0)
        self.assertIn("repo: deployed; check passed.", self.cli("deploy", "repo", "--approved", "yes")[1])
        self.assertEqual(self.sleeps, [])

    def test_dry_run_shows_the_plan_and_runs_nothing(self):
        self.configure(check="curl -fsS https://example.test/health", rollback="./rollback.sh", auto_rollback=True)
        code, output = self.cli("deploy", "repo", "--dry-run", key=None)  # any shell, no quote needed
        self.assertEqual(code, 0, output)
        self.assertIn("dry run: nothing ran on prod", output)
        self.assertIn("check: curl -fsS https://example.test/health now and after 10 minutes", output)
        self.assertIn("rollback: ./rollback.sh (runs by itself on a failed check)", output)
        self.assertIn('next: boss deploy repo --approved "<captain words>"', output)
        self.assertEqual(self.ssh_runs(), [])
        self.assertEqual(self.deploys(), [])

    def test_refusals_run_nothing(self):
        self.configure()
        cases = [(("--approved", " "), OWNER, "deploying needs the captain's exact words"),
                 ((), OWNER, "deploying needs the captain's exact words"),
                 (("--approved", "no, not now"), OWNER, "his words say no"),
                 (("--approved", "yes"), None, "runs only in the Hermes session that owns boss"),
                 (("--approved", "yes"), "agent:other", "boss is owned by Discord DM")]
        for extra, key, message in cases:
            code, output = self.cli("deploy", "repo", *extra, key=key)
            self.assertEqual(code, 2, output)
            self.assertIn(message, output)
        self.assertEqual(self.ssh_runs(), [])
        self.assertEqual(self.deploys(), [])

    def test_no_touch_window_refuses(self):
        self.configure(no_touch=[self.now_window()])
        code, output = self.cli("deploy", "repo", "--approved", "yes")
        self.assertEqual(code, 2, output)
        self.assertIn(f"reason: {self.now_window()} UTC is a no-touch window for repo", output)
        code, output = self.cli("deploy", "repo", "--dry-run")
        self.assertEqual(code, 2, output)
        self.assertIn("would refuse: ", output)
        self.assertEqual(self.ssh_runs(), [])

    def test_windows_wrap_past_midnight(self):
        at = lambda h, m: datetime(2026, 1, 1, h, m, tzinfo=timezone.utc)
        self.assertTrue(inside("04:00-07:00", at(4, 0)))
        self.assertTrue(inside("04:00-07:00", at(6, 59)))
        self.assertFalse(inside("04:00-07:00", at(7, 0)))
        self.assertFalse(inside("04:00-07:00", at(3, 59)))
        self.assertTrue(inside("22:00-02:00", at(23, 30)))
        self.assertTrue(inside("22:00-02:00", at(1, 0)))
        self.assertFalse(inside("22:00-02:00", at(12, 0)))

    def test_project_fence_refuses_until_dropped_and_task_fence_does_not(self):
        self.configure()
        meta = self.spawn()
        self.assertEqual(self.cli("steer", "repo", "only this one waits", "--task", meta["id"], "--fence")[0], 0)
        code, output = self.cli("steer", "repo", "no deploys this week", "--fence")
        fence = output.split(":", 1)[0]
        code, output = self.cli("deploy", "repo", "--approved", "yes")
        self.assertEqual(code, 2, output)
        self.assertIn("reason: the captain fenced repo: no deploys this week", output)
        self.assertNotIn("only this one waits", output)
        self.assertEqual(self.cli("steer", "drop", "repo", fence)[0], 0)
        self.assertEqual(self.cli("deploy", "repo", "--approved", "yes")[0], 0)

    def test_open_or_refused_deploy_decision_refuses(self):
        self.configure()
        meta = self.spawn()
        self.assertEqual(self.cli("decide", "add", "Deploy the fix?", "--task", meta["id"], "--action", "deploy")[0], 0)
        code, output = self.cli("deploy", "repo", "--approved", "yes")
        self.assertEqual(code, 2, output)
        self.assertIn("reason: the deploy question about Fix thing is still open", output)
        self.assertEqual(self.cli("decide", "close", self.decision_id(), "no, wait for Monday")[0], 0)
        code, output = self.cli("deploy", "repo", "--approved", "yes")
        self.assertEqual(code, 2, output)
        self.assertIn("reason: the captain said no to deploying Fix thing: no, wait for Monday", output)
        self.assertEqual(self.cli("log", "ok deploy it now", "--task", meta["id"], "--approves", "deploy")[0], 0)
        self.assertEqual(self.cli("deploy", "repo", "--approved", "ok deploy it now")[0], 0)
        self.assertEqual(len(self.ssh_runs()), 1)

    def decision_id(self):
        return json.loads(next((self.home / "decisions").glob("*.json")).read_text())["id"]

    def test_missing_deploy_config(self):
        code, output = self.cli("deploy", "repo", "--approved", "yes")
        self.assertEqual(code, 1, output)
        self.assertIn("no deploy configured for repo", output)

    def test_config_validation(self):
        bad = [({"host": "prod"}, "projects.repo.deploy.command: required"),
               ({"command": "x"}, "projects.repo.deploy.host: required"),
               ({"host": "-oProxyCommand=x", "command": "x"}, "invalid ssh destination"),
               ({"host": "prod", "command": "x", "check_minutes": -1}, "check_minutes: must be 0"),
               ({"host": "prod", "command": "x", "no_touch": ["4-7"]}, 'like \\"04:00-07:00\\"'),
               ({"host": "prod", "command": "x", "no_touch": ["05:00-05:00"]}, "no_touch"),
               ({"host": "prod", "command": "x", "auto_rollback": True}, "auto_rollback: needs deploy.rollback"),
               ({"host": "prod", "command": "x", "timeout": 3}, "deploy.timeout: unknown key")]
        for deploy, message in bad:
            with (self.home / "config.toml").open("w") as stream:
                stream.write(self.base)
                stream.write("[projects.repo.deploy]\n" + "".join(f"{k} = {json.dumps(v)}\n" for k, v in deploy.items()))
            code, output = self.cli("deploy", "repo", "--dry-run")
            self.assertEqual(code, 1, output)
            self.assertIn(message, output)
        self.configure(no_touch=["22:00-02:00"])
        code, output = self.cli("config", "show", "--project", "repo")
        self.assertEqual(code, 0, output)
        # The deploy table stays out of the worker settings layer.
        self.assertNotIn("\ndeploy.", "\n" + output)
        self.assertIn('projects.repo.deploy.command = "echo deployed', output)
