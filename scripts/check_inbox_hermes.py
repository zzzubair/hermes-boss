#!/usr/bin/env python3
"""Drive boss's inbox hooks through real Hermes hook dispatch (model_tools + hermes_cli.plugins).

Usage, with the Hermes venv's python: python -I scripts/check_inbox_hermes.py <hermes-agent checkout>
Temporary BOSS_HOME and HERMES_HOME only; no network, no model calls, Hermes source untouched.
"""
import json, os, sys, tempfile
from pathlib import Path

repo, hermes = Path(__file__).resolve().parent.parent, Path(sys.argv[1])
sys.path[:0] = [str(hermes), str(repo)]
boss_home = Path(tempfile.mkdtemp(prefix="boss-smoke-"))
os.environ.update(BOSS_HOME=str(boss_home), HERMES_HOME=tempfile.mkdtemp(prefix="hermes-smoke-"))

from hermes_cli import plugins
from hermes_cli.lifecycle import invoke_hook
import model_tools
import plugin as boss_plugin
from boss.common import Redactor
from boss.inbox import Inbox
from boss.state import Store

manager = plugins.get_plugin_manager()
for name, cb in (("pre_llm_call", boss_plugin.turn_hook(None)), ("transform_tool_result", boss_plugin.on_tool_result),
                 ("on_session_end", boss_plugin.turn_end_hook)):
    manager._hooks.setdefault(name, []).append(cb)

# Owner + one task in a real ledger; one worker event held in the inbox.
(boss_home / "owner.json").write_text(json.dumps({"version": 1, "session_key": "agent:main:discord:dm:1", "surface": "discord",
                                                  "platform": "discord", "label": "Discord DM", "claimed_at": "2026-10-26T15:00:00+00:00"}))
store = Store(boss_home, Redactor())
store.save({"id": "a1b2c3d4e5f6", "title": "Fix the ledger lock", "settled": False,
            "observed": {"run_id": "run-1", "requests": [], "prs": []}}, "launched")
from gateway.session_context import set_session_vars
set_session_vars(session_key="agent:main:discord:dm:1", platform="discord")

ctx = invoke_hook("pre_llm_call", session_id="sess-1", task_id="t", turn_id="turn-1", user_message="hi",
                  conversation_history=[], is_first_turn=False, model="m", platform="discord", parent_session_id="", sender_id="")
print("turn start, empty inbox:", ctx)
Inbox(boss_home).hold("thread-1:done:run-1", "a1b2c3d4e5f6", "done", "Fix the ledger lock: finished: lock fixed, 40 tests pass", run="run-1")
ids = model_tools._CallIds("t", "sess-1", "call-1", "turn-1", "")
out = model_tools._apply_transform_tool_result_hook("terminal", {"command": "ls"}, '{"output": "ok"}', 5, ids)
print("owner tool result:", json.dumps(out))
print("same tool again (acked once):", json.dumps(model_tools._apply_transform_tool_result_hook("terminal", {}, '{"output": "ok"}', 5, ids)))
Inbox(boss_home).hold("thread-1:blocked:run-2", "a1b2c3d4e5f6", "blocked", "Fix the ledger lock: blocked: no token", run="run-1")
sub = model_tools._CallIds("t", "sess-subagent", "call-2", "turn-9", "")
print("subagent tool result (same session key, other session id):",
      json.dumps(model_tools._apply_transform_tool_result_hook("terminal", {}, '{"output": "ok"}', 5, sub)))
invoke_hook("on_session_end", session_id="sess-1", task_id="t", turn_id="turn-1", completed=True, failed=False,
            interrupted=False, turn_exit_reason="text_response(stop)", model="m", platform="discord")
print("turn mark after on_session_end:", json.loads((boss_home / "owner-turn.json").read_text())["busy"])
ctx = invoke_hook("pre_llm_call", session_id="sess-1", task_id="t", turn_id="turn-2", user_message="next",
                  conversation_history=[], is_first_turn=False, model="m", platform="discord", parent_session_id="", sender_id="")
print("next turn start:", json.dumps(ctx))
print("inbox log:")
for r in Inbox(boss_home).records():
    print("  ", {k: r[k] for k in ("event", "key", "keys", "via", "reason") if k in r})
