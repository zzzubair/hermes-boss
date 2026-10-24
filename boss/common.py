"""Boundary errors, secret filtering and compact output."""
import json
import re


class BossError(Exception):
    def __init__(self, code, message, hint="boss --help", exit_code=3):
        super().__init__(message)
        self.code, self.message, self.hint, self.exit_code = code, message, hint, exit_code


def refuse(message, hint):
    raise BossError("refused", message, hint, 2)


class Redactor:
    def __init__(self):
        self.secrets = set()

    def add(self, secret):
        if secret:
            self.secrets.add(secret)

    def __call__(self, value):
        text = str(value)
        for secret in sorted(self.secrets, key=len, reverse=True):
            text = text.replace(secret, "[REDACTED]")
        return re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [REDACTED]", text)

    def data(self, value):
        if isinstance(value, str):
            return self(value)
        if isinstance(value, dict):
            return {self(k): self.data(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.data(v) for v in value]
        return value


def compact(value, limit=240):
    original = str(value)
    text = " ".join(original.split())
    if limit is not None and len(text) > limit:
        text = text[:limit] + f"... [{len(original)} chars; --full]"
    # Quote records that could inject another output line or field.
    return json.dumps(text, ensure_ascii=False) if any(c in text for c in ',"\\') else text


def require_text(value, label, limit=120000):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise BossError("usage", f"{label} must contain 1..{limit} characters", exit_code=1)
    return value
