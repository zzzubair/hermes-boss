import contextlib
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from boss.cli import main
from boss.common import BossError, Redactor
from boss.config import Config
from boss.integrations import ci_summary, inspect_worktree
from boss.mcp import MCP
from boss.state import Store, atomic_write
from tests.support import HermeticTestCase


TOKEN = "test-secret-token-never-print"
URL = "https://github.com/example/repo/pull/7"
SCHEMAS = {
    "t3_project_list": [], "orchestrator_capabilities": [],
    "t3_thread_launch": ["title"], "t3_thread_read": ["threadId"],
    "t3_thread_list": [], "t3_thread_send": ["threadId", "message"],
    "t3_thread_interrupt": [], "t3_pending_request_list": [],
    "t3_pending_request_read": ["requestId"],
    "t3_pending_request_respond": ["requestId", "answers"],
    "list_thread_pull_requests": [], "unwatch_pull_request": [],
    "t3_thread_organize": ["action"],
}


class FakeT3:
    def __init__(self):
        self.calls, self.headers = [], []
        self.threads, self.pending, self.prs = {}, {}, []
        self.projects = [{"id": "p1", "title": "repo", "workspaceRoot": "/fake/repo",
                          "defaultModelSelection": None,
                          "repositoryIdentity": {"owner": "example", "name": "repo"}}]
        self.failure, self.drop_launch, self.sse, self.missing_session = None, False, False, False
        self.launch_selection = None
        self.catalog = {"providers": [{"providerInstanceId": "codex", "models": [
            {"id": "gpt-test", "options": [{"id": "reasoningEffort", "values": ["low", "high", "max"]},
                                           {"id": "serviceTier", "values": ["default", "priority"]}]}]}]}
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.headers.append(dict(self.headers))
                if self.headers.get("Authorization") != "Bearer " + TOKEN:
                    self.send_error(401)
                    return
                method, ident = body["method"], body.get("id")
                if method != "initialize" and self.headers.get("Mcp-Session-Id") != "fake-session":
                    self.send_error(400)
                    return
                if method == "initialize":
                    result = {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}}, "serverInfo": {"name": "fake", "version": "1"}}
                elif method == "notifications/initialized":
                    self.send_response(202)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                elif method == "tools/list":
                    result = {"tools": [{"name": n, "inputSchema": {"type": "object", "required": required, "additionalProperties": True}}
                                        for n, required in SCHEMAS.items()]}
                elif method == "tools/call":
                    name, arguments = body["params"]["name"], body["params"]["arguments"]
                    fake.calls.append((name, arguments))
                    data = fake.tool(name, arguments)
                    if name == "t3_thread_launch" and fake.drop_launch:
                        self.connection.shutdown(2)
                        self.connection.close()
                        return
                    result = {"content": [{"type": "text", "text": json.dumps(data)}], "structuredContent": data}
                    if fake.failure and name == fake.failure:
                        result = {"isError": True, "content": [{"type": "text", "text": "bad Bearer " + TOKEN + " echoed=" + TOKEN}]}
                else:
                    raise AssertionError(method)
                encoded = json.dumps({"jsonrpc": "2.0", "id": ident, "result": result}).encode()
                self.send_response(200)
                if method == "initialize" and not fake.missing_session:
                    self.send_header("mcp-session-id", "fake-session")
                if fake.sse:
                    encoded = b": comment\n\ndata: {\"jsonrpc\":\"2.0\",\"method\":\"notifications/progress\"}\n\ndata: " + encoded + b"\n\n"
                self.send_header("Content-Type", "text/event-stream" if fake.sse else "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.01), daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/mcp"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def tool(self, name, args):
        if name == "t3_project_list":
            return {"projects": self.projects, "nextCursor": None}
        if name == "orchestrator_capabilities":
            return self.catalog
        if name == "t3_thread_launch":
            tid = "thread-" + str(len(self.threads) + 1)
            self.threads[tid] = {"threadId": tid, "title": args["title"], "projectId": args["projectId"],
                                 "latestRunId": "run-1", "status": "running", "settled": False,
                                 "branch": args["workspaceStrategy"]["branch"], "worktreePath": "/fake/worktree"}
            return {"threadId": tid, "projectId": args["projectId"], "status": "running",
                    "modelSelection": self.launch_selection or args.get("modelSelection", {})}
        if name == "t3_thread_list":
            return {"threads": list(self.threads.values()), "nextCursor": None}
        if name == "t3_thread_read":
            thread = self.threads[args["threadId"]]
            items = thread.get("items", [])
            if args.get("itemId"):
                items = [{**items[-1], "text": "complete report", "nextTextOffset": None, "textTruncated": False}]
            return {"thread": thread, "items": items, "hasMore": False, "nextPosition": None, "recentRuns": []}
        if name == "t3_pending_request_list":
            return {"requestIds": list(self.pending)}
        if name == "t3_pending_request_read":
            return {"questions": self.pending[args["requestId"]], "requestId": args["requestId"]}
        if name == "t3_pending_request_respond":
            self.pending.pop(args["requestId"])
            return {"sequence": 1}
        if name == "list_thread_pull_requests":
            return {"pullRequests": [{"url": url} for url in self.prs], "chains": []}
        if name == "t3_thread_organize":
            self.threads[args["threadId"]]["settled"] = args["action"] == "settle"
            return {"settled": True}
        if name in ("t3_thread_interrupt", "t3_thread_send", "unwatch_pull_request"):
            return {"sequence": 2}
        raise AssertionError(name)


class FakeGitHub:
    def __init__(self):
        self.branch, self.merges = "trunk", []
        self.pr = {"url": URL, "state": "OPEN", "isDraft": False, "headRefOid": "a" * 40,
                   "headRefName": "", "baseRefName": "trunk", "mergeStateStatus": "CLEAN", "reviewDecision": "APPROVED",
                   "statusCheckRollup": [{"conclusion": "SUCCESS"}], "mergeCommit": None}

    def default_branch(self, repository):
        return self.branch

    def view(self, url):
        result = copy.deepcopy(self.pr)
        result["ci"] = ci_summary(result["statusCheckRollup"])
        return result

    def merge(self, pr):
        self.merges.append(copy.deepcopy(pr))
        self.pr.update(state="MERGED", mergeCommit={"oid": "b" * 40})


class CLITest(HermeticTestCase):
    def setUp(self):
        HermeticTestCase.setUp(self)
        os.environ.update(BOSS_HOME=str(self.home), T3_TEST_MCP_TOKEN=TOKEN)
        self.fake, self.gh = FakeT3(), FakeGitHub()
        (self.home / "config.toml").write_text(f'''[hosts.test]
url = "{self.fake.url}"
token_env = "T3_TEST_MCP_TOKEN"
ssh = ""
[projects.repo]
host = "test"
project_id = "p1"
repository = "example/repo"
delivery = "direct-PR"
''')
        self.gh_patch = patch("boss.service.GitHub", return_value=self.gh)
        self.gh_patch.start()
        self.redact = Redactor()
        self.redact.add(TOKEN)
        self.store = Store(self.home, self.redact)
        self.evidence = {"head": "a" * 40, "branch": "", "origin": "https://github.com/example/repo.git",
                         "dirty": False, "in_default": True, "pushed": False}
        self.probe = patch("boss.service.inspect_worktree", side_effect=lambda *a: dict(self.evidence))
        self.probe.start()

    def tearDown(self):
        self.probe.stop()
        self.gh_patch.stop()
        self.fake.close()

    def cli(self, *args, stdin="investigate this"):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch("sys.stdin", io.StringIO(stdin)):
            code = main(list(args))
        return code, output.getvalue()

    def spawn(self, *extra):
        code, output = self.cli("spawn", "repo", "--title", "Fix thing", *extra)
        self.assertEqual(code, 0, output)
        task_id = output.split(":", 1)[0]
        meta = self.store.load(task_id)
        self.gh.pr["headRefName"] = meta["branch"]
        self.evidence["branch"] = meta["branch"]
        return meta

    def completed(self, meta, report=None):
        thread = self.fake.threads[meta["thread_id"]]
        thread["status"] = "completed"
        if report is not None:
            thread["items"] = [{"type": "assistant_message", "createdBy": "agent", "text": report,
                                 "itemId": "last", "runId": "run-1", "textTruncated": False}]

    def test_empty_fleet_and_help_and_usage(self):
        code, output = self.cli()
        self.assertEqual(code, 0)
        self.assertIn("0 active tasks", output)
        self.assertIn("0 open decisions", output)
        self.assertEqual(self.cli("poll"), (0, ""))
        self.assertEqual(self.cli("bogus")[0], 1)
        self.assertEqual(self.cli("spawn", "repo", "--title", "T", "--typo")[0], 1)
        self.assertEqual(self.cli("status", "--fu")[0], 1)

    def test_spawn_worktree_footer_and_persistence(self):
        meta = self.spawn("--model", "codex:gpt-test", "--effort", "max")
        launch = next(a for n, a in self.fake.calls if n == "t3_thread_launch")
        self.assertEqual(launch["workspaceStrategy"], {"type": "worktree", "branch": "boss/" + meta["id"], "baseRef": "trunk", "startFromOrigin": True})
        self.assertEqual(launch["modelSelection"], {"instanceId": "codex", "model": "gpt-test", "options": {"reasoningEffort": "max"}})
        brief = (self.store.directory(meta["id"]) / "brief.md").read_text()
        for text in ("investigate this", "Never merge", "Never deploy", "configured Git user", "exactly ONE PR"):
            self.assertIn(text, brief)
        for name in ("meta.json", "events.log", "brief.md"):
            self.assertTrue((self.store.directory(meta["id"]) / name).exists())
        self.assertTrue(all(h.get("Mcp-Session-Id") == "fake-session" for h in self.fake.headers[1:]))

    def test_brief_file_and_scout_contract(self):
        brief = self.home / "brief"
        brief.write_text("read-only question")
        meta = self.spawn("--kind", "scout", "--brief-file", str(brief))
        text = (self.store.directory(meta["id"]) / "brief.md").read_text()
        self.assertIn("read-only question", text)
        self.assertIn("BOSS_REPORT:", text)
        self.assertIn("Do not open a PR", text)

    def test_local_only_contract(self):
        p = self.home / "config.toml"
        p.write_text(p.read_text().replace('delivery = "direct-PR"', 'delivery = "local-only"'))
        meta = self.spawn()
        text = (self.store.directory(meta["id"]) / "brief.md").read_text()
        self.assertIn("Do not push or open a PR", text)
        self.assertEqual(self.cli("land", meta["id"], "--approved", "merge it")[0], 2)

    def test_unsupported_model_options_do_not_launch(self):
        code, output = self.cli("spawn", "repo", "--title", "T", "--model", "gpt-test", "--effort", "ultra")
        self.assertEqual(code, 1, output)
        self.assertFalse(self.fake.threads)

    def test_null_project_model_options_and_configured_defaults(self):
        config = self.home / "config.toml"
        config.write_text(config.read_text() + '''
[projects.repo.model_selection]
instanceId = "codex"
model = "gpt-test"
[projects.repo.model_selection.options]
reasoningEffort = "high"
serviceTier = "priority"
''')
        self.spawn()
        launched = next(a for n, a in self.fake.calls if n == "t3_thread_launch")
        self.assertEqual(launched["modelSelection"]["options"], {"reasoningEffort": "high", "serviceTier": "priority"})
        from boss.service import Boss
        config = Config(self.home, self.redact)
        boss = Boss(config, self.store, self.redact, self.gh)
        selected = boss.model_selection(boss.client("test"), {"defaultModelSelection": {"model": "gpt-test", "options": None}}, None, None, None)
        self.assertEqual(selected["options"], {})

    def test_lost_launch_response_reconciles_without_duplicate(self):
        self.fake.drop_launch = True
        code, output = self.cli("spawn", "repo", "--title", "T")
        self.assertEqual(code, 3, output)
        meta = self.store.tasks()[0]
        self.assertEqual(meta["status"], "launch_unknown")
        self.fake.drop_launch = False
        self.assertEqual(self.cli("status", meta["id"])[0], 0)
        self.assertEqual(len(self.fake.threads), 1)
        self.assertEqual(len([n for n, _ in self.fake.calls if n == "t3_thread_launch"]), 1)

    def test_poll_running_silent_and_terminal_deterministic(self):
        meta = self.spawn()
        self.assertEqual(self.cli("poll"), (0, ""))
        self.completed(meta, "done")
        expected = (0, meta["id"] + ": finished\n")
        self.assertEqual(self.cli("poll"), expected)
        revision = self.store.load(meta["id"])["revision"]
        self.assertEqual(self.cli("poll"), expected)
        self.cli("status", meta["id"])
        self.assertEqual(self.cli("poll"), expected)
        self.assertEqual(self.store.load(meta["id"])["revision"], revision)

    def test_poll_ci_aggregates_changes_and_unlinked_pr_retained(self):
        meta = self.spawn()
        self.fake.prs = [URL]
        first = self.cli("poll")
        self.assertIn("PR opened " + URL, first[1])
        self.assertIn("1 passed,0 failed,0 pending,0 skipped", first[1])
        self.assertEqual(first, self.cli("poll"))
        self.gh.pr["statusCheckRollup"] = [{"conclusion": "FAILURE"}]
        second = self.cli("poll")
        self.assertNotEqual(first, second)
        self.assertEqual(second, self.cli("poll"))
        self.fake.prs = []
        self.assertEqual(second, self.cli("poll"))
        self.assertEqual(self.store.load(meta["id"])["pr_urls"], [URL])

    def test_poll_failed_interrupted_and_waiting(self):
        meta = self.spawn()
        for status, label in [("failed", "failed"), ("interrupted", "interrupted"), ("waiting", "waiting for input")]:
            self.fake.threads[meta["thread_id"]]["status"] = status
            first = self.cli("poll")
            self.assertIn(label, first[1])
            self.assertEqual(first, self.cli("poll"))

    def test_pending_input_and_answer(self):
        meta = self.spawn()
        self.fake.pending = {"request-1": [{"id": "q1", "question": "Which path?", "options": []}]}
        self.assertIn("Which path?", self.cli("status", meta["id"])[1])
        self.assertIn("waiting for input requests=request-1", self.cli("poll")[1])
        self.assertEqual(self.cli("answer", meta["id"], "wrong", "A")[0], 2)
        self.assertEqual(self.cli("answer", meta["id"], "request-1", "A")[0], 0)
        respond = next(a for n, a in self.fake.calls if n == "t3_pending_request_respond")
        self.assertEqual(respond["answers"], {"q1": "A"})

    def test_multi_question_answers_need_all_keys(self):
        meta = self.spawn()
        self.fake.pending = {"r": [{"id": "a", "question": "A?"}, {"id": "b", "question": "B?"}]}
        self.assertEqual(self.cli("answer", meta["id"], "r", "hello")[0], 1)
        self.assertEqual(self.cli("answer", meta["id"], "r", '{"a":"x"}')[0], 1)
        self.assertEqual(self.cli("answer", meta["id"], "r", '{"a":"x","b":"y"}')[0], 0)

    def test_send_and_stop_preserve_work(self):
        meta = self.spawn()
        self.assertEqual(self.cli("send", meta["id"], "please inspect")[0], 0)
        self.assertEqual(self.cli("stop", meta["id"])[0], 0)
        send = next(a for n, a in self.fake.calls if n == "t3_thread_send")
        self.assertTrue(send["clientRequestId"])
        self.assertFalse(self.store.load(meta["id"])["settled"])

    def test_land_requires_quote_and_green_checks(self):
        meta = self.spawn()
        self.completed(meta)
        self.fake.prs = [URL]
        for quote in (None, "", "   "):
            argv = ["land", meta["id"]] + (["--approved", quote] if quote is not None else [])
            self.assertEqual(self.cli(*argv)[0], 2)
        for checks in ([], [{"conclusion": "FAILURE"}], [{"status": "IN_PROGRESS"}], [{"conclusion": "SKIPPED"}]):
            self.gh.pr["statusCheckRollup"] = checks
            self.assertEqual(self.cli("land", meta["id"], "--approved", "merge it")[0], 2)
        self.assertEqual(self.gh.merges, [])

    def test_land_refuses_draft_blocked_and_wrong_branch(self):
        meta = self.spawn()
        self.completed(meta)
        self.fake.prs = [URL]
        for field, value in [("isDraft", True), ("mergeStateStatus", "BLOCKED"), ("headRefName", "wrong"), ("baseRefName", "wrong"), ("reviewDecision", "CHANGES_REQUESTED")]:
            before = self.gh.pr[field]
            self.gh.pr[field] = value
            self.assertEqual(self.cli("land", meta["id"], "--approved", "merge it")[0], 2)
            self.gh.pr[field] = before
        self.assertFalse(self.gh.merges)

    def test_land_green_records_actual_merge_and_exact_approval(self):
        meta = self.spawn()
        self.completed(meta)
        self.fake.prs = [URL]
        self.assertEqual(self.cli("land", meta["id"], "--approved", "I reviewed it, merge this.")[0], 0)
        landed = self.store.load(meta["id"])["landed"]
        self.assertEqual(landed["approved"], "I reviewed it, merge this.")
        self.assertEqual(landed["head"], "a" * 40)
        self.assertEqual(landed["merge"], {"oid": "b" * 40})
        self.assertEqual(self.cli("land", meta["id"], "--approved", "same")[0], 0)
        self.assertEqual(len(self.gh.merges), 1)

    def test_settle_refuses_open_pr_unpushed_dirty_and_missing_report(self):
        meta = self.spawn("--kind", "scout")
        self.completed(meta)
        self.assertEqual(self.cli("settle", meta["id"])[0], 2)
        self.completed(meta, "BOSS_REPORT: findings")
        self.fake.prs = [URL]
        self.assertIn("open PR", self.cli("settle", meta["id"])[1])
        self.fake.prs = []
        self.gh.pr["state"] = "CLOSED"
        self.evidence.update(in_default=False, pushed=False)
        self.assertIn("unpushed branch", self.cli("settle", meta["id"])[1])
        self.evidence.update(in_default=True, dirty=True)
        self.assertIn("uncommitted work", self.cli("settle", meta["id"])[1])
        self.assertFalse(any(n == "t3_thread_organize" for n, _ in self.fake.calls))

    def test_settle_discard_quote_overrides_unlanded_but_not_running(self):
        meta = self.spawn("--kind", "scout")
        self.assertEqual(self.cli("settle", meta["id"], "--discard-approved", "discard scratch")[0], 2)
        self.completed(meta)
        self.fake.prs = [URL]
        self.assertEqual(self.cli("settle", meta["id"], "--discard-approved", "discard scratch")[0], 0)
        self.assertEqual(self.store.load(meta["id"])["discard_approved"], "discard scratch")
        self.assertTrue(any(n == "unwatch_pull_request" for n, _ in self.fake.calls))
        self.assertEqual(self.cli("poll"), (0, ""))
        self.assertEqual(self.cli("settle", meta["id"])[0], 0)

    def test_scout_report_saved_and_settled(self):
        meta = self.spawn("--kind", "scout")
        self.completed(meta, "BOSS_REPORT: evidence and findings")
        self.assertEqual(self.cli("settle", meta["id"])[0], 0)
        self.assertEqual((self.store.directory(meta["id"]) / "report.md").read_text(), "evidence and findings\n")
        self.assertTrue(self.fake.threads[meta["thread_id"]]["settled"])

    def test_report_from_old_run_does_not_allow_settlement(self):
        meta = self.spawn("--kind", "scout")
        self.completed(meta, "BOSS_REPORT: old report")
        self.cli("poll")
        self.fake.threads[meta["thread_id"]]["latestRunId"] = "run-2"
        self.assertIn("no report for latest run", self.cli("settle", meta["id"])[1])

    def test_report_pagination_recovers_full_text(self):
        meta = self.spawn("--kind", "scout")
        self.completed(meta, "BOSS_REPORT: ")
        item = self.fake.threads[meta["thread_id"]]["items"][0]
        item.update(textTruncated=True, nextTextOffset=13)
        self.assertEqual(self.cli("settle", meta["id"])[0], 0)
        self.assertEqual((self.store.directory(meta["id"]) / "report.md").read_text(), "complete report\n")

    def test_squash_merged_pr_matches_worktree_head(self):
        meta = self.spawn()
        self.completed(meta)
        self.fake.prs = [URL]
        self.gh.pr["state"] = "MERGED"
        self.evidence.update(in_default=False, pushed=False)
        self.assertEqual(self.cli("settle", meta["id"])[0], 0)

    def test_git_inspection_failure_refuses_safely(self):
        meta = self.spawn()
        self.completed(meta)
        with patch("boss.service.inspect_worktree", side_effect=BossError("git", "remote unavailable")):
            self.assertEqual(self.cli("settle", meta["id"])[0], 2)

    def test_token_redaction_in_output_and_durable_ledger(self):
        meta = self.spawn()
        self.completed(meta, "BOSS_REPORT: secret " + TOKEN)
        _, output = self.cli("status", meta["id"], "--full")
        self.assertNotIn(TOKEN, output)
        self.fake.failure = "t3_thread_send"
        code, output = self.cli("send", meta["id"], "echo " + TOKEN)
        self.assertEqual(code, 3)
        self.assertNotIn(TOKEN, output)
        for p in self.store.directory(meta["id"]).iterdir():
            self.assertNotIn(TOKEN, p.read_text(), p.name)

    def test_token_redaction_happens_before_error_truncation(self):
        secret = "very-long-secret-" * 80
        with patch.dict(os.environ, {"T3_EXTRA_MCP_TOKEN": secret}):
            code, output = self.cli("status", "--invented=" + secret)
        self.assertEqual(code, 1)
        self.assertNotIn("very-long-secret-", output)
        self.assertIn("[REDACTED]", output)

    def test_settle_refuses_other_origin_and_different_branch(self):
        meta = self.spawn()
        self.completed(meta)
        self.evidence["origin"] = "https://github.com/other/repo.git"
        self.assertIn("worktree origin differs", self.cli("settle", meta["id"])[1])
        self.evidence["origin"] = "https://github.com/example/repo.git"
        self.evidence["branch"] = "some-other-task"
        self.assertIn("worktree branch mismatch", self.cli("settle", meta["id"])[1])

    def test_missing_token_is_structured_transport_failure(self):
        with patch.dict(os.environ, {"T3_TEST_MCP_TOKEN": ""}):
            code, output = self.cli("spawn", "repo", "--title", "T")
        self.assertEqual(code, 3)
        self.assertIn("error: auth", output)
        self.assertNotIn(TOKEN, output)

    def test_poll_continues_after_one_task_fails(self):
        first, second = self.spawn(), self.spawn()
        self.completed(first)
        self.completed(second)
        meta = self.store.load(first["id"])
        meta["host"] = "missing-host"
        self.store.save(meta, "host_test")
        code, output = self.cli("poll")
        self.assertEqual(code, 1)
        self.assertIn(first["id"] + ": failed", output)
        self.assertIn(second["id"] + ": finished", output)

    def test_ledger_writer_lock_refuses_without_waiting(self):
        with self.store.locked():
            code, output = self.cli("decide", "add", "blocked write")
        self.assertEqual(code, 2)
        self.assertIn("holds the ledger", output)
        self.assertEqual(self.store.decisions(), [])

    def test_model_mismatch_requests_interrupt(self):
        self.fake.launch_selection = {"instanceId": "codex", "model": "wrong", "options": {}}
        code, output = self.cli("spawn", "repo", "--title", "T", "--model", "gpt-test")
        self.assertEqual(code, 3)
        self.assertIn("different model settings", output)
        self.assertTrue(any(n == "t3_thread_interrupt" for n, _ in self.fake.calls))

    def test_land_rejects_more_than_one_pr_and_running_worker(self):
        meta = self.spawn()
        self.fake.prs = [URL]
        self.assertEqual(self.cli("land", meta["id"], "--approved", "merge")[0], 2)
        self.completed(meta)
        self.fake.prs.append("https://github.com/example/repo/pull/8")
        self.assertEqual(self.cli("land", meta["id"], "--approved", "merge")[0], 2)
        self.assertFalse(self.gh.merges)

    def test_squash_head_mismatch_cannot_hide_unpushed_work(self):
        meta = self.spawn()
        self.completed(meta)
        self.fake.prs = [URL]
        self.gh.pr["state"] = "MERGED"
        self.evidence.update(in_default=False, pushed=False, head="c" * 40)
        self.assertIn("unpushed branch", self.cli("settle", meta["id"])[1])

    def test_decisions_survive_restart_close_idempotent(self):
        code, out = self.cli("decide", "add", "Pick storage")
        self.assertEqual(code, 0)
        decision_id = out.split(":", 1)[0]
        self.assertIn("1 open decisions", self.cli()[1])
        self.assertEqual(self.cli("decide", "close", decision_id, "SQLite")[0], 0)
        self.assertEqual(self.cli("decide", "close", decision_id, "ignored")[0], 0)
        self.assertIn("0 open decisions", self.cli()[1])
        self.assertEqual(self.store.decisions()[0]["resolution"], "SQLite")

    def test_meta_recovers_from_committed_journal(self):
        meta = self.spawn()
        (self.store.directory(meta["id"]) / "meta.json").write_text("corrupt")
        self.assertEqual(self.store.load(meta["id"])["thread_id"], meta["thread_id"])
        self.assertEqual(self.cli("status", meta["id"])[0], 0)

    def test_sse_transport_and_session_required(self):
        self.fake.sse = True
        self.spawn()
        self.fake.missing_session = True
        self.assertEqual(self.cli("spawn", "repo", "--title", "T")[0], 3)

    def test_schema_discovery_missing_tool_refuses(self):
        client = MCP(self.fake.url, TOKEN, self.redact)
        with self.assertRaises(BossError) as caught:
            client.call("nonexistent")
        self.assertEqual(caught.exception.code, "capability")

    def test_truncation_and_full_output(self):
        meta = self.spawn()
        text = "long " * 200
        self.completed(meta, text)
        self.assertIn("1000 chars; --full", self.cli("status", meta["id"])[1])
        self.assertNotIn("1000 chars; --full", self.cli("status", meta["id"], "--full")[1])

    def test_hermes_env_no_shell_evaluation_and_env_precedence(self):
        fake_home = self.home / "user"
        (fake_home / ".hermes").mkdir(parents=True)
        (fake_home / ".hermes/.env").write_text("T3_TEST_MCP_TOKEN='file-secret'\nTOKEN_LITERAL=$(touch /tmp/should-not-exist-boss)\n")
        with patch("boss.config.Path.home", return_value=fake_home):
            config = Config(self.home, self.redact)
            self.assertEqual(config.host("test")[1], TOKEN)
            self.assertEqual(self.redact("file-secret"), "[REDACTED]")
            self.assertEqual(config.credentials["TOKEN_LITERAL"], "$(touch /tmp/should-not-exist-boss)")


class GitEvidenceTest(HermeticTestCase):
    def test_installed_symlink_can_import_package_and_show_help(self):
        root = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory(prefix="boss install ") as temporary:
            env = {**os.environ, "HOME": temporary, "BOSS_HOME": str(Path(temporary) / "state")}
            result = subprocess.run([str(root / "install.sh")], cwd=root, env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            executable = Path(temporary) / ".local/bin/boss"
            self.assertTrue(executable.is_symlink())
            version = subprocess.run([str(executable), "--version"], env=env, capture_output=True, text=True)
            self.assertEqual(version.returncode, 0, version.stderr)
            self.assertEqual(version.stdout, "boss 0.1.0\n")
            for args in ([], ["--help"], ["spawn", "--help"], ["status", "--help"], ["send", "--help"],
                         ["answer", "--help"], ["stop", "--help"], ["land", "--help"], ["settle", "--help"],
                         ["poll", "--help"], ["decide", "--help"], ["decide", "add", "--help"],
                         ["decide", "list", "--help"], ["decide", "close", "--help"],
                         ["project", "--help"], ["project", "list", "--help"],
                         ["project", "add", "--help"], ["project", "show", "--help"]):
                p = subprocess.run([str(executable), *args], env=env, capture_output=True, text=True)
                self.assertEqual(p.returncode, 0, p.stderr)
                self.assertTrue(p.stdout)

    def test_real_git_detects_unpushed_dirty_and_landed_work(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            remote, checkout = root / "remote.git", root / "checkout"

            def git(*args, input=None):
                p = subprocess.run(["git", *map(str, args)], input=input, capture_output=True, text=True)
                self.assertEqual(p.returncode, 0, p.stderr)
                return p.stdout

            git("init", "--bare", remote)
            git("init", "-b", "main", checkout)
            # Build disposable Git objects using the repository's existing
            # author/committer headers, without reading host Git configuration.
            source = Path(__file__).resolve().parent.parent
            headers = git("-C", source, "cat-file", "commit", "HEAD").split("\n\n", 1)[0]
            identity = "\n".join(line for line in headers.splitlines() if line.startswith(("author ", "committer ")))
            tree = git("-C", checkout, "mktree", input="").strip()

            def commit_object(message, parent=None):
                body = f"tree {tree}\n" + (f"parent {parent}\n" if parent else "") + identity + f"\n\n{message}\n"
                return git("-C", checkout, "hash-object", "-t", "commit", "-w", "--stdin", input=body).strip()

            base = commit_object("base")
            git("-C", checkout, "update-ref", "refs/heads/main", base)
            git("-C", checkout, "remote", "add", "origin", remote)
            git("-C", checkout, "push", "origin", "main")
            git("-C", checkout, "checkout", "-b", "boss/test")
            git("-C", checkout, "update-ref", "refs/heads/boss/test", commit_object("worker", base))
            state = inspect_worktree({}, str(checkout), "boss/test", "main")
            self.assertFalse(state["pushed"])
            self.assertFalse(state["in_default"])
            git("-C", checkout, "push", "origin", "boss/test")
            self.assertTrue(inspect_worktree({}, str(checkout), "boss/test", "main")["pushed"])
            (checkout / "unfinished").write_text("not landed")
            self.assertTrue(inspect_worktree({}, str(checkout), "boss/test", "main")["dirty"])
            git("-C", checkout, "push", "origin", "HEAD:main")
            self.assertTrue(inspect_worktree({}, str(checkout), "boss/test", "main")["in_default"])

    def test_ci_aggregate_handles_legacy_and_check_runs(self):
        ci = ci_summary([{"state": "SUCCESS"}, {"conclusion": "SKIPPED"}, {"status": "IN_PROGRESS"}])
        self.assertEqual(ci, {"state": "pending", "passed": 1, "failed": 0, "pending": 1, "skipped": 1})


if __name__ == "__main__":
    unittest.main()
