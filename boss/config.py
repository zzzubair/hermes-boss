"""Strict layered TOML configuration and narrow KEY=value credential loading."""
import copy
import json
import os
from pathlib import Path
import re
import sys
import tomllib
from urllib.parse import urlsplit

from .common import BossError, refuse
from .preferences import project_name
from .state import atomic_write


DEFAULTS = {"default_host": "", "delivery": "direct-PR", "merge": "approved",
            "no_mistakes": False, "kind": "ship"}
SETTINGS = {"host": str, "worker": str, "model": str, "effort": str, "kind": str,
            "delivery": str, "merge": str, "auto_merge": bool, "test_host": str,
            "no_mistakes": bool, "model_selection": dict}
# Allow-lists of instance:model per task kind. Unset means no restriction.
RULES = {"ship_models": list, "scout_models": list}
ROLE = {**{k: SETTINGS[k] for k in ("host", "model", "effort", "kind")}, "kinds": list}


def boss_home():
    # The ledger deliberately stays at its original location.
    return Path(os.environ.get("BOSS_HOME", "~/.boss")).expanduser()


def hermes_home():
    return Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))).expanduser()


def config_path(home):
    if "BOSS_HOME" in os.environ:
        return home / "config.toml"
    modern = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))).expanduser() / "boss/config.toml"
    legacy = home / "config.toml"
    return modern if modern.exists() or not legacy.exists() else legacy


def literal(value):
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{json.dumps(k, ensure_ascii=False)} = {literal(v)}" for k, v in value.items()) + " }"
    if isinstance(value, list):
        return "[" + ", ".join(literal(v) for v in value) + "]"
    return json.dumps(value, ensure_ascii=False)


def dumps(data):
    """Small TOML writer. Dotted names are always quoted, including project titles."""
    lines = []

    def table(data, path=()):
        for key, value in data.items():
            if not isinstance(value, dict):
                lines.append(f"{json.dumps(key, ensure_ascii=False)} = {literal(value)}")
        for key, value in data.items():
            if isinstance(value, dict):
                lines.append("\n[" + ".".join(json.dumps(k, ensure_ascii=False) for k in (*path, key)) + "]")
                table(value, (*path, key))
    table(data)
    return "\n".join(lines) + "\n"


def key_parts(key):
    try:
        node = tomllib.loads(key + " = 0")
        parts = []
        while isinstance(node, dict) and len(node) == 1:
            part, node = next(iter(node.items()))
            parts.append(part)
        if node != 0 or not parts:
            raise ValueError()
        return parts
    except ValueError:
        raise BossError("config", f"invalid key path {key}", "use dotted keys, e.g. projects.demo.merge", 1) from None


def flatten(data, path=()):
    for key, value in data.items():
        parts = (*path, key)
        if isinstance(value, dict) and value:
            yield from flatten(value, parts)
        else:
            yield parts, value


def display_key(parts):
    return ".".join(k if re.fullmatch(r"[A-Za-z0-9_-]+", k) else json.dumps(k, ensure_ascii=False) for k in parts)


class Config:
    def __init__(self, home, redact, *, hermes=None):
        self.home = Path(home)
        self.path = config_path(self.home)
        self.redact = redact
        self.hermes = Path(hermes) if hermes is not None else hermes_home()
        self.credentials, self.file_credentials = self._credentials()
        try:
            self.data = tomllib.loads(self.path.read_text()) if self.path.exists() else {}
        except (OSError, ValueError) as exc:
            raise BossError("config", f"cannot read {self.path}: {exc}", exit_code=1) from None
        self.validate(self.data)
        self._load()
        for path, value in flatten(self.data):
            if path[-1] == "auto_merge":
                print(self.redact(f"warning: {display_key(path)} is deprecated; use merge = {literal('auto' if value else 'approved')}"), file=sys.stderr)

    def _load(self):
        self.hosts = self.data.get("hosts", {})
        self.projects = self.data.get("projects", {})
        self.workers = self.data.get("workers", {})
        self.default_host = self.data.get("default_host", "")

    def fail(self, key, message):
        raise BossError("config", f"{key}: {message}", f"edit {self.path}", 1)

    def validate(self, data):
        def fields(table, schema, prefix="", *, legacy_selection=False):
            if not isinstance(table, dict):
                self.fail(prefix, "must be a table")
            for key, value in table.items():
                path = prefix + "." + key if prefix else key
                if key == "extra_rules":
                    raise BossError("config", f"{path}: extra_rules was removed",
                                    f"move codebase instructions into the repo's AGENTS.md and remove {path} from {self.path}", 1)
                if key not in schema:
                    self.fail(path, "unknown key")
                expected = schema[key]
                if type(value) is not expected:
                    self.fail(path, f"must be {expected.__name__}")
                if expected is str and not value.strip() and key not in ("default_host", "ssh"):
                    self.fail(path, "must be a nonempty string")
                choices = {"delivery": ("direct-PR", "local-only"), "merge": ("never", "approved", "auto"),
                           "kind": ("ship", "scout")}
                if key in choices and value not in choices[key]:
                    self.fail(path, "must be one of " + ", ".join(choices[key]))
                if key == "model" and not legacy_selection and (":" not in value or any(not p for p in value.split(":", 1))):
                    self.fail(path, "must be instance:model")
                if key == "kinds" and any(item not in choices["kind"] for item in value):
                    self.fail(path, "must list only " + ", ".join(choices["kind"]))
                if key in RULES and any(type(item) is not str or ":" not in item or any(not p for p in item.split(":", 1))
                                        for item in value):
                    self.fail(path, "must list instance:model strings")
                if key == "model_selection":
                    # Legacy explicit model selections remain readable.
                    fields(value, {"model": str, "instanceId": str, "options": dict}, path, legacy_selection=True)
                    if not value.get("model"):
                        self.fail(path + ".model", "required")
                    for option, setting in value.get("options", {}).items():
                        if type(setting) not in (str, bool):
                            self.fail(path + ".options.'" + option + "'", "must be str or bool")
            if "merge" in table and "auto_merge" in table:
                self.fail(prefix or "config", "use merge or deprecated auto_merge, not both")

        # Register explicit token names even when unrelated validation fails.
        for host in data.get("hosts", {}).values() if isinstance(data.get("hosts", {}), dict) else ():
            if isinstance(host, dict) and isinstance(host.get("token_env", "T3_MCP_TOKEN"), str):
                key = host.get("token_env", "T3_MCP_TOKEN")
                self.redact.add(self.file_credentials.get(key))
                self.redact.add(self.credentials.get(key))
        fields(data, {"default_host": str, "default_worker": str, "hosts": dict, "projects": dict, "workers": dict, "wake": dict,
                      "rules": dict, **{k: v for k, v in SETTINGS.items() if k != "worker"}})
        fields(data.get("rules", {}), RULES, "rules")
        wake = data.get("wake", {})
        fields(wake, {"enabled": bool, "interval_seconds": int, "origin": dict, "session_key": str}, "wake")
        if wake.get("interval_seconds", 300) <= 0:
            self.fail("wake.interval_seconds", "must be a positive integer")
        if "origin" in wake and "session_key" in wake:
            self.fail("wake", "use origin or session_key, not both")
        if "origin" in wake:
            strings = ("platform", "chat_id", "chat_type", "thread_id", "user_id", "user_name", "chat_name",
                       "chat_topic", "user_id_alt", "chat_id_alt", "scope_id", "guild_id", "parent_chat_id",
                       "message_id", "profile", "auto_thread_initial_name", "prospective_thread_id")
            fields(wake["origin"], {**dict.fromkeys(strings, str), "auto_thread_created": bool}, "wake.origin")
            for key in ("platform", "chat_id"):
                if key not in wake["origin"]:
                    self.fail("wake.origin." + key, "required")
        for name, host in data.get("hosts", {}).items():
            prefix = "hosts." + name
            fields(host, {"url": str, "token_env": str, "ssh": str}, prefix)
            if "url" not in host:
                self.fail(prefix + ".url", "required")
            try:
                url = urlsplit(host["url"])
                port = url.port
                if url.username or url.password or url.query or url.fragment:
                    self.fail(prefix + ".url", "credentials/query/fragment are forbidden in URLs")
                if url.scheme not in ("http", "https") or not url.hostname or port == 0:
                    self.fail(prefix + ".url", "invalid MCP URL")
                if url.scheme == "http" and url.hostname not in ("localhost", "127.0.0.1", "::1"):
                    self.fail(prefix + ".url", "use HTTPS or a loopback SSH tunnel")
            except ValueError:
                self.fail(prefix + ".url", "invalid MCP URL")
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", host.get("token_env", "T3_MCP_TOKEN")):
                self.fail(prefix + ".token_env", "invalid environment variable")
            ssh = host.get("ssh", "")
            if ssh and (not re.fullmatch(r"[A-Za-z0-9_@.:-]+", ssh) or ssh.startswith("-")):
                self.fail(prefix + ".ssh", "invalid ssh destination")
        for name, worker in data.get("workers", {}).items():
            fields(worker, ROLE, "workers." + name)
        for name, project in data.get("projects", {}).items():
            project_name(name)
            fields(project, {**SETTINGS, "project_id": str, "repository": str, "workers": dict, "rules": dict}, "projects." + name)
            fields(project.get("rules", {}), RULES, f"projects.{name}.rules")
        hosts, workers = data.get("hosts", {}), data.get("workers", {})
        for name, project in data.get("projects", {}).items():
            for role, settings in project.get("workers", {}).items():
                fields(settings, ROLE, f"projects.{name}.workers.{role}")
        tables = [("", data), *(("workers." + k, v) for k, v in workers.items())]
        for name, project in data.get("projects", {}).items():
            tables.append(("projects." + name, project))
            tables.extend((f"projects.{name}.workers.{role}", settings) for role, settings in project.get("workers", {}).items())
        for prefix, table in tables:
            for key in ("host", "default_host"):
                if table.get(key) and table[key] not in hosts:
                    self.fail((prefix + "." if prefix else "") + key, "must name a configured host")
        if data.get("default_worker") and data["default_worker"] not in workers:
            self.fail("default_worker", "unknown role; available: " + (", ".join(sorted(workers)) or "none"))

    def _credentials(self):
        values = {}
        path = self.hermes / ".env"
        if path.exists():
            for line in path.read_text().splitlines():
                key, sep, value = line.partition("=")
                key = key.strip()
                if sep and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                    value = value.strip()
                    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                        value = value[1:-1]
                    values[key] = value
        self.redact.discover(values)
        self.redact.discover(os.environ)
        return {**values, **os.environ}, values

    def host(self, name):
        if not self.hosts:
            raise BossError("config", "no hosts configured", "run boss init", 1)
        if not name or name not in self.hosts:
            raise BossError("config", f"unknown or unset host {name!r}", "boss host list; boss config set default_host <name>", 1)
        host = self.hosts[name]
        key = host.get("token_env", "T3_MCP_TOKEN")
        token = self.credentials.get(key)
        if not token:
            raise BossError("auth", f"missing {key}", f"set it in env or {self.hermes / '.env'}")
        self.redact.add(token)
        if any(c.isspace() for c in token):
            raise BossError("auth", f"invalid whitespace in {key}", "replace it with a valid MCP token")
        return host, token

    def project(self, name):
        project_name(name)
        return self.projects.get(name, {})

    @staticmethod
    def normalized(settings):
        settings = dict(settings)
        if "auto_merge" in settings:
            settings["merge"] = "auto" if settings.pop("auto_merge") else "approved"
        return settings

    def roles(self, name=None):
        return sorted(set(self.workers) | set(self.project(name).get("workers", {}) if name else {}))

    def effective(self, name=None, overrides=None):
        values, origins = {}, {}

        def apply(layer, origin):
            for key in layer:
                for old in list(origins):
                    if old[0] == key:
                        del origins[old]
            for key, value in flatten(layer):
                origins[key] = origin
            values.update(copy.deepcopy(layer))
        overrides = {k: v for k, v in (overrides or {}).items() if v is not None}
        apply(DEFAULTS, "built-in")
        apply(self.normalized({k: v for k, v in self.data.items() if k not in ("hosts", "projects", "workers", "wake", "rules")}), str(self.path))
        project = self.normalized({k: v for k, v in (self.project(name) if name else {}).items() if k not in ("workers", "rules")})
        apply(project, f"{self.path} [projects.{name}]")
        roles = self.roles(name)
        role = overrides.get("worker")
        role_origin = "CLI"
        if not role and overrides.get("kind") == "scout" and "scout" in roles:
            role, role_origin = "scout", "CLI --kind scout"
        if not role:
            role = project.get("worker") or self.data.get("default_worker")
            role_origin = f"{self.path} [projects.{name}]" if project.get("worker") else str(self.path)
        if role:
            if role not in roles:
                self.fail("worker", f"unknown role {role}; available for {name or 'global'}: " + (", ".join(roles) or "none"))
            apply(self.workers.get(role, {}), f"{self.path} [workers.{role}]")
            if name:
                apply(self.project(name).get("workers", {}).get(role, {}), f"{self.path} [projects.{name}.workers.{role}]")
            apply({"worker": role}, role_origin)
        apply(overrides, "CLI")
        # Rules merge per key: a project list replaces only that kind's global list.
        rules = {}
        for layer, origin in ((self.data.get("rules", {}), f"{self.path} [rules]"),
                              (self.project(name).get("rules", {}) if name else {}, f"{self.path} [projects.{name}.rules]")):
            for key, allowed in layer.items():
                rules[key] = list(allowed)
                origins[("rules", key)] = origin
        if rules:
            values["rules"] = rules
        if not values.get("host"):
            values["host"] = values["default_host"]
            origins[("host",)] = origins[("default_host",)]
        return values, origins

    def violations(self, values, origins, model):
        """Deterministic captain rules for one spawn; model is instance:model, or None for T3's own default."""
        found = []
        kind, kinds = values["kind"], values.get("kinds")
        if kinds is not None and kind not in kinds:
            found.append(f"role {values['worker']} may run only {', '.join(kinds) or 'no'} tasks, not {kind} "
                         f"(kinds from {origins[('kinds',)]})")
        key = kind + "_models"
        allowed = values.get("rules", {}).get(key)
        if allowed is not None and model not in allowed:
            found.append(f"{'model ' + model if model else 'T3 default model'} is not allowed for {kind} tasks; "
                         f"rules.{key} = {literal(allowed)} (from {origins[('rules', key)]})")
        return found

    def write(self, data):
        self.validate(data)
        atomic_write(self.path, dumps(data))
        self.data = data
        self._load()

    def change(self, key, value=None, *, unset=False):
        parts = key_parts(key)
        updated = copy.deepcopy(self.data)
        node = updated
        for part in parts[:-1]:
            if part not in node:
                if unset:
                    self.fail(key, "key is not set")
                node[part] = {}
            node = node[part]
            if not isinstance(node, dict):
                self.fail(key, "parent must be a table")
        if unset:
            if parts[-1] not in node:
                self.fail(key, "key is not set")
            del node[parts[-1]]
        else:
            node[parts[-1]] = value
        self.write(updated)

    def add_project(self, name, settings):
        project_name(name)
        original = self.path.read_bytes().decode("utf-8") if self.path.exists() else ""
        current = tomllib.loads(original)
        if name in current.get("projects", {}):
            refuse(f"project {name} already has a registry entry", f"boss project show {name}")
        candidate = copy.deepcopy(current)
        candidate.setdefault("projects", {})[name] = settings
        self.validate(candidate)
        lines = [f"[projects.{json.dumps(name, ensure_ascii=False)}]"]
        lines.extend(f"{key} = {literal(value)}" for key, value in settings.items())
        updated = original + ("\n" if original and not original.endswith("\n") else "") + "\n" + "\n".join(lines) + "\n"
        atomic_write(self.path, updated)
        self.data = candidate
        self._load()
