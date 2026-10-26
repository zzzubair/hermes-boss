"""The owner's inbox on disk: hold once, drop stale, one line per task, ack once, idle-only busy mark."""
from datetime import datetime, timedelta, timezone
import json
import os
import re
import unittest
from unittest.mock import patch

from boss import inbox
from boss.common import BossError
from boss.inbox import NOTE, Inbox, owner_busy, render, turn_ended, turn_started
from tests.support import HermeticTestCase

PR = "https://github.com/acme/widget/pull/7"


class Ledger:
    """Task metadata the inbox reads for staleness; a missing task raises like Store.load."""

    def __init__(self, **tasks):
        self.tasks = tasks

    def load(self, task):
        if task not in self.tasks:
            raise BossError("usage", f"unknown task {task}", "boss status", 1)
        return self.tasks[task]


def meta(title="Fix the widget", **extra):
    return {"title": title, "observed": {"run_id": "run-1", "requests": [], "prs": []}, **extra}


class InboxTest(HermeticTestCase):
    def setUp(self):
        super().setUp()
        inbox._quiet.clear()
        self.box = Inbox(self.home)

    def hold(self, key, task="a1", kind="done", line="Fix the widget: finished: shipped", **extra):
        return self.box.hold(key, task, kind, line, run=extra.pop("run", "run-1"), **extra)

    def events(self, name):
        return [r for r in self.box.records() if r["event"] == name]

    def test_hold_is_deduped_by_key(self):
        self.assertIsNotNone(self.hold("k1"))
        self.assertIsNone(self.hold("k1"))
        self.assertEqual(len(self.box.open_items()), 1)

    def test_take_acks_once_and_only_when_sent(self):
        ledger = Ledger(a1=meta())
        self.hold("k1")
        self.assertIsNone(self.box.take(ledger.load, "inject", "Discord DM", send=lambda text: False))
        self.assertEqual(len(self.box.open_items()), 1)  # refused: still open
        text = self.box.take(ledger.load, "tool", "Discord DM")
        self.assertEqual(text, f"[boss inbox]\nFix the widget: finished: shipped\n{NOTE}")
        self.assertIsNone(self.box.take(ledger.load, "turn", "Discord DM"))
        self.assertEqual([(r["keys"], r["via"], r["owner"]) for r in self.events("acked")], [(["k1"], "tool", "Discord DM")])

    def test_one_line_per_task_newest_wins(self):
        ledger = Ledger(a1=meta(), b2=meta("Add panel"))
        self.hold("k1", line="Fix the widget: paused, waiting on CI")
        self.hold("k2", task="b2", line="Add panel: blocked: no token")
        self.hold("k3", line="Fix the widget: finished: shipped")
        text = self.box.take(ledger.load, "turn", "owner")
        self.assertEqual(text, f"[boss inbox]\nAdd panel: blocked: no token\nFix the widget: finished: shipped\n{NOTE}")
        self.assertEqual([(r["key"], r["reason"]) for r in self.events("dropped")],
                         [("k1", "superseded by a newer event for the same task")])
        self.assertEqual(self.events("acked")[0]["keys"], ["k2", "k3"])

    def test_stale_items_are_dropped_with_a_reason(self):
        cases = {
            "task settled": meta(settled=True),
            "PR landed": meta(landed={"url": PR}),
            "PR merged": meta(observed={"run_id": "run-1", "requests": [], "prs": [{"url": PR, "state": "MERGED"}]}),
            "PR closed": meta(observed={"run_id": "run-1", "requests": [], "prs": [{"url": PR, "state": "CLOSED"}]}),
            "question already answered": meta(),
            "already handled: boss replied to or reviewed the worker after this": meta(handled_at="9999-01-01T00:00:00+00:00"),
            "worker moved on to a newer turn": meta(observed={"run_id": "run-2", "requests": [], "prs": []}),
        }
        for reason, task in cases.items():
            with self.subTest(reason=reason):
                key = re.sub(r"\W+", "-", reason)
                extra = {"request_ids": ["req-1"]} if "answered" in reason else {"pr_urls": [PR]} if "PR" in reason else {}
                self.hold(key, **extra)
                self.assertIsNone(self.box.take(Ledger(a1=task).load, "turn", "owner"))
                self.assertEqual(self.events("dropped")[-1], {**self.events("dropped")[-1], "key": key, "reason": reason})
        self.hold("orphan", task="gone")
        self.assertIsNone(self.box.take(Ledger().load, "turn", "owner"))
        self.assertEqual(self.events("dropped")[-1]["reason"], "task is not in the ledger")
        self.assertEqual(self.box.open_items(), [])

    def test_live_items_are_kept(self):
        live = meta(observed={"run_id": "run-1", "requests": ["req-1"], "prs": [{"url": PR, "state": "OPEN"}]},
                    handled_at="2000-01-01T00:00:00+00:00")
        self.hold("k1", kind="needs-decision", request_ids=["req-1"], pr_urls=[PR])
        self.assertIsNotNone(self.box.take(Ledger(a1=live).load, "turn", "owner"))

    def test_render_is_plain_bounded_and_ends_with_the_note(self):
        items = [{"line": f"Task {n}: finished: " + "word " * 200} for n in range(10)]
        text = render(items)
        self.assertTrue(text.startswith("[boss inbox]\nTask 0: finished: word"))
        self.assertTrue(text.endswith("\n" + NOTE))
        self.assertLess(len(text), inbox.LIMIT + 100)
        self.assertNotRegex(text, r"boss status|\b[0-9a-f]{12}\b")

    def test_fast_path_skips_reads_until_the_file_changes(self):
        ledger = Ledger(a1=meta())
        self.assertFalse(self.box.maybe_open())
        self.hold("k1")
        self.assertTrue(self.box.maybe_open())
        self.box.take(ledger.load, "tool", "owner")
        self.assertTrue(self.box.maybe_open())  # the ack changed the file: one more look
        self.assertIsNone(self.box.take(ledger.load, "tool", "owner"))
        self.assertFalse(self.box.maybe_open())
        self.hold("k2", run="run-1")
        self.assertTrue(self.box.maybe_open())

    def test_pre_inbox_wake_log_is_migrated_without_losing_held_wakes(self):
        legacy = self.home / "wakes.jsonl"
        legacy.write_text("".join(json.dumps(r) + "\n" for r in (
            {"event": "held", "key": "k1", "task": "a1", "kind": "error", "text": "error: thread failed", "at": "2026-10-26T10:00:00+00:00"},
            {"event": "delivered", "keys": ["k1"], "owner": "x", "at": "2026-10-26T10:00:01+00:00"},
            {"event": "held", "key": "k2", "task": "a1", "kind": "ended_unclear", "text": "turn ended unclear",
             "at": "2026-10-26T10:00:02+00:00"})))
        self.assertTrue(self.box.maybe_open())
        text = self.box.take(Ledger(a1=meta()).load, "turn", "owner")
        self.assertEqual(text, f"[boss inbox]\nFix the widget: turn ended unclear\n{NOTE}")
        self.assertFalse(legacy.exists())


class TurnMarkTest(HermeticTestCase):
    def test_busy_only_for_the_owners_running_turn(self):
        self.assertFalse(owner_busy(self.home, "agent:owner"))
        turn_started(self.home, "agent:owner", "sess-1")
        self.assertTrue(owner_busy(self.home, "agent:owner"))
        self.assertFalse(owner_busy(self.home, "agent:phone"))
        self.assertFalse(turn_ended(self.home, session_id="sess-other"))
        self.assertTrue(owner_busy(self.home, "agent:owner"))
        self.assertTrue(turn_ended(self.home, session_id="sess-1"))
        self.assertFalse(owner_busy(self.home, "agent:owner"))

    def test_a_crashed_turn_does_not_block_idle_delivery_forever(self):
        turn_started(self.home, "agent:owner", "sess-1")
        record = json.loads((self.home / inbox.TURN).read_text())
        (self.home / inbox.TURN).write_text(json.dumps({**record, "pid": 2 ** 22 + 12345}))
        with patch("os.kill", side_effect=ProcessLookupError):
            self.assertFalse(owner_busy(self.home, "agent:owner"))
        old = (datetime.now(timezone.utc) - inbox.TURN_STALE - timedelta(minutes=1)).isoformat()
        (self.home / inbox.TURN).write_text(json.dumps({**record, "pid": os.getpid(), "at": old}))
        self.assertFalse(owner_busy(self.home, "agent:owner"))


if __name__ == "__main__":
    unittest.main()
