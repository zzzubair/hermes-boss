import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from plugin import on_pre_llm_call, register
from tests import test_boss as harness


class AmbientTest(unittest.TestCase):
    setUp = harness.CLITest.setUp
    tearDown = harness.CLITest.tearDown
    cli = harness.CLITest.cli
    spawn = harness.CLITest.spawn
    completed = harness.CLITest.completed

    def test_clean_ledger_injects_nothing_and_prints_nothing(self):
        self.cli("poll")
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            result = on_pre_llm_call(session_id="s1", user_message="hello", conversation_history=[])
        self.assertIsNone(result)
        self.assertEqual(output.getvalue(), "")

    def test_one_line_for_decision_and_input_without_network(self):
        decision = self.cli("decide", "add", "Choose an API")[1].split(":", 1)[0]
        meta = self.spawn()
        self.fake.pending = {"request-1": [{"id": "q1", "question": "Which API?", "options": []}]}
        self.cli("poll")
        before = len(self.fake.calls)
        with patch("socket.socket.connect", side_effect=AssertionError("network forbidden")), \
                patch("subprocess.run", side_effect=AssertionError("subprocess forbidden")):
            result = on_pre_llm_call(task_id="irrelevant", turn_id="t", model="m", is_first_turn=True)
        self.assertEqual(len(self.fake.calls), before)
        self.assertEqual(result, {"context": f"boss: 2 need you ({decision} decision, {meta['id']} input) - run boss status"})
        self.assertNotIn("\n", result["context"])
        self.assertNotIn("Choose an API", result["context"])
        self.assertNotIn("Which API?", result["context"])

    def test_completion_needs_poll_and_status_acknowledges_only_that_run(self):
        meta = self.spawn()
        self.completed(meta, "finished report")
        self.assertIsNone(on_pre_llm_call())
        self.cli("poll")
        self.assertIn(meta["id"] + " finished", on_pre_llm_call()["context"])
        self.cli()  # Fleet overview does not acknowledge individual work.
        self.assertIsNotNone(on_pre_llm_call())
        self.cli("status", meta["id"])
        self.assertIsNone(on_pre_llm_call())
        self.assertEqual(self.cli("poll"), (0, ""))
        self.fake.threads[meta["thread_id"]]["latestRunId"] = "run-2"
        self.assertIn("finished", self.cli("poll")[1])
        self.assertIn("finished", on_pre_llm_call()["context"])

    def test_status_before_poll_does_not_leave_a_false_review_reminder(self):
        meta = self.spawn()
        self.completed(meta)
        self.cli("status", meta["id"])
        self.assertEqual(self.cli("poll"), (0, ""))
        self.assertIsNone(on_pre_llm_call())

    def test_poll_only_uses_read_tools_and_suppresses_repeats_after_restart(self):
        meta = self.spawn()
        self.completed(meta)
        start = len(self.fake.calls)
        self.assertIn("finished", self.cli("poll")[1])
        self.assertEqual(self.cli("poll"), (0, ""))
        allowed = {"t3_thread_read", "t3_pending_request_list", "list_thread_pull_requests"}
        self.assertTrue({name for name, _ in self.fake.calls[start:]}.issubset(allowed))
        # Each self.cli invocation constructs a new Boss and MCP session.
        self.assertEqual(self.cli("poll"), (0, ""))

    def test_status_does_not_consume_an_input_poll_change(self):
        meta = self.spawn()
        self.fake.pending = {"r": [{"id": "q", "question": "Choice?", "options": []}]}
        self.cli("status", meta["id"])
        self.assertIn("waiting for input", self.cli("poll")[1])
        self.assertIn("input", on_pre_llm_call()["context"])
        self.assertEqual(self.cli("poll"), (0, ""))

    def test_answers_and_closed_decisions_clear_the_context(self):
        decision = self.cli("decide", "add", "Pick A or B")[1].split(":", 1)[0]
        meta = self.spawn()
        self.fake.pending = {"r": [{"id": "q", "question": "Choice?", "options": []}]}
        self.cli("poll")
        self.cli("answer", meta["id"], "r", "A")
        self.assertNotIn("input", on_pre_llm_call()["context"])
        self.cli("decide", "close", decision, "A")
        self.assertIsNone(on_pre_llm_call())

    def test_send_and_settle_remove_old_completion(self):
        meta = self.spawn()
        self.completed(meta)
        self.cli("poll")
        self.cli("send", meta["id"], "One more thing")
        self.assertIsNone(on_pre_llm_call())
        self.fake.threads[meta["thread_id"]]["latestRunId"] = "run-2"
        self.cli("poll")
        self.assertIsNotNone(on_pre_llm_call())
        self.assertEqual(self.cli("settle", meta["id"])[0], 0)
        self.assertIsNone(on_pre_llm_call())

    def test_context_is_bounded_with_precomputed_total(self):
        for i in range(9):
            self.store.decision({"id": f"d{i}", "text": "secret detail", "closed": False})
        self.store.refresh_attention()
        result = on_pre_llm_call()["context"]
        self.assertIn("9 need you", result)
        self.assertIn("+3 more", result)
        self.assertLess(len(result), 300)
        self.assertNotIn("secret detail", result)

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
        self.assertIn("1 need you", on_pre_llm_call()["context"])

    def test_only_one_hook_is_registered_with_actual_keyword_contract(self):
        class Context:
            def __init__(self):
                self.hooks = []

            def register_hook(self, event, callback):
                self.hooks.append((event, callback))

        context = Context()
        register(context)
        self.assertEqual([name for name, _ in context.hooks], ["pre_llm_call"])
        self.cli("decide", "add", "Review")
        result = context.hooks[0][1](session_id="s", task_id="t", turn_id="turn", user_message="hello",
                                     conversation_history=[], is_first_turn=True, model="test", platform="cli",
                                     parent_session_id="", sender_id="")
        self.assertTrue(result["context"].startswith("boss: 1 need you"))

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
