"""Bounded local context hook, /boss ownership, /away and /back, the owner's inbox, plus the wake listener.

The owner's inbox (boss/inbox.py) reaches its model through `boss watch`, a tracked
background command whose exit Hermes shows as a system notice, and appended to a
tool result mid-turn (transform_tool_result). Nothing goes into turn-start context
or an injected message by default: Hermes shows both as if the captain sent them.
Turn start and end hooks keep the on-disk busy mark the listener checks.
"""
from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys


BOSS_WORD = re.compile(r"\bboss\b", re.IGNORECASE)
DEAF = "boss: worker updates are not arriving; run boss doctor"


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


def listening(home):
    """True while some process holds wake.lock; a lock nobody holds means no listener runs.

    The probe takes the lock without waiting and drops it at once, so a listener
    starting right now waits a moment, never misses its turn."""
    try:
        descriptor = os.open(home / "wake.lock", os.O_RDWR | os.O_NONBLOCK)
    except FileNotFoundError:
        return False
    except OSError:
        return True  # unsure: never cry wolf
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        return False
    except BlockingIOError:
        return True
    except OSError:
        return True
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


def boss_home_path():
    return Path(os.environ.get("BOSS_HOME", "~/.boss")).expanduser()


def owner_turn(owner, kwargs):
    """The owner's session key when this pre_llm_call is the owner's own top-level turn, else None.

    A subagent inherits its parent's session key, so a turn with a parent session never counts.
    """
    if not isinstance(owner, dict) or not isinstance(owner.get("session_key"), str) or kwargs.get("parent_session_id"):
        return None
    sessions = {current_session_key(), str(kwargs.get("session_id") or "")} - {""}
    return owner["session_key"] if owner["session_key"] in sessions else None


def turn_start(kwargs):
    """Mark the owner's top-level turn busy, so its tool results carry the inbox; nothing else."""
    home = boss_home_path()
    key = owner_turn(read_small(home / "owner.json"), kwargs)
    if key is None:
        return
    boss_package()
    from boss import inbox
    from boss.wake import poke
    inbox.turn_started(home, key, str(kwargs.get("session_id") or ""))
    # The owner's turn is busy now, so this sweep (stalls, merged PRs) reaches it mid-turn, never by injection.
    poke(home)


def on_tool_result(**kwargs):
    """transform_tool_result: a worker that reported mid-turn is seen in this same turn.

    Only a tool the owner's running turn called (its session id, recorded at turn
    start) gets the inbox appended; every other session's results are untouched.
    """
    try:
        result = kwargs.get("result")
        session_id = str(kwargs.get("session_id") or "")
        if not isinstance(result, str) or not session_id:
            return None
        boss_package()
        from boss import inbox
        home = boss_home_path()
        box = inbox.Inbox(home)
        turn = inbox.live_turn(home)
        if turn is None or turn.get("session_id") != session_id:
            return None
        inbox.turn_touched(home, turn)
        if not box.maybe_open():
            return None
        owner = read_small(home / "owner.json")
        if not isinstance(owner, dict) or owner.get("session_key") != turn.get("session_key"):
            return None
        from boss.state import Store
        block = box.take(Store(home, None).load, "tool", owner.get("label", ""))
        return f"{result}\n\n{block}" if block else None
    except Exception:
        # Never break the owner's tool call over the inbox.
        return None


def turn_end_hook(**kwargs):
    """on_session_end / agent_loop_stopped: the owner is idle, so the listener may inject what is left."""
    try:
        boss_package()
        from boss import inbox
        from boss.wake import poke
        home = boss_home_path()
        if inbox.turn_ended(home, session_id=str(kwargs.get("session_id") or "") or None,
                            session_key=str(kwargs.get("session_key") or "") or None):
            poke(home)
    except Exception:
        pass
    return None


def on_pre_llm_call(**kwargs):
    """Hermes passes turn metadata as kwargs and accepts {context: text} or None.

    The app shows this context inside the captain's own message, so the owner gets
    at most the one-line dead-listener warning; inbox items, away state and the
    open count reach the supervisor through boss watch instead.
    """
    try:
        home = boss_home_path()
        data = read_small(home / "attention.json")
        if not isinstance(data, dict) or data.get("version") != 1:
            return None
        # Only the owner session gets the attention line. Elsewhere, one ownership line,
        # and only on a turn that mentions boss or while something is waiting.
        owner = read_small(home / "owner.json")
        foreign = owner_line(owner, kwargs)
        if foreign is not None:
            message = kwargs.get("user_message")
            mentioned = isinstance(message, str) and BOSS_WORD.search(message)
            return {"context": foreign} if mentioned or (type(data.get("count")) is int and data["count"] > 0) else None
        active = data.get("active")
        if owner_turn(owner, kwargs) and type(active) is int and active > 0 and not listening(home):
            return {"context": DEAF}
        return None
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
        home = boss_home_path()
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
        except Exception:
            pass
        try:
            turn_start(kwargs)
        except Exception:
            pass
        return result
    return hook


def register(ctx):
    ctx.register_hook("pre_llm_call", turn_hook(ctx))
    ctx.register_hook("transform_tool_result", on_tool_result)
    ctx.register_hook("on_session_end", turn_end_hook)
    ctx.register_hook("agent_loop_stopped", turn_end_hook)
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
