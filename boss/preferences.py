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


def compose_brief(brief, home, name, settings, contract, redact, *, goal="", steers=()):
    require_text(brief, "task brief", BRIEF_LIMIT)
    rules = layer_text(home / "rules.md", "global rules", redact)
    notes = layer_text(notes_path(home, name), "project notes", redact)
    rendered = []
    if "test_host" in settings:
        rendered.append(f"test_host: {settings['test_host']}")
    if "no_mistakes" in settings:
        rendered.append(f"no_mistakes: {str(settings['no_mistakes']).lower()}")
    layers = [("task brief", brief)]
    for label, heading, text in (("global rules", "Rules", rules),
                                 ("project settings", "Project settings", "\n".join(rendered)),
                                 ("project notes", "Project notes", notes)):
        if text:
            layers.append((label, f"\n\n## {heading}\n{text}"))
    layers.extend(steering_sections(goal, steers, redact))
    precedence = ("\n\nProject settings and project notes win over global rules when they conflict; "
                  "the boss worker contract below always applies.") if len(layers) > 1 else ""
    suffix = redact(precedence + contract)
    composed = ""
    for label, text in layers:
        composed += redact(text)
        if len(composed) + len(suffix) > BRIEF_LIMIT:
            raise BossError("usage", f"{label} layer makes the composed brief exceed {BRIEF_LIMIT} characters",
                            f"shorten the {label} layer", 1)
    # Layer files have already been refused if redaction changes them. Redact
    # the task text too, so the saved brief and transmitted brief are identical.
    return composed + suffix
