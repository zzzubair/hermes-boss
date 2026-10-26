"""Isolated homes and a small environment allowlist for every test fixture."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


class HermeticTestCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.user_home = self.home / "user"
        self.user_home.mkdir()
        self.env = patch.dict(os.environ, {
            "PATH": os.environ.get("PATH", os.defpath),
            "HOME": str(self.user_home),
            "LC_ALL": "C.UTF-8",
            "GIT_CONFIG_NOSYSTEM": "1",
        }, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def fake_command(self, name, script="exit 0"):
        """Put an executable `name` first on PATH, as if installed on the local host."""
        directory = self.home / "bin"
        directory.mkdir(exist_ok=True)
        path = directory / name
        path.write_text("#!/bin/sh\n" + script + "\n")
        path.chmod(0o755)
        os.environ["PATH"] = f"{directory}{os.pathsep}{os.environ['PATH']}"
        return path
