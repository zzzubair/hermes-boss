"""One Hermes session owns boss: claim, refuse, take, release, the mutation gate, /boss and doctor."""
import contextlib
import fcntl
import io
import json
import os
import subprocess
from unittest.mock import patch

from boss.wake import HEARTBEAT, LOCK
import plugin
from tests import test_boss as harness
from tests.support import HermeticTestCase
from tests.test_wake import EVENTS_TOKEN, Context, FakePush


class OwnerTest(HermeticTestCase):
    setUp = harness.CLITest.setUp
    tearDown = harness.CLITest.tearDown

    def spawn(self):
        cli, self.cli = self.cli, lambda *args: harness.CLITest.cli(self, *args)
        try:
            return harness.CLITest.spawn(self)
        finally:
            self.cli = cli

    def cli(self, *args, key=None, stdin="investigate this"):
        """(exit code, stdout, stderr) as session `key`; None is a human shell."""
        output, errors = io.StringIO(), io.StringIO()
        env = {"HERMES_SESSION_KEY": key} if key else {}
        with patch.dict(os.environ, env), contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors), \
                patch("sys.stdin", io.StringIO(stdin)):
            if not key:
                os.environ.pop("HERMES_SESSION_KEY", None)
            from boss.cli import main
            code = main(list(args))
        return code, output.getvalue(), errors.getvalue()

    def events(self):
        return [json.loads(line) for line in (self.home / "captain.jsonl").read_text().splitlines()]

    def test_claim_reopen_refuse_take_release(self):
        code, output, _ = self.cli("own", "--label", "Discord DM", key="agent:discord")
        self.assertEqual(code, 0, output)
        self.assertIn("boss is open in Discord DM; wakes come here.", output)
        self.assertIn("First time boss is open", output)
        record = json.loads((self.home / "owner.json").read_text())
        self.assertEqual((record["session_key"], record["label"]), ("agent:discord", "Discord DM"))
        self.assertEqual((self.home / "owner.json").stat().st_mode & 0o777, 0o600)
        self.assertIn("boss is open here (Discord DM)", self.cli("own", key="agent:discord")[1])
        code, output, _ = self.cli("own", "--label", "Hermes app", key="agent:app")
        self.assertEqual(code, 2)
        self.assertRegex(output, r"message: boss is owned by Discord DM since \d\d:\d\d\nnext: Run /boss take")
        code, output, _ = self.cli("own", "--take", "--label", "Hermes app", key="agent:app")
        self.assertEqual(code, 0, output)
        self.assertIn("boss moved here from Discord DM; wakes now come to Hermes app.", output)
        self.assertEqual(json.loads((self.home / "owner.json").read_text())["session_key"], "agent:app")
        self.assertIn("owner: Hermes app", self.cli("own", "status")[1])
        code, output, _ = self.cli("release", key="agent:app")
        self.assertEqual(code, 0, output)
        self.assertFalse((self.home / "owner.json").exists())
        self.assertIn("owner: none; wakes are held", self.cli("own", "status")[1])
        self.assertEqual([(e["action"], e["label"]) for e in self.events() if e["event"] == "own"],
                         [("claim", "Discord DM"), ("look", "Discord DM"), ("take", "Hermes app"), ("release", "Hermes app")])

    def test_own_needs_a_session(self):
        code, output, _ = self.cli("own")
        self.assertEqual(code, 1)
        self.assertIn("HERMES_SESSION_KEY", output)

    def test_mutations_from_a_foreign_session_are_refused_and_reads_work(self):
        meta = self.spawn()
        self.cli("own", "--label", "Discord DM", key="agent:discord")
        mutations = [("spawn", "repo", "--title", "T"), ("send", meta["id"], "hi"), ("answer", meta["id"], "r", "A"),
                     ("stop", meta["id"]), ("land", meta["id"], "--approved", "ok"), ("settle", meta["id"]),
                     ("steer", "repo", "Prefer SQLite"), ("goal", "repo", "Ship it"), ("decide", "add", "Pick"),
                     ("decide", "close", "1", "yes"), ("away",), ("back",), ("log", "said hi"), ("release",),
                     ("config", "set", "default_worker", "x")]
        before = len(self.store.journal(meta["id"]))
        for argv in mutations:
            code, output, _ = self.cli(*argv, key="agent:other")
            self.assertEqual(code, 2, argv)
            self.assertIn("boss is owned by Discord DM since", output, argv)
            self.assertIn("next: Run /boss take (or boss own --take) to move it here", output, argv)
        self.assertEqual(len(self.store.journal(meta["id"])), before)
        self.assertTrue((self.home / "owner.json").exists())
        for argv in [("status",), ("status", meta["id"]), ("decide", "list"), ("steer", "list", "repo"),
                     ("goal", "repo"), ("project", "show", "repo"), ("own", "status"), ("brief", "repo"), ("back", "--full")]:
            self.assertEqual(self.cli(*argv, key="agent:other")[0], 0, argv)
        self.assertEqual(self.cli("decide", "add", "Pick", key="agent:discord")[0], 0)

    def test_a_session_cannot_mutate_before_boss_is_opened(self):
        code, output, _ = self.cli("decide", "add", "Pick", key="agent:discord")
        self.assertEqual(code, 2)
        self.assertIn("boss is not open in any session", output)

    def test_human_shell_mutates_with_a_one_line_warning(self):
        code, _, errors = self.cli("decide", "add", "Pick")
        self.assertEqual((code, errors), (0, ""))
        self.cli("own", "--label", "Discord DM", key="agent:discord")
        code, _, errors = self.cli("decide", "add", "Pick again")
        self.assertEqual(code, 0)
        self.assertRegex(errors, r"^warning: boss is owned by Discord DM since \d\d:\d\d; this shell is not a Hermes session\n$")
        self.assertEqual(self.cli("status")[2], "")  # reads never warn

    def test_catch_up_shows_changes_since_last_look_and_numbered_decisions(self):
        self.cli("own", "--label", "Discord DM", key="agent:discord")
        self.cli("decide", "add", "Pick storage", key="agent:discord")
        self.cli("log", "said keep going", key="agent:discord")
        code, output, _ = self.cli("own", "--take", "--label", "Hermes app", key="agent:app")
        self.assertEqual(code, 0, output)
        self.assertRegex(output, r"Since you last looked at \d\d:\d\d \(2 events\):")
        self.assertIn("decision opened: Pick storage", output)
        self.assertIn("check-in: said keep going", output)
        self.assertIn('1 open decision (round 1; reply like "1 yes 2 no"):\n1. Pick storage', output)
        self.assertIn("Nothing changed since you last looked", self.cli("own", key="agent:app")[1])

    def test_transfers_appear_in_the_back_timeline(self):
        self.cli("own", "--label", "Discord DM", key="agent:discord")
        self.cli("away", "bed", key="agent:discord")
        self.cli("own", "--take", "--label", "Telegram DM", key="agent:phone")
        self.assertEqual(self.cli("back", key="agent:phone")[0], 0)
        self.assertIn("boss moved from Discord DM to Telegram DM", self.cli("back", "--full", key="agent:phone")[1])

    def test_slash_command_claims_refuses_takes_and_tells_the_previous_owner(self):
        ctx = Context(self.home / "profile")
        handler = plugin.boss_command(ctx)
        source = type("Source", (), {"chat_type": "dm", "chat_name": "captain"})()
        self.assertIn("boss is open in Discord DM", handler("", session_key="agent:discord", platform="discord", source=source))
        refused = handler("", session_key="agent:tui", platform="tui")
        self.assertRegex(refused, r"^boss is owned by Discord DM since \d\d:\d\d\. Run /boss take \(or boss own --take\) to move it here\.$")
        self.assertEqual(ctx.messages, [])
        self.assertIn("boss moved here from Discord DM", handler("take", session_key="agent:tui", platform="tui"))
        self.assertEqual(ctx.messages, [("[boss] boss moved to Hermes TUI; this session no longer gets boss wakes."
                                         " /boss take moves it back.", {"role": "user", "session_key": "agent:discord"})])
        self.assertIn("owner: Hermes TUI", handler("status"))
        self.assertIn("boss is owned by Hermes TUI", handler("release", session_key="agent:discord", platform="discord"))
        self.assertIn("Released boss from Hermes TUI", handler("release", session_key="agent:tui", platform="tui"))
        self.assertEqual(handler("bogus"), "usage: /boss [take|release|status]")
        self.assertIn("did not say which session", handler(""))

    def test_away_from_a_foreign_session_is_refused(self):
        handler = plugin.boss_command(Context(self.home / "profile"))
        handler("", session_key="agent:discord", platform="discord")
        self.assertIn("boss is owned by Discord chat", plugin.on_away("bed", session_key="agent:tui", platform="tui"))
        self.assertIn("Got it. Away mode on since", plugin.on_away("bed", session_key="agent:discord", platform="discord"))

    def test_hook_tells_other_sessions_only_when_boss_comes_up(self):
        self.cli("own", "--label", "Discord DM", key="agent:discord")
        with patch("plugin.current_session_key", return_value="agent:tui"):
            self.assertIsNone(plugin.on_pre_llm_call(session_id="x", user_message="what's for lunch"))
            line = plugin.on_pre_llm_call(session_id="x", user_message="what is Boss doing?")["context"]
        self.assertRegex(line, r"^boss: owned by Discord DM since \d\d:\d\d; /boss take to move it here$")
        self.cli("decide", "add", "Pick", key="agent:discord")
        with patch("plugin.current_session_key", return_value="agent:tui"):
            self.assertIn("owned by Discord DM", plugin.on_pre_llm_call(session_id="x", user_message="lunch?")["context"])
        with patch("plugin.current_session_key", return_value="agent:discord"):
            # The owner's own message stays clean; boss watch carries what is open.
            self.assertIsNone(plugin.on_pre_llm_call(session_id="x", user_message="lunch?"))

    def doctor(self, push=None):
        paths = {key: self.home / key for key in ("plugins", "skills")}
        for key, filename in (("plugins", "plugin.yaml"), ("skills", "SKILL.md")):
            (paths[key] / "boss").mkdir(parents=True, exist_ok=True)
            (paths[key] / "boss" / filename).touch()

        def command(argv, **kwargs):
            text = "true" if argv[:3] == ["hermes", "config", "get"] else '[{"name":"boss","status":"enabled"}]'
            return subprocess.CompletedProcess(argv, 0, text, "")
        with patch("boss.setup.install_paths", return_value=paths), patch("boss.setup.subprocess.run", side_effect=command), \
                patch("boss.setup.endpoints", side_effect=lambda url: ("", push.url.replace("/mcp", "/api/auth/websocket-ticket/rpc"))):
            return self.cli("doctor")[:2]

    def test_doctor_reports_owner_listener_and_push_stream(self):
        push = FakePush()
        self.addCleanup(push.close)
        os.environ["T3_EVENTS_TOKEN"] = EVENTS_TOKEN
        code, output = self.doctor(push)
        self.assertEqual(code, 0, output)
        self.assertIn("warn: no owner: run /boss in the session that should get wakes", output)
        self.assertIn("ok: host test push stream", output)
        self.cli("own", "--label", "Discord DM", key="agent:discord")
        code, output = self.doctor(push)
        self.assertEqual(code, 1)
        self.assertRegex(output, r"ok: owner Discord DM since \d\d:\d\d surface=unknown")
        self.assertIn("warn: no boss watch waiting", output)
        self.assertIn("fail: wake listener: no Hermes process holds wake.lock", output)
        watching = os.open(self.home / "watch.lock", os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(watching, fcntl.LOCK_EX)
        self.assertIn("ok: boss watch waiting in the owner session", self.doctor(push)[1])
        os.close(watching)
        # Opt-in idle injection reports whether the last injected wake reached the owner instead.
        self.assertEqual(self.cli("config", "set", "wake.inject_when_idle", "true")[0], 0)
        self.assertIn("info: owner reachable: unknown until the first wake", self.doctor(push)[1])
        descriptor = os.open(self.home / LOCK, os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, descriptor)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        (self.home / HEARTBEAT).write_text(f"2026-10-26T13:00:00+00:00 {os.getpid()} listening host=test\n")
        (self.home / "wake-last.json").write_text(json.dumps(
            {"at": "2026-10-26T13:01:00+00:00", "owner": "Discord DM", "result": "refused"}))
        code, output = self.doctor(push)
        self.assertEqual(code, 0, output)
        self.assertIn(f"ok: wake listener pid {os.getpid()}", output)
        self.assertIn("warn: owner unreachable: last wake refused", output)
        os.environ["T3_EVENTS_TOKEN"] = "wrong-but-long-token-value"
        code, output = self.doctor(push)
        self.assertEqual(code, 1)
        self.assertIn("fail: host test push stream: T3 refused the events token (HTTP 401)", output)
        self.assertNotIn("wrong-but-long-token-value", output)
