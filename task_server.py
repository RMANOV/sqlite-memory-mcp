#!/usr/bin/env python3
"""Thin MCP server exposing only task management tools.

Shares the same SQLite database as the main sqlite-kb server.
Exists because Claude Code 2.x has a tool-count limit per MCP server
(~9 tools visible out of 50), so task tools are split into a separate server.
"""

from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path
from typing import Any

from fastmcp import FastMCP

from db_utils import (
    get_conn as _get_conn,
    TaskDAO,
    TASK_ACTIVE_EXCLUSIONS as _TASK_ACTIVE_EXCLUSIONS,
    TASK_PRIORITIES as _TASK_PRIORITIES,
    MERGEABLE_FIELDS as _MERGEABLE_FIELDS,
    validate_task_fields as _validate_task_fields,
    build_priority_order_sql,
    now_iso as _now,
    upsert_field_versions as _upsert_field_versions,
)

# Pre-built SQL for active-task exclusion
_EXCL_PH = ",".join("?" for _ in _TASK_ACTIVE_EXCLUSIONS)

# ── Logging (file-only, NEVER stdout — breaks MCP stdio) ────────────────
LOG_PATH = Path.home() / ".claude" / "memory" / "task_server.log"
LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
logger = logging.getLogger("sqlite-tasks")
logger.setLevel(logging.DEBUG)
_fh = logging.FileHandler(LOG_PATH, encoding="utf-8")
_fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
if not logger.handlers:
    logger.addHandler(_fh)

# ── Unified search engine (shared across query_tasks calls) ──────────────
from task_search import TaskSearchEngine

_search_engine = TaskSearchEngine()


def _vec_sync_task_safe(conn, task_id: str) -> None:
    """Sync task embedding, swallowing errors for graceful degradation."""
    try:
        from vec_search import vec_sync_task

        vec_sync_task(conn, task_id)
    except Exception as e:
        logger.debug("vec_sync_task(%s) skipped: %s", task_id, e)


# ── FastMCP app ──────────────────────────────────────────────────────────

mcp = FastMCP(
    "sqlite-tasks",
    instructions=(
        "Task management tools for SQLite-backed persistent memory. "
        "Create, update, query, and digest tasks. Shares DB with sqlite-kb."
    ),
)


# ═══════════════════════════════════════════════════════════════════════════
# Tool 1: create_task
# ═══════════════════════════════════════════════════════════════════════════
@mcp.tool()
def create_task(
    title: str,
    type: str = "task",
    description: str = "",
    section: str = "inbox",
    priority: str = "medium",
    due_date: str = "",
    project: str = "",
    parent_id: str = "",
    notes: str = "",
    recurring: str = "",
    reminder_at: str = "",
) -> str:
    """Create a new task or note. Returns the UUID.

    Args:
        title: Task title (required).
        type: task | note.
        description: Task description/details.
        section: inbox | today | next | someday | waiting.
        priority: low | medium | high | critical.
        due_date: YYYY-MM-DD format or empty to skip.
        project: Project tag for grouping.
        parent_id: UUID of parent task (for subtasks).
        notes: Freeform notes.
        recurring: JSON config for recurrence (e.g. '{"every":"week","day":"monday"}').
        reminder_at: ISO datetime for reminder (e.g. '2026-03-15T14:00:00').
    """
    # Normalize empty strings to None
    description = description or None
    due_date = due_date or None
    project = project or None
    parent_id = parent_id or None
    notes = notes or None
    recurring = recurring or None
    reminder_at = reminder_at or None

    task_id = str(uuid.uuid4())
    now = _now()

    if err := _validate_task_fields(
        section=section,
        priority=priority,
        type=type,
        due_date=due_date,
        recurring=recurring,
        reminder_at=reminder_at,
    ):
        return json.dumps({"error": err})

    with _get_conn() as conn:
        if parent_id:
            if not TaskDAO.exists(conn, parent_id):
                return json.dumps({"error": f"Parent task {parent_id} not found"})
        TaskDAO.create(
            conn,
            task_id,
            title,
            now,
            description=description,
            priority=priority,
            section=section,
            due_date=due_date,
            project=project,
            parent_id=parent_id,
            notes=notes,
            recurring=recurring,
            reminder_at=reminder_at,
            type=type,
        )
        _upsert_field_versions(conn, task_id, _MERGEABLE_FIELDS, now)
        _vec_sync_task_safe(conn, task_id)

    logger.info("create_task: %s (%s)", title, task_id)
    return json.dumps(
        {"task_id": task_id, "title": title, "type": type, "status": "not_started"}
    )


# ═══════════════════════════════════════════════════════════════════════════
# Tool 2: update_task
# ═══════════════════════════════════════════════════════════════════════════
@mcp.tool()
def update_task(
    task_id: str,
    title: str = "",
    description: str = "",
    status: str = "",
    priority: str = "",
    section: str = "",
    due_date: str = "",
    project: str = "",
    parent_id: str = "",
    notes: str = "",
    recurring: str = "",
    reminder_at: str = "",
    type: str = "",
) -> str:
    """Update a task's fields. Only non-empty fields are changed.

    Pass special value "CLEAR" to set a field to NULL.

    Args:
        task_id: UUID of the task to update (required).
        title: New title.
        description: New description.
        status: not_started | in_progress | done | archived | cancelled.
        priority: low | medium | high | critical.
        section: inbox | today | next | someday | waiting.
        due_date: YYYY-MM-DD or "CLEAR" to remove.
        project: Project tag or "CLEAR" to remove.
        parent_id: Parent UUID or "CLEAR" to remove.
        notes: New notes or "CLEAR" to remove.
        recurring: JSON config or "CLEAR" to remove.
        reminder_at: ISO datetime or "CLEAR" to remove.
        type: task | note.
    """
    fields = {
        "title": title,
        "description": description,
        "status": status,
        "priority": priority,
        "section": section,
        "due_date": due_date,
        "project": project,
        "parent_id": parent_id,
        "notes": notes,
        "recurring": recurring,
        "reminder_at": reminder_at,
        "type": type,
    }
    updates = {}
    for k, v in fields.items():
        if v == "CLEAR":
            updates[k] = None
        elif v:  # non-empty string = update
            updates[k] = v
    if not updates:
        return json.dumps({"error": "No fields to update. Pass non-empty values."})

    val_fields = {
        k: v
        for k, v in updates.items()
        if k
        in (
            "status",
            "section",
            "priority",
            "type",
            "due_date",
            "recurring",
            "reminder_at",
        )
        and v is not None
    }
    if err := _validate_task_fields(**val_fields):
        return json.dumps({"error": err})

    updates["updated_at"] = _now()

    with _get_conn() as conn:
        if TaskDAO.update(conn, task_id, updates) == 0:
            return json.dumps({"error": f"Task {task_id} not found"})
        changed = [k for k in updates if k != "updated_at"]
        _upsert_field_versions(conn, task_id, changed, updates["updated_at"])
        # Re-embed if content fields changed
        if {"title", "description", "notes"} & set(changed):
            _vec_sync_task_safe(conn, task_id)

    logger.info("update_task: %s updated %s", task_id, list(updates.keys()))
    return json.dumps({"updated": task_id, "fields": list(updates.keys())})


# ═══════════════════════════════════════════════════════════════════════════
# Tool 3: query_tasks
# ═══════════════════════════════════════════════════════════════════════════
@mcp.tool()
def query_tasks(
    section: str = "",
    status: str = "",
    priority: str = "",
    project: str = "",
    parent_id: str = "",
    type: str = "",
    overdue_only: bool = False,
    search: str = "",
    summary_only: bool = False,
    offset: int = 0,
    limit: int = 50,
) -> str:
    """Query tasks with optional filters. Returns markdown table.

    Filters are combined with AND. Leave empty to skip a filter.
    overdue_only=True shows only tasks past due_date.
    search: full-text search across title, description, notes.
    summary_only=True omits description/notes (faster).
    """
    conditions: list[str] = []
    params: list[Any] = []

    if section:
        conditions.append("t.section = ?")
        params.append(section)
    if status:
        conditions.append("t.status = ?")
        params.append(status)
    if priority:
        conditions.append("t.priority = ?")
        params.append(priority)
    if project:
        conditions.append("t.project = ?")
        params.append(project)
    if parent_id:
        conditions.append("t.parent_id = ?")
        params.append(parent_id)
    if type:
        conditions.append("t.type = ?")
        params.append(type)
    if overdue_only:
        conditions.append("t.due_date < date('now')")
        conditions.append(f"t.status NOT IN ({_EXCL_PH})")
        params.extend(_TASK_ACTIVE_EXCLUSIONS)

    if summary_only:
        cols = "t.id, t.title, t.status, t.priority, t.section, t.due_date, t.project, t.parent_id"
    else:
        cols = "t.id, t.title, t.description, t.notes, t.status, t.priority, t.section, t.due_date, t.project, t.parent_id"

    from_clause = "tasks t"
    order_clause = (
        f"{build_priority_order_sql('t.')}, t.due_date ASC NULLS LAST, t.created_at ASC"
    )

    where = " AND ".join(conditions) if conditions else "1=1"
    sql = (
        f"SELECT {cols} FROM {from_clause} WHERE {where} "
        f"ORDER BY {order_clause} "
        f"LIMIT ? OFFSET ?"
    )
    params.extend([limit, offset])

    with _get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
        count_params = params[:-2]
        count_sql = f"SELECT COUNT(*) FROM {from_clause} WHERE {where}"
        total = conn.execute(count_sql, count_params).fetchone()[0]

        # Unified search: route through TaskSearchEngine (FTS5 + vector + RRF)
        if search and rows:
            filtered_tasks = [dict(r) for r in rows]
            results = _search_engine.search(search, filtered_tasks, conn=conn)
            rows = results
            total = len(results)

    rows = [dict(r) if not isinstance(r, dict) else r for r in rows] if rows else []

    if not rows:
        return json.dumps(
            {"tasks": [], "count": 0, "total": total, "message": "No tasks match"}
        )

    lines = [
        "| # | Title | Status | Priority | Section | Due | Project | Notes |",
        "|---|-------|--------|----------|---------|-----|---------|-------|",
    ]
    for i, r in enumerate(rows, 1):
        due = r["due_date"] or "—"
        proj = r["project"] or "—"
        notes = (r.get("notes") or "—")[:80]
        lines.append(
            f"| {i + offset} | {r['title']} | {r['status']} | {r['priority']} "
            f"| {r['section']} | {due} | {proj} | {notes} |"
        )

    result = {
        "tasks": [dict(r) for r in rows],
        "count": len(rows),
        "total": total,
        "offset": offset,
        "limit": limit,
        "markdown": "\n".join(lines),
    }
    if total > offset + limit:
        result["has_more"] = True
        result["next_offset"] = offset + limit
    return json.dumps(result)


# ═══════════════════════════════════════════════════════════════════════════
# Tool 4: task_digest
# ═══════════════════════════════════════════════════════════════════════════
@mcp.tool()
def task_digest(
    include_overdue: bool = True,
    limit: int = 20,
) -> str:
    """Generate a formatted task digest for session start.

    Shows pending/in-progress tasks grouped by section,
    plus overdue tasks highlighted separately.
    """
    target_sections = ["today", "inbox", "next"]

    with _get_conn() as conn:
        ph = ",".join("?" * len(target_sections))
        active = conn.execute(
            f"SELECT id, title, description, notes, status, priority, section, due_date, project "
            f"FROM tasks "
            f"WHERE section IN ({ph}) AND status IN ('not_started', 'in_progress') AND type = 'task' "
            f"ORDER BY CASE section WHEN 'today' THEN 0 WHEN 'inbox' THEN 1 "
            f"WHEN 'next' THEN 2 WHEN 'waiting' THEN 3 WHEN 'someday' THEN 4 END, "
            f"{build_priority_order_sql()} LIMIT ?",
            target_sections + [limit],
        ).fetchall()

        overdue = []
        if include_overdue:
            overdue = conn.execute(
                "SELECT id, title, description, notes, status, priority, section, due_date, project "
                "FROM tasks "
                f"WHERE due_date < date('now') AND status NOT IN ({_EXCL_PH}) AND type = 'task' "
                "ORDER BY due_date ASC LIMIT 10",
                list(_TASK_ACTIVE_EXCLUSIONS),
            ).fetchall()

        counts = conn.execute(
            "SELECT status, COUNT(*) as cnt FROM tasks "
            "WHERE status NOT IN ('archived', 'cancelled') GROUP BY status"
        ).fetchall()

    lines = ["## Task Digest"]
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
            note_hint = f" | {t['notes'][:60]}..." if t.get("notes") else ""
            lines.append(
                f"- [{t['priority'].upper()}] {t['title']} (due: {t['due_date']}){note_hint}"
            )
        lines.append("")

    by_section: dict[str, list] = {}
    for t in active:
        by_section.setdefault(t["section"], []).append(t)

    for sec in target_sections:
        tasks = by_section.get(sec, [])
        if tasks:
            lines.append(f"### {sec.upper()} ({len(tasks)})")
            for t in tasks:
                due = f" [due: {t['due_date']}]" if t["due_date"] else ""
                prio = (
                    f"[{t['priority'].upper()}] " if t["priority"] != "medium" else ""
                )
                note_hint = f" | {t['notes'][:60]}..." if t.get("notes") else ""
                lines.append(f"- {prio}{t['title']}{due}{note_hint}")
            lines.append("")

    return json.dumps(
        {
            "digest": "\n".join(lines),
            "active_count": len(active),
            "overdue_count": len(overdue),
        }
    )


# ═══════════════════════════════════════════════════════════════════════════
# Tool 5: archive_done_tasks
# ═══════════════════════════════════════════════════════════════════════════
@mcp.tool()
def archive_done_tasks(older_than_days: int = 7) -> str:
    """Archive completed tasks older than N days.

    Moves tasks with status='done' and updated_at older than threshold to 'archived'.
    """
    if older_than_days < 0:
        return json.dumps({"error": "older_than_days must be non-negative"})

    with _get_conn() as conn:
        now = _now()
        affected_ids = TaskDAO.archive_done(conn, older_than_days)
        for tid in affected_ids:
            _upsert_field_versions(conn, tid, ("status",), now)

    logger.info(
        "archive_done_tasks: %d archived (older than %d days)",
        len(affected_ids),
        older_than_days,
    )
    return json.dumps(
        {"archived": len(affected_ids), "threshold_days": older_than_days}
    )


# ═══════════════════════════════════════════════════════════════════════════
# Tool 6: bump_overdue_priority
# ═══════════════════════════════════════════════════════════════════════════
@mcp.tool()
def bump_overdue_priority(target_priority: str = "high") -> str:
    """Bump priority of overdue tasks not done/archived.

    Only bumps tasks with priority lower than target.
    """
    if target_priority not in _TASK_PRIORITIES:
        return json.dumps({"error": f"Invalid priority: {target_priority}"})

    priority_rank = {p: i for i, p in enumerate(_TASK_PRIORITIES)}
    target_rank = priority_rank[target_priority]
    lower_priorities = [p for p, r in priority_rank.items() if r < target_rank]
    if not lower_priorities:
        return json.dumps({"bumped": 0, "message": "No lower priorities to bump"})

    ph = ",".join("?" * len(lower_priorities))
    now = _now()

    with _get_conn() as conn:
        affected = conn.execute(
            f"SELECT id FROM tasks "
            f"WHERE due_date < date('now') AND status NOT IN ({_EXCL_PH}) "
            f"AND priority IN ({ph})",
            list(_TASK_ACTIVE_EXCLUSIONS) + lower_priorities,
        ).fetchall()
        bumped = 0
        for row in affected:
            TaskDAO.update(
                conn, row["id"], {"priority": target_priority, "updated_at": now}
            )
            _upsert_field_versions(conn, row["id"], ("priority",), now)
            bumped += 1

    logger.info("bump_overdue_priority: %d bumped to %s", bumped, target_priority)
    return json.dumps({"bumped": bumped, "target_priority": target_priority})


# ── Entry point ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    mcp.run(transport="stdio")
