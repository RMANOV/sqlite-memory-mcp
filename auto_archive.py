#!/usr/bin/env python3
"""auto_archive.py — Archive done tasks older than N days.

Usage:
    python3 auto_archive.py [--db PATH] [--days N] [--dry-run]
"""

import argparse
import sqlite3

from db_utils import DB_PATH, apply_task_mutation, get_conn, now_iso


def dry_run(conn: sqlite3.Connection, days: int) -> None:
    rows = conn.execute(
        "SELECT id, title, status, updated_at FROM tasks "
        "WHERE status = 'done' "
        "AND updated_at < datetime('now', ? || ' days')",
        (f"-{days}",),
    ).fetchall()

    if not rows:
        print(f"[dry-run] No tasks would be archived (threshold: {days} days).")
        return

    print(f"[dry-run] {len(rows)} task(s) would be archived (older than {days} days):")
    for row in rows:
        print(
            f"  id={row['id']}  status={row['status']}  updated_at={row['updated_at']}  title={row['title']}"
        )


def archive(conn: sqlite3.Connection, days: int) -> None:
    iso_now = now_iso()
    rows = conn.execute(
        "SELECT id FROM tasks WHERE status = 'done' "
        "AND updated_at < datetime('now', ? || ' days')",
        (f"-{days}",),
    ).fetchall()
    archived = 0
    for row in rows:
        result = apply_task_mutation(
            conn,
            row["id"],
            {"status": "archived"},
            timestamp=iso_now,
            tool_name="auto_archive.archive",
        )
        archived += int(result.get("updated", 0))
    print(f"Archived {archived} tasks (older than {days} days).")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Archive done tasks older than N days."
    )
    parser.add_argument("--db", default=DB_PATH, help="Path to the SQLite DB")
    parser.add_argument(
        "--days", type=int, default=7, help="Threshold in days (default: 7)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be archived without modifying",
    )
    args = parser.parse_args()

    with get_conn(args.db) as conn:
        if args.dry_run:
            dry_run(conn, args.days)
        else:
            archive(conn, args.days)


if __name__ == "__main__":
    main()
