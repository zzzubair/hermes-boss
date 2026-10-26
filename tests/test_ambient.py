import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from boss.watch import still_open
from plugin import on_pre_llm_call, register
from tests import test_boss as harness


class AmbientTest(unittest.TestCase):
    setUp = harness.CLITest.setUp
    tearDown = harness.CLITest.tearDown
    cli = harness.CLITest.cli
    spawn = harness.CLITest.spawn
    completed = harness.CLITest.completed
    as_owner = harness.CLITest.as_owner

    def test_clean_ledger_injects_nothing_and_prints_nothing(self):
        self.cli("status")
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            result = on_pre_llm_call(session_id="s1", user_message="hello", conversation_history=[])
        self.assertIsNone(result)
        self.assertEqual(output.getvalue(), "")

    def test_open_items_never_reach_turn_context_and_watch_counts_them_without_ids(self):
        # Turn context shows inside the person's own message, so open items go to boss watch instead.
        self.as_owner()
        decision = self.cli("decide", "add", "Choose an API")[1].split(":", 1)[0]
        meta = self.spawn()
        self.fake.pending = {"request-1": [{"id": "q1", "question": "Which API?", "options": []}]}
        self.cli("status", meta["id"])
        self.cli("release")  # no owner turn, so no dead-listener line either
        before = len(self.fake.calls)
        with patch("socket.socket.connect", side_effect=AssertionError("network forbidden")), \
                patch("subprocess.run", side_effect=AssertionError("subprocess forbidden")):
            result = on_pre_llm_call(task_id="irrelevant", turn_id="t", model="m", is_first_turn=True)
            line = still_open(self.home)
        self.assertEqual(len(self.fake.calls), before)
        self.assertIsNone(result)
        self.assertEqual(line, "Still open: 1 open decision, 1 unanswered worker question. Run boss status.")
        for text in (decision, meta["id"], "Choose an API", "Which API?"):
            self.assertNotIn(text, line)

    def test_answers_and_closed_decisions_clear_the_open_line(self):
        decision = self.cli("decide", "add", "Pick A or B")[1].split(":", 1)[0]
        meta = self.spawn()
        self.fake.pending = {"r": [{"id": "q", "question": "Choice?", "options": []}]}
        self.cli("status", meta["id"])
        self.cli("answer", meta["id"], "r", "A")
        self.assertEqual(still_open(self.home), "Still open: 1 open decision. Run boss status.")
        self.cli("decide", "close", decision, "A")
        self.assertIsNone(still_open(self.home))

    def test_open_line_is_bounded_with_precomputed_total(self):
        for i in range(9):
            self.store.decision({"id": f"d{i}", "text": "secret detail", "closed": False})
        self.store.refresh_attention()
        self.assertEqual(still_open(self.home), "Still open: 6 open decisions and 3 more. Run boss status.")

    def test_missing_and_corrupt_ledgers_never_raise(self):
        cache = self.home / "attention.json"
        self.assertIsNone(on_pre_llm_call())
        for value in ("not json", "[]", "null", '{"version":1,"count":true,"items":[]}',
                      '{"version":1,"count":1,"items":[{"id":"bad\\nline","kind":"decision"}]}',
                      '{"version":1,"count":1,"items":[{"id":"d1","kind":"arbitrary instructions"}]}',
                      "[" * 3000):
            cache.write_text(value)
            self.assertIsNone(on_pre_llm_call(), value[:80])
        cache.write_text(" " * 5000)
        self.assertIsNone(on_pre_llm_call())
        cache.unlink()
        os.mkfifo(cache)
        self.assertIsNone(on_pre_llm_call())

    def test_corrupt_record_does_not_hide_valid_decision(self):
        self.cli("decide", "add", "Valid decision")
        (self.home / "decisions/corrupt.json").write_text("{")
        directory = self.home / "tasks/corrupt"
        directory.mkdir(parents=True)
        (directory / "events.log").write_text("{")
        self.store.refresh_attention()
        self.assertEqual(still_open(self.home), "Still open: 1 open decision. Run boss status.")

    def test_hooks_are_registered_with_actual_keyword_contract(self):
        class Context:
            def __init__(self):
                self.hooks = []

            def register_hook(self, event, callback):
                self.hooks.append((event, callback))

        context = Context()
        register(context)
        self.assertEqual([name for name, _ in context.hooks],
                         ["pre_llm_call", "transform_tool_result", "on_session_end", "agent_loop_stopped"])
        self.cli("decide", "add", "Review")
        hook = context.hooks[0][1]

        def turn(session):
            return hook(session_id=session, task_id="t", turn_id="turn", user_message="hello", conversation_history=[],
                        is_first_turn=True, model="test", platform="cli", parent_session_id="", sender_id="")
        self.assertTrue(turn("s")["context"].startswith("boss: not open in any session"))
        with patch.dict(os.environ, {"HERMES_SESSION_KEY": "s"}):
            self.assertEqual(self.cli("own", "--label", "Hermes CLI")[0], 0)
        self.assertIsNone(turn("s"))  # the owner's own message never carries boss lines
        self.assertRegex(turn("other")["context"], r"^boss: owned by Hermes CLI since \d\d:\d\d; /boss take to move it here$")

    def test_installer_is_repeatable_and_preserves_config(self):
        root = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / ".hermes/config.yaml"
            config.parent.mkdir()
            config.write_text("plugins:\n  enabled: [existing]\n")
            before = config.read_bytes()
            env = {**os.environ, "HOME": temporary}
            for _ in range(2):
                result = subprocess.run([str(root / "install.sh")], env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(config.read_bytes(), before)
                self.assertEqual((Path(temporary) / ".agents/skills/boss").resolve(), root / "skill")
                self.assertEqual((Path(temporary) / ".hermes/plugins/boss").resolve(), root / "plugin")

    def test_installer_refuses_existing_skill_without_partial_install(self):
        root = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as temporary:
            skill = Path(temporary) / ".agents/skills/boss"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text("existing skill")
            result = subprocess.run([str(root / "install.sh")], env={**os.environ, "HOME": temporary}, capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertEqual((skill / "SKILL.md").read_text(), "existing skill")
            self.assertFalse((Path(temporary) / ".local/bin/boss").exists())


if __name__ == "__main__":
    unittest.main()
