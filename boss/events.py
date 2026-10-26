"""What a worker thread needs from the captain right now, from one snapshot view.

classify() is pure: it reads the dict Boss.snapshot() returns ("thread", "status",
"requests", "approvals", "prs", "assistant") and makes no calls. A turn that ends without a
question, a report or a PR it names is ended_unclear, never swallowed: workers
are told to ask through the harness question tool, so plain text at the end of
a turn is probably a question nobody would otherwise see.
"""
from dataclasses import dataclass

KINDS = ("input", "approval", "finished_pr", "finished_report", "ended_unclear", "error", "working")
ERROR_STATUSES = {"failed", "interrupted", "cancelled", "rolled_back"}
ENDED_STATUSES = {"completed", "idle"}
QUOTE_CHARS = 300


@dataclass(frozen=True)
class Event:
    kind: str
    thread_id: str
    turn_id: str | None
    dedupe_key: str | None  # None only for working: there is nothing to wake for.
    detail: str = ""
    request_ids: tuple = ()
    pr_urls: tuple = ()


def classify(view):
    thread = view["thread"]
    thread_id, turn_id = thread["threadId"], thread.get("latestRunId")
    status = view["status"]
    requests = tuple(sorted(view.get("requests") or ()))
    text = view.get("assistant") or ""

    def event(kind, detail="", key=None, **extra):
        return Event(kind, thread_id, turn_id, f"{thread_id}:{kind}:{key or turn_id}", detail, **extra)

    if requests:
        # A new or answered request changes the set, which is a new thing to surface.
        return event("input", f"{len(requests)} pending input requests", ",".join(requests), request_ids=requests)
    if view.get("approvals"):
        # Only a pending non-question runtime request is a permission prompt. T3 also
        # reports a bare "waiting" for a moment as a turn finishes; that is not one.
        return event("approval", f"{view['approvals']} pending permission requests in T3")
    if status in ERROR_STATUSES:
        return event("error", f"thread {status}")
    if status not in ENDED_STATUSES or turn_id is None:  # includes a bare "waiting"
        return Event("working", thread_id, turn_id, None)
    if text.lstrip().startswith("BOSS_REPORT:") and text.partition("BOSS_REPORT:")[2].strip():
        return event("finished_report", "BOSS_REPORT")
    # PR links outlive the turn that made them, so the final reply must name the PR,
    # as the contract asks; otherwise a later plain-text question would be swallowed.
    named = tuple(sorted(p["url"] for p in view.get("prs") or () if p["url"] in text))
    if named:
        return event("finished_pr", " ".join(named), pr_urls=named)
    quote = " ".join(text.split())[:QUOTE_CHARS]
    return event("ended_unclear", f'turn ended without a question, report or PR; probable plain-text question: "{quote}"'
                 if quote else "turn ended without a question, report, PR or assistant message")
