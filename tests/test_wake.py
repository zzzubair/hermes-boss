"""Fake host/injector tests, with real boss subprocesses for durable poll state."""
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from types import SimpleNamespace
from unittest.mock import patch

from boss.common import BossError, Redactor
from boss.config import Config
from boss.setup import wake_cron
from boss.wake import CONSENT_LINE, POLL_TIMEOUT, WakeLoop
import plugin
from tests import test_boss as harness
from tests.support import HermeticTestCase


class Context:
    def __init__(self, home, *, live=True, cli=None, accepted=True):
        self._manager = SimpleNamespace(home_path=home, _cli_ref=cli, has_gateway_message_injector=live)
        self.accepted = accepted
        self.messages, self.hooks, self.cleanups = [], [], []

    def inject_message(self, content, **target):
        self.messages.append((content, target))
        return self.accepted

    def register_hook(self, event, callback):
        self.hooks.append((event, callback))

    def on_unload(self, callback):
        self.cleanups.append(callback)


class WakeTest(HermeticTestCase):
    def setUp(self):
        super().setUp()
        os.environ["BOSS_HOME"] = str(self.home)
        self.ctx = Context(self.home / "profile")
        self.config = Config(self.home, Redactor(), hermes=self.ctx._manager.home_path)
        self.config.write({"wake": {"session_key": "agent:captain", "interval_seconds": 300}})
        self.loop = WakeLoop(self.ctx, self.config, self.home)

    def tick(self, stdout="", stderr="", code=0):
        result = subprocess.CompletedProcess([], code, stdout, stderr)
        with patch("boss.wake.subprocess.run", return_value=result) as run:
            self.loop.tick()
        return run

    def test_quiet_poll_uses_bounded_subprocess_and_no_injection(self):
        run = self.tick(" \n")
        self.assertEqual(self.ctx.messages, [])
        self.assertFalse((self.home / "poll-errors.log").exists())
        self.assertEqual(run.call_args.args[0][-1], "poll")
        self.assertEqual(run.call_args.kwargs["timeout"], POLL_TIMEOUT)
        self.assertFalse(run.call_args.kwargs.get("shell", False))
        self.assertEqual(run.call_args.kwargs["env"]["HERMES_HOME"], str(self.config.hermes))

    def test_changed_poll_injects_user_turn_with_exact_session_key(self):
        self.tick("task7: finished\n")
        message, target = self.ctx.messages[0]
        self.assertTrue(message.startswith("[boss wake] task7: finished"))
        self.assertIn("boss status", message)
        self.assertNotIn("Cron delivery", message)
        self.assertEqual(target, {"role": "user", "session_key": "agent:captain"})

    def test_origin_is_forwarded_without_session_key(self):
        origin = {"platform": "discord", "chat_id": "chat7", "user_id": "captain", "thread_id": "thread8"}
        self.config.data["wake"] = {"origin": origin}
        self.loop = WakeLoop(self.ctx, self.config, self.home)
        self.tick("task7: waiting for input")
        self.assertEqual(self.ctx.messages[0][1], {"role": "user", "origin": origin})

    def test_errors_are_redacted_before_truncation_and_never_injected(self):
        secret = "tiny-custom-token"
        self.config.redact.add(secret)
        self.tick("task7: finished\nerror " + "a" * 1995 + secret, "Bearer abcdef", 3)
        log = (self.home / "poll-errors.log").read_text()
        self.assertNotIn(secret, log)
        self.assertNotIn("Bearer abcdef", log)
        self.assertEqual(self.ctx.messages, [])
        self.assertEqual((self.home / "poll-errors.log").stat().st_mode & 0o777, 0o600)

    def test_stderr_alone_is_logged_without_waking(self):
        self.config.redact.add("private-token")
        self.tick("", "warning private-token")
        self.assertEqual(self.ctx.messages, [])
        self.assertIn("[REDACTED]", (self.home / "poll-errors.log").read_text())

    def test_refusal_is_logged_once_and_later_polls_continue(self):
        self.ctx.accepted = False
        self.tick("task1: finished")
        self.tick("task2: finished")
        self.assertEqual(len(self.ctx.messages), 2)
        log = (self.home / "poll-errors.log").read_text()
        self.assertEqual(log.count("gateway refused"), 1)
        self.assertIn(CONSENT_LINE, log)
        self.assertIn("--with-cron", log)

    def test_not_ready_or_cli_host_does_not_consume_poll_state(self):
        self.ctx._manager.has_gateway_message_injector = False
        self.tick("task7: finished").assert_not_called()
        self.ctx._manager.has_gateway_message_injector = True
        self.ctx._manager._cli_ref = object()
        self.tick("task7: finished").assert_not_called()

    def test_stopping_during_poll_suppresses_delivery(self):
        def poll(*args, **kwargs):
            self.loop.stop_event.set()
            return subprocess.CompletedProcess([], 0, "task7: finished", "")
        with patch("boss.wake.subprocess.run", side_effect=poll):
            self.loop.tick()
        self.assertEqual(self.ctx.messages, [])

    def test_host_change_during_poll_never_delivers_into_cli(self):
        def poll(*args, **kwargs):
            self.ctx._manager._cli_ref = object()
            return subprocess.CompletedProcess([], 0, "task7: finished", "")
        with patch("boss.wake.subprocess.run", side_effect=poll):
            self.loop.tick()
        self.assertEqual(self.ctx.messages, [])

    def test_timeout_and_injector_exception_do_not_kill_loop(self):
        called = threading.Event()
        attempts = []

        def tick():
            attempts.append(1)
            if len(attempts) == 1:
                raise subprocess.TimeoutExpired("secret-in-command", POLL_TIMEOUT)
            if len(attempts) == 2:
                raise RuntimeError("secret-in-injector")
            called.set()
            self.loop.stop_event.set()
        self.loop.wake["interval_seconds"] = 0.001
        with patch.object(self.loop, "tick", side_effect=tick):
            self.loop.thread.start()
            self.assertTrue(called.wait(2))
            self.loop.stop()
        log = (self.home / "poll-errors.log").read_text()
        self.assertIn("TimeoutExpired", log)
        self.assertIn("RuntimeError", log)
        self.assertNotIn("secret-in-", log)
        self.assertEqual(len(attempts), 3)

    def test_unwritable_log_does_not_raise(self):
        with patch("boss.wake.os.open", side_effect=PermissionError):
            self.tick("failure", code=3)
        self.assertEqual(self.ctx.messages, [])

    def test_registration_starts_one_daemon_and_unload_stops_it(self):
        with patch.dict(os.environ, {"_HERMES_GATEWAY": "1"}), \
                patch.dict(sys.modules, {"gateway.run": SimpleNamespace()}), \
                patch("boss.wake.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", "")) as poll:
            try:
                plugin.register(self.ctx)
                importlib.reload(plugin)
                plugin.register(self.ctx)
                self.assertEqual(len(self.ctx.cleanups), 1)
                from boss.wake import _loops
                loop = _loops[(str(self.config.hermes.resolve()), str(self.home.resolve()))]
                self.assertTrue(loop.thread.daemon)
                self.assertTrue(loop.thread.is_alive())
                self.ctx.cleanups[0]()
                self.assertFalse(loop.thread.is_alive())
                self.assertLessEqual(poll.call_count, 1)
                # A real unload followed by reload starts a working replacement.
                replacement = Context(self.config.hermes)
                plugin.register(replacement)
                self.assertEqual(len(replacement.cleanups), 1)
                replacement.cleanups[0]()
            finally:
                for cleanup in self.ctx.cleanups:
                    cleanup()

    def test_cli_and_one_shot_never_start_a_thread_even_with_inherited_marker(self):
        with patch.dict(os.environ, {"_HERMES_GATEWAY": "1"}), \
                patch.dict(sys.modules, {"gateway.run": SimpleNamespace()}), \
                patch("boss.wake.start_gateway_wake") as start:
            self.ctx._manager._cli_ref = object()
            plugin.register(self.ctx)
            start.assert_not_called()

        self.ctx._manager._cli_ref = None
        with patch.dict(os.environ, {"_HERMES_GATEWAY": "1"}), \
                patch("boss.wake.start_gateway_wake") as start:
            plugin.register(self.ctx)
            start.assert_not_called()
        with patch.dict(sys.modules, {"gateway.run": SimpleNamespace()}), \
                patch("boss.wake.start_gateway_wake") as start:
            plugin.register(self.ctx)
            start.assert_not_called()

    def test_gateway_as_python_module_is_recognized(self):
        with patch.dict(os.environ, {"_HERMES_GATEWAY": "1"}), \
                patch.object(sys.modules["__main__"], "__spec__", SimpleNamespace(name="gateway.run")), \
                patch("boss.wake.start_gateway_wake") as start:
            plugin.register(self.ctx)
            start.assert_called_once_with(self.ctx)

    def test_missing_target_disabled_and_bad_config_start_no_thread(self):
        for data in ({}, {"wake": {}}, {"wake": {"enabled": False, "session_key": "captain"}}):
            self.config.write(data)
            with patch.dict(os.environ, {"_HERMES_GATEWAY": "1"}), \
                    patch.dict(sys.modules, {"gateway.run": SimpleNamespace()}), patch("boss.wake.threading.Thread") as thread:
                plugin.register(self.ctx)
                thread.assert_not_called()
        self.config.path.write_text("[wake]\ninterval_seconds=false\n")
        with patch.dict(os.environ, {"_HERMES_GATEWAY": "1"}), \
                patch.dict(sys.modules, {"gateway.run": SimpleNamespace()}), patch("boss.wake.threading.Thread") as thread:
            plugin.register(self.ctx)
            thread.assert_not_called()
        self.assertIn("setup failed", (self.home / "poll-errors.log").read_text())

    def test_wake_validation_names_bad_fields_and_rejects_ambiguous_target(self):
        for wake, key in (({"interval_seconds": False}, "wake.interval_seconds"),
                          ({"interval_seconds": 0}, "wake.interval_seconds"),
                          ({"session_key": " "}, "wake.session_key"),
                          ({"origin": {}, "session_key": "s"}, "wake"),
                          ({"origin": {"platform": "discord"}}, "wake.origin.chat_id"),
                          ({"origin": {"platform": "discord", "chat_id": 7}}, "wake.origin.chat_id"),
                          ({"origin": {"platform": "discord", "chat_id": "7", "typo": "x"}}, "wake.origin.typo")):
            with self.assertRaises(BossError) as error:
                self.config.write({"wake": wake})
            self.assertIn(key, error.exception.message)

    def test_profile_credentials_and_subprocess_home_use_manager_home(self):
        profile = self.config.hermes
        profile.mkdir()
        (profile / ".env").write_text("CUSTOM=short\n")
        os.environ["HERMES_HOME"] = str(self.home / "other-profile")
        config = Config(self.home, Redactor(), hermes=profile)
        config.write({"wake": {"session_key": "captain"}, "hosts": {"test": {"url": "http://localhost/mcp", "token_env": "CUSTOM"}}})
        config = Config(self.home, Redactor(), hermes=profile)
        self.assertEqual(config.host("test")[1], "short")
        loop = WakeLoop(self.ctx, config, self.home)
        self.assertEqual(loop.env["HERMES_HOME"], str(profile))
        self.assertEqual(config.redact("short"), "[REDACTED]")


class WakeSetupTest(HermeticTestCase):
    setUp = harness.CLITest.setUp
    tearDown = harness.CLITest.tearDown
    cli = harness.CLITest.cli

    def test_init_adds_wake_to_existing_config_without_host_probe_or_hermes_edits(self):
        hermes = self.user_home / ".hermes"
        hermes.mkdir()
        (hermes / "config.yaml").write_text("plugins: {}\n")
        original = Config(self.home, Redactor()).data
        before = len(self.fake.calls)
        code, output = self.cli("init", "--with-wake", "--wake-origin", '{"platform":"telegram","chat_id":"7","user_id":"8"}')
        self.assertEqual(code, 0, output)
        self.assertIn(CONSENT_LINE, output)
        data = Config(self.home, Redactor()).data
        self.assertEqual(data["wake"]["interval_seconds"], 300)
        self.assertEqual({k: v for k, v in data.items() if k != "wake"}, original)
        self.assertEqual(len(self.fake.calls), before)
        self.assertEqual((hermes / "config.yaml").read_text(), "plugins: {}\n")
        self.assertFalse((hermes / "cron").exists())
        self.assertEqual(self.cli("config", "get", "wake.origin.chat_id"), (0, '"7"\n'))
        self.assertIn("wake.origin.chat_id", self.cli("config", "show", "--origin")[1])
        self.assertEqual(self.cli("init", "--with-wake", "--wake-session-key", "captain")[0], 0)
        self.assertNotIn("origin", Config(self.home, Redactor()).data["wake"])

    def test_fresh_init_with_wake_probes_and_preserves_config_on_failed_probe(self):
        path = self.home / "config.toml"
        path.unlink()
        code, output = self.cli("init", "--url", self.fake.url, "--token-env", "T3_TEST_MCP_TOKEN",
                                "--with-wake", "--wake-session-key", "captain", "--wake-interval", "12")
        self.assertEqual(code, 0, output)
        original = path.read_bytes()
        self.assertEqual(Config(self.home, Redactor()).data["wake"]["interval_seconds"], 12)
        self.assertNotEqual(self.cli("init", "--force", "--url", self.fake.url, "--token-env", "MISSING",
                                     "--with-wake", "--wake-session-key", "other")[0], 0)
        self.assertEqual(path.read_bytes(), original)

    def test_bad_setup_options_do_not_write(self):
        path = self.home / "config.toml"
        before = path.read_bytes()
        for args in (("--wake-interval", "3"), ("--with-wake", "--wake-origin", "[]"),
                     ("--with-wake", "--wake-origin", "not json"), ("--with-wake", "--wake-interval", "0"),
                     ("--with-wake", "--with-cron"),
                     ("--with-wake", "--wake-origin", "{}", "--wake-session-key", "s")):
            self.assertNotEqual(self.cli("init", *args)[0], 0)
            self.assertEqual(path.read_bytes(), before)

    def test_doctor_checks_consent_target_and_both_legacy_cron_names(self):
        self.assertEqual(self.cli("init", "--with-wake", "--wake-session-key", "captain")[0], 0)
        consent = False

        def command(argv, **kwargs):
            text = json.dumps(consent) if argv[:3] == ["hermes", "config", "get"] else (
                '[{"name":"boss","status":"enabled"}]' if argv[:2] == ["hermes", "plugins"] else "ok")
            return subprocess.CompletedProcess(argv, 0, text, "")
        with patch("boss.setup.subprocess.run", side_effect=command):
            code, output = self.cli("doctor")
            self.assertEqual(code, 1)
            self.assertIn("fail: wake gateway consent", output)
            consent = "true"  # JSON string must never count as boolean consent.
            self.assertIn("fail: wake gateway consent", self.cli("doctor")[1])
            consent = True
            self.assertIn("ok: wake gateway consent", self.cli("doctor")[1])
            cron = self.user_home / ".hermes/cron/jobs.json"
            cron.parent.mkdir(parents=True)
            for name in ("boss-wake", "boss-poll", "custom"):
                cron.write_text(json.dumps({"jobs": [{"name": name, "script": "boss-poll-gate.sh"}]}))
                self.assertTrue(wake_cron())
                self.assertIn("fail: single wake source", self.cli("doctor")[1])
                self.assertNotEqual(self.cli("init", "--with-cron")[0], 0)
            cron.write_text(json.dumps({"jobs": [{"name": "boss-wake", "enabled": False}]}))
            self.assertFalse(wake_cron())
            self.assertIn("ok: single wake source", self.cli("doctor")[1])
            self.cli("config", "unset", "wake.session_key")
            self.assertIn("fail: wake target", self.cli("doctor")[1])

    def test_installer_is_repeatable_preserves_live_files_and_supports_xdg(self):
        root = Path(__file__).resolve().parent.parent
        hermes = self.user_home / "hermes profile"
        hermes.mkdir()
        config, cron = hermes / "config.yaml", hermes / "cron/jobs.json"
        config.write_text("plugins: {}\n")
        cron.parent.mkdir()
        cron.write_text('{"jobs":[{"name":"boss-wake"}]}')
        env = {**os.environ, "HERMES_HOME": str(hermes), "XDG_CONFIG_HOME": str(self.user_home / "xdg")}
        env.pop("BOSS_HOME")
        for _ in range(2):
            result = subprocess.run([str(root / "install.sh"), "--with-wake", "--wake-session-key", "captain"],
                                    capture_output=True, text=True, env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(CONSENT_LINE, result.stdout)
            self.assertEqual(config.read_text(), "plugins: {}\n")
            self.assertEqual(cron.read_text(), '{"jobs":[{"name":"boss-wake"}]}')
        data = (self.user_home / "xdg/boss/config.toml").read_text()
        self.assertIn('"session_key" = "captain"', data)
        # The installer wrote only [wake]. First init must still discover/save
        # a host, without requiring --force or dropping the wake target.
        result = subprocess.run([str(self.user_home / ".local/bin/boss"), "init", "--url", self.fake.url,
                                 "--token-env", "T3_TEST_MCP_TOKEN"], capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        with patch.dict(os.environ, env, clear=True):
            config = Config(self.user_home / ".boss", Redactor())
        self.assertEqual(config.data["wake"]["session_key"], "captain")
        self.assertEqual(config.hosts["local"]["url"], self.fake.url)
        self.assertEqual(config.data["default_host"], "local")
        self.assertEqual((hermes / "config.yaml").read_text(), "plugins: {}\n")
        self.assertEqual(cron.read_text(), '{"jobs":[{"name":"boss-wake"}]}')


class WakeIntegrationTest(HermeticTestCase):
    setUp = harness.CLITest.setUp
    tearDown = harness.CLITest.tearDown
    cli = harness.CLITest.cli
    spawn = harness.CLITest.spawn
    completed = harness.CLITest.completed

    def exercise(self, transcript=lambda line: None):
        self.cli("init", "--with-wake", "--wake-session-key", "captain")
        config = Config(self.home, Redactor())
        ctx = Context(config.hermes)
        loop = WakeLoop(ctx, config, self.home)
        task = self.spawn()
        loop.tick()
        self.assertEqual(ctx.messages, [])
        transcript("running worker: 0 injections")
        self.completed(task)
        loop.tick()
        self.assertEqual(len(ctx.messages), 1)
        self.assertIn(task["id"] + ": finished", ctx.messages[0][0])
        transcript("completed: " + ctx.messages[0][0].replace(task["id"], "<TASK>"))
        # New loop, new CLI subprocess, same persisted ledger. No repeat wake.
        loop = WakeLoop(ctx, Config(self.home, Redactor()), self.home)
        loop.tick()
        self.assertEqual(len(ctx.messages), 1)
        transcript("repeat after loop restart: still 1 injection")
        self.fake.threads[task["thread_id"]]["latestRunId"] = "run-2"
        loop.tick()
        self.assertEqual(len(ctx.messages), 2)
        self.assertEqual(ctx.messages[0], ctx.messages[1])
        transcript("new completed run with identical summary: 2 injections total")
        self.fake.failure = "t3_thread_read"
        loop.tick()
        self.assertEqual(len(ctx.messages), 2)
        log = (self.home / "poll-errors.log").read_text()
        self.assertNotIn(harness.TOKEN, log)
        self.assertIn("[REDACTED]", log)
        transcript("poll failure: still 2 injections; redacted poll-errors.log")
        self.fake.failure = None
        self.fake.pending = {"r": [{"id": "q", "question": "Which API?", "options": []}]}
        loop.tick()
        self.assertEqual(len(ctx.messages), 3)
        self.assertIn("waiting for input", ctx.messages[-1][0])
        self.assertEqual(ctx.messages[-1][1], {"role": "user", "session_key": "captain"})
        transcript("new input request: 3 injections total; role=user; session_key=captain")

    def test_real_subprocess_poll_dedupes_and_never_injects_errors(self):
        self.exercise()
