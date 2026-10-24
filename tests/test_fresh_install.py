"""Exercise the installed executable in independent processes, not patched CLI functions."""
import unittest

from scripts.check_fresh_install import run


class FreshInstallTest(unittest.TestCase):
    def test_fresh_install(self):
        transcript = []
        try:
            run(transcript.append)
        except Exception:
            self.fail("\n".join(transcript))
