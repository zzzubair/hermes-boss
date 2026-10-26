"""The worker contract: how to wait, paused deadlines, the test host and standing rules."""
from boss.events import status_line
from boss.service import footer
from tests import test_boss as harness
from tests.support import HermeticTestCase


class ContractTest(HermeticTestCase):
    setUp = harness.CLITest.setUp
    tearDown = harness.CLITest.tearDown
    cli = harness.CLITest.cli

    def steer(self, text):
        code, output = self.cli("steer", "repo", text)
        self.assertEqual(code, 0, output)
        return output.split(":", 1)[0]

    def brief(self, *flags):
        code, output = self.cli("brief", "repo", *flags)
        self.assertEqual(code, 0, output)
        return output

    def sent(self, *flags):
        code, output = self.cli("spawn", "repo", "--title", "Task", "--fits", "Fits", *flags, stdin="<task>")
        self.assertEqual(code, 0, output)
        message = [a["message"] for n, a in self.fake.calls if n == "t3_thread_launch"][-1]
        self.assertEqual((self.store.directory(output.split(":")[0]) / "brief.md").read_text(), message)
        return message

    def configure(self, text):
        path = self.home / "config.toml"
        path.write_text(path.read_text() + text)

    @staticmethod
    def contract(message):
        return message.split("### boss worker contract", 1)[1]

    def test_workers_wait_inside_one_blocking_command_instead_of_ending_the_turn(self):
        for kind, delivery in (("ship", "direct-PR"), ("ship", "local-only"), ("scout", "direct-PR")):
            text = footer(kind, delivery)
            with self.subTest(kind=kind, delivery=delivery):
                self.assertIn("Never end your turn to wait for something your shell can watch", text)
                self.assertIn("Wait inside ONE blocking command", text)
                self.assertIn("with your shell tool's largest timeout, and reissue the same command if it times out", text)
                self.assertIn("Never sleep and then check in a separate step, and never background a job to poll it.", text)
                self.assertIn("Hitting the same obstacle twice is a blocker", text)
                self.assertNotIn("Never poll, sleep or wait in a loop spending turns", text)

    def test_paused_must_name_a_utc_deadline_and_still_parses(self):
        text = footer("ship", "direct-PR")
        self.assertIn("BOSS: paused: <what you wait on> until <UTC time>", text)
        self.assertIn("Use paused only for a wait you cannot watch yourself, and name until <UTC time>.", text)
        self.assertEqual(status_line("Waiting on the vendor.\nBOSS: paused: vendor quota reset until 2026-10-27T09:00Z"),
                         ("paused", "vendor quota reset until 2026-10-27T09:00Z"))

    def test_test_host_reaches_the_worker_only_when_set(self):
        self.assertNotIn("Run heavy tests", self.brief())
        self.assertNotIn("Run heavy tests", self.sent())
        self.configure('test_host = "workstation"\n')
        line = "Run heavy tests (full suites, browsers, app stacks) on workstation, not on this host."
        self.assertIn(line, self.contract(self.brief()))
        self.assertIn(line, self.contract(self.sent()))

    def test_ui_screenshots_are_asked_of_ship_workers_only(self):
        line = "If the change touches UI, put before and after screenshots of the real page in the PR or final report."
        self.assertIn(line, footer("ship", "direct-PR"))
        self.assertIn(line, footer("ship", "local-only"))
        self.assertNotIn(line, footer("scout", "direct-PR"))

    def test_standing_rules_from_the_steer_store_reach_every_matching_brief(self):
        self.configure('[workers.scout]\nkind = "scout"\n')
        self.steer("Keep it small")
        self.steer("rule: No secrets in logs")
        self.steer("Rule ship: Mobile-first; test on WSL")
        self.steer("rule direct-PR: PR body follows the pr-description shape")
        self.steer("rule scout: Cite file paths")
        heading = "Standing rules from the captain. Follow every one:"
        ship = self.sent()
        self.assertIn(heading + "\n- No secrets in logs\n- Mobile-first; test on WSL\n"
                      "- PR body follows the pr-description shape\n", self.contract(ship))
        scout = self.sent("--worker", "scout")
        self.assertIn(heading + "\n- No secrets in logs\n- PR body follows the pr-description shape\n"
                      "- Cite file paths\n", self.contract(scout))
        for message in (ship, scout, self.brief(), self.brief("--worker", "scout")):
            steers = message.split("## Captain steers", 1)[1].split("### boss worker contract", 1)[0]
            self.assertIn("Keep it small", steers)
            self.assertNotIn("rule", steers.lower())
            self.assertEqual(message.count("No secrets in logs"), 1)
        self.assertNotIn("Mobile-first", scout)
        self.assertNotIn("Cite file paths", ship)

    def test_standing_rules_are_read_from_disk_on_every_spawn_and_drop_with_the_steer(self):
        self.assertNotIn("Standing rules", self.sent())
        rule = self.steer("rule: No secrets in logs")
        self.assertIn("- No secrets in logs", self.contract(self.sent()))
        self.assertEqual(self.cli("steer", "drop", "repo", rule)[0], 0)
        message = self.sent()
        self.assertNotIn("Standing rules", message)
        self.assertNotIn("No secrets in logs", message)

    def test_local_only_rules_skip_direct_pr_briefs_and_origin_names_the_steer_log(self):
        self.steer("rule local-only: Leave a summary in the final message")
        self.assertNotIn("Leave a summary", self.brief())
        path = self.home / "config.toml"
        path.write_text(path.read_text().replace('delivery = "direct-PR"', 'delivery = "local-only"'))
        output = self.brief("--origin")
        self.assertIn(f"Follow every one:\n<!-- source: {self.home / 'projects/repo.steers.jsonl'} -->\n"
                      "- Leave a summary in the final message", output)
        self.assertNotIn("source:", self.brief())

    def test_steers_that_only_mention_rule_stay_ordinary_steers(self):
        self.steer("rules: keep the old naming")
        self.steer("rule ui: phone first")
        message = self.brief()
        self.assertNotIn("Standing rules", message)
        steers = message.split("## Captain steers", 1)[1]
        self.assertIn("rules: keep the old naming", steers)
        self.assertIn("rule ui: phone first", steers)
