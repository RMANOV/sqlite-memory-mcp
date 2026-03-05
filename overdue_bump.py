#!/usr/bin/env python3
"""Bump priority of overdue tasks to a target priority level.

Finds tasks where due_date < today, status not in (done, archived, cancelled),
and current priority is lower than the target. Updates them to target priority.
"""

import argparse
import sys

from db_utils import (
    DB_PATH,
    PRIORITY_RANK,
    TASK_ACTIVE_EXCLUSIONS,
    TASK_PRIORITIES,
    get_conn,
    now_iso,
)

# Pre-built SQL fragment for active-task exclusion filter
_EXCL_PH = ",".join("?" for _ in TASK_ACTIVE_EXCLUSIONS)


def run(db_path: str, target_priority: str, dry_run: bool) -> int:
    if target_priority not in PRIORITY_RANK:
        print(
            f"Error: invalid priority '{target_priority}'. "
            f"Choose from: {', '.join(TASK_PRIORITIES)}",
            file=sys.stderr,
        )
        return 1

    target_rank = PRIORITY_RANK[target_priority]
    lower_priorities = [p for p, r in PRIORITY_RANK.items() if r < target_rank]

    if not lower_priorities:
        print(f"No priorities lower than '{target_priority}' — nothing to bump.")
        return 0

    ph = ",".join("?" * len(lower_priorities))

    with get_conn(db_path) as conn:
        if dry_run:
            rows = conn.execute(
                f"SELECT id, title, priority, due_date FROM tasks "
                f"WHERE due_date < date('now') "
                f"AND status NOT IN ({_EXCL_PH}) "
                f"AND priority IN ({ph})",
                list(TASK_ACTIVE_EXCLUSIONS) + lower_priorities,
            ).fetchall()
            if not rows:
                print("Dry run: no overdue tasks would be bumped.")
            else:
                print(
                    f"Dry run: {len(rows)} task(s) would be bumped to '{target_priority}':"
                )
                for row in rows:
                    print(
                        f"  [{row['id']}] {row['title']!r}  "
                        f"priority={row['priority']}  due={row['due_date']}"
                    )
        else:
            now = now_iso()
            cur = conn.execute(
                f"UPDATE tasks SET priority = ?, updated_at = ? "
                f"WHERE due_date < date('now') "
                f"AND status NOT IN ({_EXCL_PH}) "
                f"AND priority IN ({ph})",
                [target_priority, now]
                + list(TASK_ACTIVE_EXCLUSIONS)
                + lower_priorities,
            )
            print(f"Bumped {cur.rowcount} task(s) to '{target_priority}'.")

    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Bump priority of overdue tasks to a target priority level."
    )
    parser.add_argument("--db", default=DB_PATH, help="Path to the SQLite DB")
    parser.add_argument(
        "--target",
        default="high",
        choices=TASK_PRIORITIES,
        help="Target priority to bump overdue tasks to (default: high)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show matching tasks without updating",
    )
    args = parser.parse_args()

    sys.exit(run(args.db, args.target, args.dry_run))


if __name__ == "__main__":
    main()
