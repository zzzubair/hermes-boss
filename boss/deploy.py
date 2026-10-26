"""boss deploy: the supervisor deploys a project on the captain's word, never a worker.

Guards are deterministic: his quote, no no-touch window, no project fence and no
open or refused deploy decision. The run is one blocking command: deploy over ssh,
check once, wait check_minutes with progress lines, check again. It holds the
ledger lock only to journal, so other boss commands keep working during the wait.
Remote output is scrubbed of env lines and secrets before it is printed.
"""
from datetime import datetime, timezone
import re
import shlex
import subprocess
from time import sleep

from . import captain
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


def settings(config, project):
    deploy = config.project(project).get("deploy")
    if not deploy:
        raise BossError("config", f"no deploy configured for {project}",
                        f"set projects.{project}.deploy.host and .command in {config.path} (see README: Deploys)", 1)
    return {"check_minutes": 10, "no_touch": [], "auto_rollback": False, **deploy}


def blockers(store, project, deploy, at):
    """Plain reasons boss may not deploy right now; empty when it may."""
    found = [f"{window} UTC is a no-touch window for {project}" for window in deploy["no_touch"] if inside(window, at)]
    found += [f"the captain fenced {project}: {captain.plain(s['text'], 200)}"
              for s in store.steers(project) if s.get("fence") and s.get("task") is None]
    asks = [d for d in store.decisions() if d.get("action") == "deploy"]
    for meta in store.tasks():
        mine = [d for d in asks if d.get("task") == meta["id"]] if meta["project"] == project else []
        if any(not d.get("closed") for d in mine):
            found.append(f"the deploy question about {captain.plain(meta['title'], 120)} is still open")
            continue
        # His latest answer wins, as in captain.approval.
        answer = max(mine, key=lambda d: d.get("closed_at") or "", default=None)
        if answer is not None and answer.get("verdict") == "no":
            found.append(f"the captain said no to deploying {captain.plain(meta['title'], 120)}: "
                         f"{captain.plain(answer['resolution'], 160)}")
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


def run(boss, project, approved, dry_run, session_key, out):
    """The whole guarded deploy; returns the exit code."""
    deploy = settings(boss.config, project)
    store, redact = boss.store, boss.redact
    if dry_run:
        found = blockers(store, project, deploy, datetime.now(timezone.utc))
        out(f"dry run: nothing ran on {deploy['host']}")
        out(f"deploy: {deploy['command']}")
        later = f" and after {captain.plural(deploy['check_minutes'], 'minute')}" if deploy["check_minutes"] else ""
        out("check: " + (f"{deploy['check']} now{later}" if deploy.get("check") else "none"))
        out("rollback: " + (deploy.get("rollback") or "none") + (" (runs by itself on a failed check)" if deploy["auto_rollback"] else ""))
        for reason in found:
            out("would refuse: " + reason)
        out("next: " + (f'boss deploy {project} --approved "<captain words>"' if not found else "wait until nothing above holds"))
        return 2 if found else 0
    if not approved or not approved.strip():
        refuse("deploying needs the captain's exact words", f'boss deploy {project} --approved "<captain words>"')
    require_text(approved, "captain words", 2000)
    if captain.verdict(approved) == "no":
        refuse(f"his words say no: {captain.plain(approved, 200)}", "deploy only when he says yes")
    if not session_key:
        refuse("boss deploy runs only in the Hermes session that owns boss", "run it from the owner session")
    with store.locked():
        owner.guard(store.home, session_key)
        found = blockers(store, project, deploy, datetime.now(timezone.utc))
        if found:
            raise BossError("refused", f"not deploying {project}", "never work around it: wait out the window, or ask the captain (boss steer drop lifts a fence he lifts)", 2,
                            context=["reason: " + reason for reason in found])
        store.captain("deploy", phase="start", project=project, approved=approved.strip(), host=deploy["host"])
    out(f"deploying {project} on {deploy['host']}...")
    code = remote(deploy["host"], deploy["command"], redact, out)
    if code != 0:
        result, ok = f"the deploy command failed (exit {code})", False
    elif not deploy.get("check"):
        result, ok = "deployed; no check is configured", True
    elif not check(deploy, project, redact, out, "right away"):
        result, ok = "deployed, but the check failed right away", False
    elif deploy["check_minutes"]:
        minutes = captain.plural(deploy["check_minutes"], "minute")
        for left in range(deploy["check_minutes"], 0, -1):
            out(f"waiting {captain.plural(left, 'more minute')} before the second check...")
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
        store.captain("deploy", phase="done", project=project, ok=ok, result=result,
                      **({"rolled_back": rolled == 0} if rolled is not None else {}))
    out(f"{project}: {result}.")
    if ok:
        out("next: tell the captain it is live")
    elif rolled == 0:
        out("next: tell the captain it failed and was rolled back")
    else:
        out("next: tell the captain it failed" + (" and ask whether to run the rollback above" if deploy.get("rollback") and rolled is None else ""))
    return 0 if ok else 3
