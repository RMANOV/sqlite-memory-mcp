#!/usr/bin/env python3
"""Bump priority of overdue tasks to a target priority level.

Finds tasks where due_date < today, status not in (done, archived, cancelled),
and current priority is lower than the target. Updates them to target priority.
"""

import argparse
import sys

from db_utils import (
    DB_PATH,
    TASK_PRIORITIES,
    TaskDAO,
    get_conn,
)


def run(db_path: str, target_priority: str, dry_run: bool) -> int:
    if target_priority not in TASK_PRIORITIES:
        print(
            f"Error: invalid priority '{target_priority}'. "
            f"Choose from: {', '.join(TASK_PRIORITIES)}",
            file=sys.stderr,
        )
        return 1

    if target_priority == TASK_PRIORITIES[0]:
        print(f"No priorities lower than '{target_priority}' — nothing to bump.")
        return 0

    with get_conn(db_path) as conn:
        candidates = TaskDAO.bump_overdue_priority(
            conn,
            target_priority,
            dry_run=dry_run,
            tool_name="overdue_bump.run",
        )
        if dry_run:
            if not candidates:
                print("Dry run: no overdue tasks would be bumped.")
            else:
                print(
                    f"Dry run: {len(candidates)} task(s) would be bumped to '{target_priority}':"
                )
                for row in candidates:
                    print(
                        f"  [{row['id']}] {row['title']!r}  "
                        f"priority={row['priority']}  due={row['due_date']}"
                    )
        else:
            print(f"Bumped {len(candidates)} task(s) to '{target_priority}'.")

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
