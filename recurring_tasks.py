#!/usr/bin/env python3
"""recurring_tasks.py — Recreate recurring tasks based on schedule.

Finds tasks with status='done' and a recurring JSON config, checks if today
matches the schedule, and inserts a new not_started copy if no active duplicate
exists (idempotent).

Usage:
    python3 recurring_tasks.py [--db PATH] [--dry-run]
"""

import argparse
import json
import sqlite3
import sys
import uuid
from datetime import date, timedelta

from db_utils import DB_PATH, get_conn, now_iso


def matches_schedule(config: dict, today: date) -> bool:
    """Return True if today matches the recurring schedule config."""
    every = config.get("every", "").lower()
    interval = int(config.get("interval", 1))
    if every == "day":
        if interval == 1:
            return True
        return today.toordinal() % interval == 0
    if every == "week":
        day_name = config.get("day", "").lower()
        if today.strftime("%A").lower() != day_name:
            return False
        if interval == 1:
            return True
        return today.isocalendar()[1] % interval == 0
    if every == "month":
        day_num = config.get("day")
        if day_num is None:
            return False
        if today.day != int(day_num):
            return False
        if interval == 1:
            return True
        return today.month % interval == 0
    if every == "year":
        cfg_month = config.get("month")
        cfg_day = config.get("day")
        if cfg_month is not None and today.month != int(cfg_month):
            return False
        if cfg_day is not None and today.day != int(cfg_day):
            return False
        if interval > 1:
            return today.year % interval == 0
        return True
    return False


def next_due_date(config: dict, today: date) -> str:
    """Calculate the due_date for the new task based on the recurring config."""
    import calendar

    every = config.get("every", "").lower()
    interval = int(config.get("interval", 1))
    if every == "day":
        return (today + timedelta(days=interval)).isoformat()
    if every == "week":
        day_name = config.get("day", "").lower()
        weekday_map = {
            "monday": 0,
            "tuesday": 1,
            "wednesday": 2,
            "thursday": 3,
            "friday": 4,
            "saturday": 5,
            "sunday": 6,
        }
        target_weekday = weekday_map.get(day_name)
        if target_weekday is None:
            return (today + timedelta(weeks=interval)).isoformat()
        days_ahead = (target_weekday - today.weekday()) % 7
        if days_ahead == 0:
            days_ahead = 7 * interval
        else:
            days_ahead += 7 * (interval - 1)
        return (today + timedelta(days=days_ahead)).isoformat()
    if every == "month":
        day_num = config.get("day")
        if day_num is None:
            return today.isoformat()
        day_num = int(day_num)
        # Advance by interval months
        new_month = today.month + interval
        new_year = today.year + (new_month - 1) // 12
        new_month = (new_month - 1) % 12 + 1
        last_day = calendar.monthrange(new_year, new_month)[1]
        return date(new_year, new_month, min(day_num, last_day)).isoformat()
    if every == "year":
        cfg_month = int(config.get("month", today.month))
        cfg_day = int(config.get("day", today.day))
        # Try this year first
        try:
            candidate = date(today.year, cfg_month, cfg_day)
        except ValueError:
            last_day = calendar.monthrange(today.year, cfg_month)[1]
            candidate = date(today.year, cfg_month, min(cfg_day, last_day))
        if candidate <= today:
            # Move to next interval year
            target_year = today.year + interval
            try:
                candidate = date(target_year, cfg_month, cfg_day)
            except ValueError:
                last_day = calendar.monthrange(target_year, cfg_month)[1]
                candidate = date(target_year, cfg_month, min(cfg_day, last_day))
        return candidate.isoformat()
    return today.isoformat()


def has_active_duplicate(conn: sqlite3.Connection, title: str) -> bool:
    """Return True if an active (not_started or in_progress) task with the same title exists."""
    row = conn.execute(
        "SELECT id FROM tasks WHERE title = ? AND status IN ('not_started', 'in_progress') LIMIT 1",
        (title,),
    ).fetchone()
    return row is not None


def get_recurring_done_tasks(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, title, description, status, priority, section, due_date,"
        " project, parent_id, notes, recurring, type, assignee, shared_by,"
        " created_at, updated_at, visibility, publish_requested_at"
        " FROM tasks WHERE recurring IS NOT NULL AND status = 'done'"
    ).fetchall()


def build_new_task(source: sqlite3.Row, due: str, timestamp: str) -> dict:
    return {
        "id": uuid.uuid4().hex[:16],
        "title": source["title"],
        "description": source["description"],
        "status": "not_started",
        "priority": source["priority"],
        "section": source["section"],
        "due_date": due,
        "project": source["project"],
        "parent_id": source["parent_id"],
        "notes": source["notes"],
        "recurring": source["recurring"],
        "reminder_at": None,
        "created_at": timestamp,
        "updated_at": timestamp,
    }


def process_recurring(conn: sqlite3.Connection, dry_run: bool) -> list[dict]:
    today = date.today()
    timestamp = now_iso()

    done_tasks = get_recurring_done_tasks(conn)
    created = []

    for task in done_tasks:
        raw_recurring = task["recurring"]
        try:
            config = json.loads(raw_recurring)
        except (json.JSONDecodeError, TypeError):
            print(
                f"  [warn] Skipping task id={task['id']} — invalid recurring JSON: {raw_recurring!r}",
                file=sys.stderr,
            )
            continue

        if not matches_schedule(config, today):
            continue

        if has_active_duplicate(conn, task["title"]):
            print(
                f"  [skip] '{task['title']}' — active task already exists",
            )
            continue

        due = next_due_date(config, today)
        new_task = build_new_task(task, due, timestamp)

        if dry_run:
            print(
                f"  [dry-run] Would create: title='{new_task['title']}'"
                f"  priority={new_task['priority']}"
                f"  due={new_task['due_date']}"
                f"  recurring={raw_recurring}"
            )
        else:
            conn.execute(
                """
                INSERT INTO tasks
                    (id, title, description, status, priority, section, due_date,
                     project, parent_id, notes, recurring, reminder_at,
                     created_at, updated_at)
                VALUES
                    (:id, :title, :description, :status, :priority, :section, :due_date,
                     :project, :parent_id, :notes, :recurring, :reminder_at,
                     :created_at, :updated_at)
                """,
                new_task,
            )
            print(
                f"  Created: title='{new_task['title']}'"
                f"  id={new_task['id']}"
                f"  due={new_task['due_date']}"
            )

        created.append(new_task)

    if not dry_run and created:
        conn.commit()

    return created


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Recreate recurring tasks based on schedule."
    )
    parser.add_argument("--db", default=DB_PATH, help="Path to the SQLite DB")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be created without inserting",
    )
    args = parser.parse_args()

    with get_conn(args.db) as conn:
        created = process_recurring(conn, dry_run=args.dry_run)

    if args.dry_run:
        print(f"\n[dry-run] {len(created)} task(s) would be created.")
    else:
        print(f"\nCreated {len(created)} recurring task(s).")


if __name__ == "__main__":
    main()
