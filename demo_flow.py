#!/usr/bin/env python3
"""Seed a safe local demo database for sqlite-memory-mcp."""

from __future__ import annotations

import argparse
import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import schema
from db_utils import create_task_with_ledger, now_iso


DEFAULT_DEMO_DB = "/tmp/sqlite-memory-mcp-demo.db"
DEMO_PROJECT = "sqlite-memory-demo"


def seed_demo(db_path: str = DEFAULT_DEMO_DB, *, reset: bool = False) -> dict[str, Any]:
    """Create a small demo graph plus task/note rows in a demo DB."""
    target = Path(db_path).expanduser()
    if reset:
        for path in (
            target,
            Path(f"{target}-wal"),
            Path(f"{target}-shm"),
            Path(f"{target}-journal"),
        ):
            if path.exists():
                path.unlink()
    schema.init_db(str(target))
    conn = sqlite3.connect(str(target), isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN")
        now = now_iso()
        entity_name = "SQLite Memory MCP Demo"
        conn.execute(
            "INSERT OR IGNORE INTO entities "
            "(name, entity_type, project, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (entity_name, "project", DEMO_PROJECT, now, now),
        )
        entity_id = conn.execute(
            "SELECT id FROM entities WHERE name = ?", (entity_name,)
        ).fetchone()["id"]
        for observation in [
            "Local-first memory with SQLite WAL and FTS5.",
            "Task tray supports due dates, reminders, recurring schedules, and bridge sync.",
        ]:
            conn.execute(
                "INSERT OR IGNORE INTO observations "
                "(entity_id, content, created_at) VALUES (?, ?, ?)",
                (entity_id, observation, now),
            )

        tomorrow = (datetime.now(timezone.utc).date() + timedelta(days=1)).isoformat()
        reminder_at = (datetime.now(timezone.utc) + timedelta(hours=1)).replace(
            second=0,
            microsecond=0,
        ).isoformat()
        task_id = str(uuid.uuid4())
        note_id = str(uuid.uuid4())
        create_task_with_ledger(
            conn,
            task_id,
            "Demo: open the tray and inspect this task",
            now,
            description=(
                "Created by sqlite-memory-demo. It has a due date, reminder, "
                "recurring schedule, project tag, and internal notes."
            ),
            notes="This row is safe demo data; delete it after trying the tray.",
            priority="high",
            section="today",
            due_date=tomorrow,
            project=DEMO_PROJECT,
            recurring=json.dumps({"every": "week", "day": "monday"}),
            reminder_at=reminder_at,
            type="task",
            tool_name="sqlite-memory-demo",
            source_kind="demo",
            source_ref="sqlite-memory-demo",
        )
        create_task_with_ledger(
            conn,
            note_id,
            "Demo note: why this stack exists",
            now,
            description=(
                "SQLite avoids JSONL lock corruption while staying local, "
                "simple, and inspectable."
            ),
            priority="medium",
            section="inbox",
            project=DEMO_PROJECT,
            type="note",
            tool_name="sqlite-memory-demo",
            source_kind="demo",
            source_ref="sqlite-memory-demo",
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()

    return {
        "db_path": str(target),
        "project": DEMO_PROJECT,
        "entity": entity_name,
        "task_id": task_id,
        "note_id": note_id,
        "next_steps": [
            f"sqlite-memory-doctor --db {target}",
            f"SQLITE_MEMORY_DB={target} task-tray",
        ],
    }


def _print_text(result: dict[str, Any]) -> None:
    print("sqlite-memory-mcp demo database ready")
    print(f"database: {result['db_path']}")
    print(f"project: {result['project']}")
    print(f"task_id: {result['task_id']}")
    print(f"note_id: {result['note_id']}")
    print("next:")
    for command in result["next_steps"]:
        print(f"  {command}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create a safe demo DB with sample memory + task tray data."
    )
    parser.add_argument(
        "--db",
        default=DEFAULT_DEMO_DB,
        help=f"Demo DB path. Default: {DEFAULT_DEMO_DB}",
    )
    parser.add_argument("--reset", action="store_true", help="Delete demo DB first.")
    parser.add_argument("--json", action="store_true", help="Print JSON output.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = seed_demo(db_path=args.db, reset=args.reset)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        _print_text(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
