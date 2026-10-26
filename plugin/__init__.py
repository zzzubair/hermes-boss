"""Bounded local context hook, /boss ownership, /away and /back, plus the owner-routed wake loop."""
from datetime import datetime
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys


BOSS_WORD = re.compile(r"\bboss\b", re.IGNORECASE)


def when(at):
    """Local HH:MM (with weekday when not today); ValueError when the stamp is unusable."""
    since = datetime.fromisoformat(at)
    if since.tzinfo is None:
        raise ValueError("timestamp has no timezone")
    local = since.astimezone()
    return local.strftime("%H:%M" if local.date() == datetime.now().astimezone().date() else "%a %H:%M")


def read_small(path):
    """A tiny local JSON record, or None. Nonblocking open makes an accidentally replaced FIFO harmless."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 4096:
            return None
        return json.loads(os.read(descriptor, 4097))
    finally:
        os.close(descriptor)


def current_session_key():
    """The session whose turn is running: Hermes' plugin helper, else its session env."""
    try:
        from hermes_cli.plugin_session import session_key
        return session_key() or ""
    except Exception:
        pass
    try:
        from gateway.session_context import get_session_env
        return get_session_env("HERMES_SESSION_KEY", "") or ""
    except Exception:
        return os.environ.get("HERMES_SESSION_KEY", "")


def owner_line(owner, kwargs):
    """None when this turn belongs to the owner (or the session is unknown); else the one line to add."""
    sessions = {current_session_key(), str(kwargs.get("session_id") or "")} - {""}
    if not sessions:
        return None
    if owner is None:
        return "boss: not open in any session; /boss opens it here and sends its wakes here"
    if not isinstance(owner, dict) or not isinstance(owner.get("session_key"), str) or not isinstance(owner.get("label"), str):
        return None
    if owner["session_key"] in sessions:
        return None
    label = " ".join(owner["label"].split())[:60]
    return f"boss: owned by {label} since {when(owner['claimed_at'])}; /boss take to move it here"


def away_line(away):
    """One line while the captain is away; None when the record is malformed."""
    if not isinstance(away, dict) or not isinstance(away.get("since"), str) or not isinstance(away.get("note", ""), str):
        return None
    try:
        since = when(away["since"])
    except ValueError:
        return None
    note = " ".join(away.get("note", "").split())[:80]
    return (f"boss: captain away since {since}" + (f" ({note})" if note else "") +
            "; don't ask him anything, act on your own judgement and queue decisions for /back;"
            " anything he says or decides in a check-in counts in full, record it with boss log.")


def on_pre_llm_call(**kwargs):
    """Hermes passes turn metadata as kwargs and accepts {context: text} or None."""
    try:
        home = Path(os.environ.get("BOSS_HOME", "~/.boss")).expanduser()
        data = read_small(home / "attention.json")
        if not isinstance(data, dict) or data.get("version") != 1:
            return None
        # Only the owner session gets the attention line. Elsewhere, one ownership line,
        # and only on a turn that mentions boss or while something is waiting.
        foreign = owner_line(read_small(home / "owner.json"), kwargs)
        if foreign is not None:
            message = kwargs.get("user_message")
            mentioned = isinstance(message, str) and BOSS_WORD.search(message)
            return {"context": foreign} if mentioned or (type(data.get("count")) is int and data["count"] > 0) else None
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
    except (OSError, ValueError, TypeError, KeyError, OverflowError, RuntimeError):
        # A missing/corrupt local cache must never break an unrelated Hermes turn.
        return None


def run(argv, session_key="", surface=""):
    """Run the CLI as the calling session (so ownership applies); (exit code, text without hints)."""
    command = [sys.executable, str(Path(__file__).resolve().parent.parent / "bin/boss"), *argv]
    env = {k: v for k, v in os.environ.items() if k not in ("HERMES_SESSION_KEY", "HERMES_SESSION_PLATFORM")}
    if session_key:
        env.update(HERMES_SESSION_KEY=session_key, HERMES_SESSION_PLATFORM=surface or "")
    # Never drop his command because another boss command holds the ledger: wait for it.
    env["BOSS_WAIT_FOR_LEDGER"] = "1"
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=60, check=False, env=env)
    except (OSError, subprocess.SubprocessError) as exc:
        return 3, f"boss: could not run ({type(exc).__name__}); try boss {' '.join(argv[:1])} in a shell"
    lines = result.stdout.splitlines()
    if result.returncode == 2 and lines[:1] == ["error: refused"]:
        # Refusals read as one sentence in chat: "boss is owned by X since 14:02. Run /boss take ..."
        message = next((l[len("message: "):] for l in lines if l.startswith("message: ")), "refused")
        hint = next((l[len("next: "):] for l in lines if l.startswith("next: ")), "")
        return 2, f"{message}. {hint[:1].upper() + hint[1:]}." if hint else message
    text = "\n".join(line for line in lines if not line.startswith("next: ")).strip()
    return result.returncode, text or "boss: no output"


def session(kwargs):
    """Which session ran a slash command. Hermes passes these keywords when the handler declares them."""
    key = str(kwargs.get("session_key") or "") or current_session_key()
    surface = str(kwargs.get("platform") or "")
    if not surface:
        try:
            from hermes_cli.plugin_session import session_surface
            surface = session_surface() or ""
        except Exception:
            surface = os.environ.get("HERMES_SESSION_PLATFORM", "")
    return key, surface


def on_away(raw_args="", session_key="", platform="", source=None, session_id=""):
    note = " ".join(str(raw_args or "").split())
    return run(["away", *(["--", note] if note else [])], *session(locals()))[1]


def on_back(raw_args="", session_key="", platform="", source=None, session_id=""):
    return run(["back"], *session(locals()))[1]


def boss_command(ctx):
    """/boss opens boss here; /boss take moves it here; /boss release leaves nobody owning it."""
    def on_boss(raw_args="", session_key="", platform="", source=None, session_id=""):
        words = str(raw_args or "").split()
        action = words[0].lower() if words else ""
        if len(words) > 1 or action not in ("", "take", "release", "status"):
            return "usage: /boss [take|release|status]"
        key, surface = session(locals())
        if action == "status":
            return run(["own", "status"])[1]
        if action == "release":
            return run(["release"], key, surface)[1]
        if not key:
            return "boss: this Hermes did not say which session ran /boss; update Hermes, or run boss own from a tool"
        boss_package()
        from boss.owner import label_for
        label = label_for(surface, source)
        home = Path(os.environ.get("BOSS_HOME", "~/.boss")).expanduser()
        try:
            before = read_small(home / "owner.json")
        except (OSError, ValueError):
            before = None
        code, text = run(["own", *(["--take"] if action == "take" else []), "--label", label], key, surface)
        previous = before.get("session_key") if isinstance(before, dict) else None
        if code == 0 and action == "take" and isinstance(previous, str) and previous and previous != key:
            try:
                # Best effort: an offline previous owner simply misses this note.
                ctx.inject_message(f"[boss] boss moved to {label}; this session no longer gets boss wakes."
                                   " /boss take moves it back.", role="user", session_key=previous)
            except Exception:
                pass
        return text
    return on_boss


def boss_package():
    root = str(Path(__file__).resolve().parent.parent)
    if root not in sys.path:
        sys.path.insert(0, root)


def turn_hook(ctx):
    """The context hook, plus the two event-driven listener chores no timer does for us."""
    started = []

    def hook(**kwargs):
        result = on_pre_llm_call(**kwargs)
        try:
            if not started:
                # A host this process turned out to be (it can inject now): listen here.
                boss_package()
                from boss.wake import start_wake
                if start_wake(ctx) is not None:
                    started.append(True)
            context = (result or {}).get("context", "")
            if context.startswith("boss:") and " need you " in context and "owned by" not in context:
                # The owner is back and something is waiting: ask the listener to deliver held wakes.
                from boss.wake import poke
                poke(Path(os.environ.get("BOSS_HOME", "~/.boss")).expanduser())
        except Exception:
            pass
        return result
    return hook


def register(ctx):
    ctx.register_hook("pre_llm_call", turn_hook(ctx))
    command = getattr(ctx, "register_command", None)
    if command is not None:
        command("boss", boss_command(ctx), description="Open boss in this session: it gets wakes and a catch-up",
                args_hint="[take|release|status]")
        # Only these two commands change presence; other chat is a check-in.
        command("away", on_away, description="Captain away: no questions, decisions queue for /back", args_hint="[note]")
        command("afk", on_away, description="Alias for /away", args_hint="[note]")
        command("back", on_back, description="Captain back: catch-up timeline and numbered decisions")
    # Plugins load before the gateway marks itself (_HERMES_GATEWAY), so never gate
    # on it. Host processes (gateway, TUI, desktop) start the listener now; any
    # other process that later proves it can inject starts it from its first turn.
    boss_package()
    from boss.wake import start_wake
    start_wake(ctx)
