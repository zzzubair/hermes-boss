"""Every finished-turn meaning, from snapshot-shaped views with no T3 calls."""
import re
import unittest

from datetime import datetime, timezone

from boss.events import KINDS, Event, QUOTE_CHARS, classify, describe, paused_until, pr_events, stall_text, status_line

PR = "https://github.com/acme/widget/pull/7"


def view(status="completed", *, run="run-1", requests=(), approvals=0, prs=(), assistant="", assistant_run=None):
    return {"thread": {"threadId": "thread-1", "latestRunId": run, "status": status},
            "status": status, "requests": list(requests), "approvals": approvals,
            "prs": [{"url": url, "state": "OPEN", "title": "Fix the widget"} for url in prs],
            "assistant": assistant, "assistant_run": assistant_run, "alerts": []}


class StatusLineTests(unittest.TestCase):
    def test_each_state_and_its_payload(self):
        for state in ("done", "needs-decision", "blocked", "paused"):
            with self.subTest(state=state):
                self.assertEqual(status_line(f"Work notes.\n\nBOSS: {state}: the payload  \n"), (state, "the payload"))

    def test_only_the_last_non_empty_line_counts(self):
        self.assertIsNone(status_line("BOSS: done: early\nthen I kept talking"))
        self.assertEqual(status_line("BOSS: blocked: old\nBOSS: done: new\n\n"), ("done", "new"))

    def test_markdown_wrapping_is_tolerated(self):
        self.assertEqual(status_line("`BOSS: done: shipped`"), ("done", "shipped"))
        self.assertEqual(status_line("**BOSS: paused: CI on the PR**"), ("paused", "CI on the PR"))

    def test_malformed_lines_are_not_status_lines(self):
        for text in ("", "BOSS: done:", "BOSS: done:   ", "BOSS: finished: x", "boss: done: x", "BOSS_REPORT: x",
                     "I think BOSS: done: x"):
            with self.subTest(text=text):
                self.assertIsNone(status_line(text))


class ClassifyTests(unittest.TestCase):
    def test_pending_question_is_a_decision_with_its_request_ids(self):
        event = classify(view("waiting", requests=["req-b", "req-a"], assistant="BOSS: needs-decision: Which path?"))
        self.assertEqual((event.kind, event.detail), ("needs-decision", "Which path?"))
        self.assertEqual(event.request_ids, ("req-a", "req-b"))
        self.assertEqual(event.dedupe_key, "thread-1:needs-decision:req-a,req-b")

    def test_pending_question_wins_over_a_finished_looking_turn(self):
        event = classify(view("completed", requests=["req-1"], prs=[PR], assistant=f"BOSS: done: {PR}"))
        self.assertEqual((event.kind, event.detail), ("needs-decision", ""))

    def test_pending_permission_request_is_an_approval(self):
        event = classify(view("waiting", approvals=1))
        self.assertEqual((event.kind, event.dedupe_key), ("approval", "thread-1:approval:run-1"))
        self.assertIn("1 pending permission", event.detail)

    def test_pending_question_wins_over_a_pending_permission(self):
        self.assertEqual(classify(view("waiting", requests=["req-1"], approvals=1)).kind, "needs-decision")

    def test_bare_waiting_as_a_turn_finishes_is_not_an_approval(self):
        # Live on oracle: T3 pushed "waiting" with no pending request in the same second
        # a scout finished, then "completed" with its report.
        self.assertEqual(classify(view("waiting", assistant="BOSS: done: report")).kind, "working")
        self.assertEqual(classify(view("waiting")).dedupe_key, None)
        self.assertEqual(classify(view("completed", assistant="BOSS: done: report")).kind, "done")

    def test_done_with_the_pr_it_names(self):
        event = classify(view(prs=[PR], assistant=f"120 tests pass.\nBOSS: done: {PR}"))
        self.assertEqual((event.kind, event.pr_urls, event.detail), ("done", (PR,), PR))
        self.assertEqual(event.dedupe_key, "thread-1:done:run-1")

    def test_done_with_a_report(self):
        event = classify(view(assistant="Found three callers.\nBOSS: done: three callers, all in cli.py"))
        self.assertEqual((event.kind, event.pr_urls, event.detail), ("done", (), "three callers, all in cli.py"))

    def test_blocked_and_paused_and_status_only_decisions(self):
        self.assertEqual(classify(view(assistant="BOSS: blocked: no GH token")).kind, "blocked")
        self.assertEqual(classify(view(assistant="BOSS: paused: PR checks")).detail, "PR checks")
        self.assertEqual(classify(view(assistant="BOSS: needs-decision: keep the flag?")).kind, "needs-decision")

    def test_a_pr_named_without_a_status_line_is_ended_unclear(self):
        # The status line is the signal; a PR link alone could sit above a plain-text question.
        self.assertEqual(classify(view(prs=[PR], assistant=f"Opened {PR}. Should I also bump the version?")).kind,
                         "ended_unclear")

    def test_an_earlier_turns_status_line_does_not_count(self):
        event = classify(view(run="run-2", assistant="BOSS: done: shipped", assistant_run="run-1"))
        self.assertEqual(event.kind, "ended_unclear")
        self.assertEqual(classify(view(run="run-2", assistant="BOSS: done: shipped", assistant_run="run-2")).kind, "done")

    def test_plain_text_question_is_ended_unclear_and_quoted(self):
        question = "Should I keep the old flag?\nIt is used by two scripts. " + "x" * 400
        event = classify(view(assistant=question))
        self.assertEqual((event.kind, event.dedupe_key), ("ended_unclear", "thread-1:ended_unclear:run-1"))
        self.assertEqual(event.detail, " ".join(question.split())[:QUOTE_CHARS])

    def test_turn_with_no_assistant_message_is_ended_unclear(self):
        event = classify(view(assistant=""))
        self.assertEqual((event.kind, event.detail), ("ended_unclear", ""))

    def test_idle_after_a_run_counts_as_ended(self):
        self.assertEqual(classify(view("idle", assistant="Done?")).kind, "ended_unclear")
        self.assertEqual(classify(view("idle", run=None)).kind, "working")

    def test_failed_and_interrupted_are_errors(self):
        for status in ("failed", "interrupted", "cancelled", "rolled_back"):
            with self.subTest(status=status):
                event = classify(view(status, prs=[PR], assistant=f"BOSS: done: {PR}"))
                self.assertEqual((event.kind, event.dedupe_key), ("error", "thread-1:error:run-1"))
                self.assertIn(status.replace("_", " "), event.detail)

    def test_running_threads_have_no_event(self):
        for status in ("preparing", "queued", "starting", "running"):
            with self.subTest(status=status):
                event = classify(view(status, assistant="BOSS: blocked: should I keep going?"))
                self.assertEqual((event.kind, event.dedupe_key), ("working", None))

    def test_one_event_per_turn_and_per_request_set(self):
        first, again = classify(view(assistant="Which one?")), classify(view(assistant="Which one?"))
        self.assertEqual(first.dedupe_key, again.dedupe_key)
        self.assertNotEqual(first.dedupe_key, classify(view(run="run-2", assistant="Which one?")).dedupe_key)
        self.assertNotEqual(classify(view("waiting", requests=["a"])).dedupe_key,
                            classify(view("waiting", requests=["a", "b"])).dedupe_key)

    def test_every_kind_is_covered(self):
        seen = {classify(v).kind for v in (
            view("waiting", requests=["r"]), view("waiting", approvals=1), view(assistant="BOSS: blocked: x"),
            view(assistant="BOSS: paused: x"), view(assistant="BOSS: done: x"), view(assistant="Hm?"),
            view("failed"), view("running"))}
        self.assertEqual(seen, set(KINDS))


class DescribeTests(unittest.TestCase):
    """The supervisor's line: title first, the payload, never an id or a robot instruction."""

    def line(self, v, questions=()):
        return describe("Fix the widget", classify(v), v["prs"], questions)

    def test_lines_carry_the_payload(self):
        self.assertEqual(self.line(view(prs=[PR], assistant=f"BOSS: done: {PR}")),
                         f'Fix the widget: finished, PR "Fix the widget" {PR}')
        self.assertEqual(self.line(view(assistant="BOSS: done: three callers")), "Fix the widget: finished: three callers")
        self.assertEqual(self.line(view("waiting", requests=["req-1"]),
                                   [{"id": "q1", "question": "Which API?", "options": [{"label": "REST"}, "GraphQL"]}]),
                         'Fix the widget: needs a decision: "Which API?" (options: REST / GraphQL)')
        self.assertEqual(self.line(view(assistant="BOSS: needs-decision: keep the flag?")),
                         'Fix the widget: needs a decision: "keep the flag?"')
        self.assertEqual(self.line(view(assistant="BOSS: blocked: no token")), "Fix the widget: blocked: no token")
        self.assertEqual(self.line(view(assistant="BOSS: paused: PR checks")), "Fix the widget: paused, waiting on PR checks")
        self.assertEqual(self.line(view("waiting", approvals=1)), "Fix the widget: waiting on a permission prompt in T3")
        self.assertEqual(self.line(view("failed")), "Fix the widget: stopped, the thread failed")
        self.assertEqual(self.line(view(assistant="Should I?")),
                         'Fix the widget: ended its turn without a status line; last words: "Should I?"')

    def test_no_ids_or_instructions(self):
        for v in (view(prs=[PR], assistant=f"BOSS: done: {PR}"), view("waiting", requests=["req-1"]),
                  view(assistant="hm"), view("failed"), view("waiting", approvals=2)):
            with self.subTest(kind=classify(v).kind):
                text = self.line(v)
                self.assertNotIn("thread-1", text)
                self.assertNotIn("req-1", text)
                self.assertNotIn("run-1", text)
                self.assertNotRegex(text, re.compile(r"boss status|Run |\b[0-9a-f]{12}\b"))


if __name__ == "__main__":
    unittest.main()


class PullRequestEventTests(unittest.TestCase):
    """A PR merged or closed outside boss (the captain on GitHub) is one plain line, once per PR."""

    def test_merged_and_closed_prs_each_make_one_event_open_ones_none(self):
        v = view(prs=[PR, PR + "0", PR + "1"])
        v["prs"][0]["state"], v["prs"][1]["state"] = "MERGED", "closed"
        events = pr_events(v)
        self.assertEqual([(e.kind, e.dedupe_key, e.turn_id) for e in events],
                         [("pr-merged", f"thread-1:pr-merged:{PR}", None), ("pr-closed", f"thread-1:pr-closed:{PR}0", None)])
        self.assertEqual([describe("Fix the widget", e) for e in events],
                         [f"Fix the widget: PR merged {PR}", f"Fix the widget: PR closed {PR}0"])
        self.assertEqual(pr_events(view(prs=[PR])), [])


class StallTests(unittest.TestCase):
    NOON = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc).timestamp()

    def at(self, text):
        return datetime(*map(int, re.findall(r"\d+", text)), tzinfo=timezone.utc).timestamp()

    def test_paused_until_reads_the_utc_time_the_worker_named(self):
        for detail, expected in (
                ("CI on the PR until 2026-10-26T14:30Z", "2026-10-26 14:30"),
                ("the deploy until 2026-10-27 09:05:30 UTC", "2026-10-27 09:05:30"),
                ("staging until 12:30 UTC", "2026-10-26 12:30"),
                ("the nightly run, until 11:00", "2026-10-27 11:00")):  # a time already past today is tomorrow's
            with self.subTest(detail=detail):
                self.assertEqual(paused_until(detail, self.NOON), self.at(expected))
        for detail in ("the captain's answer", "until later", "until 25:00 UTC", "until 2026-13-40T10:00Z", ""):
            with self.subTest(detail=detail):
                self.assertIsNone(paused_until(detail, self.NOON))

    def test_stall_text_names_the_quiet_time_and_the_tail_of_the_last_words(self):
        self.assertEqual(stall_text(25, "  Running the\nsuite  "), "no progress for 25 min (last: Running the suite)")
        self.assertEqual(stall_text(15, ""), "no progress for 15 min")
        long = "a" * 200 + " the end"
        self.assertEqual(stall_text(30, long), "no progress for 30 min (last: ..." + long[-117:] + ")")
        stalled = Event("stalled", "thread-1", "run-1", "thread-1:stalled:run-1:t", stall_text(25, "Running the suite"))
        self.assertEqual(describe("Fix the widget", stalled), "Fix the widget: no progress for 25 min (last: Running the suite)")
