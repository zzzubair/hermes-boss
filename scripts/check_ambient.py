#!/usr/bin/env python3
"""Measure the actual local hook callback, including open/read/decode/render."""
import json
import os
from pathlib import Path
import platform
import statistics
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from plugin import on_pre_llm_call


def main():
    print(f"machine: {platform.node()}")
    print(f"python: {platform.python_version()}")
    print("measurement: callback wall time; includes local file read; excludes ledger production and Hermes startup")
    prior = os.environ.get("BOSS_HOME")
    try:
        with tempfile.TemporaryDirectory(prefix="boss-hook-check-") as directory:
            os.environ["BOSS_HOME"] = directory
            cache = Path(directory) / "attention.json"
            for name, count, items in (
                ("clean", 0, []),
                ("actionable", 3, [{"id": "d3", "kind": "decision"}, {"id": "t7", "kind": "input"}, {"id": "t9", "kind": "finished"}]),
                ("large_total", 2000, [{"id": f"t{i}", "kind": "input"} for i in range(6)]),
            ):
                cache.write_text(json.dumps({"version": 1, "count": count, "items": items}))
                samples = []
                for _ in range(1000):
                    start = time.perf_counter_ns()
                    result = on_pre_llm_call(session_id="benchmark", user_message="hello")
                    samples.append((time.perf_counter_ns() - start) / 1_000_000)
                assert (result is None) == (count == 0)
                p99 = sorted(samples)[989]
                maximum = max(samples)
                print(f"{name}: samples=1000 bytes={cache.stat().st_size} median_ms={statistics.median(samples):.3f} p99_ms={p99:.3f} max_ms={maximum:.3f}")
                if result:
                    print("context: " + result["context"])
                else:
                    print("context: none (zero tokens)")
                if maximum >= 50:
                    print("result: callback exceeded 50 ms")
                    return 1
        print("result: all callback samples under 50 ms")
        return 0
    finally:
        if prior is None:
            os.environ.pop("BOSS_HOME", None)
        else:
            os.environ["BOSS_HOME"] = prior


if __name__ == "__main__":
    raise SystemExit(main())
