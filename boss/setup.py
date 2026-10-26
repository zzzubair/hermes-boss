"""Portable first-run setup and read-only environment checks."""
import copy
import fcntl
import json
import os
from pathlib import Path
import shlex
import socket
import subprocess
import sys
import urllib.error
import urllib.request

from .common import BossError
from .config import allows, hermes_home, literal, model_rules, rule_entry
from .integrations import command_path
from . import owner
from .wake import CONSENT_KEY, CONSENT_LINE, HEARTBEAT, LAST, LOCK, endpoints, legacy_target


DEFAULT_URL = "http://127.0.0.1:3773/mcp"


def install_paths():
    return {
        "bin": Path(os.environ.get("BOSS_BIN_DIR", str(Path.home() / ".local/bin"))).expanduser(),
        "skills": Path(os.environ.get("BOSS_SKILLS_DIR", str(hermes_home() / "skills"))).expanduser(),
        "agent_skills": Path(os.environ.get("BOSS_AGENT_SKILLS_DIR", str(Path.home() / ".agents/skills"))).expanduser(),
        "plugins": Path(os.environ.get("BOSS_PLUGINS_DIR", str(hermes_home() / "plugins"))).expanduser(),
    }


WAKE_SETUP = ["wakes: the Hermes gateway (or app) listens to T3's push stream; nothing polls",
              "wake consent: add allow_gateway_injection: true under plugins > entries > boss in"
              " $HERMES_HOME/config.yaml (exact setting: " + CONSENT_LINE + ")",
              "events token: t3 auth session issue --scope orchestration:read --label boss-events --token-only"
              " -> T3_EVENTS_TOKEN in env or $HERMES_HOME/.env",
              "next: run /boss in the Hermes chat that should own boss and get its wakes"]


def initialize(boss, args):
    config = boss.config
    data = copy.deepcopy(config.data)
    if config.path.exists() and config.hosts and not args.force:
        return [f"config exists: {config.path} (host unchanged; --force permits updating the host)",
                "next: boss host list; boss doctor"], 0
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
            "next: hermes plugins enable boss", "next: boss doctor; boss project list", *WAKE_SETUP], 0


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
    if (boss.store.home / "rules.md").is_file():
        lines.append("info: rules.md is no longer sent to workers")
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
    # Every host a no-mistakes project spawns on needs the no-mistakes command; boss spawn refuses without it.
    pipeline = {}
    for name in boss.config.projects:
        settings = boss.config.effective(name)[0]
        if settings["delivery"] == "no-mistakes" and settings["host"]:
            pipeline.setdefault(settings["host"], []).append(name)
    for name, projects in pipeline.items():
        def installed(n=name):
            if n not in boss.config.hosts:
                raise ValueError("host is not configured")
            path = command_path(boss.config.hosts[n], "no-mistakes")
            if not path:
                raise ValueError("no no-mistakes command")
            return path
        check(f"host {name} no-mistakes (for {', '.join(projects)})", installed,
              f"install no-mistakes on {name}, or set another host for {', '.join(projects)}")
    for level, text, fix in rule_findings(boss):
        failures += level == "fail"
        lines.append(f"{level}: {text}" + (f"; fix: {fix}" if fix else ""))
    paths = install_paths()
    check("plugin installed", lambda: (paths["plugins"] / "boss/plugin.yaml").is_file(), "./install.sh")

    def enabled():
        plugins = json.loads(command(["hermes", "plugins", "list", "--json"]))
        return any(p.get("name") == "boss" and p.get("status") == "enabled" for p in plugins)
    check("plugin enabled", enabled, "hermes plugins enable boss")
    check("skill installed", lambda: (paths["skills"] / "boss/SKILL.md").is_file(), "./install.sh")
    lines.extend(owner_lines(boss))
    wake = boss.config.data.get("wake", {})
    if "interval_seconds" in wake:
        lines.append("info: wake.interval_seconds is unused now that wakes are pushed; boss config unset wake.interval_seconds")
    try:
        routed = wake.get("enabled", True) and (owner.read(boss.store.home) is not None or legacy_target(wake) is not None)
    except BossError:
        routed = False  # owner_lines already failed on the unreadable record
    check("wake gateway consent", lambda: json.loads(command(["hermes", "config", "get", CONSENT_KEY, "--json"])) is True,
          "add " + CONSENT_LINE + " in the profile's config.yaml", required=routed)
    for name, host in boss.config.hosts.items():
        def stream(n=name, h=host):
            # Ticket exchange only: proves the token opens the push stream without subscribing.
            token = boss.config.events_token(n)
            request = urllib.request.Request(endpoints(h["url"])[1], data=b"", method="POST",
                                             headers={"Authorization": "Bearer " + token})
            try:
                with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=15) as response:
                    json.load(response)["ticket"]
            except urllib.error.HTTPError as exc:
                raise ValueError(f"T3 refused the events token (HTTP {exc.code})") from None
            except (OSError, KeyError) as exc:
                raise ValueError(f"no T3 push endpoint: {type(exc).__name__}") from None
            return True
        check(f"host {name} push stream", stream,
              "t3 auth session issue --scope orchestration:read --label boss-events --token-only; set "
              + host.get("events_token_env", "T3_EVENTS_TOKEN"), required=routed)

    def listener():
        # Whoever holds wake.lock is the listener; if doctor can take it, nobody is listening.
        descriptor = os.open(boss.store.home / LOCK, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            raise ValueError("no Hermes process holds wake.lock")
        except BlockingIOError:
            pass
        finally:
            os.close(descriptor)
        stamp, pid, state = (boss.store.home / HEARTBEAT).read_text().split(maxsplit=2)
        return f"pid {pid} since {owner.stamp(stamp)} ({state.strip()})"
    check("wake listener", listener, "start or restart the Hermes gateway (or app); then rerun boss doctor",
          required=routed)
    return lines, 1 if failures else 0


def owner_lines(boss):
    """Who owns boss, whether the last wake reached them, and what to do when nobody does."""
    wake = boss.config.data.get("wake", {})
    enabled = wake.get("enabled", True)
    lines = ["plugin wake: " + ("on" if enabled else "off (wake.enabled = false)")]
    try:
        record = owner.read(boss.store.home)
    except BossError as exc:
        return lines + [f"fail: owner: {exc.message}; fix: {exc.hint}"]
    if record is None:
        if legacy_target(wake):
            return lines + ["warn: no owner; wakes use the legacy [wake] target as a migration fallback; "
                            "fix: run /boss in the session that should get wakes, then boss config unset the target"]
        return lines + ["warn: no owner: run /boss in the session that should get wakes; until then wakes are held on disk"]
    lines.append(f"ok: owner {record['label']} since {owner.stamp(record['claimed_at'])}"
                 f" surface={record['surface'] or 'unknown'}")
    if not wake.get("inject_when_idle", False):
        from .watch import waiting
        return lines + ["ok: boss watch waiting in the owner session" if waiting(boss.store.home) else
                        "warn: no boss watch waiting: the owner sees worker updates only on its next tool call;"
                        " fix: the owner session runs boss watch as a background command"]
    try:
        last = json.loads((boss.store.home / LAST).read_text())
        if last.get("owner") != record["label"]:
            raise ValueError
    except (OSError, ValueError, AttributeError):
        return lines + ["info: owner reachable: unknown until the first wake"]
    when = owner.stamp(last["at"])
    if last.get("result") == "delivered":
        return lines + [f"ok: owner reachable: last wake delivered at {when}"]
    return lines + [f"warn: owner unreachable: last wake refused at {when}; changes are held on disk;"
                    " fix: reopen that session, or run /boss take where you are"]


def rule_findings(boss):
    """Lint worker rules: unknown models or efforts on the live host, roles that break them, empty lists."""
    config, findings, known, seen = boss.config, [], {}, set()
    scopes = [None, *config.projects]
    checked = False
    for scope in scopes:
        rules = model_rules(config.project(scope).get("rules", {}) if scope else config.data.get("rules", {}))
        prefix = f"projects.{scope}.rules.models" if scope else "rules.models"
        host = config.effective(scope)[0]["host"]
        for key, allowed in rules.items():
            checked = True
            if not allowed:
                findings.append(("warn", f"{prefix}.{key} is empty; every {key} spawn needs --allow-model",
                                 f"list allowed models or boss config unset {prefix}.{key}"))
            for entry in allowed:
                model_id, effort = rule_entry(entry)
                instance, model = model_id.split(":", 1)
                if not host:
                    findings.append(("warn", f"{prefix}.{key}: cannot check {entry} without a host", "boss init"))
                    continue
                if (host, model_id) not in known:
                    try:
                        catalog = boss.client(host).call("orchestrator_capabilities", search=model)
                        known[host, model_id] = next((m for p in catalog["providers"] if p["providerInstanceId"] == instance
                                                      for m in p["models"] if m["id"] == model), None)
                    except BossError as exc:
                        known[host, model_id] = exc
                result = known[host, model_id]
                if isinstance(result, BossError):
                    findings.append(("optional", f"{prefix}.{key}: cannot check {entry} on host {host}: {result.message}",
                                     f"boss host test {shlex.quote(host)}"))
                elif result is None:
                    findings.append(("fail", f"{prefix}.{key}: {model_id} is not in host {host}'s live T3 catalog",
                                     "use providerInstanceId:model from orchestrator_capabilities"))
                elif effort and not any(o["id"] in ("reasoningEffort", "effort") and effort in o.get("values", [])
                                        for o in result.get("options", [])):
                    findings.append(("fail", f"{prefix}.{key}: {model_id} has no effort {effort} on host {host}",
                                     "use an effort value from orchestrator_capabilities"))
        for role in config.roles(scope):
            values, origins = config.effective(scope, {"worker": role})
            where = f"role {role}" + (f" in project {scope}" if scope else "")
            kinds = values.get("kinds")
            if kinds is not None:
                checked = True
                if values["kind"] not in kinds and ("kinds", role, values["kind"], tuple(kinds)) not in seen:
                    seen.add(("kinds", role, values["kind"], tuple(kinds)))
                    findings.append(("fail", f"{where}: default kind {values['kind']} is not in kinds {literal(kinds)}",
                                     f"set kind or kinds in {origins[('kinds',)]}"))
            models = values.get("rules", {}).get("models", {})
            for kind in kinds if kinds is not None else [values["kind"]]:
                key = role if role in models else kind
                allowed, model, effort = models.get(key), values.get("model"), values.get("effort")
                if allowed is None or not model or allows(allowed, model, effort) or (role, model, effort, key, tuple(allowed)) in seen:
                    continue
                seen.add((role, model, effort, key, tuple(allowed)))
                asked = " ".join(filter(None, (model, effort)))
                findings.append(("fail", f"{where}: model {asked} breaks rules.models.{key} {literal(allowed)}",
                                 f"change {origins[('model',)]} or {origins[('rules', 'models', key)]}"))
    if checked and not any(level == "fail" for level, _, _ in findings):
        findings.insert(0, ("ok", "worker rules", ""))
    return findings


def install(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description="Install boss links. Hermes config.yaml is never edited.")
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
    return 0


if __name__ == "__main__":
    raise SystemExit(install())
