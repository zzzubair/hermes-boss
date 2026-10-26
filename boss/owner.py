"""One Hermes session owns boss at a time (Firstmate model).

boss is inactive until a session opens it with /boss (or `boss own`). Only the
owner gets wakes and may mutate the ledger; other sessions are refused with the
owner's name and how to take over. Claims, transfers and releases go to the
captain journal and show up in the /back timeline.
"""
from datetime import datetime
import json
import os
from types import SimpleNamespace

from . import captain
from .common import BossError, refuse
from .state import atomic_write, now


FILE = "owner.json"
FIELDS = {"session_key", "surface", "platform", "label", "claimed_at"}
SURFACE_LABELS = {"cli": "Hermes CLI", "tui": "Hermes TUI", "desktop": "Hermes app", "acp": "T3"}
TAKE_HINT = "Run /boss take (or boss own --take) to move it here"


def label_for(surface, source=None):
    """Short human name for a session: 'Discord DM', 'Hermes app', 'T3'."""
    surface = str(surface or "").strip().lower()
    if surface in SURFACE_LABELS or not surface:
        return SURFACE_LABELS.get(surface, "Hermes session")
    name = surface.replace("_", " ").title()
    chat_type = str(getattr(source, "chat_type", "") or "").lower()
    chat = " ".join(str(getattr(source, "chat_name", "") or "").split())[:40]
    return f"{name} DM" if chat_type == "dm" else f"{name} {chat}" if chat else f"{name} chat"


def sender_label(env):
    """Who ran a command, from the session vars Hermes gives its terminal: 'Discord DM', 'T3', 'a shell'."""
    surface = env.get("HERMES_SESSION_PLATFORM") or env.get("HERMES_SESSION_SOURCE") or ""
    if not surface and not env.get("HERMES_SESSION_KEY"):
        return "a shell"
    return label_for(surface, SimpleNamespace(chat_type=env.get("HERMES_SESSION_CHAT_TYPE", ""),
                                              chat_name=env.get("HERMES_SESSION_CHAT_NAME", "")))


def read(home):
    """The current owner record, or None. A malformed file is a ledger error, never 'no owner'."""
    path = home / FILE
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise BossError("ledger", f"unreadable {FILE}: {exc}", f"inspect {path}; boss release resets it") from None
    if (not isinstance(record, dict) or not FIELDS <= set(record) or
            not all(isinstance(record[k], str) for k in FIELDS) or not record["session_key"]):
        raise BossError("ledger", f"invalid {FILE}", f"inspect {path}; boss release resets it")
    return record


def stamp(at):
    local = datetime.fromisoformat(at).astimezone()
    return local.strftime("%H:%M" if local.date() == datetime.now().astimezone().date() else "%a %H:%M")


def owned(record):
    return f"boss is owned by {record['label']} since {stamp(record['claimed_at'])}"


def guard(home, key):
    """Gate one mutating command. Returns a warning for a human shell, raises for a foreign session."""
    record = read(home)
    if not key:
        return f"warning: {owned(record)}; this shell is not a Hermes session" if record else None
    if record is None:
        refuse("boss is not open in any session",
               "run /boss (or boss own) in the session that should own boss and get its wakes")
    if record["session_key"] != key:
        refuse(owned(record), TAKE_HINT)
    return None


def status_lines(home):
    record = read(home)
    if record is None:
        return ["owner: none; wakes are held", "next: run /boss in the session that should get wakes"]
    return [f"owner: {record['label']}", f"since: {stamp(record['claimed_at'])}",
            f"surface: {record['surface'] or 'unknown'}", "next: boss"]


def last_look(events):
    """When any owner last saw a catch-up; None before the first claim."""
    looks = [r["at"] for r in events if r["event"] == "own" and r.get("action") in ("claim", "take", "look")]
    return looks[-1] if looks else None


def catch_up(store, events, end):
    """What changed since the owner last looked, then open numbered decisions."""
    lines = []
    start = last_look(events)
    if start is None:
        lines.append("First time boss is open; nothing earlier to compare.")
    else:
        # Strictly after the last look: that look (or claim) itself is not news.
        rows = [row for row in captain.timeline(store, captain.parse(start), captain.parse(end), events)
                if row[0] > captain.parse(start)]
        if rows:
            lines.append(f"Since you last looked at {captain.clock(start)} ({captain.plural(len(rows), 'event')}):")
            lines.extend(captain.timeline_lines(rows))
        else:
            lines.append(f"Nothing changed since you last looked at {captain.clock(start)}.")
    away = captain.presence(events)
    if away:
        # Opening boss is not /back: presence stays, and nothing is asked while he is away.
        lines.append(f"Captain away {captain.since_text(away)}.")
        lines.extend(captain.numbered_view(store, events))
    else:
        lines.extend(captain.ask(store, reason="catchup")[0])
    return lines


def own(store, key, surface, label, take=False):
    """Claim, re-open or take over boss for one session."""
    if not key:
        raise BossError("usage", "boss own needs HERMES_SESSION_KEY; run it from a Hermes session or use /boss",
                        "/boss in the chat that should own boss", 1)
    record = read(store.home)
    events = store.captain_events()
    end = now()
    if record is not None and record["session_key"] == key:
        label, action, previous = record["label"], "look", None
        lines = [f"boss is open here ({label}) since {stamp(record['claimed_at'])}."]
    elif record is not None and not take:
        refuse(owned(record), TAKE_HINT)
    else:
        label = label or label_for(surface)
        atomic_write(store.home / FILE, json.dumps({
            "version": 1, "session_key": key, "surface": surface or "", "platform": surface or "",
            "label": label, "claimed_at": end}, sort_keys=True) + "\n")
        action, previous = ("take", record) if record else ("claim", None)
        lines = [f"boss moved here from {record['label']}; wakes now come to {label}." if record else
                 f"boss is open in {label}; wakes come here."]
    lines.extend(catch_up(store, events, end))
    store.captain("own", action=action, label=label, surface=surface or "",
                  **({"previous": previous["label"]} if previous else {}))
    from .watch import REMIND, waiting
    # A watch already waiting can only be this session's when it re-opens; a claim or take has none yet.
    if action != "look" or not waiting(store.home):
        lines.append(REMIND)
    return lines + ["next: boss"]


def release(store):
    record = read(store.home)
    if record is None:
        return ["Nobody owns boss; nothing changed.", "next: run /boss in the session that should get wakes"]
    os.unlink(store.home / FILE)
    store.captain("own", action="release", label=record["label"], surface=record["surface"])
    return [f"Released boss from {record['label']}. Wakes are held until a session runs /boss.",
            "next: run /boss in the session that should get wakes"]
