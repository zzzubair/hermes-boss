"""Gateway-owned polling. No Hermes imports, model calls, or external dependencies."""
from datetime import datetime, timezone
import os
from pathlib import Path
import subprocess
import sys
import threading

from .common import Redactor
from .config import Config, boss_home


CONSENT_KEY = "plugins.entries.boss.allow_gateway_injection"
CONSENT_LINE = CONSENT_KEY + ": true"
POLL_TIMEOUT = 30
HEARTBEAT = "wake-heartbeat"
_lock = threading.Lock()
_loops = {}


def target_configured(wake):
    return bool(wake.get("origin") or wake.get("session_key"))


def log_error(home, redact, message):
    """Redact before shortening; never let an unwritable log break Hermes."""
    try:
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
        text = " ".join(redact(message).split())[:2000]
        descriptor = os.open(home / "poll-errors.log", os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(descriptor, "a") as stream:
            stream.write(datetime.now(timezone.utc).isoformat() + " " + text + "\n")
    except (OSError, UnicodeError):
        pass


class WakeLoop:
    def __init__(self, ctx, config, home):
        self.ctx, self.config, self.home = ctx, config, home
        self.wake = config.data["wake"]
        self.env = dict(os.environ, HERMES_HOME=str(config.hermes))
        self.argv = [sys.executable, str(Path(__file__).resolve().parent.parent / "bin/boss"), "poll"]
        self.stop_event = threading.Event()
        self.refusal_logged = False
        self.thread = threading.Thread(target=self.run, name="boss-wake", daemon=True)

    def beat(self, state):
        """Liveness for boss doctor: a silent dead loop is the failure that matters."""
        try:
            path = self.home / HEARTBEAT
            path.write_text(f"{datetime.now(timezone.utc).isoformat()} {os.getpid()} {state}\n")
        except OSError:
            pass

    def tick(self):
        # Discovery precedes host publication. Do not consume changed-only state until
        # Hermes has an injector, and never fall through to the interactive CLI queue.
        manager = self.ctx._manager
        if manager._cli_ref is not None or not manager.has_gateway_message_injector:
            return
        self.beat("armed")
        result = subprocess.run(self.argv, env=self.env, capture_output=True, text=True,
                                timeout=POLL_TIMEOUT, check=False)
        if result.returncode:
            log_error(self.home, self.config.redact,
                      f"boss poll exited {result.returncode}: {result.stdout}\n{result.stderr}")
            return
        if result.stderr.strip():
            log_error(self.home, self.config.redact, "boss poll stderr: " + result.stderr)
        content = self.config.redact(result.stdout).strip()
        if (not content or self.stop_event.is_set() or manager._cli_ref is not None or
                not manager.has_gateway_message_injector):
            return
        # Poll owns durable deduplication; do not hash the text here. The same
        # 'finished' text can describe two different completed runs of one task.
        summary = " ".join(content.split())[:800]
        target = {key: self.wake[key] for key in ("origin", "session_key") if key in self.wake}
        accepted = self.ctx.inject_message(
            "[boss wake] " + summary + ". Run boss status to review the changed workers and pending decisions.",
            role="user", **target)
        if not accepted and not self.refusal_logged:
            log_error(self.home, self.config.redact,
                      "gateway refused boss wake; check " + CONSENT_LINE +
                      " and the target/allowlist; legacy fallback: disable wake.enabled, then ./install.sh --with-cron")
            self.refusal_logged = True

    def run(self):
        while not self.stop_event.is_set():
            try:
                self.tick()
            except Exception as exc:
                # Injector/dependency errors must not escape into Hermes. Raw
                # exceptions may contain credentials unknown to the redactor.
                log_error(self.home, self.config.redact, "boss wake failed: " + type(exc).__name__)
            self.stop_event.wait(self.wake.get("interval_seconds", 300))

    def stop(self):
        self.stop_event.set()
        # One subprocess, bounded by POLL_TIMEOUT. Finish it before a reload can
        # start its replacement, so two pollers never consume the same ledger.
        self.thread.join(POLL_TIMEOUT + 1)


def start_gateway_wake(ctx):
    """Called only by the gateway bridge; registry survives plugin module reloads."""
    home, redact = boss_home(), Redactor()
    try:
        manager = ctx._manager
        config = Config(home, redact, hermes=manager.home_path)
        wake = config.data.get("wake", {})
        if not wake.get("enabled", True) or not target_configured(wake):
            return
        key = (str(config.hermes.resolve()), str(home.resolve()))
        with _lock:
            current = _loops.get(key)
            if current is not None and current.thread.is_alive():
                return
            loop = WakeLoop(ctx, config, home)
            ctx.on_unload(loop.stop)
            _loops[key] = loop
            loop.thread.start()
    except Exception as exc:
        log_error(home, redact, "boss wake setup failed: " + type(exc).__name__)
