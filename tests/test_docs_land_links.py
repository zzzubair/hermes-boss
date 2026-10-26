"""Docs-only PRs land under fast_land_docs without the captain; machine-only links are flagged."""
import unittest

from boss.common import BossError
from boss.events import classify, describe, machine_links
from boss.inbox import Inbox
from boss.service import FAST_LAND_DOCS, footer
from tests import test_boss as harness
from tests import test_events
from tests import test_wake as wake
from tests.support import HermeticTestCase

URL = harness.URL


class MachineLinkTest(unittest.TestCase):
    def test_links_that_open_only_on_that_machine_are_found(self):
        for text, expected in (
                ("Demo: http://localhost:5173/login.", ("http://localhost:5173/login",)),
                ("open http://127.0.0.1:8080", ("http://127.0.0.1:8080",)),
                ("see http://[::1]:9000/", ("http://[::1]:9000/",)),
                ("served at http://0.0.0.0:3000", ("http://0.0.0.0:3000",)),
                ("preview http://devbox:3000/demo", ("http://devbox:3000/demo",)),
                ("[demo](http://app.localhost:4000)", ("http://app.localhost:4000",)),
                ("run it and go to localhost:3000 then", ("localhost:3000",)),
                ("127.0.0.1:8080 and http://localhost:1/x", ("http://localhost:1/x", "127.0.0.1:8080"))):
            with self.subTest(text=text):
                self.assertEqual(machine_links(text), expected)

    def test_phone_safe_and_ordinary_text_passes(self):
        for text in ("https://devbox.tail1234.ts.net:8443/demo", "https://login-fix.preview.example.net/",
                     "https://github.com/example/repo/pull/7", "http://devbox/", "boss/service.py:41 and tests:120",
                     "at 10:30 UTC", "user@localhost:22", ""):
            with self.subTest(text=text):
                self.assertEqual(machine_links(text), ())

    def test_done_line_flags_report_and_pr_body_links_in_plain_words(self):
        view = test_events.view(prs=[test_events.PR], assistant=f"Shipped. Try http://localhost:5173\nBOSS: done: {test_events.PR}")
        view["prs"][0]["body"] = "## Testing\nChecked on http://devbox:3000/login"
        event = classify(view)
        self.assertEqual(event.machine_links, ("http://localhost:5173", "http://devbox:3000/login"))
        self.assertEqual(describe("Login fix", event, view["prs"]),
                         f'Login fix: finished, PR "Fix the widget" {test_events.PR} '
                         "(link only works on that machine: http://localhost:5173, http://devbox:3000/login)")

    def test_a_report_with_tailnet_links_is_not_flagged(self):
        event = classify(test_events.view(assistant="Demo at https://devbox.tail1.ts.net/x\nBOSS: done: demo ready"))
        self.assertEqual(event.machine_links, ())
        self.assertEqual(describe("Demo", event), "Demo: finished: demo ready")

    def test_unnamed_pr_bodies_are_not_read(self):
        view = test_events.view(prs=[test_events.PR], assistant="BOSS: done: notes only")
        view["prs"][0]["body"] = "http://localhost:1"
        self.assertEqual(classify(view).machine_links, ())

    def test_contract_asks_ship_workers_for_phone_links(self):
        for delivery in ("direct-PR", "local-only"):
            text = footer("ship", delivery)
            self.assertIn("tailnet (*.ts.net) or preview URL the captain can open on his phone", text)
            self.assertIn("Never link localhost, 127.0.0.1 or a bare host:port", text)


class FastLandDocsTest(HermeticTestCase):
    setUp = harness.CLITest.setUp
    tearDown = harness.CLITest.tearDown
    cli = harness.CLITest.cli
    spawn = harness.CLITest.spawn
    completed = harness.CLITest.completed

    def configure(self, text):
        path = self.home / "config.toml"
        path.write_text(path.read_text() + text)

    def docs_pr(self, *settings):
        self.configure("".join(settings))
        meta = self.spawn()
        self.completed(meta)
        self.fake.prs = [URL]
        self.gh.paths = ["README.md", "docs/guide.MD"]
        return meta

    def test_off_by_default_and_shown(self):
        meta = self.docs_pr()
        code, output = self.cli("project", "show", "repo")
        self.assertIn("fast_land_docs: false", output)
        self.assertEqual(self.cli("land", meta["id"])[0], 2)
        self.assertEqual(self.gh.merges, [])
        self.configure("fast_land_docs = true\n")
        self.assertIn("fast_land_docs: true", self.cli("project", "show", "repo")[1])

    def test_setting_must_be_a_boolean(self):
        self.configure('fast_land_docs = "yes"\n')
        code, output = self.cli("project", "show", "repo")
        self.assertEqual(code, 1)
        self.assertIn("projects.repo.fast_land_docs: must be bool", output)

    def test_docs_only_pr_lands_without_quote_or_waiting_for_checks(self):
        meta = self.docs_pr("fast_land_docs = true\n")
        self.gh.pr.update(statusCheckRollup=[{"status": "IN_PROGRESS"}], mergeStateStatus="UNSTABLE")
        code, output = self.cli("land", meta["id"])
        self.assertEqual(code, 0, output)
        self.assertEqual(len(self.gh.merges), 1)
        self.assertEqual(self.store.load(meta["id"])["landed"]["approved"], FAST_LAND_DOCS)

    def test_captain_words_are_kept_when_given(self):
        meta = self.docs_pr("fast_land_docs = true\n")
        self.assertEqual(self.cli("land", meta["id"], "--approved", "ship the docs")[0], 0)
        self.assertEqual(self.store.load(meta["id"])["landed"]["approved"], "ship the docs")

    def test_a_pr_touching_anything_but_markdown_still_needs_the_captain_and_green_checks(self):
        meta = self.docs_pr("fast_land_docs = true\n")
        # ["docs/new.md", "boss/old.py"] is how files() reports a rename from .py to .md.
        for paths in (["README.md", "boss/cli.py"], ["notes.md.bak"], ["docs/new.md", "boss/old.py"], []):
            with self.subTest(paths=paths):
                self.gh.paths = paths
                code, output = self.cli("land", meta["id"])
                self.assertEqual(code, 2, output)
                self.assertIn("landing requires the captain's words", output)
                self.gh.pr["statusCheckRollup"] = [{"status": "IN_PROGRESS"}]
                self.assertEqual(self.cli("land", meta["id"], "--approved", "merge it")[0], 2)
                self.gh.pr["statusCheckRollup"] = [{"conclusion": "SUCCESS"}]
        self.assertEqual(self.gh.merges, [])

    def test_failed_checks_conflicts_drafts_and_github_blocks_still_refuse(self):
        meta = self.docs_pr("fast_land_docs = true\n")
        for field, value in (("statusCheckRollup", [{"conclusion": "FAILURE"}]), ("mergeStateStatus", "DIRTY"),
                             ("mergeStateStatus", "BLOCKED"), ("isDraft", True), ("reviewDecision", "CHANGES_REQUESTED")):
            with self.subTest(field=field, value=value):
                before = self.gh.pr[field]
                self.gh.pr[field] = value
                code, output = self.cli("land", meta["id"])
                self.assertEqual(code, 2, output)
                self.assertIn("PR is not ready", output)
                self.gh.pr[field] = before
        self.assertEqual(self.gh.merges, [])

    def test_merge_never_fence_wins(self):
        meta = self.docs_pr("fast_land_docs = true\n", 'merge = "never"\n')
        code, output = self.cli("land", meta["id"])
        self.assertEqual(code, 2)
        self.assertIn("landing requires the captain's words", output)
        self.assertIn("merge policy is never", self.cli("land", meta["id"], "--approved", "yes")[1])
        self.assertEqual(self.gh.merges, [])

    def test_a_fence_holds_a_docs_only_pr_and_lifts_when_dropped(self):
        meta = self.docs_pr("fast_land_docs = true\n")
        code, output = self.cli("steer", "repo", "no merges until I say", "--fence")
        self.assertEqual(code, 0, output)
        fence = output.split(":", 1)[0]
        code, output = self.cli("land", meta["id"])
        self.assertEqual(code, 2)
        self.assertIn("fenced", output)
        self.assertNotIn("docs-only PR", self.cli("status", meta["id"])[1])
        self.assertEqual(self.cli("decide", "add", "Merge the docs?", "--task", meta["id"])[0], 0)
        self.assertEqual(self.gh.merges, [])
        self.assertEqual(self.cli("steer", "drop", "repo", fence)[0], 0)
        self.assertEqual(self.cli("land", meta["id"])[0], 0)

    def test_his_no_to_merging_holds_even_a_docs_only_pr(self):
        meta = self.docs_pr()
        self.assertEqual(self.cli("decide", "add", "Merge the docs?", "--task", meta["id"], "--action", "merge")[0], 0)
        self.assertEqual(self.cli("decide", "ask")[0], 0)
        self.assertEqual(self.cli("decide", "answer", "1 no")[0], 0)
        self.configure("fast_land_docs = true\n")
        code, output = self.cli("land", meta["id"])
        self.assertEqual(code, 2)
        self.assertIn("he answered: no", output)
        self.assertNotIn("docs-only PR", self.cli("status", meta["id"])[1])
        self.assertEqual(self.gh.merges, [])

    def test_running_worker_and_scouts_are_never_fast_landed(self):
        meta = self.docs_pr("fast_land_docs = true\n")
        self.fake.threads[meta["thread_id"]]["status"] = "running"
        self.assertIn("worker must stop", self.cli("land", meta["id"])[1])
        scout = self.spawn("--kind", "scout")
        self.completed(scout)
        self.assertIn("landing requires the captain's words", self.cli("land", scout["id"])[1])
        self.assertEqual(self.gh.merges, [])

    def test_docs_only_pr_is_never_raised_as_a_decision(self):
        meta = self.docs_pr("fast_land_docs = true\n")
        code, output = self.cli("decide", "add", "Merge the docs PR?", "--task", meta["id"])
        self.assertEqual(code, 2)
        self.assertIn("never a captain decision", output)
        self.assertIn(f"boss land {meta['id']}", output)
        self.assertEqual(self.store.decisions(), [])
        self.assertEqual(self.cli("decide", "add", "Rename the project?")[0], 0)
        self.gh.paths = ["README.md", "boss/cli.py"]
        self.assertEqual(self.cli("decide", "add", "Merge the code PR?", "--task", meta["id"])[0], 0)
        self.assertEqual(len(self.store.decisions()), 2)

    def test_decisions_are_untouched_when_the_rule_is_off(self):
        meta = self.docs_pr()
        self.gh.paths = BossError("external", "gh must not be called")
        self.assertEqual(self.cli("decide", "add", "Merge the docs PR?", "--task", meta["id"])[0], 0)

    def test_status_points_at_land_without_a_quote(self):
        meta = self.docs_pr("fast_land_docs = true\n")
        self.gh.pr["statusCheckRollup"] = [{"status": "IN_PROGRESS"}]
        output = self.cli("status", meta["id"])[1]
        self.assertIn(f"next: boss land {meta['id']} (docs-only PR under fast_land_docs; no need to ask the captain)", output)
        self.gh.paths = ["boss/cli.py"]
        self.gh.pr["statusCheckRollup"] = [{"conclusion": "SUCCESS"}]
        self.assertIn(f'next: boss land {meta["id"]} --approved "<captain quote>"', self.cli("status", meta["id"])[1])


class WakeLineTest(HermeticTestCase):
    setUp_cli = harness.CLITest.setUp
    tearDown_cli = harness.CLITest.tearDown
    cli = harness.CLITest.cli
    spawn = harness.CLITest.spawn
    completed = harness.CLITest.completed
    setUp = wake.WakeTest.setUp
    stop_all = wake.WakeTest.stop_all
    config = wake.WakeTest.config
    state = wake.WakeTest.state
    listen = wake.WakeTest.listen
    send = wake.WakeTest.send
    own = wake.WakeTest.own

    def finish(self, report):
        self.own("agent:captain", "Discord DM")
        ctx = wake.Context(self.home / "profile")
        self.listen(ctx)
        self.fake.prs = [URL]
        self.completed(self.task, report)
        self.send(wake.updated(self.state("completed"), 2))
        self.assertEqual(len(ctx.messages), 1)
        return ctx.messages[0][0]

    def test_docs_only_pr_wake_says_boss_can_land_it(self):
        path = self.home / "config.toml"
        path.write_text(path.read_text() + "fast_land_docs = true\n")
        self.gh.paths = ["README.md"]
        content = self.finish(f"Docs updated.\nBOSS: done: {URL}")
        self.assertIn(f"Fix thing: finished, PR {URL}; docs only, so boss can land it without asking the captain", content)

    def test_a_failed_docs_check_still_delivers_the_wake(self):
        path = self.home / "config.toml"
        path.write_text(path.read_text() + "fast_land_docs = true\n")
        self.gh.paths = BossError("external", "gh exited 1")
        content = self.finish(f"Docs updated.\nBOSS: done: {URL}")
        self.assertIn(f"Fix thing: finished, PR {URL}", content)
        self.assertNotIn("docs only", content)
        self.assertIn("docs check", (self.home / "wake-errors.log").read_text())

    def test_machine_only_demo_link_is_flagged_in_the_wake(self):
        self.gh.pr["body"] = "## Testing\nhttps://devbox.tail1.ts.net/ok"
        content = self.finish(f"Demo at http://localhost:5173/login\nBOSS: done: {URL}")
        self.assertIn(f"Fix thing: finished, PR {URL} (link only works on that machine: http://localhost:5173/login)", content)
        self.assertNotIn("docs only", content)
        self.assertEqual(Inbox(self.home).open_items(), [])
