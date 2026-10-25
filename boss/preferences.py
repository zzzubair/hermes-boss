"""Project names and the ordered, bounded worker brief."""

from .common import BossError, refuse, require_text


BRIEF_LIMIT = 115000


def project_name(name):
    require_text(name, "project name", 200)
    if name in (".", "..") or any(c in name for c in "/\\") or any(ord(c) < 32 or ord(c) == 127 for c in name):
        raise BossError("usage", "project name must be a single filename component", "boss project list", 1)
    return name


def notes_path(home, name):
    return home / "projects" / (project_name(name) + ".md")


def goal_path(home, name):
    return home / "projects" / (project_name(name) + ".goal")


def steers_path(home, name):
    return home / "projects" / (project_name(name) + ".steers.jsonl")


def layer_text(path, label, redact):
    if not path.exists():
        return ""
    with path.open(encoding="utf-8") as stream:
        text = stream.read(BRIEF_LIMIT + 1)
    if len(text) > BRIEF_LIMIT:
        raise BossError("usage", f"{label} layer exceeds the {BRIEF_LIMIT}-character composed brief limit",
                        f"shorten {path}", 1)
    if redact(text) != text:
        refuse(f"{label} layer contains a token or secret", f"remove credentials from {path}")
    return text.strip()


def source_note(path, origin):
    return f"<!-- source: {path} -->\n" if origin else ""


def steering_sections(goal, steers, redact):
    rendered = "\n".join(f"- {s['id']}: {s['text']}" + (f" [task {s['task']}]" if s.get("task") else "")
                         for s in steers)
    sections = []
    for label, heading, text in (("project goal", "Project goal", goal),
                                 ("captain steers", "Captain steers", rendered)):
        if redact(text) != text:
            refuse(f"{label} layer contains a token or secret", "remove credentials from the project context")
        if text:
            sections.append((label, f"\n\n## {heading}\n{text}"))
    return sections


def compose_brief(brief, home, name, contract, redact, *, goal="", steers=(), origin=False):
    require_text(brief, "task brief", BRIEF_LIMIT)
    parts = [("task brief", brief)]
    for label, text in steering_sections(goal, steers, redact):
        if origin:
            path = goal_path(home, name) if label == "project goal" else steers_path(home, name)
            heading, body = text.lstrip().split("\n", 1)
            text = "\n\n" + heading + "\n" + source_note(path, True) + body
        parts.append((label, text))
    parts.append(("worker contract", contract))
    composed = ""
    for label, text in parts:
        composed += redact(text)
        if len(composed) > BRIEF_LIMIT:
            raise BossError("usage", f"{label} layer makes the composed brief exceed {BRIEF_LIMIT} characters",
                            f"shorten the {label} layer", 1)
    # Redact task text too, so the saved and transmitted briefs are identical.
    return composed
