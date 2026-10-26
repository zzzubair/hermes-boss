"""Captain presence, numbered decision rounds and the deterministic /back catch-up.

Only `away` and `back` change presence. Everything the captain says in between is a
check-in with full authority; the supervisor records it and never asks him things.
Captain-facing text carries no task ids or commit SHAs.
"""
from datetime import datetime, timezone
import re

from .common import BossError, refuse


STATUS_WORDS = {"completed": "finished", "failed": "failed", "rolled_back": "failed",
                "interrupted": "stopped", "cancelled": "stopped", "waiting": "waiting for input"}
GROUP_AFTER = 12


def plain(value, limit=240):
    """One line of plain words for the captain; no JSON quoting."""
    text = " ".join(str(value).split())
    return text if limit is None or len(text) <= limit else text[:limit - 1].rstrip() + "…"


def plural(count, word):
    return f"{count} {word}" + ("" if count == 1 else "s")


def parse(at):
    return datetime.fromisoformat(at)


def clock(at):
    return parse(at).astimezone().strftime("%H:%M")


def presence(events):
    """The open away record, or None. A repeated away keeps the first one."""
    away = None
    for record in events:
        if record["event"] == "away" and away is None:
            away = record
        elif record["event"] == "back":
            away = None
    return away


def duration(start, end):
    minutes = int((end - start).total_seconds() // 60)
    if minutes < 1:
        return "under a minute"
    days, rest = divmod(minutes, 1440)
    hours, minutes = divmod(rest, 60)
    parts = [f"{days}d"] if days else []
    parts += [f"{hours}h"] if hours else []
    parts += [f"{minutes}m"] if minutes and not days else []
    return " ".join(parts)


def since_text(away):
    note = " ".join(str(away.get("note") or "").split())
    return f"since {clock(away['at'])}" + (f" ({plain(note, 80)})" if note else "")


def away(store, note):
    current = presence(store.captain_events())
    if current:
        return [f"Already away {since_text(current)}; nothing changed.", "next: boss back"]
    record = store.captain("away", note=" ".join(note.split()) or None)
    return [f"Away {since_text(record)}. I won't ask you anything; anything you decide in a check-in counts.",
            "next: boss back"]


# Decisions -------------------------------------------------------------------

def open_decisions(store):
    decisions = [d for d in store.decisions() if not d.get("closed")]
    return sorted(decisions, key=lambda d: d.get("created_at") or "")


def rounds(events):
    return [r for r in events if r["event"] == "ask"]


def asked_counts(events):
    counts = {}
    for record in rounds(events):
        for decision_id in record["items"]:
            counts[decision_id] = counts.get(decision_id, 0) + 1
    return counts


def unclear_notes(events):
    """Latest unclear note per decision that no later round has re-asked yet."""
    notes = {}
    for record in events:
        if record["event"] == "unclear":
            notes[record["decision"]] = record["note"]
        elif record["event"] == "ask":
            for decision_id in record["items"]:
                notes.pop(decision_id, None)
    return notes


def ask(store, requested=None, *, reason="ask"):
    """Number every open decision 1..N and record that round."""
    events = store.captain_events()
    if presence(events) and reason == "ask" and not (requested and requested.strip()):
        refuse("the captain is away; do not ask him, queue decisions for /back",
               'boss decide ask --requested "<his words>" only when he asks for the list')
    decisions = open_decisions(store)
    counts, notes = asked_counts(events), unclear_notes(events)
    if not decisions:
        return ["No open decisions."], None
    record = store.captain("ask", items=[d["id"] for d in decisions], reason=reason,
                           **({"requested": requested.strip()} if requested and requested.strip() else {}))
    number = len(rounds(events)) + 1
    lines = [f"{plural(len(decisions), 'open decision')} (round {number}; reply like \"1 yes 2 no\"):"]
    for index, decision in enumerate(decisions, 1):
        marks = []
        if counts.get(decision["id"]):
            marks.append("asked again")
        if decision["id"] in notes:
            marks.append("unclear last time: " + plain(notes[decision["id"]], 160))
        lines.append(f"{index}. {plain(decision['text'], 300)}" + (f" ({'; '.join(marks)})" if marks else ""))
    return lines, record


def numbered_view(store, events, full=False):
    """Read-only: latest round's numbers for still-open decisions; never records a round."""
    decisions = open_decisions(store)
    latest = rounds(events)[-1] if rounds(events) else None
    numbers = {d: i for i, d in enumerate(latest["items"], 1)} if latest else {}
    lines = [f"{len(decisions)} open decisions" + (f" (round {len(rounds(events))})" if latest else "")]
    limit = None if full else 180
    for decision in sorted(decisions, key=lambda d: numbers.get(d["id"], len(numbers) + 1)):
        if decision["id"] in numbers:
            lines.append(f"{numbers[decision['id']]}. {plain(decision['text'], limit)}")
        else:
            lines.append(f"- {plain(decision['text'], limit)} (not asked yet)")
    return lines


def resolve(store, reference, round_number=None):
    """A decision id, or a number from the latest ask round. Older rounds are refused."""
    decisions = {d["id"]: d for d in store.decisions()}
    if reference in decisions:
        return decisions[reference]
    if not re.fullmatch(r"[1-9][0-9]{0,3}", reference):
        raise BossError("usage", f"unknown decision {reference}", "boss decide list", 1)
    asked = rounds(store.captain_events())
    if not asked:
        refuse("no ask round yet, so numbers mean nothing", "boss decide ask")
    if round_number is not None and round_number != len(asked):
        refuse(f"round {round_number} is old; the latest is round {len(asked)}",
               "boss decide ask, then use that round's numbers")
    items = asked[-1]["items"]
    if int(reference) > len(items):
        refuse(f"number {reference} is not in round {len(asked)} (1..{len(items)})", "boss decide ask")
    decision = decisions.get(items[int(reference) - 1])
    if decision is None:
        raise BossError("ledger", f"round {len(asked)} names a missing decision", "inspect decisions/")
    return decision


# Timeline --------------------------------------------------------------------

def title(meta):
    return f"{plain(meta.get('title') or 'untitled', 120)} ({plain(meta.get('project') or '?', 60)})"


def task_events(store):
    """Plain-word task history. State changes come from the journal itself, not polling."""
    rows = []
    for meta in store.tasks():
        status, prs = None, set()
        for record in store.journal(meta["id"]):
            at, event, current = record.get("at"), record["event"], record["meta"]
            words = []
            if event == "launched":
                words.append(("started", "started worker: " + title(current)))
            elif event == "launch_unknown":
                words.append(("failed", "worker launch unclear: " + title(current)))
            elif event == "sent":
                text = (record.get("details") or {}).get("text") or ""
                words.append(("sent", f"messaged worker {title(current)}: {plain(text, 200)}"))
            elif event == "answered":
                words.append(("answered", "answered a worker question: " + title(current)))
            elif event == "interrupt_requested":
                words.append(("stopped", "stopped worker: " + title(current)))
            elif event in ("landed", "land_reconciled"):
                quote = (current.get("landed") or {}).get("approved") or ""
                words.append(("landed", f"landed: {title(current)}; approved: {plain(quote, 200)}"))
            elif event == "settled":
                discard = current.get("discard_approved")
                words.append(("settled", "settled: " + title(current) +
                              (f"; discard approved: {plain(discard, 200)}" if discard else "")))
            new_status = current.get("status")
            if event in ("observed", "polled", "reviewed") and new_status != status and new_status in STATUS_WORDS:
                word = STATUS_WORDS[new_status]
                words.append((word, f"{word}: {title(current)}"))
            for pr in (current.get("observed") or {}).get("prs") or []:
                if pr.get("state") == "OPEN" and pr.get("url") not in prs:
                    number = pr["url"].rstrip("/").rsplit("/", 1)[-1]
                    words.append(("pr", f"opened PR #{number}: {title(current)}"))
                prs.add(pr.get("url"))
            status = new_status
            if at:
                rows.extend((at, kind, text) for kind, text in words)
    return rows


def steer_events(store):
    rows = []
    root = store.home / "projects"
    for path in sorted(root.glob("*.steers.jsonl")) if root.exists() else []:
        project = path.name[:-len(".steers.jsonl")]
        records = store.steers(project, all=True)
        texts = {r["id"]: r.get("text") for r in records}
        for record in records:
            if "drops" in record:
                rows.append((record["created_at"], "steer",
                             f"dropped steer ({plain(project, 60)}): {plain(texts.get(record['drops']) or '', 200)}"))
            else:
                rows.append((record["created_at"], "steer",
                             f"steer ({plain(project, 60)}{', replaces an earlier one' if record.get('replaces') else ''}): "
                             f"{plain(record['text'], 300)}"))
    return rows


def decision_events(store):
    rows = []
    for decision in store.decisions():
        if decision.get("created_at"):
            rows.append((decision["created_at"], "decision", "decision opened: " + plain(decision["text"], 300)))
        if decision.get("closed") and decision.get("closed_at"):
            rows.append((decision["closed_at"], "decided",
                         f"decided: {plain(decision['text'], 200)} -> {plain(decision.get('resolution') or '', 200)}"))
    return rows


def captain_rows(store, events):
    decisions = {d["id"]: d for d in store.decisions()}
    rows = []
    for record in events:
        if record["event"] == "log":
            rows.append((record["at"], "log", "check-in: " + plain(record["text"], 400)))
        elif record["event"] == "unclear":
            text = (decisions.get(record["decision"]) or {}).get("text") or "a decision"
            rows.append((record["at"], "unclear", f"marked unclear: {plain(text, 160)} ({plain(record['note'], 160)})"))
        elif record["event"] == "own" and record.get("action") in ("claim", "take", "release"):
            label = plain(record.get("label") or "a session", 80)
            rows.append((record["at"], "owner", {
                "claim": f"boss opened in {label}",
                "take": f"boss moved from {plain(record.get('previous') or 'another session', 80)} to {label}",
                "release": f"boss released by {label}; wakes held"}[record["action"]]))
        elif record["event"] == "ask" and record.get("reason") == "ask":
            rows.append((record["at"], "ask", f"listed {len(record['items'])} open decisions" +
                         (f" because you asked: {plain(record['requested'], 160)}" if record.get("requested") else "")))
    return rows


def timeline(store, start, end, events):
    rows = [*task_events(store), *steer_events(store), *decision_events(store), *captain_rows(store, events)]
    window = [(parse(at), kind, text) for at, kind, text in rows if start <= parse(at) <= end]
    # Stable sort keeps same-instant records in source order.
    return sorted(window, key=lambda row: row[0])


def timeline_lines(rows):
    if len(rows) <= GROUP_AFTER:
        return [f"{when.astimezone().strftime('%H:%M')} {text}" for when, _, text in rows]
    lines, hour = [], None
    days = len({when.astimezone().date() for when, _, _ in rows}) > 1
    for when, _, text in rows:
        local = when.astimezone()
        key = local.replace(minute=0, second=0, microsecond=0)
        if key != hour:
            hour = key
            lines.append(local.strftime("%a %H:00" if days else "%H:00"))
        lines.append(f"  {local.strftime('%H:%M')} {text}")
    return lines


def back(store):
    events = store.captain_events()
    current = presence(events)
    lines = []
    if current is None:
        lines.append("You weren't marked away; nothing changed.")
    else:
        record = store.captain("back")
        start, end = parse(current["at"]), parse(record["at"])
        lines.append(f"Welcome back. You were away {duration(start, end)}, {since_text(current)}.")
        rows = timeline(store, start, end, events)
        if rows:
            lines.append(f"While you were away ({plural(len(rows), 'event')}):")
            lines.extend(timeline_lines(rows))
        else:
            lines.append("Nothing happened while you were away.")
        tally = {k: sum(1 for _, kind, _ in rows if kind == k) for k in ("finished", "landed", "failed")}
        lines.append(f"Tasks: {tally['finished']} finished, {tally['landed']} landed, {tally['failed']} failed.")
    asked, _ = ask(store, reason="back")
    lines.extend(asked)
    lines.append('next: boss decide close <n> "<captain words>"' if len(asked) > 1 else "next: boss")
    return lines

