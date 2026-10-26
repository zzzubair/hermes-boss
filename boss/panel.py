"""Panel runs: N workers on one brief, then a blind judge, with an optional revise loop.

A judge flow, made deterministic. Writers get the same brief and are
labelled A, B, C... in shuffled order. When every writer has an entry, boss
spawns a fresh scout judge that sees only the brief and the entries under their
labels, with model names scrubbed. Its final message carries one JSON object of
scores. With --rounds, the best entry goes back to its writer with the judge's
reasons and fixes, and a new judge (same frozen rubric, nothing from earlier
rounds) scores the revision, until the target score or the round cap.

Nothing here runs on a timer: the wake listener calls advance() when a panel
task's turn ends. Writers' and judges' own "finished" events are consumed;
the supervisor gets one plain line per judged round.
"""
import json
import random
import re

from .common import BossError, require_text
from .service import selection_effort
from .state import atomic_write, identifier, now

LABELS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
MAX_ROUNDS = 20
HIDDEN = "[name hidden]"
WRITER_NOTE = ("You are one of {count} writers working on this brief independently. A blind judge will compare "
               "the entries without knowing who wrote which. Never name yourself, your model, your provider or your "
               "harness in your work, your PR or your messages. {entry}\n\n")
ENTRY = {"scout": "Your final message is your entry: put the complete work in it, above the status line.",
         "ship": "Your final message is your entry: say what you built and how you verified it, with your PR url."}
RUBRIC = """You are a strict, independent, blind judge. {count} written independently for the brief below.
You do not know who or what wrote any entry. Do not guess, and do not let length or labels sway you.
You are not a writer: do not rewrite the entries. Score each one from 0 to 10 against the brief, and say exactly
what would have to change for it to score higher.

Your final message is your verdict. End it with one JSON object, then your status line, in this shape:
{{"scores": {{{shape}}}}}
Every entry label must appear, each score a number from 0 to 10.

# Brief

{brief}
"""


def directory(store, panel_id):
    return store.home / "panels" / identifier(panel_id)


def load(store, panel_id):
    path = directory(store, panel_id) / "panel.json"
    if not path.exists():
        raise BossError("usage", f"unknown panel {panel_id}", "boss", 1)
    return json.loads(path.read_text(encoding="utf-8"))


def save(store, panel):
    panel["updated_at"] = now()
    atomic_write(directory(store, panel["id"]) / "panel.json",
                 json.dumps(store.redact.data(panel), indent=2, sort_keys=True) + "\n")


def hidden_terms(selection):
    """Words that would tell the judge which model wrote an entry: the model id, its words, the provider."""
    if not selection:
        return []
    terms = {selection.get("model") or "", selection.get("instanceId") or ""}
    terms |= {part for part in re.split(r"[^A-Za-z]+", selection.get("model") or "") if len(part) >= 3}
    return sorted((t for t in terms if t), key=lambda t: (-len(t), t))


def scrub(text, terms):
    for term in sorted(terms, key=len, reverse=True):
        text = re.sub(r"(?<![A-Za-z0-9])" + re.escape(term) + r"(?![A-Za-z0-9])", HIDDEN, text, flags=re.I)
    return text


def preflight(boss, project, roles, kind, judge):
    """Refuse the whole panel before launching anything when any seat breaks a rule or the writers pass the cap."""
    active = len(boss.store.tasks(active=True))
    if active + len(roles) > boss.config.max_active:
        raise BossError("refused", f"{active} workers are active, {len(roles)} more would pass the cap "
                        f"workers.max_active = {boss.config.max_active}",
                        "settle finished workers first, or use fewer --workers", 2)
    for role, seat_kind in [(r, kind) for r in roles] + [(judge, "scout")]:
        overrides = {k: v for k, v in {"worker": role, "kind": seat_kind}.items() if v is not None}
        settings, info, _ = boss.resolve_project(project, None, overrides)
        selection = boss.model_selection(boss.client(settings["host"]), info, settings.get("model_selection"),
                                         settings.get("model"), settings.get("effort"))
        _, origins = boss.config.effective(project, overrides)
        broken = boss.config.violations(project, settings, origins,
                                        f"{selection['instanceId']}:{selection['model']}" if selection else None,
                                        selection_effort(selection))
        violations = [text for text, _, _ in broken]
        if violations:
            raise BossError("refused", f"panel seat {role or 'judge'} breaks a worker rule",
                            "choose other --workers / --judge roles, or spawn that seat alone with --allow-model", 2,
                            context=violations)


def start(boss, project, brief, roles, *, title=None, judge=None, rounds=1, target=None, kind="scout", fits=None):
    require_text(brief, "task brief", 100000)
    roles = [r.strip() for r in roles if r.strip()]
    if not roles or len(roles) > len(LABELS):
        raise BossError("usage", f"--workers needs 1 to {len(LABELS)} worker roles", "boss panel --help", 1)
    if not 1 <= rounds <= MAX_ROUNDS:
        raise BossError("usage", f"--rounds must be 1 to {MAX_ROUNDS}", "boss panel --help", 1)
    if target is not None and not 0 < target <= 10:
        raise BossError("usage", "--target is a score above 0 and at most 10", "boss panel --help", 1)
    title = " ".join((title or next((line.strip("# ").strip() for line in brief.splitlines() if line.strip("# ").strip()),
                                    "Panel")).split())[:80]
    require_text(title, "title", 300)
    preflight(boss, project, roles, kind, judge)
    labels = list(LABELS[:len(roles)])
    random.shuffle(labels)  # the first role listed is not always A
    panel = {"id": boss.store.new_id(), "project": project, "title": title, "kind": kind, "judge": judge,
             "rounds": rounds, "target": target, "fits": fits, "round": 1, "stage": "writing",
             "members": [], "judges": [], "results": [], "consumed": [], "created_at": now()}
    atomic_write(directory(boss.store, panel["id"]) / "brief.md", boss.redact(brief))
    message = WRITER_NOTE.format(count=len(roles), entry=ENTRY[kind]) + brief if len(roles) > 1 else brief
    for label, role in sorted(zip(labels, roles)):
        try:
            meta = boss.spawn(project, f"{title} ({role})", message, kind, None, None, None, fits, role)
        except BossError as exc:
            if not panel["members"]:
                raise
            stop(panel, f"writer {label} ({role}) could not start")
            save(boss.store, panel)
            launched = " ".join(m["task"] for m in panel["members"])
            raise BossError(exc.code, f"panel {panel['id']} stopped: writer {role}: {exc.message}",
                            f"boss stop <task> for the writers already launched: {launched}", exc.exit_code) from None
        meta["panel"] = panel["id"]
        boss.store.save(meta, "panel_seat", panel=panel["id"], label=label)
        panel["members"].append({"label": label, "worker": role, "task": meta["id"],
                                 "hidden": hidden_terms(meta.get("model_selection")), "entry_run": None, "out": False})
        save(boss.store, panel)
    return panel


def entries(boss, panel):
    """(label, scrubbed entry text) for every seat this round judges."""
    seats = [m for m in panel["members"] if not m["out"]]
    if panel["round"] > 1:
        seats = [m for m in seats if m["label"] == panel["results"][-1]["best"]]
    terms = sorted({t for m in panel["members"] for t in m["hidden"]})
    rows = []
    for member in seats:
        path = boss.store.directory(member["task"]) / "report.md"
        rows.append((member["label"], scrub(path.read_text(encoding="utf-8").strip(), terms)))
    return rows


def judge_brief(boss, panel, rows):
    brief = (directory(boss.store, panel["id"]) / "brief.md").read_text(encoding="utf-8").strip()
    shape = ", ".join(f'"{label}": {{"score": 0.0, "reasons": "what works and what does not", '
                      f'"fixes": "the changes that would raise the score"}}' for label, _ in rows)
    text = RUBRIC.format(count="1 entry was" if len(rows) == 1 else f"{len(rows)} entries were", shape=shape, brief=brief)
    return text + "".join(f"\n# Entry {label}\n\n{body}\n" for label, body in rows)


def scores(text, labels):
    """The judge's last JSON object with a score for every label, or None."""
    decoder, found = json.JSONDecoder(), None
    for match in re.finditer(r"\{", text or ""):
        try:
            value, _ = decoder.raw_decode(text, match.start())
        except ValueError:
            continue
        if isinstance(value, dict) and isinstance(value.get("scores"), dict):
            found = value["scores"]
    if not found or set(found) != set(labels):
        return None
    result = {}
    for label in labels:
        item = found[label] if isinstance(found[label], dict) else {"score": found[label]}
        score = item.get("score")
        if isinstance(score, bool) or not isinstance(score, (int, float)) or not 0 <= score <= 10:
            return None
        result[label] = {"score": float(score), "reasons": " ".join(str(item.get("reasons") or "").split()),
                         "fixes": " ".join(str(item.get("fixes") or "").split())}
    return result


def clip(text, limit):
    """Plain words: one line, cut at a word with '...'."""
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit].rpartition(" ")[0] + "..."


def number(score):
    return f"{score:g}"


def seat(panel, label):
    return next(m for m in panel["members"] if m["label"] == label)


def round_line(panel, result, ending):
    """One plain line: round, best entry and its model, scores, the judge's words. No ids."""
    best = result["best"]
    top = result["scores"][best]
    name = f"{best} ({seat(panel, best)['worker']})"
    target = panel["target"]
    score = f"{number(top['score'])}/10"
    if ending == "revising":
        verdict = f"{name} best at {score}" + (f", below the {number(target)} target" if target is not None else "") + \
                  f"; {best} is revising"
    elif ending == "target":
        verdict = f"done: {name} scored {score}, reaching the {number(target)} target"
    elif target is not None:
        verdict = (f"done at the round cap: best {name} {score}" if panel["rounds"] > 1 else f"done: best {name} {score}") + \
                  f", short of the {number(target)} target"
    else:
        verdict = f"done: best {name} {score}"
    others = [f"{label} ({seat(panel, label)['worker']}) {number(item['score'])}"
              for label, item in sorted(result["scores"].items())]
    out = [f"{m['label']} ({m['worker']})" for m in panel["members"] if m["out"]]
    rounds = f"round {panel['round']} of {panel['rounds']}" if panel["rounds"] > 1 else "judged"
    line = f"{panel['title']}: panel {rounds}: {verdict}."
    if len(others) > 1:
        line += " Scores: " + ", ".join(others) + "."
    if out:
        line += " Stopped without an entry: " + ", ".join(out) + "."
    words = top["reasons"] or top["fixes"]
    return line + (f' Judge: "{clip(words, 140)}"' if words else "")


def revision(result, label):
    top = result["scores"][label]
    return (f"A blind judge scored your entry {number(top['score'])}/10.\n\n"
            f"What works and what does not: {top['reasons'] or 'not given'}\n\n"
            f"What would raise the score: {top['fixes'] or 'not given'}\n\n"
            "Revise your entry against the same brief. Your final message is the complete revised entry, "
            "above the status line.")


def stop(panel, why):
    panel["stage"] = "stopped"
    panel["stopped"] = why
    return f"{panel['title']}: panel stopped, {why}."


def advance(boss, meta, event):
    """React to one ended turn of a panel task. Returns (consumed, line or None).

    consumed=True means the task's own event is not surfaced on its own; line is
    the round line to hold for the supervisor. Re-running for the same event changes nothing.
    """
    panel = load(boss.store, meta["panel"])
    turn = f"{meta['id']}:{event.turn_id}"
    if event.kind == "done" and turn in panel["consumed"]:
        return True, None  # replayed after a restart: already part of a round
    if panel["stage"] in ("done", "stopped"):
        return False, None
    member = next((m for m in panel["members"] if m["task"] == meta["id"]), None)
    judge = next((j for j in panel["judges"] if j["task"] == meta["id"]), None)
    line = None
    if member is not None:
        if event.kind == "error":
            if member["entry_run"] is None or panel["stage"] == "revising":
                member["out"] = True
            consumed = False  # the supervisor hears that a writer stopped
        elif event.kind == "done" and event.turn_id != member["entry_run"] and panel["stage"] in ("writing", "revising"):
            member["entry_run"] = event.turn_id
            consumed = True
        else:
            return False, None
    elif judge is not None and judge["round"] == panel["round"] and panel["stage"] == "judging":
        if event.kind == "error":
            line = stop(panel, f"the round {panel['round']} judge stopped before scoring")
            consumed = False
        elif event.kind == "done":
            consumed = True
            line = judged(boss, panel, judge)
        else:
            return False, None
    else:
        return False, None
    if consumed:
        panel["consumed"].append(turn)
    if panel["stage"] in ("writing", "revising"):
        line = launch_judge(boss, panel) or line
    save(boss.store, panel)
    return consumed, line


def launch_judge(boss, panel):
    """Spawn this round's judge once every judged seat has an entry; a stop line if it cannot start."""
    seats = [m for m in panel["members"] if not m["out"]]
    if panel["round"] > 1:
        seats = [m for m in seats if m["label"] == panel["results"][-1]["best"]]
        if not seats:
            return stop(panel, "the writer revising the best entry stopped")
        if seats[0]["entry_run"] == panel["results"][-1]["entry_run"]:
            return None
    elif not seats:
        return stop(panel, "every writer stopped without an entry")
    if any(m["entry_run"] is None for m in panel["members"] if not m["out"]):
        return None
    try:
        rows = entries(boss, panel)
        meta = boss.spawn(panel["project"], f"{panel['title']}: judge round {panel['round']}", judge_brief(boss, panel, rows),
                          "scout", None, None, None, panel["fits"], panel["judge"])
    except (BossError, OSError) as exc:
        return stop(panel, f"the round {panel['round']} judge could not start: "
                           f"{clip(exc.message if isinstance(exc, BossError) else str(exc), 160)}")
    meta["panel"] = panel["id"]
    boss.store.save(meta, "panel_judge", panel=panel["id"], round=panel["round"])
    panel["judges"].append({"round": panel["round"], "task": meta["id"], "labels": [label for label, _ in rows]})
    panel["stage"] = "judging"
    save(boss.store, panel)  # before anything else can fail: a replayed event never spawns a second judge
    return None


def judged(boss, panel, judge):
    report = boss.store.directory(judge["task"]) / "report.md"
    result = scores(report.read_text(encoding="utf-8") if report.exists() else "", judge["labels"])
    if result is None:
        return stop(panel, f"the round {panel['round']} judge's scores could not be read")
    best = max(sorted(result), key=lambda label: result[label]["score"])  # ties go to the earlier label
    record = {"round": panel["round"], "scores": result, "best": best, "entry_run": seat(panel, best)["entry_run"]}
    panel["results"].append(record)
    reached = panel["target"] is not None and result[best]["score"] >= panel["target"]
    if reached or panel["round"] >= panel["rounds"]:
        panel["stage"] = "done"
        return round_line(panel, record, "target" if reached else "cap")
    line = round_line(panel, record, "revising")
    panel["round"] += 1
    panel["stage"] = "revising"
    save(boss.store, panel)  # a replayed event never sends the notes twice
    try:
        boss.send(seat(panel, best)["task"], revision(record, best))
    except BossError as exc:
        return stop(panel, f"could not send the judge's notes to {best}: {clip(exc.message, 160)}")
    return line


def lines(panel):
    """CLI result of boss panel: for the supervisor, so ids are fine here."""
    out = [f"{panel['id']}: panel started, {len(panel['members'])} writers"
           + (f", up to {panel['rounds']} judged rounds" if panel["rounds"] > 1 else ", one judged round")
           + (f", target {number(panel['target'])}/10" if panel["target"] is not None else "")]
    out += [f"{m['label']}: {m['task']} {m['worker']}" for m in sorted(panel["members"], key=lambda m: m["label"])]
    return out + ["next: the judge starts when every writer finishes; one inbox line per judged round"]

