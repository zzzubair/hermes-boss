#!/usr/bin/env python3
"""Opt-in live scratch-thread check. Never merges, pushes or deploys anything."""
import argparse
import contextlib
import io
import os
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from boss.cli import main
from boss.common import Redactor
from boss.config import Config
from boss.mcp import MCP
from boss.state import Store, atomic_write


def smoke():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:3773/mcp")
    parser.add_argument("--token-env", default="T3_ORACLE_MCP_TOKEN")
    parser.add_argument("--model", default="codex:gpt-6-luna")
    args = parser.parse_args()
    redactor = Redactor()
    token = os.environ[args.token_env]
    redactor.add(token)
    home = Path(tempfile.mkdtemp(prefix="boss-live-smoke-"))
    os.environ["BOSS_HOME"] = str(home)
    atomic_write(home / "config.toml", f'[hosts.smoke]\nurl = "{args.url}"\ntoken_env = "{args.token_env}"\nssh = ""\n')
    client = MCP(args.url, token, redactor)
    instance, model = args.model.split(":", 1)
    launched = client.call("t3_thread_launch", scratch=True, title="boss v1 live smoke",
                           modelSelection={"instanceId": instance, "model": model, "options": {"reasoningEffort": "low"}},
                           message="Reply exactly BOSS_REPORT: smoke-ok. Do not use tools, edit files, create PRs or deploy.")
    store = Store(home, redactor)
    task_id = store.new_id()
    meta = {"id": task_id, "project": "scratch", "project_id": launched["projectId"],
            "host": "smoke", "repository": "", "title": "boss v1 live smoke", "kind": "scout",
            "delivery": "local-only", "branch": "", "base": "", "thread_id": launched["threadId"],
            "settled": False, "scratch": True, "status": launched.get("status")}
    store.text(task_id, "brief.md", "Reply exactly BOSS_REPORT: smoke-ok.\n")
    store.save(meta, "launched")
    print(f"spawn: scratch thread={launched['threadId']}", flush=True)

    def command(argv):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(argv)
        value = output.getvalue()
        if code:
            raise RuntimeError(f"boss {' '.join(argv)} exited {code}: {value}; ledger={home}")
        return value

    try:
        # T3 blocks until the run ends; each call stays inside the MCP client timeout.
        for _ in range(8):
            if not client.call("t3_thread_wait", threadId=launched["threadId"], timeoutMs=25000)["timedOut"]:
                break
        else:
            raise RuntimeError("scratch worker did not complete within 200 seconds")
        status = command(["status", task_id])
        if " completed" not in status.splitlines()[0]:
            raise RuntimeError(status)
        print("status: " + status.splitlines()[0].split(" ", 1)[1])
        report = store.directory(task_id) / "report.md"
        if not report.exists() or report.read_text().strip() != "smoke-ok":
            print(command(["status", task_id, "--full"]), end="")
            raise RuntimeError("scratch report was not captured")
        print("report: " + report.read_text().strip())
        print(command(["settle", task_id]), end="")
        live = client.call("t3_thread_read", threadId=launched["threadId"], limit=1)
        if not live["thread"]["settled"]:
            raise RuntimeError("live thread did not settle")
        print("verified: T3 settled=true")
        print("fleet: 0 active tasks")
        return 0
    except Exception as exc:
        print(redactor(f"smoke_failed: {exc}; thread={launched['threadId']}; ledger={home}"))
        return 1


if __name__ == "__main__":
    raise SystemExit(smoke())
