#!/usr/bin/env python3
"""Rerun wake transitions with real boss subprocesses and fake MCP/injector."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.test_wake import WakeIntegrationTest


if __name__ == "__main__":
    print("Disposable HOME; real boss poll subprocesses; fake T3 + injector; no LLM calls")
    case = WakeIntegrationTest("test_real_subprocess_poll_dedupes_and_never_injects_errors")
    try:
        case.setUp()
        try:
            case.exercise(print)
        finally:
            case.tearDown()
    finally:
        case.doCleanups()
    print("PASS: quiet, completed, restart dedupe, new run, error, and input transitions")
