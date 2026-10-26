"""What a worker thread needs from boss right now, from one snapshot view.

Workers end every turn with one status line (Firstmate's protocol):

    BOSS: done: <PR url | one-line report>
    BOSS: needs-decision: <question>
    BOSS: blocked: <why>
    BOSS: paused: <what it waits on>

classify() is pure: it reads the dict Boss.snapshot() returns ("thread", "status",
"requests", "approvals", "prs", "assistant", "assistant_run") and makes no calls.
A finished turn's status line is the primary signal. A pending T3 question or
permission prompt says the worker is waiting now; a question also carries its
answerable request ids. A turn that ends with no status line is ended_unclear,
surfaced with the worker's last words, never swallowed.

pr_events() says which of the task's PRs merged or closed (often by the captain
on GitHub, not by boss), so the supervisor can settle the task at once. The wake
listener builds 'stalled' events itself, from how long a thread was quiet.
"""
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import ipaddress
import re
from urllib.parse import urlsplit

KINDS = ("needs-decision", "approval", "blocked", "paused", "done", "error", "ended_unclear", "working")
ERROR_STATUSES = {"failed", "interrupted", "cancelled", "rolled_back"}
ENDED_STATUSES = {"completed", "idle"}
QUOTE_CHARS = 300
TAIL_CHARS = 120
STATUS_LINE = re.compile(r"BOSS:\s*(done|needs-decision|blocked|paused):\s*(\S.*)")
UNTIL = re.compile(r"\buntil\s+(?:(\d{4}-\d{2}-\d{2})[T ])?(\d{1,2}):(\d{2})(?::(\d{2}))?(?:\.\d+)?\s*(?:Z|UTC|GMT|\+00:?00)?\b",
                   re.IGNORECASE)
FINISHED_PRS = {"MERGED": "merged", "CLOSED": "closed"}
LINK = re.compile(r"https?://(?:\[[0-9A-Fa-f:.]+\])?[^\s<>()\[\]\"'`]+")
# localhost:5173 or 127.0.0.1:8080 written without a scheme; never a piece of a URL or path.
BARE_LOCAL = re.compile(r"(?<![\w./:@-])(?:localhost|127(?:\.\d{1,3}){3}|0\.0\.0\.0):\d{2,5}\b")


@dataclass(frozen=True)
class Event:
    kind: str
    thread_id: str
    turn_id: str | None
    dedupe_key: str | None  # None only for working: there is nothing to wake for.
    detail: str = ""
    request_ids: tuple = ()
    pr_urls: tuple = ()
    machine_links: tuple = ()  # links in the report or PR body that open only on the worker's machine


def status_line(text):
    """(state, payload) from the last non-empty line of a worker message, or None."""
    lines = [line.strip().strip("`*").strip() for line in (text or "").splitlines() if line.strip()]
    match = STATUS_LINE.fullmatch(lines[-1]) if lines else None
    return (match[1], match[2].strip()) if match else None


def machine_links(text):
    """Links that open only on the machine that wrote them: localhost, a loopback address,
    or a bare host with a port (http://devbox:3000). Tailnet (*.ts.net) and preview hosts pass."""
    found = []
    for link in LINK.findall(text or ""):
        link = link.rstrip(".,;:!?*_")
        try:
            parts = urlsplit(link)
            host, port = parts.hostname or "", parts.port
        except ValueError:
            continue
        try:
            address = ipaddress.ip_address(host)
            local = address.is_loopback or address.is_unspecified
        except ValueError:
            local = host == "localhost" or host.endswith(".localhost") or ("." not in host and port is not None)
        if local:
            found.append(link)
    found += BARE_LOCAL.findall(text or "")
    return tuple(dict.fromkeys(found))


def classify(view):
    thread = view["thread"]
    thread_id, turn_id = thread["threadId"], thread.get("latestRunId")
    status = view["status"]
    requests = tuple(sorted(view.get("requests") or ()))
    text = view.get("assistant") or ""
    # A status line counts only from this turn's own message, never an earlier turn's.
    line = status_line(text) if view.get("assistant_run") in (None, turn_id) else None

    def event(kind, detail="", key=None, **extra):
        return Event(kind, thread_id, turn_id, f"{thread_id}:{kind}:{key or turn_id}", detail, **extra)

    if requests:
        # A new or answered request changes the set, which is a new thing to surface.
        mirrored = line[1] if line and line[0] == "needs-decision" else ""
        return event("needs-decision", mirrored, ",".join(requests), request_ids=requests)
    if view.get("approvals"):
        # Only a pending non-question runtime request is a permission prompt. T3 also
        # reports a bare "waiting" for a moment as a turn finishes; that is not one.
        return event("approval", f"{view['approvals']} pending permission requests in T3")
    if status in ERROR_STATUSES:
        return event("error", f"the thread {status.replace('_', ' ')}")
    if status not in ENDED_STATUSES or turn_id is None:  # includes a bare "waiting"
        return Event("working", thread_id, turn_id, None)
    if line:
        state, payload = line
        if state != "done":
            return event(state, payload)
        # PR links outlive the turn that made them: only PRs this turn's message names count.
        named = tuple(sorted(p["url"] for p in view.get("prs") or () if p["url"] in text))
        bodies = [p.get("body") or "" for p in view.get("prs") or () if p["url"] in named]
        return event("done", payload, pr_urls=named, machine_links=machine_links("\n".join([text, *bodies])))
    quote = " ".join(text.split())[:QUOTE_CHARS]
    return event("ended_unclear", quote)


def pr_events(view):
    """One event per PR of the task that is merged or closed; its key names the PR, so each surfaces once."""
    thread_id = view["thread"]["threadId"]
    events = []
    for pr in view.get("prs") or ():
        word = FINISHED_PRS.get(str(pr.get("state") or "").upper())
        if word:
            # No turn: a merge is about the PR, so a newer worker turn never makes it stale.
            events.append(Event("pr-" + word, thread_id, None, f"{thread_id}:pr-{word}:{pr['url']}", pr["url"]))
    return events


def paused_until(detail, seen):
    """When a paused worker said its wait ends ("until <UTC time>"), as epoch seconds; None if it named no time.

    A bare time of day is the next one after `seen`, the moment boss read the paused line.
    """
    match = UNTIL.search(detail or "")
    if not match:
        return None
    day, hour, minute, second = match[1], int(match[2]), int(match[3]), int(match[4] or 0)
    if hour > 23 or minute > 59 or second > 59:
        return None
    base = datetime.fromtimestamp(seen, timezone.utc)
    try:
        date = datetime.strptime(day, "%Y-%m-%d").date() if day else base.date()
    except ValueError:
        return None
    until = datetime(date.year, date.month, date.day, hour, minute, second, tzinfo=timezone.utc)
    if not day and until <= base:
        until += timedelta(days=1)
    return until.timestamp()


def stall_text(minutes, last):
    """'no progress for 25 min (last: <tail of the worker's last words>)'."""
    tail = " ".join(str(last or "").split())
    if len(tail) > TAIL_CHARS:
        tail = "..." + tail[-(TAIL_CHARS - 3):]
    return f"no progress for {minutes} min" + (f" (last: {tail})" if tail else "")


def options_text(options):
    labels = [str(o.get("label") or o.get("value") or "") if isinstance(o, dict) else str(o) for o in options or ()]
    labels = [" ".join(label.split())[:60] for label in labels if label.strip()]
    return f" (options: {' / '.join(labels)})" if labels else ""


def describe(title, event, prs=(), questions=()):
    """One plain line for the supervisor: task title first, then what happened with its payload. No ids."""
    title = " ".join(str(title or "a worker").split())[:80]
    detail = " ".join(event.detail.split())
    kind = event.kind
    if kind == "done" and event.machine_links:
        flag = "link only works on that machine: " + ", ".join(event.machine_links[:3])
        return f"{describe(title, replace(event, machine_links=()), prs)} ({flag})"
    if kind == "done":
        named = [p for p in prs if p["url"] in event.pr_urls]
        if named:
            what = "; ".join(f'PR "{" ".join(str(p.get("title") or "").split())[:100]}" {p["url"]}'
                             if p.get("title") else f"PR {p['url']}" for p in named)
            extra = detail
            for p in named:
                extra = extra.replace(p["url"], "")
            extra = extra.strip(" -:;,.")
            return f"{title}: finished, {what}" + (f" ({extra})" if extra else "")
        return f"{title}: finished: {detail}" if detail else f"{title}: finished"
    if kind == "needs-decision":
        asked = [f'"{" ".join(str(q.get("question") or "").split())[:200]}"' + options_text(q.get("options"))
                 for q in questions if str(q.get("question") or "").strip()]
        what = "; ".join(asked) or (f'"{detail}"' if detail else "")
        return f"{title}: needs a decision: {what}" if what else f"{title}: needs a decision (open question in T3)"
    if kind == "blocked":
        return f"{title}: blocked: {detail}"
    if kind == "paused":
        return f"{title}: paused, waiting on {detail}"
    if kind == "stalled":
        return f"{title}: {detail}"
    if kind in ("pr-merged", "pr-closed"):
        return f"{title}: PR {kind[3:]} {detail}"
    if kind == "approval":
        return f"{title}: waiting on a permission prompt in T3"
    if kind == "error":
        return f"{title}: stopped, {detail}"
    if detail:
        return f'{title}: ended its turn without a status line; last words: "{detail}"'
    return f"{title}: ended its turn without a status line or any message"
