#!/usr/bin/env python3
"""Install/runtime doctor for sqlite-memory-mcp."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

import schema
from db_utils import DB_PATH


def _check(name: str, ok: bool, detail: str, *, required: bool = True) -> dict[str, Any]:
    return {"name": name, "ok": ok, "required": required, "detail": detail}


def run_doctor(
    *,
    db_path: str | None = None,
    init_db: bool = True,
    check_gui: bool = False,
    check_bridge: bool = False,
) -> dict[str, Any]:
    """Return a structured install/runtime health report."""
    target_db = str(Path(db_path or DB_PATH).expanduser())
    checks: list[dict[str, Any]] = []

    py_ok = sys.version_info >= (3, 10)
    checks.append(
        _check(
            "python",
            py_ok,
            f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        )
    )
    checks.append(_check("sqlite3", True, sqlite3.sqlite_version))

    fastmcp_spec = importlib.util.find_spec("fastmcp")
    checks.append(_check("fastmcp", fastmcp_spec is not None, "importable"))

    if check_gui:
        pyqt_spec = importlib.util.find_spec("PyQt6")
        checks.append(_check("pyqt6", pyqt_spec is not None, "importable"))
    else:
        checks.append(_check("pyqt6", True, "skipped; pass --check-gui", required=False))

    try:
        if init_db:
            schema.init_db(target_db)
        conn = sqlite3.connect(target_db, timeout=2, isolation_level=None)
        try:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            required_tables = {"entities", "observations", "relations", "tasks"}
            missing = sorted(required_tables - tables)
            checks.append(
                _check(
                    "schema",
                    not missing,
                    "ok" if not missing else f"missing tables: {', '.join(missing)}",
                )
            )
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("ROLLBACK")
            checks.append(_check("db_write_lock", True, "BEGIN IMMEDIATE ok"))
        finally:
            conn.close()
    except Exception as exc:
        checks.append(_check("database", False, f"{type(exc).__name__}: {exc}"))

    if check_bridge:
        try:
            import bridge_server

            payload = json.loads(bridge_server.bridge_doctor.fn(write_manifest=False))
            runtime = payload.get("runtime_parity") or {}
            checks.append(
                _check(
                    "bridge_runtime",
                    bool(runtime.get("all_synced")),
                    ", ".join(runtime.get("warnings") or []) or "in sync",
                    required=False,
                )
            )
        except Exception as exc:
            checks.append(
                _check(
                    "bridge_runtime",
                    False,
                    f"{type(exc).__name__}: {exc}",
                    required=False,
                )
            )

    ok = all(item["ok"] for item in checks if item.get("required", True))
    return {"ok": ok, "db_path": target_db, "checks": checks}


def _print_text(report: dict[str, Any]) -> None:
    print("sqlite-memory-mcp install doctor")
    print(f"database: {report['db_path']}")
    for item in report["checks"]:
        marker = "ok" if item["ok"] else "fail"
        scope = "" if item.get("required", True) else " optional"
        print(f"[{marker}]{scope} {item['name']}: {item['detail']}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify sqlite-memory-mcp install/runtime basics."
    )
    parser.add_argument("--db", help="Database path to initialize/check.")
    parser.add_argument(
        "--no-init-db",
        action="store_true",
        help="Check the database path without initializing the schema first.",
    )
    parser.add_argument(
        "--check-gui",
        action="store_true",
        help="Require PyQt6 importability for task-tray usage.",
    )
    parser.add_argument(
        "--check-bridge",
        action="store_true",
        help="Also run the bridge runtime parity check.",
    )
    parser.add_argument("--json", action="store_true", help="Print JSON output.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = run_doctor(
        db_path=args.db,
        init_db=not args.no_init_db,
        check_gui=args.check_gui,
        check_bridge=args.check_bridge,
    )
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_text(report)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
