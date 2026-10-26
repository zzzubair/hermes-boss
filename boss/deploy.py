"""boss deploy: the supervisor deploys a project on your word, never a worker.

Guards are deterministic: your quote, a valid --ref when the table names a release,
no no-touch window now or before the checks can finish, no project fence and no
open or refused deploy decision. The run is one blocking command: deploy over ssh,
check once, wait check_minutes with a progress line each minute, check again. It holds the
ledger lock only to journal, so other boss commands keep working during the wait.
Remote output is scrubbed of env lines and secrets before it is printed.
"""
from datetime import datetime, timedelta, timezone
import re
import shlex
import subprocess
from time import sleep

from . import user
from . import owner
from .common import BossError, refuse, require_text


SSH = ["ssh", "-oBatchMode=yes", "-oConnectTimeout=10", "-oServerAliveInterval=30", "-oServerAliveCountMax=4"]
WINDOW = re.compile(r"([01]\d|2[0-3]):([0-5]\d)-([01]\d|2[0-3]):([0-5]\d)")
ENV_LINE = re.compile(r"^\s*(?:export\s+|declare\s+-x\s+)?[A-Za-z_][A-Za-z0-9_]*=")
# A secret-looking key hides the rest of its line: "Authorization: Basic abc" leaks nothing.
SECRET_PAIR = re.compile(r"(?i)\b([A-Za-z0-9_.-]*(?:token|secret|passw(?:or)?d|api[_-]?key|authorization|credential|"
                         r"private[_-]?key)[A-Za-z0-9_.-]*\"?\s*[:=]\s*).+$")
SECRET_SHAPES = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,}|"
                           r"xox[abprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+)")
TAIL = 20
# --ref is a git sha; {sha} is it as given (lowercased), {sha7} its first 7 characters.
REF = re.compile(r"[0-9a-fA-F]{7,40}")
PLACEHOLDERS = ("{sha}", "{sha7}")
RELEASE_FIELDS = ("command", "check", "rollback")
# A no-touch window may not start until the checks are done plus this many minutes.
MARGIN = 5


def validate(table, fail, prefix):
    """Shape rules for one [projects.<name>.deploy] table; called from Config.validate."""
    if "command" not in table:
        fail(prefix + ".command", "required")
    host = table.get("host")
    if host is None:
        fail(prefix + ".host", "required")
    if not re.fullmatch(r"[A-Za-z0-9_@.:-]+", host) or host.startswith("-"):
        fail(prefix + ".host", "invalid ssh destination")
    if table.get("check_minutes", 0) < 0:
        fail(prefix + ".check_minutes", "must be 0 (check once) or more")
    for window in table.get("no_touch", []):
        match = WINDOW.fullmatch(window) if isinstance(window, str) else None
        if not match or match.groups()[:2] == match.groups()[2:]:
            fail(prefix + ".no_touch", 'must list UTC windows like "04:00-07:00"')
    if table.get("auto_rollback") and "rollback" not in table:
        fail(prefix + ".auto_rollback", "needs deploy.rollback")


def inside(window, at):
    """True when at (UTC) falls in "HH:MM-HH:MM"; a window may wrap past midnight."""
    match = WINDOW.fullmatch(window)
    start, end = int(match[1]) * 60 + int(match[2]), int(match[3]) * 60 + int(match[4])
    minute = at.hour * 60 + at.minute
    return start <= minute < end if start < end else minute >= start or minute < end


def run_minutes(deploy):
    """How long a run may take: the check wait (when there is a check) plus the margin."""
    return (deploy["check_minutes"] if deploy.get("check") else 0) + MARGIN


def starts_within(window, at, minutes):
    """True when window begins after at but no later than minutes from at."""
    return not inside(window, at) and any(inside(window, at + timedelta(minutes=step)) for step in range(1, minutes + 1))


def release(deploy, project, ref):
    """The deploy table with {sha} and {sha7} filled from --ref; refuses a bad or missing ref."""
    used = [field for field in RELEASE_FIELDS if any(p in (deploy.get(field) or "") for p in PLACEHOLDERS)]
    if ref is None:
        if used:
            refuse(f"the {project} deploy needs a release: deploy.{' and deploy.'.join(used)} use {{sha}} or {{sha7}}",
                   f"boss deploy {project} --ref <git sha> ...")
        return deploy
    if not REF.fullmatch(ref):
        refuse(f"--ref must be a git sha of 7 to 40 hex characters, not {user.plain(ref, 60)}",
               f"boss deploy {project} --ref <git sha> ...")
    if not used:
        refuse(f"--ref changes nothing: the {project} deploy table has no {{sha}} or {{sha7}}",
               f"drop --ref, or put {{sha7}} in projects.{project}.deploy.command")
    sha = ref.lower()
    filled = {field: deploy[field].replace("{sha7}", sha[:7]).replace("{sha}", sha) for field in used}
    return {**deploy, **filled, "ref": sha}


def settings(config, project):
    deploy = config.project(project).get("deploy")
    if not deploy:
        raise BossError("config", f"no deploy configured for {project}",
                        f"set projects.{project}.deploy.host and .command in {config.path} (see README: Deploys)", 1)
    return {"check_minutes": 10, "no_touch": [], "auto_rollback": False, **deploy}


def blockers(store, project, deploy, at):
    """Plain reasons boss may not deploy right now; empty when it may."""
    found = [f"{window} UTC is a no-touch window for {project}" for window in deploy["no_touch"] if inside(window, at)]
    minutes = run_minutes(deploy)
    wait = f"{user.plural(deploy['check_minutes'], 'minute')} of checks plus " if deploy.get("check") and deploy["check_minutes"] else ""
    found += [f"the {window} UTC no-touch window for {project} starts at {window[:5]} UTC, before this run can finish "
              f"({wait}a {MARGIN}-minute margin, so until {at + timedelta(minutes=minutes):%H:%M} UTC)"
              for window in deploy["no_touch"] if starts_within(window, at, minutes)]
    found += [f"you fenced {project}: {user.plain(s['text'], 200)}"
              for s in store.steers(project) if s.get("fence") and s.get("task") is None]
    asks = [d for d in store.decisions() if d.get("action") == "deploy"]
    for meta in store.tasks():
        mine = [d for d in asks if d.get("task") == meta["id"]] if meta["project"] == project else []
        if any(not d.get("closed") for d in mine):
            found.append(f"the deploy question about {user.plain(meta['title'], 120)} is still open")
            continue
        # The latest answer wins, as in user.approval.
        answer = max(mine, key=lambda d: d.get("closed_at") or "", default=None)
        if answer is not None and answer.get("verdict") == "no":
            found.append(f"you said no to deploying {user.plain(meta['title'], 120)}: "
                         f"{user.plain(answer['resolution'], 160)}")
    return found


def scrub(text, redact):
    """The last lines of remote output with env lines and secrets removed."""
    lines = []
    for line in str(text).splitlines():
        if ENV_LINE.match(line):
            if lines[-1:] != ["[environment line hidden]"]:
                lines.append("[environment line hidden]")
            continue
        line = SECRET_SHAPES.sub("[REDACTED]", SECRET_PAIR.sub(r"\1[REDACTED]", redact(line)))
        lines.append(line.rstrip())
    lines = [line for line in lines if line.strip()]
    return (["..."] if len(lines) > TAIL else []) + lines[-TAIL:]


def remote(host, command, redact, out):
    """Run one command on host over ssh; prints its scrubbed output and returns the exit code."""
    try:
        result = subprocess.run([*SSH, host, command], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, errors="replace")
    except OSError as exc:
        out(f"  cannot run ssh: {exc}")
        return 255
    for line in scrub(result.stdout, redact):
        out("  " + line)
    return result.returncode


def check(deploy, project, redact, out, when):
    out(f"checking {project} {when}...")
    code = remote(deploy["host"], deploy["check"], redact, out)
    out(f"check passed {when}." if code == 0 else f"check failed {when} (exit {code}).")
    return code == 0


def run(boss, project, approved, dry_run, session_key, out, ref=None):
    """The whole guarded deploy; returns the exit code."""
    deploy = release(settings(boss.config, project), project, ref)
    again = f" --ref {deploy['ref']}" if deploy.get("ref") else ""
    store, redact = boss.store, boss.redact
    if dry_run:
        found = blockers(store, project, deploy, datetime.now(timezone.utc))
        out(f"dry run: nothing ran on {deploy['host']}")
        if deploy.get("ref"):
            out(f"release: {deploy['ref']} ({{sha7}} = {deploy['ref'][:7]})")
        out(f"deploy: {deploy['command']}")
        later = f" and after {user.plural(deploy['check_minutes'], 'minute')}" if deploy["check_minutes"] else ""
        out("check: " + (f"{deploy['check']} now{later}" if deploy.get("check") else "none"))
        out("rollback: " + (deploy.get("rollback") or "none") + (" (runs by itself on a failed check)" if deploy["auto_rollback"] else ""))
        for reason in found:
            out("would refuse: " + reason)
        out("next: " + (f'boss deploy {project}{again} --approved "<your exact words>"' if not found else "wait until nothing above holds"))
        return 2 if found else 0
    if not approved or not approved.strip():
        refuse("deploying needs your exact words", f'boss deploy {project}{again} --approved "<your exact words>"')
    require_text(approved, "your words", 2000)
    if user.verdict(approved) == "no":
        refuse(f"your words say no: {user.plain(approved, 200)}", "deploy only on a yes")
    if not session_key:
        refuse("boss deploy runs only in the Hermes session that owns boss", "run it from the owner session")
    with store.locked():
        owner.guard(store.home, session_key)
        found = blockers(store, project, deploy, datetime.now(timezone.utc))
        if found:
            raise BossError("refused", f"not deploying {project}", "never work around it: wait out the window or get a new yes (boss steer drop lifts a fence once you lift it)", 2,
                            context=["reason: " + reason for reason in found])
        store.user_event("deploy", phase="start", project=project, approved=approved.strip(), host=deploy["host"],
                      **({"ref": deploy["ref"]} if deploy.get("ref") else {}))
    out(f"deploying {project}{' at ' + deploy['ref'][:7] if deploy.get('ref') else ''} on {deploy['host']}...")
    code = remote(deploy["host"], deploy["command"], redact, out)
    if code != 0:
        result, ok = f"the deploy command failed (exit {code})", False
    elif not deploy.get("check"):
        result, ok = "deployed; no check is configured", True
    elif not check(deploy, project, redact, out, "right away"):
        result, ok = "deployed, but the check failed right away", False
    elif deploy["check_minutes"]:
        minutes = user.plural(deploy["check_minutes"], "minute")
        due = datetime.now(timezone.utc) + timedelta(minutes=deploy["check_minutes"])
        # One line a minute, flushed, so a tracked background run shows it is alive.
        for left in range(deploy["check_minutes"], 0, -1):
            out(f"waiting {user.plural(left, 'more minute')} before the second check (due {due:%H:%M} UTC)...")
            sleep(60)
        ok = check(deploy, project, redact, out, f"after {minutes}")
        result = f"deployed; check passed right away and after {minutes}" if ok else \
            f"deployed, but the check failed after {minutes}"
    else:
        result, ok = "deployed; check passed", True
    rolled = None
    if not ok and deploy.get("rollback"):
        if deploy["auto_rollback"]:
            out(f"rolling back {project}...")
            rolled = remote(deploy["host"], deploy["rollback"], redact, out)
            result += "; rolled back" if rolled == 0 else f"; the rollback failed too (exit {rolled})"
        else:
            out(f"rollback (not run): ssh {deploy['host']} {shlex.quote(deploy['rollback'])}")
    with store.locked(wait=True):
        store.user_event("deploy", phase="done", project=project, ok=ok, result=result,
                      **({"rolled_back": rolled == 0} if rolled is not None else {}))
    out(f"{project}: {result}.")
    if ok:
        out("next: report that it is live")
    elif rolled == 0:
        out("next: report that it failed and was rolled back")
    else:
        out("next: report that it failed" + (" and ask whether to run the rollback above" if deploy.get("rollback") and rolled is None else ""))
    return 0 if ok else 3
