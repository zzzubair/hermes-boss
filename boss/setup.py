"""Portable first-run setup and read-only environment checks."""
import copy
import json
import os
from pathlib import Path
import shlex
import shutil
import socket
import subprocess
import sys

from .common import BossError
from .config import hermes_home
from .preferences import read_rules
from .state import atomic_write


DEFAULT_URL = "http://127.0.0.1:3773/mcp"
CRON_PROMPT = "Run boss status, review changed workers and pending decisions, and report actionable items to the captain. Never invent merge approval."


def install_paths():
    return {
        "bin": Path(os.environ.get("BOSS_BIN_DIR", str(Path.home() / ".local/bin"))).expanduser(),
        "skills": Path(os.environ.get("BOSS_SKILLS_DIR", str(hermes_home() / "skills"))).expanduser(),
        "agent_skills": Path(os.environ.get("BOSS_AGENT_SKILLS_DIR", str(Path.home() / ".agents/skills"))).expanduser(),
        "plugins": Path(os.environ.get("BOSS_PLUGINS_DIR", str(hermes_home() / "plugins"))).expanduser(),
    }


def wake_cron():
    path = hermes_home() / "cron/jobs.json"
    if not path.exists():
        return False
    data = json.loads(path.read_text())
    return any(job.get("name") == "boss-poll" and job.get("enabled", True) and job.get("state") not in ("paused", "completed") and not job.get("paused_at")
               and not job.get("no_agent", False) and job.get("script") in ("boss-poll-gate.sh", str(hermes_home() / "scripts/boss-poll-gate.sh"))
               and (hermes_home() / "scripts/boss-poll-gate.sh").is_file()
               for job in data["jobs"])


def initialize(boss, args):
    config = boss.config
    if config.path.exists() and not args.force:
        return [f"config exists: {config.path} (unchanged; --force permits updating the host)",
                "next: boss host list; boss doctor"], 0
    data = copy.deepcopy(config.data)
    data.setdefault("hosts", {})[args.name] = {"url": args.url or DEFAULT_URL, "token_env": args.token_env}
    data["default_host"] = args.name
    config.validate(data)
    # Probe the candidate before committing any config. Failure leaves the file intact.
    prior = config.data
    config.data = data
    config._load()
    try:
        projects = boss.project_catalog(args.name)
    finally:
        config.data = prior
        config._load()
    config.write(data)
    return [f"configured: {config.path}", f"host: {args.name} ({len(projects)} projects)",
            "next: hermes plugins enable boss", "next: boss doctor; boss project list",
            "optional wake-up: ./install.sh --with-cron"], 0


def doctor(boss):
    lines, failures = [], 0

    def check(label, action, fix, required=True):
        nonlocal failures
        try:
            detail = action()
            if detail is False:
                raise ValueError("not configured")
            lines.append(f"ok: {label}" + (f" {detail}" if isinstance(detail, str) else ""))
        except (BossError, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
            failures += int(required)
            message = exc.message if isinstance(exc, BossError) else str(exc)
            lines.append(f"{'fail' if required else 'optional'}: {label}: {message}; fix: {fix}")

    def command(argv):
        result = subprocess.run(argv, capture_output=True, text=True, timeout=30, check=False)
        if result.returncode:
            raise ValueError(f"{argv[0]} exited {result.returncode}")
        return result.stdout

    check("Python 3.12+", lambda: sys.version_info >= (3, 12), "install Python 3.12 or newer")
    check("gh auth", lambda: command(["gh", "auth", "status"]) is not None, "gh auth login")
    check("hosts", lambda: bool(boss.config.hosts), "boss init")
    global_rules = {}
    for path in [boss.store.home / "rules.md", *sorted((boss.store.home / "projects").glob("*.md"))]:
        def lint(path=path):
            sections, warnings = read_rules(path, "rules", boss.redact)
            if path == boss.store.home / "rules.md":
                global_rules.update((s.heading, s.text) for s in sections)
            else:
                warnings.extend(f"heading {s.heading or 'preamble'!r} is identical to global (pointless override)"
                                for s in sections if global_rules.get(s.heading) == s.text)
            lines.extend(f"warning: {path}: {warning}" for warning in warnings)
        check(f"rules {path}", lint, f"edit {path}")
    for name, host in boss.config.hosts.items():
        token_key = host.get("token_env", "T3_MCP_TOKEN")
        check(f"host {name} token", lambda n=name: bool(boss.config.host(n)[1]),
              f"set {token_key} in env or {hermes_home() / '.env'}")
        check(f"host {name} MCP + projects", lambda n=name: f"{len(boss.project_catalog(n))} projects",
              f"boss host test {shlex.quote(name)}; check T3 MCP URL/token")
        if host.get("ssh"):
            def resolve_ssh(alias=host["ssh"]):
                settings = command(["ssh", "-G", alias])
                hostname = next((line.split(maxsplit=1)[1] for line in settings.splitlines() if line.startswith("hostname ")), None)
                if not hostname:
                    raise ValueError("SSH returned no hostname")
                socket.getaddrinfo(hostname, None)
                return hostname
            check(f"host {name} SSH", resolve_ssh, f"configure {host['ssh']} in ~/.ssh/config and check DNS")
    paths = install_paths()
    check("plugin installed", lambda: (paths["plugins"] / "boss/plugin.yaml").is_file(), "./install.sh")

    def enabled():
        plugins = json.loads(command(["hermes", "plugins", "list", "--json"]))
        return any(p.get("name") == "boss" and p.get("status") == "enabled" for p in plugins)
    check("plugin enabled", enabled, "hermes plugins enable boss")
    check("skill installed", lambda: (paths["skills"] / "boss/SKILL.md").is_file(), "./install.sh")
    check("wake cron", wake_cron, "./install.sh --with-cron; keep the Hermes gateway running", required=False)
    return lines, 1 if failures else 0


def install(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description="Install boss links; Hermes config changes only with --with-cron.")
    parser.add_argument("--with-cron", action="store_true")
    parser.add_argument("--hermes-home")
    for name in ("bin", "skills", "agent-skills", "plugins"):
        parser.add_argument("--" + name + "-dir")
    args = parser.parse_args(argv)
    if sys.version_info < (3, 12):
        parser.error("boss requires Python 3.12+")
    if args.hermes_home:
        os.environ["HERMES_HOME"] = str(Path(args.hermes_home).expanduser())
    for name in ("bin", "skills", "agent_skills", "plugins"):
        value = getattr(args, name + "_dir")
        if value:
            os.environ["BOSS_" + name.upper() + "_DIR"] = str(Path(value).expanduser())
    root = Path(__file__).resolve().parent.parent
    paths = install_paths()
    targets = [(paths["bin"] / "boss", root / "bin/boss"), (paths["skills"] / "boss", root / "skill"),
               (paths["agent_skills"] / "boss", root / "skill"), (paths["plugins"] / "boss", root / "plugin")]
    # Refuse all conflicts before changing any links.
    for target, source in targets:
        if target.exists() and not target.is_symlink():
            parser.error(f"{target} exists and is not a symlink")
    for target, source in targets:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_symlink():
            target.unlink()
        target.symlink_to(source)
        print(f"installed: {target}")
    print("enable plugin (opt-in): hermes plugins enable boss")
    print("next: boss init")
    if args.with_cron:
        if not shutil.which("hermes"):
            parser.error("--with-cron needs hermes on PATH")
        script = hermes_home() / "scripts/boss-poll-gate.sh"
        # Carry explicit homes into cron, where the interactive shell env may be absent.
        assignments = {k: v for k, v in os.environ.items() if k in ("BOSS_HOME", "XDG_CONFIG_HOME", "HERMES_HOME")}
        assignments.setdefault("HERMES_HOME", str(hermes_home()))
        invocation = ["env", *(f"{k}={v}" for k, v in assignments.items()), sys.executable,
                      str(root / "bin/boss"), "poll", "--hermes-gate"]
        atomic_write(script, "#!/bin/sh\nexec " + shlex.join(invocation) + "\n")
        if wake_cron():
            print("wake cron exists: boss-poll (unchanged)")
        else:
            subprocess.run(["hermes", "cron", "create", "*/5 * * * *", CRON_PROMPT,
                            "--name", "boss-poll", "--script", script.name, "--deliver", "local"], check=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(install())
