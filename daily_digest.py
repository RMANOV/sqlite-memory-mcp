#!/usr/bin/env python3
"""daily_digest.py — Standalone task digest for sqlite-memory-mcp.

Reads tasks from the SQLite memory DB and prints a markdown digest to stdout.

Usage:
    python3 daily_digest.py
    python3 daily_digest.py --db ~/.claude/memory/memory.db
    python3 daily_digest.py --sections today,inbox,next,waiting
    python3 daily_digest.py --no-overdue --limit 30
"""

import argparse
import os
import sys

from db_utils import DB_PATH as DEFAULT_DB
from db_utils import TASK_ACTIVE_EXCLUSIONS, build_priority_order_sql, get_conn, now_iso

# Pre-built SQL fragment for active-task exclusion filter
_EXCL_PH = ",".join("?" for _ in TASK_ACTIVE_EXCLUSIONS)


def run_digest(
    db_path: str,
    sections: list[str],
    include_overdue: bool,
    limit: int,
    include_notes: bool = False,
) -> str:
    """Query the DB and return a markdown digest string."""

    with get_conn(db_path) as conn:
        # Active tasks by section — mirrors server.py task_digest SQL exactly
        ph = ",".join("?" * len(sections))
        active = conn.execute(
            f"SELECT id, title, status, priority, section, due_date, project "
            f"FROM tasks "
            f"WHERE section IN ({ph}) AND status IN ('not_started', 'in_progress') AND type = 'task' "
            f"ORDER BY "
            f"  CASE section WHEN 'today' THEN 0 WHEN 'inbox' THEN 1 "
            f"       WHEN 'next' THEN 2 WHEN 'waiting' THEN 3 WHEN 'someday' THEN 4 END, "
            f"  {build_priority_order_sql()} "
            f"LIMIT ?",
            sections + [limit],
        ).fetchall()

        # Overdue tasks
        overdue: list = []
        if include_overdue:
            overdue = conn.execute(
                "SELECT id, title, status, priority, section, due_date, project "
                "FROM tasks "
                f"WHERE due_date < date('now') AND status NOT IN ({_EXCL_PH}) AND type = 'task' "
                "ORDER BY due_date ASC LIMIT 10",
                list(TASK_ACTIVE_EXCLUSIONS),
            ).fetchall()

        # Status counts (active + done, excluding archived/cancelled)
        counts = conn.execute(
            "SELECT status, COUNT(*) as cnt FROM tasks "
            "WHERE status NOT IN ('archived', 'cancelled') AND type = 'task' GROUP BY status"
        ).fetchall()

        note_rows: list = []
        if include_notes:
            note_rows = conn.execute(
                "SELECT id, title, priority, updated_at FROM tasks "
                "WHERE type = 'note' AND status NOT IN ('archived', 'cancelled') "
                f"ORDER BY {build_priority_order_sql()}, updated_at DESC "
                "LIMIT ?",
                (limit,),
            ).fetchall()

    # ── Format markdown ───────────────────────────────────────────────────────
    now_utc = now_iso()
    lines = [
        "## Task Digest",
        f"*Generated: {now_utc}*",
        "",
    ]

    if counts:
        stats = {r["status"]: r["cnt"] for r in counts}
        total = sum(stats.values())
        lines.append(
            f"**Total active:** {total} | "
            f"Not started: {stats.get('not_started', 0)} | "
            f"In progress: {stats.get('in_progress', 0)} | "
            f"Done: {stats.get('done', 0)}"
        )
        lines.append("")

    if overdue:
        lines.append(f"### OVERDUE ({len(overdue)})")
        for t in overdue:
            priority = t["priority"] or "medium"
            lines.append(f"- [{priority.upper()}] {t['title']} (due: {t['due_date']})")
        lines.append("")

    # Group active tasks by section
    by_section: dict[str, list] = {}
    for t in active:
        by_section.setdefault(t["section"], []).append(t)

    for sec in sections:
        tasks = by_section.get(sec, [])
        if tasks:
            lines.append(f"### {sec.upper()} ({len(tasks)})")
            for t in tasks:
                due = f" [due: {t['due_date']}]" if t["due_date"] else ""
                priority = t["priority"] or "medium"
                prio = f"[{priority.upper()}] " if priority != "medium" else ""
                lines.append(f"- {prio}{t['title']}{due}")
            lines.append("")

    if note_rows:
        lines.append(f"### NOTES ({len(note_rows)})")
        for n in note_rows:
            prio = f"[{n['priority'].upper()}] " if n["priority"] != "medium" else ""
            lines.append(f"- {prio}{n['title']}")
        lines.append("")

    if not active and not overdue:
        lines.append("*No tasks found for the selected sections.*")
        lines.append("")

    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Print a markdown task digest from the sqlite-memory-mcp database.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--db",
        default=DEFAULT_DB,
        metavar="PATH",
        help=f"Path to SQLite memory DB (default: {DEFAULT_DB})",
    )
    parser.add_argument(
        "--sections",
        default="today,inbox,next",
        metavar="SECTIONS",
        help="Comma-separated section names to include (default: today,inbox,next)",
    )
    parser.add_argument(
        "--no-overdue",
        dest="include_overdue",
        action="store_false",
        default=True,
        help="Skip overdue tasks section",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=20,
        metavar="N",
        help="Max tasks to fetch per query (default: 20)",
    )
    parser.add_argument(
        "--include-notes",
        action="store_true",
        default=False,
        help="Include notes in the digest output",
    )

    args = parser.parse_args()

    db_path = os.path.expanduser(args.db)

    if not os.path.exists(db_path):
        print(f"Error: Database not found: {db_path}", file=sys.stderr)
        return 1

    sections = [s.strip() for s in args.sections.split(",") if s.strip()]
    if not sections:
        print("Error: --sections cannot be empty", file=sys.stderr)
        return 1

    digest = run_digest(
        db_path=db_path,
        sections=sections,
        include_overdue=args.include_overdue,
        limit=args.limit,
        include_notes=args.include_notes,
    )
    print(digest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
