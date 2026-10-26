#!/usr/bin/env python3
"""Exercise the local CLI across process restarts, without touching live state."""
import os
from pathlib import Path
import platform
import shlex
import subprocess
import sys
import tempfile


def main():
    root = Path(__file__).resolve().parent.parent
    print(f"machine: {platform.node()}")
    print(f"python: {platform.python_version()}")
    print("scope: disposable BOSS_HOME; real CLI processes; no T3 or GitHub calls")
    with tempfile.TemporaryDirectory(prefix="boss-steering-check-") as directory:
        home = Path(directory)
        user = home / "user"
        user.mkdir()
        state = home / "state"
        env = {"PATH": os.environ.get("PATH", os.defpath), "HOME": str(user),
               "BOSS_HOME": str(state), "LC_ALL": "C.UTF-8"}

        def command(*args, expected=0):
            print("$ boss " + shlex.join(args), flush=True)
            result = subprocess.run([sys.executable, "-m", "boss", *args], cwd=root, env=env,
                                    input="Improve the mobile navigation", capture_output=True, text=True, timeout=10)
            print(result.stdout, end="")
            print(f"exit: {result.returncode}")
            if result.returncode != expected or result.stderr:
                raise RuntimeError(f"unexpected CLI result: {result.returncode}, stderr={result.stderr!r}")
            return result.stdout

        command("goal", "demo")
        command("goal", "demo", "Ship a mobile-first app")
        first = command("steer", "demo", "Use PostHog").split(":", 1)[0]
        log = state / "projects/demo.steers.jsonl"
        original, inode = log.read_bytes(), log.stat().st_ino
        active = command("steer", "demo", "No PostHog", "--replaces", first).split(":", 1)[0]
        if not log.read_bytes().startswith(original) or log.stat().st_ino != inode:
            raise RuntimeError("supersession rewrote existing steer history")
        print("verified: original log bytes and inode preserved")
        command("goal", "demo")
        listed = command("steer", "list", "demo")
        history = command("steer", "list", "demo", "--all")
        if "Use PostHog" in listed or first + ": superseded" not in history:
            raise RuntimeError("incorrect active or superseded steer listing")
        refused = command("spawn", "demo", "--title", "Improve navigation", expected=2)
        if "Ship a mobile-first app" not in refused or active + ": No PostHog" not in refused:
            raise RuntimeError("reconciliation refusal omitted project context")
        if "Use PostHog" in refused or list((state / "tasks").glob("*")):
            raise RuntimeError("refusal included superseded context or created a task")
        before_drop = log.read_bytes()
        command("steer", "drop", "demo", active)
        if not log.read_bytes().startswith(before_drop) or log.stat().st_ino != inode:
            raise RuntimeError("drop rewrote existing steer history")
        print("verified: drop preserved original log bytes and inode")
        listed = command("steer", "list", "demo")
        history = command("steer", "list", "demo", "--all")
        if "0 active steers" not in listed or active + ": dropped" not in history or "drops=" + active not in history:
            raise RuntimeError("incorrect dropped steer listing")
        after_drop = log.read_bytes()
        command("steer", "drop", "demo", active, expected=2)
        command("steer", "drop", "demo", first, expected=2)
        command("steer", "drop", "other", active, expected=1)
        if log.read_bytes() != after_drop or (state / "projects/other.steers.jsonl").exists():
            raise RuntimeError("refused drop changed steer history")
        refused = command("spawn", "demo", "--title", "Improve navigation", expected=2)
        if "Ship a mobile-first app" not in refused or "## Steers" in refused:
            raise RuntimeError("goal gate included dropped steer context")
    print("result: durable goal, append-only supersession/drop history, active filtering and spawn refusal verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
