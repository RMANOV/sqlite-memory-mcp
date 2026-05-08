"""Phase 0.5 reflect_audit — read-only deterministic memory consolidation report.

Finds candidate consolidation actions (duplicates, stale items, orphans, etc.)
without applying any mutations. Pure SQL, no LLM, no API calls.

This is the "Extract" stage of the 5-stage Reviewable Memory Consolidation
pipeline (Ingest → Extract → Reconcile → Review → Apply). Subsequent phases
will add session-source ingestion plus review/apply tooling. MVP excludes
hard-delete; archive/supersede preserves provenance.

Spec source: notes 0ea75f2a (Reviewable Memory Consolidation Runs, 2026-05-08)
and 5a4be019 (Memory Reflection v0.7.0 spec).
"""

from __future__ import annotations

import sqlite3
from typing import Any


_HIDDEN_STATUSES = ("archived", "cancelled", "done")
_HIDDEN_PH = ",".join("?" * len(_HIDDEN_STATUSES))

_AUDIT_VERSION = "reflect_audit_v0.5_dry_run"


def audit_reflection_candidates(
    conn: sqlite3.Connection,
    project: str | None = None,
    stale_days: int = 60,
    abandoned_inbox_days: int = 30,
    limit_per_category: int = 20,
) -> dict[str, Any]:
    """Return read-only audit of consolidation candidates.

    Detects six categories without mutating the DB:
      1. exact_duplicate_titles — same lowercased title in same project, multiple active rows
      2. stale_overdue_tasks — not_started past due_date by stale_days
      3. empty_description_notes — type='note' with empty/whitespace description
      4. orphan_parent_tasks — parent_id references a missing row
      5. abandoned_inbox_items — section='inbox' not_started, untouched > abandoned_inbox_days
      6. entities_no_observations — entities with zero observations

    Each candidate carries enough evidence (ids, timestamps, suggested_action)
    that a downstream reviewer can decide merge/archive/supersede without
    re-querying. Returned as a plain dict; no JSON serialization here so
    callers can post-process before encoding.
    """
    project_clause = "AND project = ?" if project else ""
    project_clause_t1 = "AND t1.project = ?" if project else ""
    project_args: tuple = (project,) if project else ()

    candidates: dict[str, list[dict[str, Any]]] = {}

    # 1. Exact duplicate titles within same project (active only)
    dup_rows = conn.execute(
        f"""
        SELECT
          LOWER(TRIM(title)) AS title_key,
          COALESCE(project, '') AS project_key,
          COUNT(*) AS dup_count,
          GROUP_CONCAT(id, '|') AS ids,
          GROUP_CONCAT(title, '|') AS titles,
          MIN(created_at) AS first_seen,
          MAX(updated_at) AS last_touched
        FROM tasks
        WHERE status NOT IN ({_HIDDEN_PH})
          AND TRIM(title) != ''
          {project_clause}
        GROUP BY title_key, project_key
        HAVING COUNT(*) > 1
        ORDER BY dup_count DESC, last_touched DESC
        LIMIT ?
        """,
        (*_HIDDEN_STATUSES, *project_args, limit_per_category),
    ).fetchall()
    candidates["exact_duplicate_titles"] = [
        {
            "title_key": r["title_key"],
            "project": r["project_key"] or None,
            "duplicate_count": r["dup_count"],
            "task_ids": r["ids"].split("|") if r["ids"] else [],
            "raw_titles": r["titles"].split("|") if r["titles"] else [],
            "first_seen": r["first_seen"],
            "last_touched": r["last_touched"],
            "suggested_action": "merge_or_archive",
        }
        for r in dup_rows
    ]

    # 2. Stale overdue not_started tasks
    stale_rows = conn.execute(
        f"""
        SELECT id, title, due_date, project, updated_at, priority, section
        FROM tasks
        WHERE status = 'not_started'
          AND due_date IS NOT NULL
          AND TRIM(due_date) != ''
          AND date(due_date) < date('now', ?)
          {project_clause}
        ORDER BY due_date ASC
        LIMIT ?
        """,
        (f"-{stale_days} days", *project_args, limit_per_category),
    ).fetchall()
    candidates["stale_overdue_tasks"] = [
        {
            "id": r["id"],
            "title": r["title"],
            "due_date": r["due_date"],
            "project": r["project"],
            "section": r["section"],
            "priority": r["priority"],
            "last_touched": r["updated_at"],
            "suggested_action": "archive_or_reschedule",
        }
        for r in stale_rows
    ]

    # 3. Notes with empty/whitespace descriptions
    empty_rows = conn.execute(
        f"""
        SELECT id, title, project, updated_at
        FROM tasks
        WHERE type = 'note'
          AND (description IS NULL OR LENGTH(TRIM(description)) = 0)
          AND status NOT IN ({_HIDDEN_PH})
          {project_clause}
        ORDER BY updated_at DESC
        LIMIT ?
        """,
        (*_HIDDEN_STATUSES, *project_args, limit_per_category),
    ).fetchall()
    candidates["empty_description_notes"] = [
        {
            "id": r["id"],
            "title": r["title"],
            "project": r["project"],
            "last_touched": r["updated_at"],
            "suggested_action": "fill_or_archive",
        }
        for r in empty_rows
    ]

    # 4. Orphan parent_id (parent doesn't exist)
    orphan_rows = conn.execute(
        f"""
        SELECT t1.id, t1.title, t1.parent_id, t1.project, t1.updated_at
        FROM tasks t1
        LEFT JOIN tasks t2 ON t1.parent_id = t2.id
        WHERE t1.parent_id IS NOT NULL
          AND t2.id IS NULL
          AND t1.status NOT IN ({_HIDDEN_PH})
          {project_clause_t1}
        ORDER BY t1.updated_at DESC
        LIMIT ?
        """,
        (*_HIDDEN_STATUSES, *project_args, limit_per_category),
    ).fetchall()
    candidates["orphan_parent_tasks"] = [
        {
            "id": r["id"],
            "title": r["title"],
            "missing_parent_id": r["parent_id"],
            "project": r["project"],
            "last_touched": r["updated_at"],
            "suggested_action": "clear_parent_or_relink",
        }
        for r in orphan_rows
    ]

    # 5. Abandoned inbox items (untouched in N days)
    abandoned_rows = conn.execute(
        f"""
        SELECT id, title, project, created_at, updated_at, priority
        FROM tasks
        WHERE section = 'inbox'
          AND status = 'not_started'
          AND datetime(updated_at) < datetime('now', ?)
          {project_clause}
        ORDER BY updated_at ASC
        LIMIT ?
        """,
        (f"-{abandoned_inbox_days} days", *project_args, limit_per_category),
    ).fetchall()
    candidates["abandoned_inbox_items"] = [
        {
            "id": r["id"],
            "title": r["title"],
            "project": r["project"],
            "priority": r["priority"],
            "created_at": r["created_at"],
            "last_touched": r["updated_at"],
            "suggested_action": "promote_to_section_or_archive",
        }
        for r in abandoned_rows
    ]

    # 6. Entities with no observations
    entity_rows = conn.execute(
        """
        SELECT e.id, e.name, e.entity_type, e.project, e.updated_at
        FROM entities e
        LEFT JOIN observations o ON o.entity_id = e.id
        WHERE o.id IS NULL
        ORDER BY e.updated_at DESC
        LIMIT ?
        """,
        (limit_per_category,),
    ).fetchall()
    candidates["entities_no_observations"] = [
        {
            "id": r["id"],
            "name": r["name"],
            "entity_type": r["entity_type"],
            "project": r["project"],
            "last_touched": r["updated_at"],
            "suggested_action": "add_observations_or_archive",
        }
        for r in entity_rows
    ]

    summary = {
        "total_candidates": sum(len(v) for v in candidates.values()),
        "by_category": {k: len(v) for k, v in candidates.items()},
        "applied_filters": {
            "project": project,
            "stale_days": stale_days,
            "abandoned_inbox_days": abandoned_inbox_days,
            "limit_per_category": limit_per_category,
        },
    }

    return {
        "version": _AUDIT_VERSION,
        "summary": summary,
        "candidates": candidates,
    }


def format_audit_markdown(report: dict[str, Any], examples_per_category: int = 5) -> str:
    """Render audit report as human-readable markdown for review."""
    summary = report.get("summary", {})
    candidates = report.get("candidates", {})
    by_cat = summary.get("by_category", {})

    lines: list[str] = [
        f"# Reflection Audit ({report.get('version', 'unknown')})",
        "",
        f"**Total candidates:** {summary.get('total_candidates', 0)}",
        "",
        "## Counts per category",
        "",
        "| Category | Count |",
        "|---|---:|",
    ]
    for cat, count in by_cat.items():
        lines.append(f"| {cat.replace('_', ' ').title()} | {count} |")

    lines.extend(["", "## Top candidates per category", ""])

    for cat, items in candidates.items():
        if not items:
            continue
        lines.append(
            f"### {cat.replace('_', ' ').title()} "
            f"(showing {min(examples_per_category, len(items))} of {len(items)})"
        )
        lines.append("")
        for item in items[:examples_per_category]:
            label = item.get("title") or item.get("name") or item.get("title_key", "?")
            lines.append(f"- **{label}**")
            details: list[str] = []
            for key in (
                "id",
                "project",
                "due_date",
                "priority",
                "duplicate_count",
                "task_ids",
                "missing_parent_id",
                "last_touched",
                "suggested_action",
            ):
                if key in item and item[key] not in (None, "", []):
                    val = item[key]
                    if isinstance(val, list):
                        val = ",".join(str(x) for x in val[:3])
                        if len(item[key]) > 3:
                            val += f"…(+{len(item[key]) - 3})"
                    details.append(f"`{key}`={val}")
            lines.append(f"  - {' | '.join(details)}")
        lines.append("")

    return "\n".join(lines)
