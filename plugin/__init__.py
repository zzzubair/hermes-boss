"""Bounded local context hook, plus an opt-in gateway wake loop."""
import json
import os
from pathlib import Path
import re
import stat
import sys


def on_pre_llm_call(**kwargs):
    """Hermes passes turn metadata as kwargs and accepts {context: text} or None."""
    try:
        home = Path(os.environ.get("BOSS_HOME", "~/.boss")).expanduser()
        # Nonblocking open also makes an accidentally replaced FIFO harmless.
        descriptor = os.open(home / "attention.json", os.O_RDONLY | os.O_NONBLOCK)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_size > 4096:
                return None
            data = json.loads(os.read(descriptor, 4097))
        finally:
            os.close(descriptor)
        if not isinstance(data, dict) or data.get("version") != 1:
            return None
        count, items = data.get("count"), data.get("items")
        if type(count) is not int or count <= 0 or not isinstance(items, list) or not 0 < len(items) <= 6 or count < len(items):
            return None
        details = []
        for item in items:
            if (not isinstance(item, dict) or item.get("kind") not in ("decision", "input", "finished") or
                    not isinstance(item.get("id"), str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", item["id"])):
                return None
            details.append(f"{item['id']} {item['kind']}")
        if count > len(items):
            details.append(f"+{count - len(items)} more")
        return {"context": f"boss: {count} need you ({', '.join(details)}) - run boss status"}
    except (OSError, ValueError, TypeError, OverflowError, RuntimeError):
        # A missing/corrupt local cache must never break an unrelated Hermes turn.
        return None


def register(ctx):
    ctx.register_hook("pre_llm_call", on_pre_llm_call)
    # The marker alone is inherited by subprocesses. Require the gateway module
    # in THIS interpreter too, so CLI/one-shot children never start poll threads.
    main_spec = getattr(sys.modules.get("__main__"), "__spec__", None)
    gateway_loaded = "gateway.run" in sys.modules or getattr(main_spec, "name", None) == "gateway.run"
    if (os.environ.get("_HERMES_GATEWAY") != "1" or not gateway_loaded or
            getattr(getattr(ctx, "_manager", None), "_cli_ref", None) is not None):
        return
    root = str(Path(__file__).resolve().parent.parent)
    if root not in sys.path:
        sys.path.insert(0, root)
    from boss.wake import start_gateway_wake
    start_gateway_wake(ctx)
