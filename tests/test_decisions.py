"""Approve once, numbered answers, hard fences, the PR body gate and the worker cap."""
import unittest

from boss import user
from boss.service import pr_body_problems
from tests import test_boss as harness


class DecisionsTest(unittest.TestCase):
    setUp = harness.CLITest.setUp
    tearDown = harness.CLITest.tearDown
    cli = harness.CLITest.cli
    spawn = harness.CLITest.spawn
    completed = harness.CLITest.completed

    def ok(self, *args):
        code, output = self.cli(*args)
        self.assertEqual(code, 0, output)
        return output

    def refused(self, *args):
        code, output = self.cli(*args)
        self.assertEqual(code, 2, output)
        return output

    def decision(self, text, *extra):
        return self.ok("decide", "add", text, *extra).split(":", 1)[0]

    def decisions(self):
        return {d["id"]: d for d in self.store.decisions()}

    def ready(self, title="Fix login"):
        meta = self.spawn("--title", title)
        self.completed(meta)
        self.fake.prs = [harness.URL]
        return meta

    # Approve once ----------------------------------------------------------------

    def test_approved_merge_is_never_asked_again_and_lands_on_his_answer(self):
        meta = self.ready()
        merge = self.decision("Merge the login fix?", "--task", meta["id"], "--action", "merge")
        self.ok("decide", "ask")
        self.assertEqual(self.ok("decide", "answer", "1 yes").splitlines()[0], "closed: 1")
        self.assertEqual(self.decisions()[merge]["verdict"], "yes")
        # Asking again about the same merge, by subject or by the same words, is refused.
        for argv in (("Merge it now?", "--task", meta["id"], "--action", "merge"), ("merge the login fix?",)):
            self.assertIn("never asked again", self.refused("decide", "add", *argv))
        self.assertEqual(len(self.store.decisions()), 1)
        self.ok("land", meta["id"])  # no --approved: his answer already counts
        self.assertEqual(len(self.gh.merges), 1)
        self.assertEqual(self.store.load(meta["id"])["landed"]["approved"], "yes")
        self.assertIn("already landed", self.refused("decide", "add", "Merge?", "--task", meta["id"], "--action", "merge"))
        self.decision("Pick storage")
        output = self.ok("decide", "ask")
        self.assertNotIn("login", output.lower())

    def test_approval_in_an_away_checkin_counts_and_is_not_reasked(self):
        meta = self.ready()
        self.ok("away", "on my phone")
        self.ok("log", "yeah merge the login fix when it's green", "--task", meta["id"], "--approves", "merge")
        self.assertIn("never asked again", self.refused("decide", "add", "Merge login?", "--task", meta["id"], "--action", "merge"))
        self.ok("land", meta["id"])
        self.assertEqual(self.store.load(meta["id"])["landed"]["approved"], "yeah merge the login fix when it's green")
        self.ok("back")
        output = self.ok("back", "--full")
        self.assertIn("decided: merge Fix login -> yeah merge the login fix when it's green", output)
        self.assertIn("0 open decisions", output)

    def test_checkin_approval_closes_an_open_decision_and_a_no_blocks_landing(self):
        meta = self.ready()
        demo = self.decision("Accept the login demo?", "--task", meta["id"], "--action", "demo")
        merge = self.decision("Merge the login fix?", "--task", meta["id"], "--action", "merge")
        self.assertIn("already open; not added twice", self.ok("decide", "add", "Merge again?", "--task", meta["id"], "--action", "merge"))
        self.ok("decide", "ask")
        self.ok("decide", "answer", "1 yes 2 no")
        self.assertEqual((self.decisions()[demo]["verdict"], self.decisions()[merge]["verdict"]), ("yes", "no"))
        self.assertIn("the answer was: no", self.refused("land", meta["id"]))
        self.assertEqual(self.gh.merges, [])
        # He changes his mind in a check-in: his newer words win.
        self.ok("log", "ok fine merge it", "--task", meta["id"], "--approves", "merge")
        self.ok("land", meta["id"])
        self.assertEqual(self.store.load(meta["id"])["landed"]["approved"], "ok fine merge it")
        self.assertEqual(self.cli("log", "merge", "--approves", "merge")[0], 1)

    # Numbered answers ------------------------------------------------------------

    def test_numbered_reply_closes_answered_and_reasks_the_rest_with_more_detail(self):
        texts = ["Pick storage", "Rename CLI", "Drop Python 3.10", "Add Windows", "Move docs", "Ship beta"]
        ids = [self.decision(t) for t in texts]
        self.ok("decide", "ask")
        output = self.ok("decide", "answer", "1 yes 3 no 5 more info")
        self.assertEqual(output.splitlines()[:3], ["closed: 1, 3", "unclear: 5", "skipped, asked again next round: 2, 4, 6"])
        records = self.decisions()
        self.assertEqual([records[i]["resolution"] for i in ids[:3:2]], ["yes", "no"])
        self.assertFalse(any(records[i]["closed"] for i in ids[1::2] + [ids[4]]))
        self.assertIn("Move docs", self.refused("decide", "ask"))
        self.ok("decide", "detail", ids[4], "Moves the docs folder to the wiki; the README stays")
        output = self.ok("decide", "ask")
        self.assertIn("4 open decisions (round 2", output)
        self.assertIn("1. Rename CLI (asked again)\n2. Add Windows (asked again)\n"
                      "3. Move docs (asked again; unclear last time: more info)\n"
                      "   more: Moves the docs folder to the wiki; the README stays\n4. Ship beta (asked again)", output)
        for text in ("Pick storage", "Drop Python"):
            self.assertNotIn(text, output)
        # Answered numbers in a later reply never reopen anything.
        output = self.ok("decide", "answer", "1. Wasnt this done already? if not, Yes. / 2. Yes / 3. Do it / 4. More info")
        self.assertEqual(output.splitlines()[:2], ["closed: 1, 2, 3", "unclear: 4"])
        self.assertEqual(self.decisions()[ids[1]]["resolution"], "Wasnt this done already? if not, Yes.")
        self.ok("decide", "detail", "4", "Turns on the beta flag for everyone")
        output = self.ok("decide", "ask")
        self.assertIn("1 open decision (round 3", output)
        self.assertIn("1. Ship beta (asked again; unclear last time: More info)", output)

    def test_reasked_decision_names_its_task(self):
        meta = self.spawn("--title", "Fix login")
        self.decision("Ship it?", "--task", meta["id"])
        first = self.ok("decide", "ask")
        self.assertNotIn("about:", first)
        self.assertIn("   about: Fix login (repo)", self.ok("decide", "ask"))
        self.assertNotIn(meta["id"], self.ok("decide", "ask"))

    def test_answer_needs_numbers_from_the_latest_round(self):
        self.decision("Pick storage")
        self.refused("decide", "answer", "1 yes")
        self.ok("decide", "ask")
        self.ok("decide", "ask")
        self.assertIn("round 1 is old", self.refused("decide", "answer", "1 yes", "--round", "1"))
        self.assertIn("no numbered answers", self.refused("decide", "answer", "I like A"))
        self.assertIn("already answered: 1", self.ok("decide", "answer", "1 yes", "--round", "2") + self.ok("decide", "answer", "1 no"))
        self.assertEqual(next(iter(self.decisions().values()))["resolution"], "yes")

    def test_parse_answers_reads_increasing_numbers_in_the_round(self):
        self.assertEqual(user.parse_answers("1 yes 3 no 5 more info", 5), {1: "yes", 3: "no", 5: "more info"})
        self.assertEqual(user.parse_answers("1) yes, 2: no; 3.later", 3), {1: "yes", 2: "no", 3: "later"})
        # Numbers outside the round or going backwards stay part of the words.
        self.assertEqual(user.parse_answers("1 deploy at 9 tonight 2 no", 2), {1: "deploy at 9 tonight", 2: "no"})
        self.assertEqual(user.parse_answers("2 yes 1 no", 2), {2: "yes 1 no"})
        self.assertEqual(user.parse_answers("I like A", 3), {})
        for words, expected in (("yes", "yes"), ("dont deploy yet", "no"), ("I like A", None), ("go ahead", "yes")):
            self.assertEqual(user.verdict(words), expected, words)
        for words in ("more info", "5. More info needed here", "what is this?", "confused"):
            self.assertTrue(user.UNCLEAR.search(words), words)
        self.assertFalse(user.UNCLEAR.search("Wasnt this done already? if not, Yes."))

    # Fences ----------------------------------------------------------------------

    def test_fence_on_one_task_holds_its_land_until_dropped(self):
        meta = self.ready()
        steer = self.ok("steer", "repo", "don't merge the login fix until I say", "--task", meta["id"], "--fence")
        self.assertIn("fence recorded", steer)
        output = self.refused("land", meta["id"], "--approved", "merge it")
        self.assertIn("fenced", output)
        self.assertIn("don't merge the login fix until I say", output)
        self.assertEqual(self.gh.merges, [])
        self.assertIn("active fence", self.ok("steer", "list", "repo"))
        self.ok("steer", "drop", "repo", steer.split(":", 1)[0])
        self.ok("land", meta["id"], "--approved", "merge it")
        self.assertEqual(len(self.gh.merges), 1)

    def test_project_freeze_holds_every_task_and_approvals_do_not_lift_it(self):
        other = self.spawn("--title", "Other work")
        meta = self.ready()
        self.ok("away")
        self.ok("steer", "repo", "stop merging until I say", "--fence")
        self.ok("log", "merge the login fix", "--task", meta["id"], "--approves", "merge")
        self.refused("land", meta["id"])
        self.gh.pr["headRefName"] = other["branch"]
        self.completed(other)
        self.refused("land", other["id"], "--approved", "merge it")
        self.assertEqual(self.gh.merges, [])
        self.ok("back")
        self.assertIn("fence (repo): stop merging until I say", self.ok("back", "--full"))
        for argv in (("steer", "drop", "repo", "x", "--fence"), ("steer", "list", "repo", "--fence")):
            self.assertEqual(self.cli(*argv)[0], 1)

    # PR body gate ----------------------------------------------------------------

    def test_land_refuses_a_malformed_pr_body(self):
        meta = self.ready()
        self.gh.pr["body"] = "Fixed the login like the captain asked in chat."
        output = self.refused("land", meta["id"], "--approved", "merge it")
        self.assertIn("pr-description", output)
        self.assertIn("'## Testing'", output)
        self.assertIn("mentions 'captain'", output)
        self.assertEqual(self.gh.merges, [])
        self.gh.pr["body"] = harness.BODY
        self.ok("land", meta["id"], "--approved", "merge it")

    def test_project_can_narrow_the_forbidden_words(self):
        with (self.home / "config.toml").open("a") as config:
            config.write('pr_body_forbidden = ["firstmate"]\n')  # appended under [projects.repo]
        meta = self.ready()
        self.gh.pr["body"] = harness.BODY.replace("Fixes the thing", "Lets the captain approve once")
        self.ok("land", meta["id"], "--approved", "merge it")
        self.assertEqual(len(self.gh.merges), 1)

    def test_pr_body_shape(self):
        good, forbidden = harness.BODY, ["captain", "firstmate", "first mate", "chat"]
        self.assertEqual(pr_body_problems(good, forbidden), [])
        # File names and commands in code may use any word.
        self.assertEqual(pr_body_problems(good.replace("`fix.py`", "`boss/user.py`") + "\n```\nchat ok\n```\n", forbidden), [])
        appendix = good.replace("## Risk Assessment", "<!-- no-mistakes-pr-appendix:v1 -->\n### Risk Assessment")
        self.assertEqual(pr_body_problems(appendix, forbidden), [])
        cases = {
            "missing": (good.replace("## Testing", "## Tests"), "'## Testing' section, found 0"),
            "twice": (good + "\n## Intent\n", "'## Intent' section, found 2"),
            "order": (good.replace("## What changed", "## X").replace("## Risk Assessment", "## What changed")
                      .replace("## X", "## Risk Assessment"), "must come in order"),
            "attribution": (good.replace("Written by", "Made by"), "Intent must end with"),
            "attribution last": (good.replace("@owner\n", "@owner\n\nMore words.\n"), "Intent must end with"),
            "intent empty": (good.replace("Fixes the thing so it works.\n", ""), "needs a sentence"),
            "first mate": (good.replace("one function", "per First  Mate"), "mentions 'first mate'"),
            "chat": (good.replace("one function", "as discussed in chat"), "mentions 'chat'"),
        }
        for name, (body, problem) in cases.items():
            self.assertTrue(any(problem in p for p in pr_body_problems(body, forbidden)), (name, pr_body_problems(body, forbidden)))
        self.assertEqual(pr_body_problems(good.replace("one function", "chatty logs"), forbidden), [])

    # Worker cap ------------------------------------------------------------------

    def test_spawn_past_the_default_cap_of_15_is_refused_naming_it(self):
        for number in range(15):
            self.ok("spawn", "repo", "--title", f"Task {number}")
        output = self.refused("spawn", "repo", "--title", "Task 16")
        self.assertIn("15 workers are active and the cap is workers.max_active = 15", output)
        self.assertEqual(len(self.store.tasks()), 15)
        # Settled workers free their slot.
        meta = self.store.tasks()[0]
        meta["settled"] = True
        self.store.save(meta, "settled")
        self.ok("spawn", "repo", "--title", "Task 16")

    def test_cap_is_one_config_edit_and_roles_still_load(self):
        self.ok("config", "set", "workers.max_active", "1")
        self.ok("config", "set", "workers.scout.kind", "scout")
        self.ok("spawn", "repo", "--title", "One")
        self.assertIn("cap is workers.max_active = 1", self.refused("spawn", "repo", "--worker", "scout", "--title", "Two"))
        self.assertIn('"max_active" = 1', (self.home / "config.toml").read_text())
        for value in ("0", '"many"', "true"):
            code, output = self.cli("config", "set", "workers.max_active", value)
            self.assertEqual(code, 1, output)
            self.assertIn("workers.max_active: must be a positive integer", output)
        # The cap sits beside the role tables but is never a role.
        self.ok("config", "set", "default_worker", "scout")
        code, output = self.cli("config", "set", "default_worker", "max_active")
        self.assertEqual(code, 1, output)
        self.assertIn("unknown role; available: scout", output)


if __name__ == "__main__":
    unittest.main()
