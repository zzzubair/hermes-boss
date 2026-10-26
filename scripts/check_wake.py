#!/usr/bin/env python3
"""Rerun the pushed-wake and ownership checks: fake T3 WebSocket, real listener and ledger, no LLM calls."""
from pathlib import Path
import sys
import unittest

root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root))


if __name__ == "__main__":
    print("Disposable HOME; fake T3 push stream + injector; real boss listener, ledger and CLI; no LLM calls")
    suite = unittest.defaultTestLoader.loadTestsFromNames(["tests.test_wake", "tests.test_owner"])
    result = unittest.TextTestRunner(verbosity=2, stream=sys.stdout).run(suite)
    print("PASS: pushed events reach the owner once, held wakes survive refusal/no owner, one listener per machine"
          if result.wasSuccessful() else "FAIL")
    raise SystemExit(0 if result.wasSuccessful() else 1)
