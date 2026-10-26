"""boss adopt tracks a PR opened outside boss; boss park holds a task for the captain."""
from boss.common import Redactor
from boss.config import Config
from boss.inbox import Inbox
from boss.wake import Listener
from tests import test_boss as harness
from tests import test_wake as wake
from tests.support import HermeticTestCase

URL = harness.URL


class AdoptParkTest(HermeticTestCase):
    setUp_cli = harness.CLITest.setUp
    tearDown_cli = harness.CLITest.tearDown
    cli = harness.CLITest.cli
    spawn = harness.CLITest.spawn
    completed = harness.CLITest.completed

    def setUp(self):
        self.setUp_cli()
        self.addCleanup(self.tearDown_cli)
        self.gh.pr.update(headRefName="fm/old-work", title="Old Firstmate work")

    def ok(self, *args):
        code, output = self.cli(*args)
        self.assertEqual(code, 0, output)
        return output

    def refused(self, *args):
        code, output = self.cli(*args)
        self.assertEqual(code, 2, output)
        return output

    def adopt(self, *extra):
        output = self.ok("adopt", "repo", "--pr", URL, *extra)
        return self.store.load(output.split(":", 1)[0])

    def row(self, title):
        return next(line for line in self.ok().splitlines() if line.startswith(f"| {title} |"))

    def listener(self):
        return Listener(wake.Context(self.home / "profile"), Config(self.home, Redactor(), hermes=self.home / "profile"),
                        self.home)

    def t3_calls(self):
        return [name for name, _ in self.fake.calls if name != "t3_project_list"]

    # adopt -------------------------------------------------------------------

    def test_an_adopted_pr_shows_in_the_fleet_and_status_with_no_worker(self):
        meta = self.adopt()
        self.assertEqual((meta["title"], meta["branch"], meta["base"], meta["thread_id"]),
                         ("Old Firstmate work", "fm/old-work", "trunk", None))
        self.assertEqual(self.t3_calls(), [])  # no thread launched, nothing asked of T3 but the project
        row = self.row("Old Firstmate work")
        self.assertIn("| PR opened outside boss |", row)
        self.assertIn(f"{URL} CI green", row)
        self.assertTrue(row.endswith("| review, then land |"), row)
        self.assertNotIn(meta["id"], row)
        status = self.ok("status", meta["id"])
        self.assertIn("worker: none; PR opened outside boss", status)
        self.assertNotIn("thread:", status)
        self.assertIn(f'next: boss land {meta["id"]} --approved "<captain quote>"', status)
        self.gh.pr["statusCheckRollup"] = [{"conclusion": "FAILURE"}]
        self.assertTrue(self.row("Old Firstmate work").endswith("| fix its CI outside boss |"))
        self.assertIn("next: wait for its checks, or fix the PR outside boss", self.ok("status", meta["id"]))

    def test_adopt_takes_a_title_and_never_counts_against_the_worker_cap(self):
        config = self.home / "config.toml"
        config.write_text("[workers]\nmax_active = 1\n" + config.read_text())
        meta = self.adopt("--title", "Login fix from last week")
        self.assertEqual(meta["title"], "Login fix from last week")
        self.spawn()

    def test_adopt_refuses_bad_urls_other_repos_closed_prs_and_a_pr_already_tracked(self):
        self.assertEqual(self.cli("adopt", "repo", "--pr", "https://example.com/pull/7")[0], 1)
        self.assertIn("not repo's repository",
                      self.refused("adopt", "repo", "--pr", "https://github.com/other/repo/pull/7"))
        self.gh.pr["state"] = "MERGED"
        self.assertIn("only an open PR", self.refused("adopt", "repo", "--pr", URL))
        self.gh.pr["state"] = "OPEN"
        meta = self.adopt()
        self.assertIn(f"task {meta['id']} already tracks that PR", self.refused("adopt", "repo", "--pr", URL))
        self.assertEqual(len(self.store.tasks()), 1)

    def test_an_adopted_task_has_no_worker_to_message(self):
        meta = self.adopt()
        for argv in (("send", meta["id"], "hi"), ("stop", meta["id"]), ("answer", meta["id"], "r", "yes")):
            self.assertIn("no worker", self.refused(*argv))
        self.assertEqual(self.t3_calls(), [])

    def test_land_merges_an_adopted_pr_only_with_his_words_and_never_through_a_fence(self):
        meta = self.adopt()
        self.assertIn("captain's words", self.refused("land", meta["id"]))
        fence = self.ok("steer", "repo", "hold the old PRs", "--task", meta["id"], "--fence").split(":", 1)[0]
        self.assertIn("fenced", self.refused("land", meta["id"], "--approved", "merge it"))
        self.ok("steer", "drop", "repo", fence)
        self.ok("land", meta["id"], "--approved", "merge it")
        self.assertEqual(len(self.gh.merges), 1)
        self.assertEqual(self.store.load(meta["id"])["landed"]["approved"], "merge it")

    def test_settle_of_an_adopted_task_never_touches_a_worktree_or_thread(self):
        meta = self.adopt()
        self.assertIn("open PR", self.refused("settle", meta["id"]))
        self.gh.pr.update(state="MERGED", mergeCommit={"oid": "b" * 40})
        output = self.ok("settle", meta["id"])
        self.assertEqual(output.splitlines()[0], f"{meta['id']}: settled")
        self.assertNotIn("worktree", output)
        self.assertEqual((self.removals, self.t3_calls()), ([], []))
        self.assertEqual(self.ok("settle", "--prune-landed").splitlines()[0], "0 leftover worktrees")
        self.assertEqual(self.removals, [])
        self.assertTrue(self.store.load(meta["id"])["settled"])

    def test_a_merge_on_github_wakes_the_owner_once_for_an_adopted_pr(self):
        meta = self.adopt()
        listener = self.listener()
        self.assertFalse(listener.sweep(github=True))  # still open: nothing to say
        self.gh.pr.update(state="MERGED", mergeCommit={"oid": "b" * 40})
        listener.clock = lambda: 10 ** 10  # past the recheck window
        self.assertTrue(listener.sweep(github=True))
        listener.clock = lambda: 10 ** 10 + 3600
        self.assertFalse(listener.sweep(github=True))  # the ledger says merged now: never re-asked
        held = Inbox(self.home).open_items()
        self.assertEqual([(i["kind"], i["line"]) for i in held], [("pr-merged", f"Old Firstmate work: PR merged {URL}")])
        self.assertNotIn(meta["id"], held[0]["line"])

    # park --------------------------------------------------------------------

    def finished_with_pr(self):
        meta = self.spawn()
        self.fake.prs = [URL]
        self.gh.pr["headRefName"] = meta["branch"]
        self.completed(meta, f"BOSS: done: {URL}")
        return meta

    def test_a_parked_task_shows_parked_and_waits_on_the_captain(self):
        meta = self.finished_with_pr()
        self.assertTrue(self.row("Fix thing").endswith("| review, then land |"))
        self.assertIn("parked", self.ok("park", meta["id"], "wait until after the demo"))
        row = self.row("Fix thing")
        self.assertIn("| parked: wait until after the demo |", row)
        self.assertTrue(row.endswith("| waiting on captain |"), row)
        status = self.ok("status", meta["id"])
        self.assertIn("parked by captain: wait until after the demo", status)
        self.assertIn(f"next: waiting on captain; boss unpark {meta['id']} only when he says so", status)
        self.assertIn("unparked", self.ok("unpark", meta["id"]))
        self.assertTrue(self.row("Fix thing").endswith("| review, then land |"))
        self.assertIn("was not parked; nothing changed", self.ok("unpark", meta["id"]))

    def test_land_refuses_while_parked_even_with_his_words(self):
        meta = self.finished_with_pr()
        self.ok("park", meta["id"], "not yet, I want to try it first")
        output = self.refused("land", meta["id"], "--approved", "merge it")
        self.assertIn("the captain parked this: not yet, I want to try it first", output)
        self.assertEqual(self.gh.merges, [])
        self.ok("unpark", meta["id"])
        self.ok("land", meta["id"], "--approved", "merge it")
        self.assertEqual(len(self.gh.merges), 1)

    def test_a_parked_docs_only_pr_is_not_fast_landed(self):
        config = self.home / "config.toml"
        config.write_text(config.read_text() + "fast_land_docs = true\n")
        self.gh.paths = ["README.md"]
        meta = self.finished_with_pr()
        self.ok("park", meta["id"], "hold the docs too")
        self.assertIn("parked", self.refused("land", meta["id"]))
        self.assertNotIn("fast_land_docs", self.ok("status", meta["id"]))
        # Parked, it is his call again: the supervisor may queue it as a decision.
        self.ok("decide", "add", "Land the docs PR now?", "--task", meta["id"], "--action", "merge")
        self.assertEqual(self.gh.merges, [])

    def test_a_parked_task_never_asks_to_land_in_the_inbox_but_still_says_when_its_pr_merges(self):
        meta = self.finished_with_pr()
        self.ok("park", meta["id"], "wait for the release")
        listener, inbox = self.listener(), Inbox(self.home)
        listener.read(meta["id"], meta["thread_id"])
        self.assertEqual([i["kind"] for i in inbox.open_items()], ["done"])
        self.assertIsNone(inbox.take(self.store.load, "watch", "test"))  # the done line is dropped, never delivered
        dropped = [r for r in inbox.records() if r["event"] == "dropped"]
        self.assertEqual(len(dropped), 1)
        self.assertIn("parked by the captain", dropped[0]["reason"])
        attention = (self.home / "attention.json").read_text()
        self.assertNotIn('"finished"', attention)
        self.gh.pr.update(state="MERGED", mergeCommit={"oid": "b" * 40})
        listener.read(meta["id"], meta["thread_id"])
        self.assertIn(f"Fix thing: PR merged {URL}", inbox.take(self.store.load, "watch", "test"))

    def test_park_needs_his_words_and_an_active_task(self):
        meta = self.adopt()
        self.assertEqual(self.cli("park", meta["id"], "  ")[0], 1)
        self.gh.pr.update(state="MERGED", mergeCommit={"oid": "b" * 40})
        self.ok("settle", meta["id"])
        self.assertIn("settled", self.refused("park", meta["id"], "hold it"))

    def test_the_away_timeline_names_adopted_and_parked_tasks_in_plain_words(self):
        self.ok("away", "lunch")
        meta = self.adopt()
        self.ok("park", meta["id"], "after the demo")
        self.ok("unpark", meta["id"])
        timeline = self.ok("back", "--full")
        self.assertIn("adopted a PR opened outside boss: Old Firstmate work (repo)", timeline)
        self.assertIn("parked: Old Firstmate work (repo); his words: after the demo", timeline)
        self.assertIn("unparked: Old Firstmate work (repo)", timeline)
        self.assertNotIn("opened PR", timeline)
        self.assertNotIn(meta["id"], timeline)
