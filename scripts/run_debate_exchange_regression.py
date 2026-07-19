#!/usr/bin/env python3
"""Fail-closed regression gate for the debate exchange chain.

The gate is intentionally isolated from the production memory database.  It
points every default DB path at a temporary directory and runs only tmp-db
tests.  ``--self-test`` first injects a synthetic failing probe and requires a
non-zero pytest result, then requires the clean suite to pass.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time


ROOT = Path(__file__).resolve().parent.parent
TESTS = (
    "tests/test_debate_retrieval.py",
    "tests/test_debate_exchange_regression.py",
    "tests/test_debate_wake_regression.py",
    "tests/test_debate_e2e.py",
)
DEFAULT_TIMEOUT_SECONDS = 120


def _run_pytest(*, synthetic_broken: bool, timeout_seconds: int) -> dict:
    with tempfile.TemporaryDirectory(prefix="debate-regression-") as temp_dir:
        env = os.environ.copy()
        env["SQLITE_MEMORY_DB"] = str(Path(temp_dir) / "memory.db")
        env["DEBATE_REGRESSION_SYNTHETIC_BROKEN"] = (
            "1" if synthetic_broken else "0"
        )
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        selected = (
            [
                "tests/test_debate_exchange_regression.py::"
                "test_regression_probe_is_clean"
            ]
            if synthetic_broken
            else list(TESTS)
        )
        started = time.monotonic()
        try:
            completed = subprocess.run(
                [sys.executable, "-m", "pytest", "-q", *selected],
                cwd=ROOT,
                env=env,
                timeout=timeout_seconds,
                check=False,
            )
            return {
                "returncode": completed.returncode,
                "timed_out": False,
                "duration_seconds": round(time.monotonic() - started, 3),
            }
        except subprocess.TimeoutExpired:
            return {
                "returncode": 124,
                "timed_out": True,
                "duration_seconds": round(time.monotonic() - started, 3),
            }


def _write_receipt(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temp.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument(
        "--timeout-seconds", type=int, default=DEFAULT_TIMEOUT_SECONDS
    )
    parser.add_argument("--receipt", type=Path)
    args = parser.parse_args()
    timeout_seconds = max(10, min(args.timeout_seconds, 300))

    synthetic = None
    if args.self_test:
        synthetic = _run_pytest(
            synthetic_broken=True, timeout_seconds=timeout_seconds
        )
    clean = _run_pytest(synthetic_broken=False, timeout_seconds=timeout_seconds)
    success = clean["returncode"] == 0 and (
        synthetic is None or synthetic["returncode"] != 0
    )
    payload = {
        "schema_version": "debate_exchange_regression.v1",
        "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "success": success,
        "clean": clean,
        "synthetic_broken": synthetic,
        "production_db_writes": 0,
        "timeout_seconds_per_run": timeout_seconds,
        "tests": list(TESTS),
    }
    if args.receipt:
        _write_receipt(args.receipt.expanduser(), payload)
    print(json.dumps(payload, sort_keys=True))
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
