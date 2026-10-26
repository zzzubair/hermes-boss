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
"""
from dataclasses import dataclass
import re

KINDS = ("needs-decision", "approval", "blocked", "paused", "done", "error", "ended_unclear", "working")
ERROR_STATUSES = {"failed", "interrupted", "cancelled", "rolled_back"}
ENDED_STATUSES = {"completed", "idle"}
QUOTE_CHARS = 300
STATUS_LINE = re.compile(r"BOSS:\s*(done|needs-decision|blocked|paused):\s*(\S.*)")


@dataclass(frozen=True)
class Event:
    kind: str
    thread_id: str
    turn_id: str | None
    dedupe_key: str | None  # None only for working: there is nothing to wake for.
    detail: str = ""
    request_ids: tuple = ()
    pr_urls: tuple = ()


def status_line(text):
    """(state, payload) from the last non-empty line of a worker message, or None."""
    lines = [line.strip().strip("`*").strip() for line in (text or "").splitlines() if line.strip()]
    match = STATUS_LINE.fullmatch(lines[-1]) if lines else None
    return (match[1], match[2].strip()) if match else None


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
        return event("done", payload, pr_urls=named)
    quote = " ".join(text.split())[:QUOTE_CHARS]
    return event("ended_unclear", quote)


def options_text(options):
    labels = [str(o.get("label") or o.get("value") or "") if isinstance(o, dict) else str(o) for o in options or ()]
    labels = [" ".join(label.split())[:60] for label in labels if label.strip()]
    return f" (options: {' / '.join(labels)})" if labels else ""


def describe(title, event, prs=(), questions=()):
    """One plain line for the supervisor: task title first, then what happened with its payload. No ids."""
    title = " ".join(str(title or "a worker").split())[:80]
    detail = " ".join(event.detail.split())
    kind = event.kind
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
    if kind == "approval":
        return f"{title}: waiting on a permission prompt in T3"
    if kind == "error":
        return f"{title}: stopped, {detail}"
    if detail:
        return f'{title}: ended its turn without a status line; last words: "{detail}"'
    return f"{title}: ended its turn without a status line or any message"
