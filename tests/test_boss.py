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
from boss.integrations import ci_summary, inspect_worktree, remove_worktree
from boss.mcp import MCP
from boss.state import Store, atomic_write
from tests.support import HermeticTestCase


TOKEN = "test-secret-token-never-print"
URL = "https://github.com/example/repo/pull/7"
BODY = """## Intent

Fixes the thing so it works.

Written by Claude Opus 5.5 in Claude Code on behalf of @owner

## What changed

- `fix.py`: does the thing

## Risk Assessment

**Low**: one function.

## Testing

- `python3 -m unittest`: 1 passed
"""
SCHEMAS = {
    "t3_project_list": [], "orchestrator_capabilities": [],
    "t3_thread_launch": ["title"], "t3_thread_read": ["threadId"],
    "t3_thread_list": [], "t3_thread_send": ["threadId", "message"],
    "t3_thread_interrupt": [], "t3_pending_request_list": [],
    "t3_pending_request_read": ["requestId"],
    "t3_pending_request_respond": ["requestId", "answers"],
    "list_thread_pull_requests": [], "unwatch_pull_request": [],
    "t3_thread_organize": ["action"],
    "t3_attachment_prepare_upload": ["upload"], "t3_attachment_discard": ["attachmentId"],
    "t3_thread_send_attachments": ["attachments"],
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
        self.uploads, self.upload_status = [], 200
        self.catalog = {"providers": [{"providerInstanceId": "codex", "models": [
            {"id": "gpt-test", "options": [{"id": "reasoningEffort", "values": ["low", "high", "max"]},
                                           {"id": "serviceTier", "values": ["default", "priority"]}]}]}]}
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_POST(self):
                if self.path.startswith("/api/attachments/upload/"):
                    data = self.rfile.read(int(self.headers["Content-Length"]))
                    fake.uploads.append((self.path, dict(self.headers), data))
                    authorized = self.headers.get("Authorization") == "Bearer " + TOKEN
                    self.send_response(fake.upload_status if authorized else 401)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
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
        if name == "t3_attachment_prepare_upload":
            pending = f"pending-{sum(n == name for n, _ in self.calls)}"
            return {"attachmentId": pending, "relativeUrl": "/api/attachments/upload/" + pending, "expiresAt": 0}
        if name == "t3_attachment_discard":
            return {}
        if name in ("t3_thread_interrupt", "t3_thread_send", "t3_thread_send_attachments", "unwatch_pull_request"):
            return {"sequence": 2}
        raise AssertionError(name)


class FakeGitHub:
    def __init__(self):
        self.branch, self.merges, self.origin_branches, self.origin_heads = "trunk", [], set(), {}
        self.paths = ["boss/cli.py"]
        self.pr = {"url": URL, "state": "OPEN", "isDraft": False, "headRefOid": "a" * 40,
                   "headRefName": "", "baseRefName": "trunk", "mergeStateStatus": "CLEAN", "reviewDecision": "APPROVED",
                   "statusCheckRollup": [{"conclusion": "SUCCESS"}], "mergeCommit": None, "body": BODY}

    def default_branch(self, repository):
        return self.branch

    def branches(self, repository, prefix):
        return {b for b in self.origin_branches if b.startswith(prefix)}

    def branch_head(self, repository, branch):
        return self.origin_heads.get(branch)

    def view(self, url):
        result = copy.deepcopy(self.pr)
        result["ci"] = ci_summary(result["statusCheckRollup"])
        return result

    def files(self, pr):
        if isinstance(self.paths, Exception):
            raise self.paths
        return list(self.paths)

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
        self.removals, self.removal = [], {"state": "removed", "branch": None}
        self.remover = patch("boss.service.remove_worktree",
                             side_effect=lambda *a: self.removals.append(a) or dict(self.removal))
        self.remover.start()

    def tearDown(self):
        self.remover.stop()
        self.probe.stop()
        self.gh_patch.stop()
        self.fake.close()

    def cli(self, *args, stdin="investigate this"):
        output = io.StringIO()
        session = getattr(self, "session", None)
        with contextlib.redirect_stdout(output), patch("sys.stdin", io.StringIO(stdin)), \
                patch.dict(os.environ, {"HERMES_SESSION_KEY": session} if session else {}):
            code = main(list(args))
        return code, output.getvalue()

    def as_owner(self, key="agent:captain"):
        """From here on, boss commands run in the session that owns boss, like the supervisor's."""
        self.session = key
        code, output = self.cli("own")
        self.assertEqual(code, 0, output)

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
        # Wakes are pushed by T3; there is no poll command to run on a timer.
        self.assertEqual(self.cli("poll")[0], 1)
        self.assertEqual(self.cli("bogus")[0], 1)
        self.assertEqual(self.cli("spawn", "repo", "--title", "T", "--typo")[0], 1)
        self.assertEqual(self.cli("status", "--fu")[0], 1)

    def test_spawn_worktree_footer_and_persistence(self):
        meta = self.spawn("--model", "codex:gpt-test", "--effort", "max")
        launch = next(a for n, a in self.fake.calls if n == "t3_thread_launch")
        self.assertEqual(launch["workspaceStrategy"], {"type": "worktree", "branch": "boss/fix-thing", "baseRef": "trunk", "startFromOrigin": True})
        self.assertEqual(launch["modelSelection"], {"instanceId": "codex", "model": "gpt-test", "options": {"reasoningEffort": "max"}})
        brief = (self.store.directory(meta["id"]) / "brief.md").read_text()
        for text in ("investigate this", "Never merge", "Never deploy", "configured Git user", "exactly ONE PR",
                     "Stay on the assigned branch. Never rename it or switch to another branch.", "raises a T3 input request",
                     "AskUserQuestion in Claude Code, request_user_input in Codex", "Never ask in plain text",
                     "Never end your turn to wait for something your shell can watch",
                     "End EVERY turn with exactly one status line", "BOSS: needs-decision: <question>",
                     "BOSS: blocked: <why>", "BOSS: paused: <what you wait on> until <UTC time>", "end it with BOSS: done: <PR url>",
                     "A message that starts with [boss] is from the boss supervisor, not the captain"):
            self.assertIn(text, brief)
        self.assertEqual(launch["message"], brief)
        for name in ("meta.json", "events.log", "brief.md"):
            self.assertTrue((self.store.directory(meta["id"]) / name).exists())
        self.assertTrue(all(h.get("Mcp-Session-Id") == "fake-session" for h in self.fake.headers[1:]))

    def test_spawn_accepts_t3_list_shaped_options(self):
        # Live T3 echoes options as a list of {id, value} pairs.
        self.fake.launch_selection = {"instanceId": "codex", "model": "gpt-test",
                                      "options": [{"id": "reasoningEffort", "value": "max"}]}
        meta = self.spawn("--model", "codex:gpt-test", "--effort", "max")
        self.assertFalse(any(n == "t3_thread_interrupt" for n, _ in self.fake.calls))
        self.assertTrue(meta["thread_id"])

    def test_brief_file_and_scout_contract(self):
        brief = self.home / "brief"
        brief.write_text("read-only question")
        meta = self.spawn("--kind", "scout", "--brief-file", str(brief))
        text = (self.store.directory(meta["id"]) / "brief.md").read_text()
        self.assertIn("read-only question", text)
        self.assertIn("ending with BOSS: done: <one-line summary>", text)
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

    def test_pending_input_and_answer(self):
        meta = self.spawn()
        self.fake.pending = {"request-1": [{"id": "q1", "question": "Which path?", "options": []}]}
        self.assertIn("Which path?", self.cli("status", meta["id"])[1])
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
        self.assertEqual(self.cli("settle", meta["id"])[0], 0)

    def test_scout_report_saved_and_settled(self):
        meta = self.spawn("--kind", "scout")
        self.completed(meta, "BOSS_REPORT: evidence and findings")
        self.assertEqual(self.cli("settle", meta["id"])[0], 0)
        self.assertEqual((self.store.directory(meta["id"]) / "report.md").read_text(), "evidence and findings\n")
        self.assertTrue(self.fake.threads[meta["thread_id"]]["settled"])

    def test_scout_report_is_the_message_above_its_done_line(self):
        meta = self.spawn("--kind", "scout")
        self.completed(meta, "## Findings\nThree callers, all in cli.py.\n\nBOSS: done: three callers")
        self.assertEqual(self.cli("settle", meta["id"])[0], 0)
        self.assertEqual((self.store.directory(meta["id"]) / "report.md").read_text(),
                         "## Findings\nThree callers, all in cli.py.\n")
        other = self.spawn("--kind", "scout", "--title", "One liner")
        self.completed(other, "BOSS: done: nothing calls it")
        self.assertEqual(self.cli("settle", other["id"])[0], 0)
        self.assertEqual((self.store.directory(other["id"]) / "report.md").read_text(), "nothing calls it\n")

    def test_a_scout_that_stops_on_a_question_has_no_report(self):
        meta = self.spawn("--kind", "scout")
        self.completed(meta, "Half done.\nBOSS: needs-decision: which repo?")
        self.assertIn("no report for latest run", self.cli("settle", meta["id"])[1])

    def test_report_from_old_run_does_not_allow_settlement(self):
        meta = self.spawn("--kind", "scout")
        self.completed(meta, "BOSS_REPORT: old report")
        self.cli("status", meta["id"])
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

    def test_renamed_branch_squash_merged_pr_settles(self):
        meta = self.spawn()
        self.completed(meta)
        self.fake.prs = [URL]
        self.gh.pr.update(state="MERGED", headRefName="boss/steer-ledger", mergeCommit={"oid": "b" * 40})
        self.evidence.update(branch="boss/steer-ledger", in_default=False, pushed=False)

        code, output = self.cli("settle", meta["id"])

        self.assertEqual(code, 0, output)
        self.assertTrue(self.store.load(meta["id"])["settled"])
        self.assertTrue(self.fake.threads[meta["thread_id"]]["settled"])
        self.assertIn(("unwatch_pull_request", {"threadId": meta["thread_id"], "url": URL}), self.fake.calls)

    def test_renamed_branch_unmerged_pr_refuses(self):
        for state in ("OPEN", "CLOSED"):
            with self.subTest(state=state):
                meta = self.spawn()
                self.completed(meta)
                self.fake.prs = [URL]
                self.gh.pr.update(state=state, headRefName="boss/steer-ledger")
                self.evidence.update(branch="boss/steer-ledger", in_default=False, pushed=False)

                code, output = self.cli("settle", meta["id"])

                self.assertEqual(code, 2, output)
                self.assertIn("worktree branch mismatch", output)
                self.assertIn("branch is not landed", output)
                if state == "OPEN":
                    self.assertIn("open PR", output)
                self.assertFalse(self.store.load(meta["id"])["settled"])
        self.assertFalse(any(n == "t3_thread_organize" for n, _ in self.fake.calls))

    def test_renamed_branch_merged_pr_refuses_unsafe_work(self):
        for name, pr_changes, git_changes, blocker in (
                ("dirty", {}, {"dirty": True}, "uncommitted work"),
                ("foreign origin", {}, {"origin": "https://github.com/other/repo.git"}, "worktree origin differs"),
                ("foreign PR", {"url": "https://github.com/other/repo/pull/7"}, {}, "branch is not landed"),
                ("wrong base", {"baseRefName": "other-base"}, {}, "branch is not landed"),
                ("different head", {}, {"head": "c" * 40}, "branch is not landed"),
                ("different branch", {"headRefName": "boss/other-worker"}, {}, "worktree branch mismatch")):
            with self.subTest(case=name):
                meta = self.spawn()
                self.completed(meta)
                self.gh.pr.update(url=URL, state="MERGED", headRefName="boss/steer-ledger", baseRefName="trunk")
                self.gh.pr.update(pr_changes)
                self.fake.prs = [self.gh.pr["url"]]
                self.evidence.update(head="a" * 40, branch="boss/steer-ledger", dirty=False,
                                     origin="https://github.com/example/repo.git", in_default=False, pushed=False)
                self.evidence.update(git_changes)

                code, output = self.cli("settle", meta["id"])

                self.assertEqual(code, 2, output)
                self.assertIn(blocker, output)
                self.assertFalse(self.store.load(meta["id"])["settled"])
        self.assertFalse(any(n == "t3_thread_organize" for n, _ in self.fake.calls))

    def test_bare_waiting_is_not_a_permission_prompt_but_a_pending_count_is(self):
        self.as_owner()
        meta = self.spawn()
        thread = self.fake.threads[meta["thread_id"]]
        thread.update(status="waiting", pendingRequestCount=0)
        self.assertEqual(self.cli("status", meta["id"])[1].splitlines()[-1], f'next: boss send {meta["id"]} "<message>"')
        thread["pendingRequestCount"] = 1
        self.assertIn("review pending permissions", self.cli("status", meta["id"])[1])
        self.assertEqual(self.store.load(meta["id"])["observed"]["approvals"], 1)
        # A pending question is counted too, but it is input, not a permission prompt.
        self.fake.pending = {"req-1": [{"id": "q", "question": "Which?"}]}
        self.assertIn(f"next: boss answer {meta['id']}", self.cli("status", meta["id"])[1])
        self.assertEqual(self.store.load(meta["id"])["observed"]["approvals"], 0)

    def test_branch_is_a_title_slug_with_a_suffix_only_on_clash(self):
        code, output = self.cli("spawn", "repo", "--title", "Fix: the Ledger LOCK (über) — and then keep going for a while")
        meta = self.store.load(output.split(":", 1)[0])
        self.assertEqual(meta["branch"], "boss/fix-the-ledger-lock-ber-and-then-keep")
        self.assertRegex(meta["branch"], r"\Aboss/[a-z0-9-]{1,40}\Z")
        self.gh.origin_branches = {"boss/fix-thing", "boss/fix-thing-2", "boss/fix-thing-two"}
        self.assertEqual(self.spawn()["branch"], "boss/fix-thing-3")
        self.assertEqual(self.spawn()["branch"], "boss/fix-thing-4")  # boss/fix-thing-3 is in the ledger
        code, output = self.cli("spawn", "repo", "--title", "!!!")
        task_id = output.split(":", 1)[0]
        self.assertEqual(self.store.load(task_id)["branch"], "boss/" + task_id)
        launch = [a for n, a in self.fake.calls if n == "t3_thread_launch"]
        self.assertEqual([a["workspaceStrategy"]["branch"] for a in launch],
                         ["boss/fix-the-ledger-lock-ber-and-then-keep", "boss/fix-thing-3", "boss/fix-thing-4", "boss/" + task_id])
        self.assertTrue(all(a["title"].startswith("[boss:") for a in launch))  # the ledger keeps the task id

    def test_land_and_settle_match_the_saved_slug_branch(self):
        meta = self.spawn()
        self.assertEqual(meta["branch"], "boss/fix-thing")
        self.completed(meta)
        self.fake.prs = [URL]
        self.gh.pr["headRefName"] = "boss/" + meta["id"]
        self.assertIn("branches differ", self.cli("land", meta["id"], "--approved", "ship it")[1])
        self.gh.pr["headRefName"] = "boss/fix-thing"
        self.assertEqual(self.cli("land", meta["id"], "--approved", "ship it")[0], 0)
        self.evidence.update(in_default=False, pushed=False)
        self.assertEqual(self.cli("settle", meta["id"])[0], 0)

    def test_settle_removes_the_worktree_of_a_merged_pr(self):
        meta = self.spawn()
        self.completed(meta)
        self.fake.prs = [URL]
        self.gh.pr.update(state="MERGED", headRefName="boss/fix-thing")
        self.evidence.update(in_default=False, pushed=False)
        self.removal = {"state": "removed", "branch": "boss/fix-thing"}

        code, output = self.cli("settle", meta["id"])

        self.assertEqual(code, 0, output)
        self.assertIn("worktree removed on test: /fake/worktree, branch boss/fix-thing deleted", output)
        host, path, base, branches, heads = self.removals[0]
        self.assertEqual((host["url"], path, base, branches, heads),
                         (self.fake.url, "/fake/worktree", "trunk", {"boss/fix-thing"}, {"a" * 40}))
        saved = self.store.load(meta["id"])
        self.assertEqual(saved["worktree"], {"path": "/fake/worktree", "state": "removed", "branch": "boss/fix-thing"})
        # Settling again is a no-op; prune has nothing left to do.
        self.assertEqual(self.cli("settle", meta["id"])[0], 0)
        self.assertEqual(self.cli("settle", "--prune-landed")[1].splitlines()[0], "0 leftover worktrees")
        self.assertEqual(len(self.removals), 1)

    def test_settle_removes_a_finished_scout_worktree(self):
        meta = self.spawn("--kind", "scout")
        self.completed(meta, "BOSS_REPORT: findings")
        self.assertIn("worktree removed on test", self.cli("settle", meta["id"])[1])
        self.assertEqual(self.removals[0][3:], ({"boss/fix-thing"}, set()))

    def test_settle_keeps_a_worktree_the_host_refuses_to_remove(self):
        meta = self.spawn()
        self.completed(meta)
        self.fake.prs = [URL]
        self.gh.pr["state"] = "CLOSED"
        self.removal = {"state": "kept", "reason": "uncommitted work"}
        code, output = self.cli("settle", meta["id"], "--discard-approved", "drop it, keep nothing")
        self.assertEqual(code, 0, output)
        self.assertIn("worktree kept on test: uncommitted work (/fake/worktree)", output)
        self.assertEqual(self.store.load(meta["id"])["worktree"]["state"], "kept")

    def test_settle_keeps_an_unlanded_discarded_worktree_without_touching_it(self):
        meta = self.spawn()
        self.completed(meta)
        output = self.cli("settle", meta["id"], "--discard-approved", "abandon it")[1]
        self.assertIn("worktree kept on test: no merged or closed PR", output)
        self.assertEqual(self.removals, [])

    def test_prune_landed_cleans_every_settled_landed_task_on_all_hosts(self):
        config = self.home / "config.toml"
        config.write_text(config.read_text().replace("[projects.repo]", f'''[hosts.far]
url = "{self.fake.url}"
token_env = "T3_TEST_MCP_TOKEN"
ssh = "far.example"
[projects.repo]'''))
        landed, scout, unlanded, active = self.spawn(), self.spawn("--kind", "scout"), self.spawn(), self.spawn()
        for meta in (landed, scout, unlanded):
            self.completed(meta)
            self.cli("settle", meta["id"], "--discard-approved", "older boss kept worktrees")
        self.assertEqual(len(self.removals), 1)  # the scout; nothing PR-backed was landed yet
        self.removals.clear()
        # Tasks settled before this change have no worktree record; prune reads the path from T3.
        for meta in (landed, scout):
            saved = self.store.load(meta["id"])
            saved.pop("worktree", None)
            saved["host"] = "far" if meta is landed else "test"
            saved["pr_urls"] = [URL] if meta is landed else []
            self.store.save(saved, "test_setup")
        self.gh.pr.update(state="MERGED", headRefName=landed["branch"])

        code, output = self.cli("settle", "--prune-landed")

        self.assertEqual(code, 0, output)
        self.assertIn(f"{landed['id']}: worktree removed on far: /fake/worktree", output)
        self.assertIn(f"{scout['id']}: worktree removed on test", output)
        self.assertIn(f"{unlanded['id']}: worktree kept on test: no merged or closed PR", output)
        self.assertNotIn(active["id"], output)
        hosts = sorted(host.get("ssh") for host, *_ in self.removals)
        self.assertEqual(hosts, ["", "far.example"])
        self.assertTrue(all(path == "/fake/worktree" for _, path, *_ in self.removals))
        before = len(self.removals)
        self.cli("settle", "--prune-landed")
        self.assertEqual(len(self.removals), before)  # removed worktrees are not touched again

    def test_settle_with_worktree_gone_lands_from_the_merged_pr(self):
        meta = self.spawn()
        self.completed(meta)
        self.fake.prs = [URL]
        self.gh.pr.update(state="MERGED", mergeCommit={"oid": "b" * 40})
        self.evidence = {"gone": True}
        self.removal = {"state": "gone"}

        code, output = self.cli("settle", meta["id"])

        self.assertEqual(code, 0, output)
        self.assertIn(f"worktree already gone; PR merged on test: /fake/worktree, {URL}", output)
        saved = self.store.load(meta["id"])
        self.assertTrue(saved["settled"])
        self.assertEqual(saved["landed"], {"url": URL, "head": "a" * 40, "merge": {"oid": "b" * 40},
                                           "verified": "worktree already gone; PR merged"})
        self.assertTrue(self.fake.threads[meta["thread_id"]]["settled"])
        # The branch still on origin at the merged head is fine too.
        other = self.spawn()
        self.completed(other)
        self.gh.origin_heads = {other["branch"]: "a" * 40}
        self.assertEqual(self.cli("settle", other["id"])[0], 0)
        # A task boss landed keeps the captain's approval on record.
        landed = self.spawn()
        self.completed(landed)
        self.gh.pr["state"] = "OPEN"
        self.assertEqual(self.cli("land", landed["id"], "--approved", "ship it")[0], 0)
        self.assertEqual(self.cli("settle", landed["id"])[0], 0)
        self.assertEqual(self.store.load(landed["id"])["landed"]["approved"], "ship it")

    def test_settle_with_worktree_gone_refuses_without_a_merged_pr(self):
        for name, pr_changes, origin_heads in (
                ("no PR", None, {}),
                ("open PR", {"state": "OPEN"}, {}),
                ("closed PR", {"state": "CLOSED"}, {}),
                ("foreign PR", {"url": "https://github.com/other/repo/pull/7"}, {}),
                ("wrong base", {"baseRefName": "other-base"}, {}),
                ("branch moved on", {}, {"boss/fix-thing": "c" * 40})):
            with self.subTest(case=name):
                meta = self.spawn()
                self.completed(meta)
                self.gh.pr.update(url=URL, state="MERGED", baseRefName="trunk", mergeCommit={"oid": "b" * 40})
                self.gh.pr.update(pr_changes or {})
                self.fake.prs = [self.gh.pr["url"]] if pr_changes is not None else []
                self.gh.origin_heads = {meta["branch"]: h for h in origin_heads.values()}
                self.evidence = {"gone": True}

                code, output = self.cli("settle", meta["id"])

                self.assertEqual(code, 2, output)
                self.assertIn("worktree is gone and no merged PR found", output)
                self.assertNotIn("exited", output)
                saved = self.store.load(meta["id"])
                self.assertFalse(saved["settled"])
                self.assertNotIn("landed", saved)
        self.assertFalse(any(n == "t3_thread_organize" for n, _ in self.fake.calls))
        self.assertEqual(self.removals, [])

    def test_settle_needs_an_id_or_prune_alone(self):
        self.assertEqual(self.cli("settle")[0], 1)
        meta = self.spawn()
        self.assertEqual(self.cli("settle", meta["id"], "--prune-landed")[0], 1)
        self.assertEqual(self.cli("settle", "--prune-landed", "--discard-approved", "x")[0], 1)

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
            self.assertEqual((Path(temporary) / ".agents/skills/boss").resolve(), root / "skill")
            self.assertEqual((Path(temporary) / ".hermes/plugins/boss").resolve(), root / "plugin")
            self.assertIn("hermes plugins enable boss", result.stdout)
            self.assertFalse((Path(temporary) / ".hermes/config.yaml").exists())
            version = subprocess.run([str(executable), "--version"], env=env, capture_output=True, text=True)
            self.assertEqual(version.returncode, 0, version.stderr)
            self.assertEqual(version.stdout, "boss 0.1.0\n")
            for args in ([], ["--help"], ["spawn", "--help"], ["status", "--help"], ["send", "--help"],
                         ["answer", "--help"], ["stop", "--help"], ["land", "--help"], ["settle", "--help"],
                         ["own", "--help"], ["release", "--help"], ["decide", "--help"], ["decide", "add", "--help"],
                         ["decide", "list", "--help"], ["decide", "close", "--help"],
                         ["project", "--help"], ["project", "list", "--help"],
                         ["project", "add", "--help"], ["project", "show", "--help"],
                         ["goal", "--help"], ["steer", "--help"], ["steer", "list", "--help"]):
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
            # A removed worktree is reported as gone, not as a failed probe.
            self.assertEqual(inspect_worktree({}, str(root / "removed"), "boss/test", "main"), {"gone": True})

    def test_real_git_worktree_removal_never_loses_work(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            remote, checkout = root / "remote.git", root / "checkout"

            def git(*args, input=None):
                p = subprocess.run(["git", *map(str, args)], input=input, capture_output=True, text=True)
                self.assertEqual(p.returncode, 0, p.stderr)
                return p.stdout

            git("init", "--bare", remote)
            git("init", "-b", "main", checkout)
            source = Path(__file__).resolve().parent.parent
            headers = git("-C", source, "cat-file", "commit", "HEAD").split("\n\n", 1)[0]
            identity = "\n".join(line for line in headers.splitlines() if line.startswith(("author ", "committer ")))
            tree = git("-C", checkout, "mktree", input="").strip()

            def commit_object(message, parent):
                body = f"tree {tree}\nparent {parent}\n" + identity + f"\n\n{message}\n"
                return git("-C", checkout, "hash-object", "-t", "commit", "-w", "--stdin", input=body).strip()

            base = git("-C", checkout, "hash-object", "-t", "commit", "-w", "--stdin",
                       input=f"tree {tree}\n{identity}\n\nbase\n").strip()
            git("-C", checkout, "update-ref", "refs/heads/main", base)
            git("-C", checkout, "remote", "add", "origin", remote)
            git("-C", checkout, "push", "origin", "main")
            (checkout / ".git/info/exclude").write_text("__pycache__/\n")

            def worker(name):
                path = root / name
                git("-C", checkout, "worktree", "add", "-b", "boss/" + name, path, "main")
                git("-C", path, "update-ref", "refs/heads/boss/" + name, commit_object(name, base))
                git("-C", path, "reset", "--hard", "-q")
                return path, "boss/" + name, git("-C", path, "rev-parse", "HEAD").strip()

            def remove(path, branch, heads=()):
                return remove_worktree({}, str(path), "main", {branch}, set(heads))

            def branches():
                return git("-C", checkout, "branch", "--list", "boss/*").split()

            path, branch, head = worker("unpushed")
            self.assertEqual(remove(path, branch), {"state": "kept", "reason": "unpushed commits"})
            git("-C", checkout, "push", "origin", branch)
            (path / "notes.txt").write_text("not committed")
            self.assertEqual(remove(path, branch), {"state": "kept", "reason": "uncommitted work"})
            self.assertTrue(path.is_dir())
            (path / "notes.txt").unlink()
            (path / "__pycache__").mkdir()
            (path / "__pycache__/x.pyc").write_text("ignored build output")
            self.assertEqual(remove(path, "boss/other"), {"state": "kept", "reason": "worktree is on another branch: " + branch})
            self.assertEqual(remove(path, branch), {"state": "removed", "branch": branch})
            self.assertFalse(path.exists())
            self.assertNotIn(branch, branches())
            self.assertEqual(remove(path, branch), {"state": "gone"})

            # A squash-merged PR's head is safe on GitHub even after its branch is deleted.
            path, branch, head = worker("squashed")
            self.assertEqual(remove(path, branch, [head])["state"], "removed")
            self.assertNotIn(branch, branches())

            # A scout leaves its branch at the default branch: nothing to lose.
            git("-C", checkout, "worktree", "add", "-b", "boss/scout", root / "scout", "main")
            self.assertEqual(remove(root / "scout", "boss/scout")["state"], "removed")

            git("-C", checkout, "checkout", "-q", "-b", "boss/primary")
            self.assertEqual(remove(checkout, "boss/primary"), {"state": "kept", "reason": "path is the primary checkout"})
            with self.assertRaises(BossError):  # not a work tree at all: nothing is removed
                remove(checkout / ".git", "boss/primary")
            self.assertTrue((checkout / ".git").is_dir())

    def test_ci_aggregate_handles_legacy_and_check_runs(self):
        ci = ci_summary([{"state": "SUCCESS"}, {"conclusion": "SKIPPED"}, {"status": "IN_PROGRESS"}])
        self.assertEqual(ci, {"state": "pending", "passed": 1, "failed": 0, "pending": 1, "skipped": 1})


if __name__ == "__main__":
    unittest.main()
