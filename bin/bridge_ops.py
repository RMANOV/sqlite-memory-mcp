#!/usr/bin/env python3
"""Operator helpers for bridge health and recovery smoke checks."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

SMOKE_TESTS = [
    "tests/test_bridge_export.py",
    "tests/test_bridge_server.py",
    "tests/test_bridge_sync_worker.py",
    "tests/test_memory_bridge_import.py",
    "tests/test_tray_sync.py",
]


def _run(cmd: list[str]) -> int:
    return subprocess.run(cmd, cwd=ROOT, check=False).returncode


def cmd_doctor(_: argparse.Namespace) -> int:
    sys.path.insert(0, str(ROOT))
    import bridge_server

    payload = json.loads(bridge_server.bridge_doctor.fn())
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


def cmd_smoke(args: argparse.Namespace) -> int:
    cmd = [sys.executable, "-m", "pytest", *SMOKE_TESTS]
    if not args.verbose:
        cmd.append("-q")
    return _run(cmd)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Local bridge doctor and smoke helpers for sqlite_memory."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="Print bridge_doctor JSON snapshot.")
    doctor.set_defaults(func=cmd_doctor)

    smoke = sub.add_parser(
        "smoke",
        help="Run the highest-signal bridge, recovery, and tray sync smoke tests.",
    )
    smoke.add_argument(
        "--verbose",
        action="store_true",
        help="Show normal pytest output instead of -q.",
    )
    smoke.set_defaults(func=cmd_smoke)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
