#!/usr/bin/env python3
"""Fresh HOME integration check. Fake MCP/GitHub/Hermes; no paid workers or live state."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import tomllib

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.test_boss import FakeT3, TOKEN


FAKE_TOOLS = '''import json, os, pathlib, sys
home = pathlib.Path(os.environ.get("HERMES_HOME", str(pathlib.Path.home() / ".hermes")))
args = sys.argv[1:]
if pathlib.Path(sys.argv[0]).name == "gh":
    if args[:2] == ["auth", "status"]:
        print("fake GitHub authentication ok")
    elif args[:2] == ["repo", "view"]:
        print(json.dumps({"defaultBranchRef": {"name": "main"}}))
    elif args[:1] == ["api"] and "/git/matching-refs/heads/boss/" in args[1]:
        print("[]")
    else:
        sys.exit("unexpected fake gh command")
else:
    if args[:3] == ["plugins", "enable", "boss"]:
        (home / "boss-enabled").touch()
        print("boss enabled (fake Hermes)")
    elif args[:3] == ["plugins", "list", "--json"]:
        print(json.dumps([{"name": "boss", "status": "enabled" if (home / "boss-enabled").exists() else "disabled"}]))
    elif args[:2] == ["cron", "create"]:
        path = home / "cron/jobs.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        data = json.loads(path.read_text()) if path.exists() else {"jobs": []}
        data["jobs"].append({"name": args[args.index("--name") + 1], "enabled": True,
            "script": args[args.index("--script") + 1], "no_agent": "--no-agent" in args})
        path.write_text(json.dumps(data))
        print("created boss-poll every 5 minutes (fake Hermes)")
    else:
        sys.exit("unexpected fake Hermes command")
'''


def run(transcript=print):
    root = Path(__file__).resolve().parent.parent
    fake = FakeT3()
    try:
        with tempfile.TemporaryDirectory(prefix="boss fresh install ") as directory:
            home = Path(directory)
            tools_dir, hermes = home / "tools", home / "hermes profile"
            tools_dir.mkdir()
            hermes.mkdir()
            # A literal dotenv credential, never exported or printed.
            (hermes / ".env").write_text(f'T3_MCP_TOKEN="{TOKEN}"\n')
            sentinel = 'plugins:\n  enabled: []\n'
            (hermes / "config.yaml").write_text(sentinel)
            for name in ("gh", "hermes"):
                script = tools_dir / name
                script.write_text("#!" + sys.executable + "\n" + FAKE_TOOLS)
                script.chmod(0o700)
            env = {"HOME": str(home), "HERMES_HOME": str(hermes),
                   "PATH": str(tools_dir) + os.pathsep + os.environ.get("PATH", os.defpath),
                   "BOSS_PYTHON": sys.executable, "LC_ALL": "C.UTF-8", "GIT_CONFIG_NOSYSTEM": "1"}
            config = home / ".config/boss/config.toml"
            boss = home / ".local/bin/boss"

            def command(argv, *, expected=0, stdin=None, label=None):
                transcript("$ " + (label or shlex.join(map(str, argv))).replace(str(home), "<HOME>").replace(fake.url, "http://127.0.0.1:<PORT>/mcp"))
                result = subprocess.run(list(map(str, argv)), cwd=root, env=env, input=stdin,
                                        capture_output=True, text=True, timeout=30)
                for stream in (result.stdout, result.stderr):
                    if TOKEN in stream:
                        raise AssertionError("credential leaked")
                    if stream:
                        transcript(stream.rstrip().replace(str(home), "<HOME>").replace(fake.url, "http://127.0.0.1:<PORT>/mcp"))
                transcript(f"exit: {result.returncode}")
                if result.returncode != expected:
                    raise AssertionError(f"unexpected exit: {result.returncode}, wanted {expected}")
                return result.stdout

            transcript(f"Python {sys.version.split()[0]}; disposable HOME; stdlib HTTP MCP; fake gh/Hermes; no LLM calls")
            command([root / "install.sh"], label="./install.sh")
            assert not config.exists()
            assert not (hermes / "cron/jobs.json").exists()
            assert (hermes / "config.yaml").read_text() == sentinel
            assert (hermes / "plugins/boss").resolve() == root / "plugin"
            assert (hermes / "skills/boss").resolve() == root / "skill"
            command([boss, "init", "--url", fake.url], label="boss init --url http://127.0.0.1:<PORT>/mcp")
            original = config.read_bytes()
            command([boss, "init"], label="boss init (repeat; config unchanged)")
            assert config.read_bytes() == original
            assert tomllib.loads(config.read_text())["default_host"] == "local"
            output = command([boss, "doctor"], expected=1, label="boss doctor (plugin disabled; expected failure)")
            assert "fail: plugin enabled" in output
            command(["hermes", "plugins", "enable", "boss"])
            output = command([boss, "doctor"], label="boss doctor (no owner yet; wakes held)")
            assert "warn: no owner" in output
            assert not (hermes / "cron").exists()
            command([boss, "config", "set", "projects.repo.repository", "example/repo"], label="boss config set projects.repo.repository example/repo")
            command([boss, "config", "set", "workers.scout.model", "codex:gpt-test"], label="boss config set workers.scout.model codex:gpt-test")
            command([boss, "config", "set", "workers.scout.effort", "low"], label="boss config set workers.scout.effort low")
            output = command([boss, "spawn", "repo", "--title", "Fresh install scout", "--kind", "scout"],
                             stdin="Inspect this fake project and report.", label='printf brief | boss spawn repo --title "Fresh install scout" --kind scout')
            task_id = output.split(":", 1)[0]
            meta = json.loads((home / ".boss/tasks" / task_id / "meta.json").read_text())
            assert meta["worker"] == "scout" and meta["kind"] == "scout"
            assert meta["branch"] == "boss/fresh-install-scout", meta["branch"]
            launch = next(args for name, args in fake.calls if name == "t3_thread_launch")
            assert launch["modelSelection"]["options"] == {"reasoningEffort": "low"}
            assert "Never merge. Never deploy." in launch["message"]
            # Nothing polls: there is no poll command, and boss is inactive until a session owns it.
            assert "invalid choice: 'poll'" in command([boss, "poll"], expected=1, label="boss poll (removed)")
            env["HERMES_SESSION_KEY"] = "agent:main:discord:dm:owner"
            command([boss, "send", task_id, "hello"], expected=2, label="boss send (Hermes session, boss not open; refused)")
            command([boss, "own", "--label", "Discord DM"], label="boss own --label 'Discord DM'")
            command([boss, "own", "status"], label="boss own status")
            env.pop("HERMES_SESSION_KEY")
            # Failure diagnoses must produce a fix hint and a nonzero exit.
            env.pop("HERMES_HOME")
            output = command([boss, "doctor"], expected=1, label="boss doctor (wrong Hermes home; expected failure)")
            assert "fail:" in output and "fix:" in output
            assert len(fake.threads) == 1
            transcript("PASS: fresh install, idempotent init, doctor, role spawn, no poll command, ownership gate and claim")
    finally:
        fake.close()


if __name__ == "__main__":
    run()
