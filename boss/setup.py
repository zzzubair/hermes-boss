"""Portable first-run setup and read-only environment checks."""
import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shlex
import shutil
import socket
import subprocess
import sys

from .common import BossError, Redactor
from .config import Config, boss_home, hermes_home
from .preferences import read_rules
from .state import atomic_write
from .wake import CONSENT_KEY, CONSENT_LINE, target_configured


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
    return any((job.get("name") in ("boss-poll", "boss-wake") or
                job.get("script") in ("boss-poll-gate.sh", str(hermes_home() / "scripts/boss-poll-gate.sh")))
               and job.get("enabled", True) and job.get("state") not in ("paused", "completed") and not job.get("paused_at")
               and not job.get("no_agent", False)
               for job in data["jobs"])


def add_wake_arguments(parser):
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--with-wake", action="store_true", help="configure plugin wake; never edit Hermes config")
    modes.add_argument("--with-cron", action="store_true", help="install the legacy Hermes wake cron")
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--wake-origin", help="SessionSource JSON object for the captain's chat")
    target.add_argument("--wake-session-key", help="existing durable gateway routing key")
    parser.add_argument("--wake-interval", type=int, help="poll interval in seconds; default 300")


def wake_settings(config, args):
    data = copy.deepcopy(config.data)
    supplied = args.wake_origin is not None or args.wake_session_key is not None or args.wake_interval is not None
    if supplied and not args.with_wake:
        raise BossError("usage", "--wake-* options require --with-wake", exit_code=1)
    if args.with_wake:
        wake = data.setdefault("wake", {})
        wake["enabled"] = True
        wake["interval_seconds"] = args.wake_interval if args.wake_interval is not None else wake.get("interval_seconds", 300)
        if args.wake_origin is not None:
            try:
                wake["origin"] = json.loads(args.wake_origin)
            except ValueError:
                raise BossError("usage", "--wake-origin must be a JSON object", exit_code=1) from None
            wake.pop("session_key", None)
        elif args.wake_session_key is not None:
            wake["session_key"] = args.wake_session_key
            wake.pop("origin", None)
    config.validate(data)
    if args.with_cron and data.get("wake", {}).get("enabled", True) and target_configured(data.get("wake", {})):
        raise BossError("config", "plugin wake is configured; cron would compete for poll changes",
                        "boss config set wake.enabled false; then retry --with-cron", 1)
    return data


def wake_instructions(data):
    lines = [f"add to {hermes_home() / 'config.yaml'} under plugins > entries > boss:",
             "  allow_gateway_injection: true", "exact setting: " + CONSENT_LINE,
             "Hermes config unchanged; enable boss and restart the gateway after configuring wake."]
    if not target_configured(data.get("wake", {})):
        lines.append("wake target not configured: no thread; set wake.origin or wake.session_key, then run boss doctor")
    return lines


def initialize(boss, args):
    config = boss.config
    data = wake_settings(config, args)
    if config.path.exists() and config.hosts and not args.force:
        if args.with_wake:
            config.write(data)
        lines = [f"config exists: {config.path} (host unchanged; --force permits updating the host)",
                 "next: boss host list; boss doctor"]
        if args.with_wake:
            lines.extend(wake_instructions(data))
        if args.with_cron:
            lines.extend(install_cron())
        return lines, 0
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
    lines = [f"configured: {config.path}", f"host: {args.name} ({len(projects)} projects)",
             "next: hermes plugins enable boss", "next: boss doctor; boss project list",
             "optional wake-up: boss init --with-wake --wake-session-key <key>"]
    if args.with_wake:
        lines.extend(wake_instructions(data))
    if args.with_cron:
        lines.extend(install_cron())
    return lines, 0


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
    wake = boss.config.data.get("wake", {})
    requested = "wake" in boss.config.data and wake.get("enabled", True)

    def wake_target():
        if not target_configured(wake):
            raise ValueError("no target configured; no wake thread starts")
        return True

    lines.append("plugin wake: " + ("requested" if requested else "disabled"))
    check("wake target", wake_target,
          "boss init --with-wake --wake-session-key <gateway key> or --wake-origin '<JSON>'", required=requested)
    check("wake gateway consent", lambda: json.loads(command(["hermes", "config", "get", CONSENT_KEY, "--json"])) is True,
          "add " + CONSENT_LINE + " in the profile's config.yaml", required=requested)
    check("single wake source", lambda: not (requested and target_configured(wake) and wake_cron()),
          "pause/remove the boss-wake or boss-poll cron manually, or set wake.enabled false")
    def wake_alive():
        from .wake import HEARTBEAT
        text = (boss.config.home / HEARTBEAT).read_text().split()
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(text[0])).total_seconds()
        limit = 2 * wake.get("interval_seconds", 300) + 60
        if age > limit:
            raise ValueError(f"last armed poll {int(age)}s ago (limit {limit}s)")
        return True
    check("wake loop alive", wake_alive, "restart the Hermes gateway; then rerun boss doctor",
          required=requested and target_configured(wake))
    check("wake cron", wake_cron, "./install.sh --with-cron; keep the Hermes gateway running", required=False)
    return lines, 1 if failures else 0


def install_cron():
    if not shutil.which("hermes"):
        raise BossError("config", "--with-cron needs hermes on PATH", exit_code=1)
    root = Path(__file__).resolve().parent.parent
    script = hermes_home() / "scripts/boss-poll-gate.sh"
    assignments = {k: v for k, v in os.environ.items() if k in ("BOSS_HOME", "XDG_CONFIG_HOME", "HERMES_HOME")}
    assignments.setdefault("HERMES_HOME", str(hermes_home()))
    invocation = ["env", *(f"{k}={v}" for k, v in assignments.items()), sys.executable,
                  str(root / "bin/boss"), "poll", "--hermes-gate"]
    atomic_write(script, "#!/bin/sh\nexec " + shlex.join(invocation) + "\n")
    if wake_cron():
        return ["wake cron exists (unchanged)"]
    subprocess.run(["hermes", "cron", "create", "*/5 * * * *", CRON_PROMPT,
                    "--name", "boss-poll", "--script", script.name, "--deliver", "local"], check=True)
    return ["installed legacy boss-poll cron"]


def install(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description="Install boss links and optional wake settings. Hermes config.yaml is never edited.")
    add_wake_arguments(parser)
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
    config = None
    if args.with_wake or args.with_cron or any(v is not None for v in (args.wake_origin, args.wake_session_key, args.wake_interval)):
        try:
            config = Config(boss_home(), Redactor())
            data = wake_settings(config, args)
        except BossError as exc:
            parser.error(config.redact(exc.message) if config else exc.code + ": invalid boss config")
    for target, source in targets:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_symlink():
            target.unlink()
        target.symlink_to(source)
        print(f"installed: {target}")
    print("enable plugin (opt-in): hermes plugins enable boss")
    print("next: boss init")
    if args.with_wake:
        config.write(data)
        print("configured wake: " + str(config.path))
        print("\n".join(wake_instructions(data)))
    if args.with_cron:
        print("\n".join(install_cron()))
    return 0


if __name__ == "__main__":
    raise SystemExit(install())
