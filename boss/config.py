"""TOML registry and deliberately narrow KEY=value credential loading."""
import json
import os
from pathlib import Path
import re
import tomllib
from urllib.parse import urlsplit

from .common import BossError, refuse
from .preferences import project_name
from .state import atomic_write


DEFAULT_HOSTS = {
    "ser5ver": {"url": "http://127.0.0.1:3773/mcp", "token_env": "T3_SER5VER_MCP_TOKEN"},
    "oracle": {"url": "http://127.0.0.1:13773/mcp", "token_env": "T3_ORACLE_MCP_TOKEN", "ssh": "oracle"},
    "wsl": {"url": "http://127.0.0.1:23773/mcp", "token_env": "T3_WSL_MCP_TOKEN", "ssh": "wsl"},
}


class Config:
    def __init__(self, home, redact):
        self.path = home / "config.toml"
        self.redact = redact
        self.credentials, file_credentials = self._credentials()
        try:
            data = tomllib.loads(self.path.read_text()) if self.path.exists() else {}
        except (OSError, ValueError) as exc:
            raise BossError("config", f"cannot read {self.path}: {exc}", exit_code=1) from None
        try:
            self.default_host = data.get("default_host", "oracle")
            self.hosts = {k: dict(v) for k, v in DEFAULT_HOSTS.items()}
            for name, host in data.get("hosts", {}).items():
                self.hosts[name] = {**self.hosts.get(name, {}), **host}
            self.projects = data.get("projects", {})
            if not isinstance(self.projects, dict):
                raise ValueError("projects must be a table")
            if not isinstance(self.default_host, str) or self.default_host not in self.hosts:
                raise ValueError("default_host must name a configured host")
            for name, host in self.hosts.items():
                url = urlsplit(host["url"])
                if url.username or url.password or url.query or url.fragment:
                    raise ValueError(f"host {name}: credentials/query/fragment are forbidden in URLs")
                if url.scheme not in ("http", "https") or not url.hostname:
                    raise ValueError(f"host {name}: invalid MCP URL")
                if url.scheme == "http" and url.hostname not in ("localhost", "127.0.0.1", "::1"):
                    raise ValueError(f"host {name}: use HTTPS or a loopback SSH tunnel")
                if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", host["token_env"]):
                    raise ValueError(f"host {name}: invalid token_env")
                if host.get("ssh") and not re.fullmatch(r"[A-Za-z0-9_@.:-]+", host["ssh"]):
                    raise ValueError(f"host {name}: invalid ssh destination")
                if host.get("ssh", "").startswith("-"):
                    raise ValueError(f"host {name}: invalid ssh destination")
                self.redact.add(file_credentials.get(host["token_env"]))
                self.redact.add(self.credentials.get(host["token_env"]))
        except (KeyError, TypeError, AttributeError, ValueError) as exc:
            raise BossError("config", str(exc), f"edit {self.path}", 1) from None

    def _credentials(self):
        values = {}
        path = Path.home() / ".hermes/.env"
        if path.exists():
            for line in path.read_text().splitlines():
                key, sep, value = line.partition("=")
                key = key.strip()
                if sep and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                    value = value.strip()
                    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                        value = value[1:-1]
                    values[key] = value
        # Register candidates before any output, including config errors.
        self.redact.discover(values)
        self.redact.discover(os.environ)
        return {**values, **os.environ}, values

    def host(self, name):
        if name not in self.hosts:
            raise BossError("usage", f"unknown host {name}", f"edit {self.path}", 1)
        host = self.hosts[name]
        token = self.credentials.get(host["token_env"])
        if not token:
            raise BossError("auth", f"missing {host['token_env']}", "set it in env or ~/.hermes/.env")
        self.redact.add(token)
        if any(c.isspace() for c in token):
            raise BossError("auth", f"invalid whitespace in {host['token_env']}", "replace it with a valid MCP token")
        return host, token

    def project(self, name):
        project_name(name)
        project = self.projects.get(name, {})
        if not isinstance(project, dict):
            raise BossError("config", f"project {name} must be a table", f"edit {self.path}", 1)
        if project.get("delivery", "direct-PR") not in ("direct-PR", "local-only"):
            raise BossError("config", f"invalid delivery for {name}", f"edit {self.path}", 1)
        for key in ("host", "project_id", "repository", "test_host"):
            if key in project and (not isinstance(project[key], str) or not project[key].strip()):
                raise BossError("config", f"{name}.{key} must be a nonempty string", f"edit {self.path}", 1)
        for key in ("auto_merge", "no_mistakes"):
            if key in project and not isinstance(project[key], bool):
                raise BossError("config", f"{name}.{key} must be a boolean", f"edit {self.path}", 1)
        selection = project.get("model_selection")
        if selection is not None and (not isinstance(selection, dict) or not isinstance(selection.get("model"), str)):
            raise BossError("config", f"invalid model_selection for {name}", f"edit {self.path}", 1)
        return project

    def add_project(self, name, settings):
        project_name(name)
        # Read again immediately before writing so comments and edits made
        # during the lookup remain intact. The CLI holds the ledger writer lock.
        original = self.path.read_bytes().decode("utf-8") if self.path.exists() else ""
        try:
            current = tomllib.loads(original)
            if name in current.get("projects", {}):
                refuse(f"project {name} already has a registry entry", f"boss project show {name}")
            lines = [f"[projects.{json.dumps(name, ensure_ascii=False)}]"]
            for key, value in settings.items():
                literal = str(value).lower() if isinstance(value, bool) else json.dumps(value, ensure_ascii=False)
                lines.append(f"{key} = {literal}")
            updated = original + ("\n" if original and not original.endswith("\n") else "") + "\n" + "\n".join(lines) + "\n"
            tomllib.loads(updated)
        except (ValueError, TypeError) as exc:
            raise BossError("config", f"cannot append project block: {exc}", f"edit {self.path}", 1) from None
        atomic_write(self.path, updated)
        self.projects[name] = dict(settings)
