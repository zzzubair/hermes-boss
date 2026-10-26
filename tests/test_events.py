"""Every finished-turn meaning, from snapshot-shaped views with no T3 calls."""
import unittest

from boss.events import KINDS, QUOTE_CHARS, classify

PR = "https://github.com/acme/widget/pull/7"


def view(status="completed", *, run="run-1", requests=(), prs=(), assistant=""):
    return {"thread": {"threadId": "thread-1", "latestRunId": run, "status": status},
            "status": status, "requests": list(requests),
            "prs": [{"url": url, "state": "OPEN"} for url in prs],
            "assistant": assistant, "alerts": []}


class ClassifyTests(unittest.TestCase):
    def test_pending_input_request(self):
        event = classify(view("waiting", requests=["req-b", "req-a"], assistant="Which path?"))
        self.assertEqual(event.kind, "input")
        self.assertEqual(event.request_ids, ("req-a", "req-b"))
        self.assertEqual(event.dedupe_key, "thread-1:input:req-a,req-b")

    def test_input_wins_over_a_finished_looking_turn(self):
        self.assertEqual(classify(view("completed", requests=["req-1"], prs=[PR], assistant=PR)).kind, "input")

    def test_waiting_without_a_question_is_a_permission_prompt(self):
        event = classify(view("waiting"))
        self.assertEqual((event.kind, event.dedupe_key), ("approval", "thread-1:approval:run-1"))

    def test_finished_with_the_pr_named_in_the_final_reply(self):
        event = classify(view(prs=[PR], assistant=f"Opened {PR}. 120 tests pass."))
        self.assertEqual((event.kind, event.pr_urls), ("finished_pr", (PR,)))
        self.assertEqual(event.dedupe_key, "thread-1:finished_pr:run-1")

    def test_finished_report(self):
        event = classify(view(assistant="  BOSS_REPORT: found three callers"))
        self.assertEqual((event.kind, event.dedupe_key), ("finished_report", "thread-1:finished_report:run-1"))

    def test_empty_report_is_not_a_report(self):
        self.assertEqual(classify(view(assistant="BOSS_REPORT:   ")).kind, "ended_unclear")

    def test_plain_text_question_is_ended_unclear_and_quoted(self):
        question = "Should I keep the old flag?\nIt is used by two scripts. " + "x" * 400
        event = classify(view(assistant=question))
        self.assertEqual((event.kind, event.dedupe_key), ("ended_unclear", "thread-1:ended_unclear:run-1"))
        quote = " ".join(question.split())[:QUOTE_CHARS]
        self.assertIn(f'"{quote}"', event.detail)
        self.assertNotIn("x" * 301, event.detail)

    def test_plain_text_question_after_an_earlier_pr_is_not_swallowed(self):
        # Run 2 was a steer; the PR from run 1 stays linked but the reply asks in plain text.
        event = classify(view(run="run-2", prs=[PR], assistant="CI is red on an unrelated test. Should I skip it?"))
        self.assertEqual(event.kind, "ended_unclear")
        self.assertIn("Should I skip it?", event.detail)

    def test_turn_with_no_assistant_message_is_ended_unclear(self):
        event = classify(view(assistant=""))
        self.assertEqual(event.kind, "ended_unclear")
        self.assertIn("assistant message", event.detail)
        self.assertNotIn("probable", event.detail)

    def test_idle_after_a_run_counts_as_ended(self):
        self.assertEqual(classify(view("idle", assistant="Done?")).kind, "ended_unclear")
        self.assertEqual(classify(view("idle", run=None)).kind, "working")

    def test_failed_and_interrupted_are_errors(self):
        for status in ("failed", "interrupted", "cancelled", "rolled_back"):
            with self.subTest(status=status):
                event = classify(view(status, prs=[PR], assistant=PR))
                self.assertEqual((event.kind, event.dedupe_key), ("error", "thread-1:error:run-1"))
                self.assertIn(status, event.detail)

    def test_running_threads_have_no_event(self):
        for status in ("preparing", "queued", "starting", "running"):
            with self.subTest(status=status):
                event = classify(view(status, assistant="Should I keep going?"))
                self.assertEqual((event.kind, event.dedupe_key), ("working", None))

    def test_one_event_per_turn_and_per_request_set(self):
        first, again = classify(view(assistant="Which one?")), classify(view(assistant="Which one?"))
        self.assertEqual(first.dedupe_key, again.dedupe_key)
        self.assertNotEqual(first.dedupe_key, classify(view(run="run-2", assistant="Which one?")).dedupe_key)
        self.assertNotEqual(classify(view("waiting", requests=["a"])).dedupe_key,
                            classify(view("waiting", requests=["a", "b"])).dedupe_key)

    def test_every_kind_is_covered(self):
        seen = {classify(v).kind for v in (
            view("waiting", requests=["r"]), view("waiting"), view(prs=[PR], assistant=PR),
            view(assistant="BOSS_REPORT: ok"), view(assistant="Hm?"), view("failed"), view("running"))}
        self.assertEqual(seen, set(KINDS))


if __name__ == "__main__":
    unittest.main()
