"""Bounded local context hook, /away and /back, plus an opt-in gateway wake loop."""
from datetime import datetime
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time


def away_line(away):
    """One line while the captain is away; None when the record is malformed."""
    if not isinstance(away, dict) or not isinstance(away.get("since"), str) or not isinstance(away.get("note", ""), str):
        return None
    since = datetime.fromisoformat(away["since"])
    if since.tzinfo is None:
        return None
    local = since.astimezone()
    when = local.strftime("%H:%M" if local.date() == datetime.now().astimezone().date() else "%a %H:%M")
    note = " ".join(away.get("note", "").split())[:80]
    return (f"boss: captain away since {when}" + (f" ({note})" if note else "") +
            "; don't ask him anything, act on your own judgement and queue decisions for /back;"
            " anything he says or decides in a check-in counts in full, record it with boss log.")


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
        lines = []
        if "away" in data:
            line = away_line(data["away"])
            if line is None:
                return None
            lines.append(line)
        count, items = data.get("count"), data.get("items")
        if lines and count == 0 and items == []:
            return {"context": lines[0]}
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
        lines.append(f"boss: {count} need you ({', '.join(details)}) - run boss status")
        return {"context": " ".join(lines)}
    except (OSError, ValueError, TypeError, OverflowError, RuntimeError):
        # A missing/corrupt local cache must never break an unrelated Hermes turn.
        return None


def run_boss(*argv):
    """Captain slash commands run the CLI under its ledger lock and drop supervisor hints."""
    command = [sys.executable, str(Path(__file__).resolve().parent.parent / "bin/boss"), *argv]
    try:
        for _ in range(20):
            result = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)
            # A concurrent poll holds the lock for seconds at most; never drop his command.
            if not (result.returncode == 2 and "holds the ledger" in result.stdout):
                break
            time.sleep(0.5)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"boss: could not run ({type(exc).__name__}); try boss {' '.join(argv[:1])} in a shell"
    text = "\n".join(line for line in result.stdout.splitlines() if not line.startswith("next: ")).strip()
    return text or "boss: no output"


def on_away(raw_args=""):
    note = " ".join(str(raw_args or "").split())
    return run_boss("away", *(["--", note] if note else []))


def on_back(raw_args=""):
    return run_boss("back")


def register(ctx):
    ctx.register_hook("pre_llm_call", on_pre_llm_call)
    command = getattr(ctx, "register_command", None)
    if command is not None:
        # Only these two commands change presence; other chat is a check-in.
        command("away", on_away, description="Captain away: no questions, decisions queue for /back", args_hint="[note]")
        command("afk", on_away, description="Alias for /away", args_hint="[note]")
        command("back", on_back, description="Captain back: catch-up timeline and numbered decisions")
    # Plugins load before gateway.run is imported, so do not require it here.
    # The loop polls nothing until the gateway publishes its injector, which
    # CLI/one-shot children never do; a child with the inherited marker only idles.
    if (os.environ.get("_HERMES_GATEWAY") != "1" or
            getattr(getattr(ctx, "_manager", None), "_cli_ref", None) is not None):
        return
    root = str(Path(__file__).resolve().parent.parent)
    if root not in sys.path:
        sys.path.insert(0, root)
    from boss.wake import start_gateway_wake
    start_gateway_wake(ctx)
