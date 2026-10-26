"""Panel runs: N writers on one brief, a blind judge, and the revise loop, driven by real pushed events."""
import json
import os
import re
from unittest.mock import patch

from boss import panel
from boss.events import Event
from boss.inbox import Inbox
from boss.service import Boss
from tests import test_boss as harness
from tests import test_wake as wake
from tests.support import HermeticTestCase

ROLES = '''
[workers.opus]
model = "claude:claude-opus-5-5"
[workers.sol]
model = "codex:gpt-test"
[workers.fable]
model = "codex:gpt-test"
[workers.scout]
kind = "scout"
model = "claude:claude-opus-5-5"
'''


def verdict(scores, words="Clear and useful."):
    body = {label: {"score": score, "reasons": words, "fixes": "Add a chart."} for label, score in scores.items()}
    return f"Draft notes.\n\n{json.dumps({'scores': body})}\nBOSS: done: scored {len(scores)} entries"


class PanelTest(HermeticTestCase):
    setUp_cli = harness.CLITest.setUp
    tearDown_cli = harness.CLITest.tearDown
    cli = harness.CLITest.cli
    config = wake.WakeTest.config
    own = wake.WakeTest.own
    listen = wake.WakeTest.listen
    send = wake.WakeTest.send

    def setUp(self):
        self.setUp_cli()
        self.addCleanup(self.tearDown_cli)
        path = self.home / "config.toml"
        path.write_text(path.read_text() + ROLES)
        self.fake.catalog["providers"].append({"providerInstanceId": "claude", "models": [
            {"id": "claude-opus-5-5", "options": [{"id": "effort", "values": ["high", "xhigh"]}]}]})
        self.push = wake.FakePush()
        self.addCleanup(self.push.close)
        os.environ["T3_EVENTS_TOKEN"] = wake.EVENTS_TOKEN
        streams = patch("boss.wake.endpoints", return_value=(
            self.push.url.replace("http", "ws", 1).replace("/mcp", "/ws"), self.push.url.replace("/mcp", wake.TICKET)))
        streams.start()
        self.addCleanup(streams.stop)
        # Reverse the label shuffle so the first role listed is not A: sol is A, opus is B.
        shuffle = patch("boss.panel.random.shuffle", side_effect=lambda labels: labels.reverse())
        shuffle.start()
        self.addCleanup(shuffle.stop)
        self.listeners, self.listener, self.sequence = [], None, 0
        self.addCleanup(lambda: [listener.stop() for listener in self.listeners] and wake._listeners.clear())

    def start(self, *flags, workers="opus,sol", brief="# Matchmaking post\nWrite the matchmaking post."):
        code, output = self.cli("panel", "repo", "--workers", workers, *flags, stdin=brief)
        self.assertEqual(code, 0, output)
        record = panel.load(self.store, output.split(":", 1)[0])
        self.ctx = wake.Context(self.home / "profile")
        self.own("agent:owner", "Discord DM")
        self.listen(self.ctx)
        return record

    def seat(self, record, label):
        return self.store.load(next(m["task"] for m in record["members"] if m["label"] == label))

    def launches(self):
        return [a for n, a in self.fake.calls if n == "t3_thread_launch"]

    def finish(self, meta, text, run="run-1", status="completed"):
        """The worker's turn ends in fake T3, then T3 pushes that thread's shell to the listener."""
        thread = self.fake.threads[meta["thread_id"]]
        thread.update(status=status, latestRunId=run,
                      items=[{"type": "assistant_message", "text": text, "itemId": "last-" + run, "runId": run,
                              "textTruncated": False}])
        self.sequence += 1
        self.send(wake.updated(wake.shell(meta["thread_id"], status, run), self.sequence))

    def judge(self, record, round_number):
        record = panel.load(self.store, record["id"])
        return self.store.load(next(j["task"] for j in record["judges"] if j["round"] == round_number))

    def wakes(self):
        """Every line held for the owner, in order; delivery (boss watch, tool results) is tested elsewhere."""
        return [r["line"] for r in Inbox(self.home).records() if r["event"] == "held"]

    def test_writers_then_blind_judge_then_revise_until_target(self):
        record = self.start("--rounds", "3", "--target", "9", "--title", "Matchmaking post")
        launches = self.launches()
        self.assertEqual([a["title"].split("] ", 1)[1] for a in launches],
                         ["Matchmaking post (sol)", "Matchmaking post (opus)"])
        for launch in launches:
            self.assertIn("Write the matchmaking post.", launch["message"])
            self.assertIn("one of 2 writers working on this brief independently", launch["message"])
            self.assertIn("Never name yourself, your model", launch["message"])
            self.assertIn("Do not open a PR", launch["message"])  # writers default to scouts
        self.assertEqual([launch["modelSelection"]["model"] for launch in launches], ["gpt-test", "claude-opus-5-5"])
        a, b = self.seat(record, "A"), self.seat(record, "B")
        self.assertEqual((a["worker"], b["worker"], a["panel"]), ("sol", "opus", record["id"]))

        # The first writer to finish wakes no one and starts nothing.
        self.finish(a, "Matchmaking works like this, as a codex agent running GPT-test sees it.\nBOSS: done: draft ready")
        self.assertEqual((len(self.launches()), self.wakes()), (2, []))
        # The last one starts a judge that sees labels only, with model names scrubbed.
        self.finish(b, "Written by Claude (claude-opus-5-5). Opus thinks matchmaking is fair.\nBOSS: done: draft ready")
        self.assertEqual(self.wakes(), [])
        judge = self.launches()[2]
        self.assertEqual(judge["title"].split("] ", 1)[1], "Matchmaking post: judge round 1")
        self.assertEqual(judge["modelSelection"]["model"], "claude-opus-5-5")  # the scout role
        entries = judge["message"].split("# Brief", 1)[1].split("### boss worker contract", 1)[0]
        self.assertIn("# Entry A\n\nMatchmaking works like this, as a [name hidden] agent running [name hidden] sees it.", entries)
        self.assertIn("# Entry B\n\nWritten by [name hidden] ([name hidden]). [name hidden] thinks matchmaking is fair.", entries)
        for name in ("sol", "opus", "claude", "codex", "gpt"):
            self.assertIsNone(re.search(rf"\b{name}\b", entries, re.I), name)
        self.assertIn("blind judge", judge["message"])
        self.assertIn('"A": {"score": 0.0', judge["message"])
        self.assertIn("Do not open a PR", judge["message"])

        # Round 1 scored: one plain wake, and the best entry goes back to its writer with the notes.
        self.finish(self.judge(record, 1), verdict({"A": 7.5, "B": 8}, "Strong opening, thin on numbers."))
        self.assertEqual(self.wakes(), ['Matchmaking post: panel round 1 of 3: B (opus) best at 8/10, below the 9 target; '
                                        'B is revising. Scores: A (sol) 7.5, B (opus) 8. Judge: "Strong opening, thin on numbers."'])
        sends = [args for name, args in self.fake.calls if name == "t3_thread_send"]
        self.assertEqual([s["threadId"] for s in sends], [b["thread_id"]])
        self.assertTrue(sends[0]["message"].startswith("[boss] A blind judge scored your entry 8/10."))
        self.assertIn("What would raise the score: Add a chart.", sends[0]["message"])
        self.assertNotIn("7.5", sends[0]["message"])

        # The revision is judged alone by a fresh judge with the same rubric.
        self.finish(b, "Revised: matchmaking, now with numbers.\nBOSS: done: revised", run="run-2")
        second = self.launches()[3]
        self.assertEqual(second["title"].split("] ", 1)[1], "Matchmaking post: judge round 2")
        self.assertIn("# Entry B\n\nRevised: matchmaking, now with numbers.", second["message"])
        self.assertNotIn("# Entry A", second["message"])
        self.assertIn("blind judge. 1 entry was written independently", second["message"])
        for frozen in ("You are not a writer: do not rewrite the entries.", "Every entry label must appear"):
            self.assertIn(frozen, judge["message"])
            self.assertIn(frozen, second["message"])
        self.assertEqual(len(self.wakes()), 1)
        self.finish(self.judge(record, 2), verdict({"B": 9.2}, "Ready to publish."))
        self.assertEqual(self.wakes()[1], 'Matchmaking post: panel round 2 of 3: done: B (opus) scored 9.2/10, '
                                          'reaching the 9 target. Judge: "Ready to publish."')
        for line in self.wakes():
            for meta in (a, b):
                self.assertNotIn(meta["id"], line)
        record = panel.load(self.store, record["id"])
        self.assertEqual((record["stage"], [r["best"] for r in record["results"]]), ("done", ["B", "B"]))

        # A restarted listener replays every thread: nothing launches, sends or wakes again.
        self.listener.stop()
        calls = len(self.fake.calls)
        self.listen(self.ctx)
        self.assertTrue(self.push.connected(2))  # the new listener's own subscription, not the old one
        threads = [wake.shell(t["threadId"], t["status"], t["latestRunId"]) for t in self.fake.threads.values()]
        self.send(wake.snapshot(*threads))
        names = [n for n, _ in self.fake.calls[calls:]]
        self.assertNotIn("t3_thread_launch", names)
        self.assertNotIn("t3_thread_send", names)
        self.assertEqual(len(self.wakes()), 2)

    def test_round_cap_stops_short_of_the_target(self):
        record = self.start("--rounds", "2", "--target", "9.5", "--title", "Post")
        self.finish(self.seat(record, "A"), "Entry one.\nBOSS: done: ok")
        self.finish(self.seat(record, "B"), "Entry two.\nBOSS: done: ok")
        self.finish(self.judge(record, 1), verdict({"A": 8.5, "B": 8}))
        self.assertIn("A (sol) best at 8.5/10, below the 9.5 target; A is revising", self.wakes()[0])
        self.finish(self.seat(record, "A"), "Entry one, better.\nBOSS: done: ok", run="run-2")
        self.finish(self.judge(record, 2), verdict({"A": 9}))
        self.assertEqual(self.wakes()[1], 'Post: panel round 2 of 2: done at the round cap: best A (sol) 9/10, '
                                          'short of the 9.5 target. Judge: "Clear and useful."')
        self.assertEqual(len(self.launches()), 4)

    def test_one_round_without_target_judges_once(self):
        record = self.start(workers="opus,sol,fable")
        labels = {m["label"]: m["worker"] for m in record["members"]}
        self.assertEqual(labels, {"A": "fable", "B": "sol", "C": "opus"})
        self.assertEqual(record["title"], "Matchmaking post")  # the brief's first line
        for label in "ABC":
            self.finish(self.seat(record, label), f"Entry {label.lower()}.\nBOSS: done: ok")
        self.finish(self.judge(record, 1), verdict({"A": 6, "B": 8, "C": 8}))
        # Ties go to the earlier label; no revision without --rounds.
        self.assertEqual(self.wakes(), ['Matchmaking post: panel judged: done: best B (sol) 8/10. '
                                        'Scores: A (fable) 6, B (sol) 8, C (opus) 8. Judge: "Clear and useful."'])
        self.assertFalse(any(n == "t3_thread_send" for n, _ in self.fake.calls))

    def test_unreadable_scores_stop_the_panel_in_plain_words(self):
        record = self.start("--rounds", "2")
        self.finish(self.seat(record, "A"), "Entry.\nBOSS: done: ok")
        self.finish(self.seat(record, "B"), "Entry.\nBOSS: done: ok")
        self.finish(self.judge(record, 1), verdict({"A": 7}))  # B is missing
        self.assertEqual(self.wakes(), ["Matchmaking post: panel stopped, the round 1 judge's scores could not be read."])
        self.assertEqual(panel.load(self.store, record["id"])["stage"], "stopped")

    def test_a_stopped_writer_surfaces_and_the_rest_are_judged(self):
        record = self.start()
        self.finish(self.seat(record, "A"), "", status="failed")
        self.assertEqual(self.wakes(), ["Matchmaking post (sol): stopped, the thread failed"])
        self.finish(self.seat(record, "B"), "Entry.\nBOSS: done: ok")
        judge = self.launches()[2]["message"]
        self.assertIn("# Entry B", judge)
        self.assertNotIn("# Entry A", judge)
        self.finish(self.judge(record, 1), verdict({"B": 7}))
        self.assertEqual(self.wakes()[1], 'Matchmaking post: panel judged: done: best B (opus) 7/10. '
                                          'Stopped without an entry: A (sol). Judge: "Clear and useful."')

    def test_writer_questions_reach_you_and_do_not_advance(self):
        record = self.start()
        self.finish(self.seat(record, "A"), "Which season?\nBOSS: needs-decision: which season?")
        self.assertEqual(self.wakes(), ["Matchmaking post (sol): needs a decision: \"which season?\""])
        self.finish(self.seat(record, "B"), "Entry.\nBOSS: done: ok")
        self.assertEqual(len(self.launches()), 2)  # still waiting on A's entry


class PanelGuardTest(HermeticTestCase):
    setUp_cli = harness.CLITest.setUp
    tearDown_cli = harness.CLITest.tearDown
    cli = harness.CLITest.cli

    def setUp(self):
        self.setUp_cli()
        self.addCleanup(self.tearDown_cli)
        path = self.home / "config.toml"
        path.write_text(path.read_text() + ROLES)
        self.fake.catalog["providers"].append({"providerInstanceId": "claude", "models": [
            {"id": "claude-opus-5-5", "options": []}]})

    def panel(self, *flags, workers="opus,sol"):
        return self.cli("panel", "repo", "--workers", workers, *flags, stdin="Write it.")

    def launched(self):
        return [n for n, _ in self.fake.calls if n == "t3_thread_launch"]

    def test_usage_is_refused_before_anything_launches(self):
        for flags, workers in (((), "opus,ghost"), (("--rounds", "0"), "opus"), (("--target", "11"), "opus"),
                               ((), " , "), (("--judge", "ghost"), "opus")):
            with self.subTest(flags=flags, workers=workers):
                code, output = self.panel(*flags, workers=workers)
                self.assertEqual(code, 1, output)
        self.assertEqual(self.launched(), [])
        self.assertEqual(self.cli("panel", "repo", stdin="x")[0], 1)  # --workers is required

    def test_a_seat_breaking_a_worker_rule_refuses_the_whole_panel(self):
        path = self.home / "config.toml"
        path.write_text(path.read_text().replace('delivery = "direct-PR"\n', 'delivery = "direct-PR"\n'
                                                 '[projects.repo.rules]\nscout_models = ["claude:claude-opus-5-5"]\n', 1))
        code, output = self.panel()
        self.assertEqual(code, 2, output)
        self.assertIn("panel seat sol breaks a worker rule", output)
        self.assertEqual(self.launched(), [])
        code, output = self.panel(workers="opus")
        self.assertEqual(code, 0, output)
        self.assertEqual(len(self.launched()), 1)

    def test_writers_that_would_pass_the_worker_cap_refuse_the_whole_panel(self):
        path = self.home / "config.toml"
        path.write_text(path.read_text().replace("[workers.opus]", "[workers]\nmax_active = 2\n[workers.opus]", 1))
        code, output = self.panel(workers="opus,sol,fable")
        self.assertEqual(code, 2, output)
        self.assertIn("3 more would pass the cap workers.max_active = 2", output)
        self.assertEqual(self.launched(), [])
        self.assertEqual(self.panel()[0], 0)

    def test_ship_writers_are_told_their_pr_is_the_entry(self):
        code, output = self.panel("--kind", "ship")
        self.assertEqual(code, 0, output)
        for name, args in self.fake.calls:
            if name == "t3_thread_launch":
                self.assertIn("say what you built and how you verified it, with your PR url", args["message"])
                self.assertIn("exactly ONE PR", args["message"])

    def test_steers_need_fits_before_any_writer_launches(self):
        self.assertEqual(self.cli("steer", "repo", "Keep it short")[0], 0)
        code, output = self.panel()
        self.assertEqual(code, 2, output)
        self.assertEqual(self.launched(), [])
        code, output = self.panel("--fits", "A post the goal needs")
        self.assertEqual(code, 0, output)
        self.assertEqual(len(self.launched()), 2)
        record = panel.load(self.store, output.split(":", 1)[0])
        self.assertEqual(record["fits"], "A post the goal needs")
        self.assertRegex(output, r"next: the judge starts when every writer finishes")

    def test_a_writer_that_cannot_launch_stops_the_panel_and_names_the_launched_ones(self):
        real = Boss.spawn
        count = []

        def flaky(boss, *args, **kwargs):
            count.append(1)
            if len(count) == 2:
                raise panel.BossError("t3", "launch refused")
            return real(boss, *args, **kwargs)
        with patch.object(Boss, "spawn", flaky):
            code, output = self.panel()
        self.assertEqual(code, 3, output)
        self.assertRegex(output, r"stopped: writer (opus|sol): launch refused")
        first = self.store.tasks()[0]["id"]
        self.assertIn(first, output)
        record = panel.load(self.store, re.search(r"panel (\w+) stopped", output)[1])
        self.assertEqual(record["stage"], "stopped")


class PanelUnitTest(HermeticTestCase):
    def test_scores_take_the_last_complete_object_with_every_label(self):
        text = 'Example {"scores": {"A": {"score": 1}}}\nFinal {"scores": {"A": {"score": 9, "reasons": "a\\n b"}, "B": 4}}'
        self.assertEqual(panel.scores(text, ["A", "B"]),
                         {"A": {"score": 9.0, "reasons": "a b", "fixes": ""}, "B": {"score": 4.0, "reasons": "", "fixes": ""}})
        for bad in ('{"scores": {"A": {"score": 11}, "B": 1}}', '{"scores": {"A": {"score": true}, "B": 1}}',
                    '{"scores": {"A": 1, "B": 2, "C": 3}}', "no json here", '{"scores": {"A": "high", "B": 1}}'):
            self.assertIsNone(panel.scores(bad, ["A", "B"]), bad)

    def test_hidden_terms_come_from_the_model_selection_only(self):
        self.assertEqual(panel.hidden_terms({"instanceId": "claudeAgent", "model": "claude-opus-5-5"}),
                         ["claude-opus-5-5", "claudeAgent", "claude", "opus"])
        self.assertEqual(panel.hidden_terms(None), [])
        self.assertEqual(panel.scrub("Claude-Opus-5-5 and opus, not opuses.", ["claude-opus-5-5", "opus"]),
                         "[name hidden] and [name hidden], not opuses.")

    def test_advance_ignores_tasks_and_events_outside_the_current_stage(self):
        store = harness.Store(self.home, harness.Redactor())
        record = {"id": "p1", "stage": "done", "members": [], "judges": [], "consumed": []}
        panel.save(store, record)
        boss = type("B", (), {"store": store})()
        event = Event("done", "t", "run-1", "t:done:run-1")
        self.assertEqual(panel.advance(boss, {"id": "x", "panel": "p1"}, event), (False, None))
        record.update(stage="writing", members=[{"label": "A", "task": "x", "entry_run": "run-1", "out": False,
                                                 "worker": "opus", "hidden": []}])
        panel.save(store, record)
        # The same finished turn again is not a new entry.
        self.assertEqual(panel.advance(boss, {"id": "x", "panel": "p1"}, event), (False, None))
        self.assertEqual(Inbox(self.home).open_items(), [])
