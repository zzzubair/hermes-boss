"""Project names and the ordered, bounded worker brief."""
from dataclasses import dataclass
from pathlib import Path
import re

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


@dataclass(frozen=True)
class RuleSection:
    heading: str | None
    text: str
    source: Path


def read_rules(path, label, redact):
    """Read ATX/Setext sections, keeping nested headings with their parent.

    Fences protect literal headings. Duplicate sections use the last body at
    the first position; doctor reports duplicates rather than blocking work.
    """
    text = layer_text(path, label, redact)
    if "\0" in text:
        raise BossError("rules", f"unparsable rules file {path}: NUL character", f"edit {path}", 1)
    sections, warnings, seen = {}, [], set()
    heading, level, body, fence = None, 0, [], None

    def finish():
        content = "\n".join(body).strip()
        if heading is not None and not any(line.strip() for line in body[1:]):
            warnings.append(f"empty heading {heading!r} (no content)")
        if heading is not None or content:
            sections[heading] = RuleSection(heading, content, path)

    lines = text.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        index += 1
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if fence:
            body.append(line)
            if marker and marker[1][0] == fence[0] and len(marker[1]) >= len(fence) and not marker[2].strip():
                fence = None
            continue
        if marker:
            fence = marker[1]
            body.append(line)
            continue
        match = re.match(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?)|[ \t]*)$", line)
        depth, title = (len(match[1]), re.sub(r"(?:^|[ \t]+)#+[ \t]*$", "", match[2] or "").strip()) if match else (0, "")
        if not match and line.strip() and index < len(lines) and re.fullmatch(r" {0,3}(?:=+|-+)[ \t]*", lines[index]):
            depth, title = (1 if lines[index].lstrip().startswith("=") else 2), line.strip()
            index += 1
        if not depth:
            body.append(line)
            continue
        if not title:
            warnings.append("empty heading")
        if title in seen:
            warnings.append(f"duplicate heading {title!r}")
        seen.add(title)
        if heading is None or depth <= level:
            finish()
            heading, level, body = title, depth, []
        body.append("#" * min(6, 3 + depth - level) + " " + title)
    if fence:
        raise BossError("rules", f"unparsable rules file {path}: unclosed code fence", f"close the fence in {path}", 1)
    finish()
    return list(sections.values()), warnings


def merged_rules(home, name, redact):
    sections = {}
    for path, label in ((home / "rules.md", "global rules"), (notes_path(home, name), "project notes")):
        parsed, _ = read_rules(path, label, redact)
        sections.update((section.heading, section) for section in parsed)
    return list(sections.values())


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


def compose_brief(brief, home, name, settings, contract, redact, *, goal="", steers=(), origin=False, settings_source=None):
    require_text(brief, "task brief", BRIEF_LIMIT)
    rules = merged_rules(home, name, redact)
    rendered = []
    if "test_host" in settings:
        rendered.append(f"test_host: {settings['test_host']}")
    if "no_mistakes" in settings:
        rendered.append(f"no_mistakes: {str(settings['no_mistakes']).lower()}")
    parts = [("task brief", brief), ("rules", "\n\n## Rules")]
    for section in rules:
        if section.heading == "boss worker contract" or (rendered and section.heading == "Execution settings"):
            continue
        label = "global rules" if section.source == home / "rules.md" else "project notes"
        parts.append((label, "\n\n" + source_note(section.source, origin) + section.text))
    if rendered:
        parts.append(("project settings", "\n\n### Execution settings\n" +
                      source_note(settings_source or home / "config.toml", origin) + "\n".join(rendered)))
    parts.append(("worker contract", contract))
    for label, text in steering_sections(goal, steers, redact):
        if origin:
            path = goal_path(home, name) if label == "project goal" else steers_path(home, name)
            heading, body = text.lstrip().split("\n", 1)
            text = "\n\n" + heading + "\n" + source_note(path, True) + body
        parts.append((label, text))
    composed = ""
    for label, text in parts:
        composed += redact(text)
        if len(composed) > BRIEF_LIMIT:
            raise BossError("usage", f"{label} layer makes the composed brief exceed {BRIEF_LIMIT} characters",
                            f"shorten the {label} layer", 1)
    # Layer files have already been refused if redaction changes them. Redact
    # the task text too, so the saved brief and transmitted brief are identical.
    return composed
