"""Strict layered TOML configuration and narrow KEY=value credential loading."""
import copy
import json
import os
from pathlib import Path
import re
import shlex
import sys
import tomllib
from urllib.parse import urlsplit

from . import deploy
from .common import BossError, refuse
from .preferences import project_name
from .state import atomic_write


# Words a PR body may not use outside code: the PR speaks as the owner, about the change.
PR_BODY_FORBIDDEN = ["captain", "firstmate", "first mate", "chat"]
# no-mistakes is direct-PR validated through the no-mistakes pipeline first; no_mistakes = true is its old spelling.
DELIVERIES = ("direct-PR", "no-mistakes", "local-only")
DEFAULTS = {"default_host": "", "delivery": "direct-PR", "merge": "approved",
            "no_mistakes": False, "fast_land_docs": False, "kind": "ship", "pr_body_forbidden": PR_BODY_FORBIDDEN}
SETTINGS = {"host": str, "worker": str, "model": str, "effort": str, "kind": str,
            "delivery": str, "merge": str, "auto_merge": bool, "test_host": str,
            "no_mistakes": bool, "fast_land_docs": bool, "model_selection": dict, "pr_body_forbidden": list}
# [workers] holds role tables plus this one fleet-wide number.
MAX_ACTIVE = 15
# [rules.models]: allow-lists of "instance:model" or "instance:model effort", keyed by role or task kind.
# Unset means no restriction. ship_models/scout_models are the deprecated spelling of models.ship/scout.
RULES = {"models": dict, "ship_models": list, "scout_models": list}
ROLE = {**{k: SETTINGS[k] for k in ("host", "model", "effort", "kind")}, "kinds": list}
# [projects.<name>.deploy]: how the supervisor deploys on explicit approval (boss deploy).
DEPLOY = {"host": str, "command": str, "check": str, "rollback": str, "check_minutes": int, "no_touch": list,
          "auto_rollback": bool}


def rule_entry(entry):
    """'codex:gpt-5 xhigh' -> ('codex:gpt-5', 'xhigh'); 'codex:gpt-5' -> ('codex:gpt-5', None); malformed -> None."""
    parts = entry.split(" ") if isinstance(entry, str) else []
    if not 0 < len(parts) <= 2 or not all(parts) or ":" not in parts[0] or not all(parts[0].split(":", 1)):
        return None
    return parts[0], parts[1] if len(parts) == 2 else None


def model_rules(rules):
    """One rules table as {role or kind: allowed}; models.<key> wins over the deprecated <kind>_models."""
    return {**{k[:-len("_models")]: v for k, v in rules.items() if k.endswith("_models")}, **rules.get("models", {})}


def allows(allowed, model, effort):
    """An entry without an effort allows the model at any effort."""
    return any(m == model and e in (None, effort) for m, e in map(rule_entry, allowed))


def boss_home():
    # The ledger deliberately stays at its original location.
    return Path(os.environ.get("BOSS_HOME", "~/.boss")).expanduser()


def roles_table(data):
    """[workers] minus max_active: only the role tables."""
    return {k: v for k, v in data.get("workers", {}).items() if k != "max_active"}


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
            if path[-1] in ("ship_models", "scout_models") and path[-2] == "rules":
                print(self.redact(f"warning: {display_key(path)} is deprecated; use "
                                  f"{display_key((*path[:-1], 'models', path[-1][:-len('_models')]))}"), file=sys.stderr)

    def _load(self):
        self.hosts = self.data.get("hosts", {})
        self.projects = self.data.get("projects", {})
        self.workers = roles_table(self.data)
        self.max_active = self.data.get("workers", {}).get("max_active", MAX_ACTIVE)
        self.default_host = self.data.get("default_host", "")
        # The person boss works for, by name in briefs and supervisor text; unset means "you" and plain wording.
        self.owner_name = self.data.get("owner_name") or None

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
                choices = {"delivery": DELIVERIES, "merge": ("never", "approved", "auto"),
                           "kind": ("ship", "scout")}
                if key in choices and value not in choices[key]:
                    self.fail(path, "must be one of " + ", ".join(choices[key]))
                if key == "model" and not legacy_selection and (":" not in value or any(not p for p in value.split(":", 1))):
                    self.fail(path, "must be instance:model")
                if key == "kinds" and any(item not in choices["kind"] for item in value):
                    self.fail(path, "must list only " + ", ".join(choices["kind"]))
                if key == "owner_name" and (len(value) > 60 or value != " ".join(value.split())):
                    self.fail(path, "must be one line of at most 60 characters, e.g. a first name")
                if key == "pr_body_forbidden" and any(type(item) is not str or not item.strip() for item in value):
                    self.fail(path, "must list nonempty strings")
                if key in RULES and expected is list and any(rule_entry(item) is None for item in value):
                    self.fail(path, 'must list "instance:model" or "instance:model effort" strings')
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
            if (table.get("no_mistakes"), table.get("delivery")) in ((True, "local-only"), (False, "no-mistakes")):
                self.fail(prefix or "config", f"no_mistakes = {str(table['no_mistakes']).lower()} contradicts "
                          f"delivery = \"{table['delivery']}\"; keep only delivery")

        # Register explicit token names even when unrelated validation fails.
        for host in data.get("hosts", {}).values() if isinstance(data.get("hosts", {}), dict) else ():
            for name, default in (("token_env", "T3_MCP_TOKEN"), ("events_token_env", "T3_EVENTS_TOKEN")):
                if isinstance(host, dict) and isinstance(host.get(name, default), str):
                    key = host.get(name, default)
                    self.redact.add(self.file_credentials.get(key))
                    self.redact.add(self.credentials.get(key))
        fields(data, {"default_host": str, "default_worker": str, "owner_name": str, "hosts": dict, "projects": dict, "workers": dict, "wake": dict,
                      "rules": dict, **{k: v for k, v in SETTINGS.items() if k != "worker"}})
        roles = set(roles_table(data)) | {r for p in data.get("projects", {}).values() if isinstance(p, dict)
                                          for r in p.get("workers", {}) if isinstance(p.get("workers"), dict)}

        def rules(table, prefix):
            fields(table, RULES, prefix)
            models = table.get("models", {})
            for key in models:
                if key not in ("ship", "scout") and key not in roles:
                    self.fail(f"{prefix}.models.{key}", "must name a task kind (ship, scout) or a configured role")
            fields(models, dict.fromkeys(models, list), prefix + ".models")
            for key, value in models.items():
                if any(rule_entry(item) is None for item in value):
                    self.fail(f"{prefix}.models.{key}", 'must list "instance:model" or "instance:model effort" strings')
        rules(data.get("rules", {}), "rules")
        wake = data.get("wake", {})
        # interval_seconds is a leftover from timed polling; tolerated so old configs load, never read.
        fields(wake, {"enabled": bool, "host_down_minutes": int, "inject_when_idle": bool, "interval_seconds": int, "origin": dict, "session_key": str, "stall_minutes": int}, "wake")
        for key in ("stall_minutes", "host_down_minutes"):
            if wake.get(key, 0) < 0:
                self.fail("wake." + key, "must be 0 (off) or more")
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
            fields(host, {"url": str, "token_env": str, "events_token_env": str, "ssh": str}, prefix)
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
            for key, default in (("token_env", "T3_MCP_TOKEN"), ("events_token_env", "T3_EVENTS_TOKEN")):
                if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", host.get(key, default)):
                    self.fail(prefix + "." + key, "invalid environment variable")
            ssh = host.get("ssh", "")
            if ssh and (not re.fullmatch(r"[A-Za-z0-9_@.:-]+", ssh) or ssh.startswith("-")):
                self.fail(prefix + ".ssh", "invalid ssh destination")
        cap = data.get("workers", {}).get("max_active", MAX_ACTIVE)
        if type(cap) is not int or cap < 1:
            self.fail("workers.max_active", "must be a positive integer")
        for name, worker in roles_table(data).items():
            fields(worker, ROLE, "workers." + name)
        for name, project in data.get("projects", {}).items():
            project_name(name)
            fields(project, {**SETTINGS, "project_id": str, "repository": str, "workers": dict, "rules": dict, "deploy": dict},
                   "projects." + name)
            rules(project.get("rules", {}), f"projects.{name}.rules")
            if "deploy" in project:
                fields(project["deploy"], DEPLOY, f"projects.{name}.deploy")
                deploy.validate(project["deploy"], self.fail, f"projects.{name}.deploy")
        hosts, workers = data.get("hosts", {}), roles_table(data)
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

    def events_token(self, name):
        """Read-only T3 session token for the push stream; T3 keeps it apart from MCP client tokens."""
        host = self.hosts[name]
        key = host.get("events_token_env", "T3_EVENTS_TOKEN")
        token = self.credentials.get(key)
        if not token or any(c.isspace() for c in token):
            raise BossError("auth", f"missing or invalid {key}",
                            f"t3 auth session issue --scope orchestration:read --label boss-events --token-only; "
                            f"put it in {key} in env or {self.hermes / '.env'}")
        self.redact.add(token)
        return token

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
            # The no_mistakes alias: true means delivery "no-mistakes"; false undoes an inherited one.
            if layer.get("no_mistakes") and layer.get("delivery", "direct-PR") == "direct-PR":
                layer = {**layer, "delivery": "no-mistakes"}
            elif layer.get("no_mistakes") is False and "delivery" not in layer and values.get("delivery") == "no-mistakes":
                layer = {**layer, "delivery": "direct-PR"}
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
        project = self.normalized({k: v for k, v in (self.project(name) if name else {}).items() if k not in ("workers", "rules", "deploy")})
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
        # Rules merge per key: a project list replaces only that role's or kind's global list.
        rules = {}
        for layer, origin in ((self.data.get("rules", {}), f"{self.path} [rules]"),
                              (self.project(name).get("rules", {}) if name else {}, f"{self.path} [projects.{name}.rules]")):
            for key, allowed in model_rules(layer).items():
                rules[key] = list(allowed)
                origins[("rules", "models", key)] = origin
        if rules:
            values["rules"] = {"models": rules}
        values["no_mistakes"] = values["delivery"] == "no-mistakes"
        origins[("no_mistakes",)] = origins[("delivery",)]
        if not values.get("host"):
            values["host"] = values["default_host"]
            origins[("host",)] = origins[("default_host",)]
        return values, origins

    def violations(self, name, values, origins, model, effort):
        """Deterministic worker rules for one spawn; model is instance:model, or None for T3's own default.

        Each broken rule comes with a spawn retry that passes it and the one config change that
        allows this spawn from now on (None when there is none)."""
        def change(path, value):
            return f"boss config set {display_key(path)} {shlex.quote(literal(value))}"
        found = []
        role, kind, kinds = values.get("worker"), values["kind"], values.get("kinds")
        if kinds is not None and kind not in kinds:
            local = name and "kinds" in self.project(name).get("workers", {}).get(role, {})
            found.append((f"role {role} may run only {', '.join(kinds) or 'no'} tasks, not {kind} (kinds from {origins[('kinds',)]})",
                          f"--kind {kinds[0]} or another --worker" if kinds else "another --worker",
                          change(("projects", name, "workers", role, "kinds") if local else ("workers", role, "kinds"), kinds + [kind])))
        models = values.get("rules", {}).get("models", {})
        # A role's list replaces its kind's list.
        key = role if role in models else kind
        allowed = models.get(key)
        if allowed is not None and not (model and allows(allowed, model, effort)):
            asked = " ".join(filter(None, (model, effort)))
            local = name and key in model_rules(self.project(name).get("rules", {}))
            path = ("projects", name, "rules", "models", key) if local else ("rules", "models", key)
            found.append((f"{'model ' + asked if model else 'T3 default model'} is not allowed for "
                          f"{'role ' + key if key == role else key + ' tasks'}; "
                          f"{display_key(('rules', 'models', key))} = {literal(allowed)} (from {origins[('rules', 'models', key)]})",
                          "--model " + allowed[0].replace(" ", " --effort ") if allowed else None,
                          change(path, allowed + [asked]) if model else None))
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
