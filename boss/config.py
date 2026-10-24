"""TOML registry and deliberately narrow KEY=value credential loading."""
import os
from pathlib import Path
import re
import tomllib
from urllib.parse import urlsplit

from .common import BossError


DEFAULT_HOSTS = {
    "ser5ver": {"url": "http://127.0.0.1:3773/mcp", "token_env": "T3_SER5VER_MCP_TOKEN"},
    "oracle": {"url": "http://127.0.0.1:13773/mcp", "token_env": "T3_ORACLE_MCP_TOKEN", "ssh": "oracle"},
    "wsl": {"url": "http://127.0.0.1:23773/mcp", "token_env": "T3_WSL_MCP_TOKEN", "ssh": "wsl"},
}


class Config:
    def __init__(self, home, redact):
        self.path = home / "config.toml"
        self.redact = redact
        self.credentials = self._credentials()
        try:
            data = tomllib.loads(self.path.read_text()) if self.path.exists() else {}
        except (OSError, ValueError) as exc:
            raise BossError("config", f"cannot read {self.path}: {exc}", exit_code=1) from None
        try:
            self.hosts = {k: dict(v) for k, v in DEFAULT_HOSTS.items()}
            for name, host in data.get("hosts", {}).items():
                self.hosts[name] = {**self.hosts.get(name, {}), **host}
            self.projects = data.get("projects", {})
            if not isinstance(self.projects, dict):
                raise ValueError("projects must be a table")
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
                    # Register secrets before any output, including config errors.
                    if any(word in key for word in ("TOKEN", "SECRET", "PASSWORD", "API_KEY")):
                        self.redact.add(value)
        for key, value in os.environ.items():
            if any(word in key for word in ("TOKEN", "SECRET", "PASSWORD", "API_KEY")):
                self.redact.add(value)
        values.update(os.environ)
        return values

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
        project = self.projects.get(name)
        if not isinstance(project, dict) or not project.get("project_id") or not project.get("host"):
            raise BossError("usage", f"project {name} needs host and project_id", f"edit {self.path}", 1)
        if project.get("delivery", "direct-PR") not in ("direct-PR", "local-only"):
            raise BossError("config", f"invalid delivery for {name}", f"edit {self.path}", 1)
        selection = project.get("model_selection")
        if selection is not None and (not isinstance(selection, dict) or not isinstance(selection.get("model"), str)):
            raise BossError("config", f"invalid model_selection for {name}", f"edit {self.path}", 1)
        return project
