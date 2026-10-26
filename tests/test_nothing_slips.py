"""Nothing slips: finished runs stay on the attention line, a dead listener is named, one status table."""
from datetime import datetime, timedelta, timezone
import fcntl
import json
import os
import re
import unittest
from unittest.mock import patch

from boss.cli import quiet
from boss.watch import still_open
from plugin import DEAF, on_pre_llm_call
from tests import test_boss as harness


OWNER = "agent:owner"


class NothingSlipsTest(unittest.TestCase):
    def setUp(self):
        # Only the owner's status reviews a run, so these flows run as the supervisor.
        harness.CLITest.setUp(self)
        self.as_owner(OWNER)

    as_owner = harness.CLITest.as_owner
    tearDown = harness.CLITest.tearDown
    cli = harness.CLITest.cli
    spawn = harness.CLITest.spawn
    completed = harness.CLITest.completed

    def attention(self):
        return json.loads((self.home / "attention.json").read_text())

    def finished_ids(self):
        return [i["id"] for i in self.attention()["items"] if i["kind"] == "finished"]

    def own(self):
        with patch.dict(os.environ, {"HERMES_SESSION_KEY": OWNER}):
            code, output = self.cli("own", "--label", "Hermes app")
        self.assertEqual(code, 0, output)

    def fleet(self, *extra):
        code, output = self.cli("status", *extra)
        self.assertEqual(code, 0, output)
        return output

    # Gap 5: finished stays visible until someone reviews it.

    def test_finished_run_stays_on_the_attention_line_until_status_reviews_it(self):
        meta = self.spawn()
        self.fleet()
        self.assertEqual(self.finished_ids(), [])  # still running: nothing to review
        self.completed(meta, "BOSS: done: shipped it")
        self.fleet()
        self.assertEqual(self.finished_ids(), [meta["id"]])
        # Being shown is not being handled: every boss watch delivery keeps naming it.
        for _ in range(3):
            self.assertEqual(still_open(self.home), "Still open: 1 finished run to review. Run boss status.")
        self.assertEqual(self.cli("status", meta["id"])[0], 0)
        self.assertEqual(self.finished_ids(), [])
        self.assertIsNone(still_open(self.home))

    def test_errored_run_is_listed_and_send_clears_it(self):
        meta = self.spawn()
        self.fake.threads[meta["thread_id"]]["status"] = "failed"
        self.fleet()
        self.assertEqual(self.finished_ids(), [meta["id"]])
        self.assertEqual(self.cli("send", meta["id"], "try again with the fix")[0], 0)
        self.assertEqual(self.finished_ids(), [])

    def test_errored_run_reviewed_by_status_is_cleared(self):
        meta = self.spawn()
        self.fake.threads[meta["thread_id"]]["status"] = "interrupted"
        self.fleet()
        self.assertEqual(self.finished_ids(), [meta["id"]])
        self.cli("status", meta["id"])
        self.assertEqual(self.finished_ids(), [])

    def test_a_new_finished_run_comes_back_after_review(self):
        meta = self.spawn()
        self.completed(meta, "BOSS: done: first pass")
        self.cli("status", meta["id"])
        self.assertEqual(self.finished_ids(), [])
        thread = self.fake.threads[meta["thread_id"]]
        thread.update(latestRunId="run-2", status="running")
        self.fleet()
        self.assertEqual(self.finished_ids(), [])
        thread["status"] = "completed"
        self.fleet()
        self.assertEqual(self.finished_ids(), [meta["id"]])

    def test_settle_clears_a_finished_run(self):
        meta = self.spawn("--kind", "scout")
        self.completed(meta, "BOSS: done: findings are in")
        self.fleet()
        self.assertEqual(self.finished_ids(), [meta["id"]])
        self.assertEqual(self.cli("settle", meta["id"])[0], 0)
        self.assertEqual(self.finished_ids(), [])
        self.assertEqual(self.attention()["active"], 0)

    def test_a_pending_question_is_one_input_item_not_also_finished(self):
        meta = self.spawn()
        self.completed(meta, "BOSS: needs-decision: A or B?")
        self.fake.pending = {"r": [{"id": "q", "question": "A or B?", "options": []}]}
        self.fleet()
        self.assertEqual(self.attention()["items"], [{"id": meta["id"], "kind": "input"}])

    # Gap 7: a dead wake listener is named at the owner's turn start.

    def test_owner_turn_says_updates_are_not_arriving_when_nobody_listens(self):
        self.own()
        meta = self.spawn()
        context = on_pre_llm_call(session_id=OWNER, user_message="hi")["context"]
        self.assertEqual(context, DEAF)
        self.assertEqual(DEAF, "boss: worker updates are not arriving; run boss doctor")
        # Someone else's session, or a subagent of the owner, never gets it.
        self.assertNotIn(DEAF, (on_pre_llm_call(session_id="agent:other", user_message="boss?") or {}).get("context", ""))
        self.assertIsNone(on_pre_llm_call(session_id="child", parent_session_id=OWNER, user_message="hi"))
        # It is the only line: open items never ride on the person's own message.
        self.completed(meta, "BOSS: done: shipped")
        self.fleet()
        self.assertEqual(on_pre_llm_call(session_id=OWNER, user_message="hi"), {"context": DEAF})

    def test_no_line_while_a_listener_holds_the_lock_or_nothing_is_active(self):
        self.own()
        self.assertIsNone(on_pre_llm_call(session_id=OWNER, user_message="hi"))  # no active tasks
        self.spawn()
        descriptor = os.open(self.home / "wake.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            self.assertIsNone(on_pre_llm_call(session_id=OWNER, user_message="hi"))
        finally:
            os.close(descriptor)
        # The listener went away: the next owner turn says so, and the probe never kept the lock.
        self.assertEqual(on_pre_llm_call(session_id=OWNER, user_message="hi"), {"context": DEAF})
        probe = os.open(self.home / "wake.lock", os.O_RDWR)
        try:
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(probe)

    def test_away_never_rides_on_the_persons_message(self):
        self.own()
        self.spawn()
        with patch.dict(os.environ, {"HERMES_SESSION_KEY": OWNER}):
            self.cli("away", "sleeping")
        self.assertEqual(on_pre_llm_call(session_id=OWNER, user_message="still /afk"), {"context": DEAF})

    # Gap 8 / must-haves 4 and 15: one status table, scoped, no ids, short.

    def table(self, output):
        rows = [line for line in output.splitlines() if line.startswith("|")]
        return [[c.strip() for c in row.strip("|").split("|")] for row in rows if not row.startswith("|---")]

    def test_status_is_one_table_with_state_pr_ci_quiet_and_next(self):
        meta = self.spawn("--model", "codex:gpt-test", "--effort", "max")
        thread = self.fake.threads[meta["thread_id"]]
        self.completed(meta, f"Opened the PR.\nBOSS: done: {harness.URL}")
        thread["updatedAt"] = (datetime.now(timezone.utc) - timedelta(minutes=25)).isoformat().replace("+00:00", "Z")
        self.fake.prs = [harness.URL]
        output = self.fleet()
        header, row = self.table(output)
        self.assertEqual(header, ["task", "project", "host", "model", "state", "PR", "quiet", "next"])
        self.assertEqual(row, ["Fix thing", "repo", "test", "gpt-test max", f"done: {harness.URL}",
                               f"{harness.URL} CI green", "25m", "review, then land"])
        self.assertNotIn(meta["id"], output)
        self.assertNotIn(meta["thread_id"], output)
        # Outside the table, a handful of short lines.
        prose = [line for line in output.splitlines() if not line.startswith("|")]
        self.assertLess(sum(len(line.split()) for line in prose), 80, prose)

    def test_ids_column_only_with_ids(self):
        meta = self.spawn()
        header, row = self.table(self.fleet("--ids"))
        self.assertEqual(header[0], "id")
        self.assertEqual(row[:2], [meta["id"], "Fix thing"])
        self.assertEqual(row[5:], ["working", "-", "-", "wait"])

    def test_next_step_follows_how_the_turn_ended(self):
        meta = self.spawn()
        thread = self.fake.threads[meta["thread_id"]]
        cases = [
            ("completed", "BOSS: blocked: no access to the bucket", "blocked: no access to the bucket", "unblock it"),
            ("completed", "BOSS: paused: CI until 18:00 UTC", "paused: CI until 18:00 UTC", "wait; it resumes itself"),
            ("completed", "I looked around.", "unclear: I looked around.", "ask it what happened"),
            ("completed", "Report.\nBOSS: done: nothing to change", "done: nothing to change", "read its report, then settle"),
            ("failed", "", "stopped: the thread failed", "read why, then resend or settle"),
        ]
        for status, text, state, step in cases:
            self.completed(meta, text)
            thread["status"] = status
            row = self.table(self.fleet())[1]
            self.assertEqual((row[4], row[7]), (state, step), text)
        self.completed(meta, f"BOSS: done: {harness.URL}")
        self.fake.prs = [harness.URL]
        self.gh.pr["statusCheckRollup"] = [{"conclusion": "FAILURE"}]
        self.assertEqual(self.table(self.fleet())[1][5:], [f"{harness.URL} CI failed", "-", "send it the CI failure"])
        self.gh.pr["statusCheckRollup"] = [{"status": "IN_PROGRESS"}]
        self.assertEqual(self.table(self.fleet())[1][7], "wait for CI")
        self.gh.pr.update(state="MERGED")
        self.assertEqual(self.table(self.fleet())[1][5:], [f"{harness.URL} merged", "-", "settle it"])
        self.fake.pending = {"r": [{"id": "q", "question": "A or B?", "options": []}]}
        self.assertEqual(self.table(self.fleet())[1][7], "answer its question")

    def test_scope_by_project(self):
        config = self.home / "config.toml"
        config.write_text(config.read_text() + '''[projects.notebook]
host = "test"
project_id = "p1"
repository = "example/repo"
delivery = "direct-PR"
''')
        self.spawn()
        code, output = self.cli("spawn", "notebook", "--title", "Notebook page")
        self.assertEqual(code, 0, output)
        titles = lambda out: [row[0] for row in self.table(out)[1:]]
        self.assertEqual(sorted(titles(self.fleet())), ["Fix thing", "Notebook page"])
        only = self.fleet("--project", "repo")
        self.assertEqual(titles(only), ["Fix thing"])
        self.assertIn("1 active tasks in repo", only)
        skipping = self.fleet("--skip", "repo")
        self.assertEqual(titles(skipping), ["Notebook page"])
        self.assertIn("(skipping repo)", skipping)
        self.assertNotIn("|", self.fleet("--project", "nothing"))

    def test_pipes_in_text_never_break_the_table(self):
        meta = self.spawn()
        self.completed(meta, "BOSS: blocked: pick a | b")
        header, row = self.table(self.fleet())
        self.assertEqual(len(row), len(header))
        self.assertEqual(row[4], "blocked: pick a / b")

    def test_quiet_age(self):
        now = datetime.now(timezone.utc)
        self.assertEqual(quiet((now - timedelta(seconds=20)).isoformat()), "now")
        self.assertEqual(quiet((now - timedelta(minutes=7)).isoformat().replace("+00:00", "Z")), "7m")
        self.assertEqual(quiet((now - timedelta(hours=3, minutes=5)).isoformat()), "3h")
        self.assertEqual(quiet((now - timedelta(days=2)).isoformat()), "2d")
        for bad in (None, "", "yesterday", 5):
            self.assertEqual(quiet(bad), "-")
        self.assertTrue(re.fullmatch(r"\d+[mhd]|now|-", quiet(now.isoformat())))


if __name__ == "__main__":
    unittest.main()
