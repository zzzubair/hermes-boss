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
# Things the captain approves once: his answer about one of these for a task is final.
ACTIONS = ("merge", "deploy", "demo")
# "1 yes 3 no 5 more info": a number from the round, at the start or after a space or separator.
ANSWER_NUMBER = re.compile(r"(?:^|(?<=[\s/;,]))(\d{1,3})(?:[.):]\s*|\s+|$)")
UNCLEAR = re.compile(r"\b(?:more (?:info|information|details?|context)|needs? more|unclear|confus\w*|not sure|"
                     r"don'?t (?:understand|get)|what do you mean|explain|idk|no idea)\b|\?\s*$", re.I)
VERDICT = re.compile(r"\b(?:(yes|yeah|yep|yup|ok|okay|sure|approved?|go ahead|do it|(?:merge|deploy|ship|land) it|lgtm)|"
                     r"(no|nope|nah|don'?t|do not|reject(?:ed)?|not now|hold off))\b", re.I)


def now():
    # state.now; state imports this module, so it can't be imported here.
    return datetime.now(timezone.utc).isoformat()


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
        return [f"Got it. Away mode is already on {since_text(current)}; nothing changed.", "next: boss back"]
    record = store.captain("away", note=" ".join(note.split()) or None)
    note = " ".join(str(record.get("note") or "").split())
    tell(store, record, f"Captain went away at {clock(record['at'])}" + (f" ({plain(note, 80)})" if note else "") +
         ". Don't ask him anything until /back: act on your own judgement and queue decisions for /back."
         " Anything he says or decides in a check-in counts in full; record it with boss log.")
    return [f"Got it. Away mode on {since_text(record)}. I won't ask you anything; anything you decide in a"
            " check-in counts.", "next: boss back"]


def tell(store, record, line):
    """The supervisor hears of /away and /back through the owner's inbox, like any worker event."""
    from .inbox import Inbox  # inbox -> state -> captain: import late
    Inbox(store.home, store.redact).hold(f"captain:{record['event']}:{record['at']}", None, "captain", line)


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


def unclear_marks(events):
    """Latest unclear mark per decision that no later round has re-asked yet."""
    marks = {}
    for record in events:
        if record["event"] == "unclear":
            marks[record["decision"]] = record
        elif record["event"] == "ask":
            for decision_id in record["items"]:
                marks.pop(decision_id, None)
    return marks


def needs_detail(decision, mark):
    """An unclear answer is re-asked only after the supervisor explained it better."""
    return not any(d["at"] > mark["at"] for d in decision.get("details") or [])


def about(store, decision):
    """Plain words on what a re-asked decision is about: its task and PR, never ids."""
    if not decision.get("task"):
        return None
    try:
        meta = store.load(decision["task"])
    except BossError:
        return None
    prs = [p["url"] for p in (meta.get("observed") or {}).get("prs") or [] if p.get("url")]
    return "about: " + title(meta) + (f", {prs[-1]}" if prs else "")


def ask(store, requested=None, *, reason="ask"):
    """Number every open decision 1..N and record that round."""
    events = store.captain_events()
    if presence(events) and reason == "ask" and not (requested and requested.strip()):
        refuse("the captain is away; do not ask him, queue decisions for /back",
               'boss decide ask --requested "<his words>" only when he asks for the list')
    decisions = open_decisions(store)
    counts, marks = asked_counts(events), unclear_marks(events)
    if not decisions:
        return ["No open decisions."], None
    vague = [d for d in decisions if d["id"] in marks and needs_detail(d, marks[d["id"]])]
    if vague and reason == "ask":
        raise BossError("refused", f"{plural(len(vague), 'unclear answer')} {'needs' if len(vague) == 1 else 'need'} more detail before asking again",
                        'boss decide detail <id> "<plain explanation of what he is choosing>"', 2,
                        context=[f"{d['id']}: {plain(d['text'], 160)} (he said: {plain(marks[d['id']]['note'], 160)})"
                                 for d in vague])
    record = store.captain("ask", items=[d["id"] for d in decisions], reason=reason,
                           **({"requested": requested.strip()} if requested and requested.strip() else {}))
    number = len(rounds(events)) + 1
    lines = [f"{plural(len(decisions), 'open decision')} (round {number}; reply like \"1 yes 2 no\"):"]
    for index, decision in enumerate(decisions, 1):
        again = bool(counts.get(decision["id"]))
        notes = ["asked again"] if again else []
        if decision["id"] in marks:
            notes.append("unclear last time: " + plain(marks[decision["id"]]["note"], 160))
        # Re-asked items get the whole text and everything known about them.
        lines.append(f"{index}. {plain(decision['text'], None if again else 300)}" + (f" ({'; '.join(notes)})" if notes else ""))
        lines.extend("   more: " + plain(d["text"], 600) for d in decision.get("details") or [])
        context = about(store, decision) if again else None
        if context:
            lines.append("   " + context)
    return lines, record


def norm(text):
    return " ".join(str(text).lower().split())


def verdict(words):
    match = VERDICT.search(words)
    return None if match is None else "yes" if match[1] else "no"


def close(store, decision, words, **extra):
    """Record his answer; the first answer is final."""
    if decision.get("closed"):
        return False
    decision.update({"closed": True, "resolution": words, "closed_at": now(), "verdict": verdict(words), **extra})
    store.decision(decision)
    return True


def add_decision(store, text, task=None, action=None):
    """Open a decision unless the same one is open already; an answered one is never asked again."""
    if action is not None and task is None:
        raise BossError("usage", "--action needs --task", 'boss decide add "<text>" --task <id> --action merge', 1)
    meta = store.load(task) if task else None
    if action == "merge" and meta.get("landed"):
        refuse("that task is already landed; nothing to ask", f"boss settle {task}")
    for decision in sorted(store.decisions(), key=lambda d: d.get("created_at") or ""):
        same_subject = action is not None and decision.get("task") == task and decision.get("action") == action
        if not same_subject and norm(decision["text"]) != norm(text):
            continue
        if decision.get("closed"):
            refuse(f"already answered: {plain(decision['text'], 160)} -> {plain(decision.get('resolution') or '', 160)}; "
                   "an answered decision is never asked again", "act on that answer")
        return decision, False
    record = {"id": store.new_id(), "text": text, "task": task, "closed": False, "created_at": now(),
              **({"action": action} if action else {})}
    store.decision(record)
    return record, True


def approval(store, task, action="merge"):
    """His latest answer on doing action for task, or None."""
    answers = [d for d in store.decisions() if d.get("closed") and d.get("task") == task and d.get("action") == action]
    return max(answers, key=lambda d: d.get("closed_at") or "", default=None)


def approve(store, task, action, words):
    """An approval he gives in passing, here or in a check-in, closes the matching decision for good."""
    meta = store.load(task)
    closed = [d for d in store.decisions() if d.get("task") == task and d.get("action") == action
              and close(store, d, words, verdict="yes")]
    latest = approval(store, task, action)
    if not closed and (latest is None or latest.get("verdict") != "yes"):
        # Nothing was open, or he changed an earlier no: his newer words win.
        record = {"id": store.new_id(), "text": f"{action} {plain(meta['title'], 200)}", "task": task,
                  "action": action, "closed": False, "created_at": now()}
        close(store, record, words, verdict="yes")
        closed.append(record)
    return closed


def add_detail(store, decision, text):
    """More context for the next time a decision is asked."""
    if decision.get("closed"):
        refuse("that decision is already answered; it is never asked again", "boss decide ask")
    decision["details"] = [*(decision.get("details") or []), {"at": now(), "text": " ".join(text.split())}]
    store.decision(decision)


def parse_answers(reply, count):
    """{number: his words} for numbers 1..count, read in increasing order."""
    found, last = [], 0
    for match in ANSWER_NUMBER.finditer(reply):
        number = int(match[1])
        if last < number <= count:
            found.append((number, match.start(), match.end()))
            last = number
    return {number: reply[end:(found[i + 1][1] if i + 1 < len(found) else len(reply))].strip(" \t\n/;,-")
            for i, (number, _, end) in enumerate(found)}


def answer(store, reply, round_number=None):
    """Apply a reply like "1 yes 3 no 5 more info" to the latest round."""
    asked = rounds(store.captain_events())
    if not asked:
        refuse("no ask round yet, so numbers mean nothing", "boss decide ask")
    if round_number is not None and round_number != len(asked):
        refuse(f"round {round_number} is old; the latest is round {len(asked)}",
               "boss decide ask, then use that round's numbers")
    items = asked[-1]["items"]
    parts = {n: words for n, words in parse_answers(reply, len(items)).items() if words}
    if not parts:
        refuse(f"no numbered answers for round {len(asked)} (1..{len(items)}) in his reply",
               'boss decide close <n> "<his words>" for an answer without numbers')
    decisions = {d["id"]: d for d in store.decisions()}
    done = {"closed": [], "unclear": [], "already answered": []}
    for number, words in sorted(parts.items()):
        decision = decisions[items[number - 1]]
        if decision.get("closed"):
            done["already answered"].append(number)
        elif UNCLEAR.search(words):
            store.captain("unclear", decision=decision["id"], note=words)
            done["unclear"].append(number)
        else:
            close(store, decision, words)
            done["closed"].append(number)
    skipped = [n for n in range(1, len(items) + 1) if n not in parts and not decisions[items[n - 1]].get("closed")]
    lines = [f"{label}: {', '.join(map(str, numbers))}" for label, numbers in done.items() if numbers]
    if skipped:
        lines.append("skipped, asked again next round: " + ", ".join(map(str, skipped)))
    return lines, bool(done["unclear"])


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
                             f"{'fence' if record.get('fence') else 'steer'} ({plain(project, 60)}"
                             f"{', replaces an earlier one' if record.get('replaces') else ''}): "
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
        lines.append("Welcome back. You weren't marked away; nothing changed.")
    else:
        record = store.captain("back")
        tell(store, record, f"Captain is back at {clock(record['at'])}. Away mode is off; you can ask him again.")
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

