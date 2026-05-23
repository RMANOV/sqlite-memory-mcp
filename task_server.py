#!/usr/bin/env python3
"""Thin MCP server exposing only task management tools.

Shares the same SQLite database as the main sqlite-kb server.
Exists because Claude Code 2.x has a tool-count limit per MCP server
(~9 tools visible out of 50), so task tools are split into a separate server.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any

from fastmcp_compat import FastMCP

from db_utils import (
    get_conn as _get_conn,
    TaskDAO,
    TASK_ACTIVE_EXCLUSIONS as _TASK_ACTIVE_EXCLUSIONS,
    TASK_PRIORITIES as _TASK_PRIORITIES,
    validate_task_fields as _validate_task_fields,
    build_priority_order_sql,
    now_iso as _now,
    setup_logger,
    apply_task_mutation as _apply_task_mutation,
    create_task_with_ledger as _create_task_with_ledger,
)
from retrieval_contract import (
    RETRIEVAL_CONTRACT_VERSION,
    classify_lookup_confidence,
    is_visible_lookup_match,
    order_surface_hits,
    score_lookup_surface,
)
from premium_runtime import maybe_mount_premium_extensions
from task_search import TaskSearchEngine
from smart_retrieval import (
    READY_CONTEXT_CONTRACT_VERSION as _READY_CONTEXT_CONTRACT_VERSION,
    prime_context as _prime_context,
    ready_context as _ready_context,
    suggested_ready as _suggested_ready,
)

# Pre-built SQL for active-task exclusion
_EXCL_PH = ",".join("?" for _ in _TASK_ACTIVE_EXCLUSIONS)

# ── Logging (file-only, NEVER stdout — breaks MCP stdio) ────────────────

logger = setup_logger("sqlite-tasks", "task_server.log")

_search_engine: TaskSearchEngine | None = None


def _get_search_engine() -> TaskSearchEngine:
    global _search_engine
    if _search_engine is None:
        _search_engine = TaskSearchEngine()
    return _search_engine


def _vec_sync_task_safe(conn, task_id: str) -> None:
    """Sync task embedding, logging failures for graceful degradation."""
    try:
        from vec_search import vec_sync_task

        vec_sync_task(conn, task_id)
    except Exception as e:
        logger.debug("vec_sync_task(%s) skipped: %s", task_id, e, exc_info=True)


def _normalize_title_key(value: str | None) -> str:
    return " ".join((value or "").casefold().split())


def _find_note_by_title_project(
    conn, *, title: str, project: str | None
) -> dict[str, Any] | None:
    title_key = _normalize_title_key(title)
    if not title_key:
        return None
    if project is None:
        rows = conn.execute(
            "SELECT id, title, description, notes, status, section, priority, "
            "project, updated_at, created_at "
            "FROM tasks WHERE type = 'note' AND project IS NULL"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, title, description, notes, status, section, priority, "
            "project, updated_at, created_at "
            "FROM tasks WHERE type = 'note' AND project = ?",
            (project,),
        ).fetchall()
    for row in rows:
        if _normalize_title_key(row["title"]) == title_key:
            return dict(row)
    return None


# ── FastMCP app ──────────────────────────────────────────────────────────

mcp = FastMCP(
    "sqlite-tasks",
    instructions=(
        "Task management tools for SQLite-backed persistent memory. "
        "Create, update, query, and digest tasks. "
        "Use find_by_title when only a remembered phrase is known: it searches tasks, notes, "
        "and entities across title/name, description, notes, observations, and project "
        "regardless of status, section, or project filters, using retrieval contract "
        f"{RETRIEVAL_CONTRACT_VERSION} with confidence gating. "
        "Use upsert_note_by_title_project for idempotent research/decision notes "
        "when a repeated agent run must update an existing title/project instead of "
        "creating duplicates. "
        "Use description as the default primary body for task/note content; "
        "use notes only for auxiliary/internal metadata. Shares DB with sqlite-kb."
    ),
)


# ═══════════════════════════════════════════════════════════════════════════
# Tool 1: create_task_or_note
# ═══════════════════════════════════════════════════════════════════════════
@mcp.tool()
def create_task_or_note(
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

    Put the main long-form task/note text in ``description`` by default.
    Use ``notes`` only for auxiliary, internal, or machine-readable metadata.

    Args:
        title: Task title (required).
        type: task | note.
        description: Primary task/note body and main long-form content.
        section: inbox | today | next | someday | waiting.
        priority: low | medium | high | critical.
        due_date: YYYY-MM-DD format or empty to skip.
        project: Project tag for grouping.
        parent_id: UUID of parent task (for subtasks).
        notes: Secondary/internal notes or machine-readable metadata.
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
        _create_task_with_ledger(
            conn,
            task_id,
            title,
            now,
            description=description,
            status="not_started",
            priority=priority,
            section=section,
            due_date=due_date,
            project=project,
            parent_id=parent_id,
            notes=notes,
            recurring=recurring,
            reminder_at=reminder_at,
            type=type,
            tool_name="sqlite-tasks.create_task_or_note",
        )
        _vec_sync_task_safe(conn, task_id)

    logger.info("create_task_or_note: %s (%s)", title, task_id)
    return json.dumps(
        {"task_id": task_id, "title": title, "type": type, "status": "not_started"}
    )


# ═══════════════════════════════════════════════════════════════════════════
# Tool 1b: upsert_note_by_title_project
# ═══════════════════════════════════════════════════════════════════════════
@mcp.tool()
def upsert_note_by_title_project(
    title: str,
    project: str = "",
    description: str = "",
    notes: str = "",
    section: str = "",
    priority: str = "",
    update_if_found: bool = True,
) -> str:
    """Create or update a note by exact normalized title + project.

    This is the idempotent write surface for durable research/decision notes.
    It prevents repeated agent retries from creating near-duplicate note rows.

    Matching rule:
    - exact normalized title + project;
    - no fuzzy overwrite;
    - updates go through the task mutation ledger.

    Args:
        title: Note title (required).
        project: Project tag for matching and grouping.
        description: Primary long-form note body.
        notes: Optional auxiliary/internal metadata.
        section: Section for new notes; updates only when explicitly set.
        priority: Priority for new notes; updates only when explicitly set.
        update_if_found: When false, return the existing row without mutation.
    """
    title = (title or "").strip()
    if not title:
        return json.dumps({"error": "title is required"})

    project_value = project.strip() if project else None
    create_section = section or "next"
    create_priority = priority or "medium"
    if err := _validate_task_fields(
        section=create_section,
        priority=create_priority,
        type="note",
    ):
        return json.dumps({"error": err})
    update_validation = {
        key: value
        for key, value in {"section": section, "priority": priority}.items()
        if value
    }
    if update_validation and (err := _validate_task_fields(**update_validation)):
        return json.dumps({"error": err})

    now = _now()
    with _get_conn() as conn:
        existing = _find_note_by_title_project(
            conn, title=title, project=project_value
        )
        if existing:
            if not update_if_found:
                return json.dumps(
                    {
                        "task_id": existing["id"],
                        "title": existing["title"],
                        "type": "note",
                        "action": "existing",
                        "matched_on": "normalized_title_project",
                    }
                )

            updates: dict[str, Any] = {}
            if description:
                updates["description"] = description
            if notes:
                updates["notes"] = notes
            if section:
                updates["section"] = section
            if priority:
                updates["priority"] = priority

            if not updates:
                return json.dumps(
                    {
                        "task_id": existing["id"],
                        "title": existing["title"],
                        "type": "note",
                        "action": "existing",
                        "matched_on": "normalized_title_project",
                    }
                )

            result = _apply_task_mutation(
                conn,
                existing["id"],
                updates,
                timestamp=now,
                tool_name="sqlite-tasks.upsert_note_by_title_project",
            )
            if {"title", "description", "notes"} & set(
                result.get("changed_fields", ())
            ):
                _vec_sync_task_safe(conn, existing["id"])
            return json.dumps(
                {
                    "task_id": existing["id"],
                    "title": title,
                    "type": "note",
                    "action": "updated",
                    "fields": result.get("changed_fields", []),
                    "matched_on": "normalized_title_project",
                }
            )

        task_id = str(uuid.uuid4())
        _create_task_with_ledger(
            conn,
            task_id,
            title,
            now,
            description=description or None,
            status="not_started",
            priority=create_priority,
            section=create_section,
            project=project_value,
            notes=notes or None,
            type="note",
            tool_name="sqlite-tasks.upsert_note_by_title_project",
        )
        _vec_sync_task_safe(conn, task_id)

    logger.info("upsert_note_by_title_project: %s (%s)", title, task_id)
    return json.dumps(
        {
            "task_id": task_id,
            "title": title,
            "type": "note",
            "status": "not_started",
            "action": "created",
            "matched_on": "normalized_title_project",
        }
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

    ``description`` is the primary body field for task/note text.
    ``notes`` is reserved for secondary/internal metadata.

    Args:
        task_id: UUID of the task to update (required).
        title: New title.
        description: New main task/note body.
        status: not_started | in_progress | done | archived | cancelled.
        priority: low | medium | high | critical.
        section: inbox | today | next | someday | waiting.
        due_date: YYYY-MM-DD or "CLEAR" to remove.
        project: Project tag or "CLEAR" to remove.
        parent_id: Parent UUID or "CLEAR" to remove.
        notes: New auxiliary/internal notes or "CLEAR" to remove.
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
        result = _apply_task_mutation(
            conn,
            task_id,
            {k: v for k, v in updates.items() if k != "updated_at"},
            timestamp=updates["updated_at"],
            tool_name="sqlite-tasks.update_task",
        )
        if result.get("updated", 0) == 0 and result.get("missing"):
            return json.dumps({"error": f"Task {task_id} not found"})
        # Re-embed if content fields changed
        if {"title", "description", "notes"} & set(result.get("changed_fields", ())):
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
        cols = "t.id, t.title, t.status, t.priority, t.section, t.due_date, t.project, t.parent_id, t.notes, t.created_at, t.updated_at"
    else:
        cols = "t.id, t.title, t.description, t.notes, t.status, t.priority, t.section, t.due_date, t.project, t.parent_id, t.created_at, t.updated_at"

    from_clause = "tasks t"
    order_clause = (
        f"{build_priority_order_sql('t.')}, t.due_date ASC NULLS LAST, t.created_at ASC"
    )

    where = " AND ".join(conditions) if conditions else "1=1"

    with _get_conn() as conn:
        if search:
            # FTS5 pre-filter: narrow rows before search engine re-ranks
            fts_tokens = search.split()
            fts_where = where
            fts_params = list(params)
            if fts_tokens:
                # Prefix match (token*) so pre-filter is broader than
                # the search engine's fuzzy/substring matching
                escaped = []
                for t in fts_tokens:
                    clean = "".join(c for c in t if c.isalnum() or c == "_")
                    if clean:
                        escaped.append(clean + "*")
                fts_match = " OR ".join(escaped) if escaped else None
                if fts_match:
                    try:
                        # Verify FTS5 query is valid before using it
                        conn.execute(
                            "SELECT 1 FROM tasks_fts WHERE tasks_fts MATCH ? LIMIT 1",
                            (fts_match,),
                        )
                        fts_where = (
                            f"{where} AND t.rowid IN "
                            f"(SELECT rowid FROM tasks_fts WHERE tasks_fts MATCH ?)"
                        )
                        fts_params.append(fts_match)
                    except sqlite3.Error as e:
                        logger.debug(
                            "Task FTS5 prefilter failed for %r: %s",
                            search,
                            e,
                            exc_info=True,
                        )

            sql = (
                f"SELECT {cols} FROM {from_clause} WHERE {fts_where} "
                f"ORDER BY {order_clause}"
            )
            all_rows = conn.execute(sql, fts_params).fetchall()
            results = _get_search_engine().search(
                search, [dict(r) for r in all_rows], conn=conn
            )
            total = len(results)
            rows = results[offset : offset + limit]
        else:
            sql = (
                f"SELECT {cols} FROM {from_clause} WHERE {where} "
                f"ORDER BY {order_clause} "
                f"LIMIT ? OFFSET ?"
            )
            rows = conn.execute(sql, params + [limit, offset]).fetchall()
            count_sql = f"SELECT COUNT(*) FROM {from_clause} WHERE {where}"
            total = conn.execute(count_sql, params).fetchone()[0]

    rows = [dict(r) if not isinstance(r, dict) else r for r in rows] if rows else []

    if not rows:
        return json.dumps(
            {"tasks": [], "count": 0, "total": total, "message": "No tasks match"}
        )

    lines = [
        "| # | Title | Status | Priority | Section | Due | Project | Created | Notes |",
        "|---|-------|--------|----------|---------|-----|---------|---------|-------|",
    ]
    for i, r in enumerate(rows, 1):
        due = r["due_date"] or "—"
        proj = r["project"] or "—"
        created = (r.get("created_at") or "—")[:16]
        notes = (r["notes"] or "—")[:80]
        lines.append(
            f"| {i + offset} | {r['title']} | {r['status']} | {r['priority']} "
            f"| {r['section']} | {due} | {proj} | {created} | {notes} |"
        )

    result = {
        "tasks": rows,
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
# Tool 3b: find_by_title
# ═══════════════════════════════════════════════════════════════════════════
@mcp.tool()
def find_by_title(title_fragment: str, limit: int = 20) -> str:
    """Find tasks, notes, or entities by partial title or remembered phrase.

    This is the cross-surface lookup tool when the caller only remembers a
    phrase, not the storage surface. It searches across task titles,
    descriptions, notes, projects, entity names, and entity observations.
    It ignores task status, section, type, and project filters.

    Args:
        title_fragment: Any distinctive substring or remembered phrase.
        limit: Maximum number of matches to return.
    """
    query = (title_fragment or "").strip()
    if not query:
        return json.dumps(
            {"matches": [], "count": 0, "message": "Empty title fragment"}
        )

    limit = max(1, min(int(limit), 100))
    matches: list[dict[str, Any]] = []

    with _get_conn() as conn:
        task_rows = conn.execute(
            "SELECT id, title, description, notes, type, status, section, priority, due_date, project, updated_at, created_at "
            "FROM tasks"
        ).fetchall()
        for row in task_rows:
            title = row["title"] or ""
            surface_scores = {
                surface: score
                for surface, score in (
                    ("title", score_lookup_surface("title", query, title)),
                    (
                        "description",
                        score_lookup_surface("description", query, row["description"]),
                    ),
                    ("notes", score_lookup_surface("notes", query, row["notes"])),
                    ("project", score_lookup_surface("project", query, row["project"])),
                )
                if score > 0
            }
            if not surface_scores:
                continue
            ordered_hits = order_surface_hits(surface_scores)
            matched_in = [surface for surface, _ in ordered_hits]
            score = float(ordered_hits[0][1])
            confidence = classify_lookup_confidence(surface_scores)
            kind = "note" if row["type"] == "note" else "task"
            matches.append(
                {
                    "kind": kind,
                    "id": row["id"],
                    "title": title,
                    "type": row["type"],
                    "status": row["status"],
                    "section": row["section"],
                    "priority": row["priority"],
                    "due_date": row["due_date"],
                    "project": row["project"],
                    "updated_at": row["updated_at"],
                    "created_at": row["created_at"],
                    "score": score,
                    "matched_in": matched_in,
                    "surface_scores": surface_scores,
                    "primary_surface": matched_in[0],
                    "confidence": confidence,
                    "ranking_contract_version": RETRIEVAL_CONTRACT_VERSION,
                }
            )

        obs_by_entity: dict[int, list[str]] = {}
        for row in conn.execute(
            "SELECT entity_id, content FROM observations ORDER BY entity_id, id"
        ):
            obs_by_entity.setdefault(row["entity_id"], []).append(row["content"])
        entity_rows = conn.execute(
            "SELECT id, name, entity_type, project, updated_at, created_at FROM entities"
        ).fetchall()
        for row in entity_rows:
            name = row["name"] or ""
            obs_text = "\n".join(obs_by_entity.get(row["id"], []))
            surface_scores = {
                surface: score
                for surface, score in (
                    ("name", score_lookup_surface("name", query, name)),
                    (
                        "observations",
                        score_lookup_surface("observations", query, obs_text),
                    ),
                    ("project", score_lookup_surface("project", query, row["project"])),
                    (
                        "entity_type",
                        score_lookup_surface("entity_type", query, row["entity_type"]),
                    ),
                )
                if score > 0
            }
            if not surface_scores:
                continue
            ordered_hits = order_surface_hits(surface_scores)
            matched_in = [surface for surface, _ in ordered_hits]
            score = float(ordered_hits[0][1])
            confidence = classify_lookup_confidence(surface_scores)
            matches.append(
                {
                    "kind": "entity",
                    "id": row["id"],
                    "title": name,
                    "entityType": row["entity_type"],
                    "project": row["project"],
                    "updated_at": row["updated_at"],
                    "created_at": row["created_at"],
                    "score": score,
                    "matched_in": matched_in,
                    "surface_scores": surface_scores,
                    "primary_surface": matched_in[0],
                    "confidence": confidence,
                    "ranking_contract_version": RETRIEVAL_CONTRACT_VERSION,
                }
            )

    matches.sort(
        key=lambda item: (
            float(item.get("score") or 0.0),
            1 if item.get("kind") in {"task", "note"} else 0,
            item.get("updated_at") or "",
            item.get("created_at") or "",
        ),
        reverse=True,
    )
    confident_matches = [
        item
        for item in matches
        if is_visible_lookup_match(item.get("surface_scores") or {})
    ]
    hidden_low_confidence = (
        len(matches) - len(confident_matches) if confident_matches else 0
    )
    visible_matches = confident_matches if confident_matches else matches
    matches = visible_matches[:limit]

    lines = [
        "| # | Kind | Title | Status/Type | Section | Project | Match | Confidence |",
        "|---|------|-------|-------------|---------|---------|-------|------------|",
    ]
    for idx, item in enumerate(matches, 1):
        if item["kind"] == "entity":
            status_or_type = item.get("entityType") or "entity"
            section = "—"
        else:
            status_or_type = (
                f"{item.get('status') or '—'} / {item.get('type') or 'task'}"
            )
            section = item.get("section") or "—"
        lines.append(
            f"| {idx} | {item['kind']} | {item['title']} | {status_or_type} | "
            f"{section} | {item.get('project') or '—'} | {', '.join(item.get('matched_in') or []) or '—'} | "
            f"{item.get('confidence') or 'low'} |"
        )

    return json.dumps(
        {
            "matches": matches,
            "count": len(matches),
            "query": query,
            "hidden_low_confidence_count": hidden_low_confidence,
            "ranking_contract_version": RETRIEVAL_CONTRACT_VERSION,
            "markdown": "\n".join(lines) if matches else "",
            "message": None if matches else "No title matches",
        }
    )


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
            note_hint = f" | {t['notes'][:60]}..." if t["notes"] else ""
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
                note_hint = f" | {t['notes'][:60]}..." if t["notes"] else ""
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
        affected_ids = TaskDAO.archive_done(conn, older_than_days)

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
            result = _apply_task_mutation(
                conn,
                row["id"],
                {"priority": target_priority},
                timestamp=now,
                tool_name="sqlite-tasks.bump_overdue_priority",
            )
            if result.get("updated", 0):
                bumped += 1

    logger.info("bump_overdue_priority: %d bumped to %s", bumped, target_priority)
    return json.dumps({"bumped": bumped, "target_priority": target_priority})


def _strip_ready_task_payload(record: dict[str, Any]) -> dict[str, Any]:
    out = dict(record)
    out.pop("task", None)
    return out


# ═══════════════════════════════════════════════════════════════════════════
# Tool 8: ready_context
# ═══════════════════════════════════════════════════════════════════════════
@mcp.tool()
def ready_context(
    mode: str = "ready",
    include_readings: bool = False,
    limit: int = 12,
) -> str:
    """Return deterministic ready/prime context with reasons and provenance.

    This is sqlite-memory-mcp's cross-project/cross-machine answer to Beads
    ``bd ready`` / ``bd prime``.

    Args:
        mode: ready | suggested | prime.
            ready returns structured ready-context records.
            suggested returns the bounded Suggested-tab candidate set.
            prime returns a compact session boot pack from the same records.
        include_readings: include reading notes in work surfaces.
        limit: max items per list.
    """
    safe_limit = max(1, min(int(limit or 12), 100))
    with _get_conn() as conn:
        tasks = TaskDAO.get_active(conn)

    normalized_mode = (mode or "ready").strip().lower()
    if normalized_mode == "suggested":
        rows = _suggested_ready(
            tasks,
            include_readings=include_readings,
            limit=safe_limit,
        )
        return json.dumps(
            {
                "contract_version": _READY_CONTEXT_CONTRACT_VERSION,
                "mode": "suggested",
                "count": len(rows),
                "items": rows,
            },
            ensure_ascii=False,
        )

    if normalized_mode == "prime":
        pack = _prime_context(
            tasks,
            include_readings=include_readings,
            limit=min(safe_limit, 50),
        )
        for key in (
            "top_ready_items",
            "blocked_or_waiting",
            "cleanup_candidates",
            "explicit_exclusions",
            "risk_or_escalation_items",
        ):
            pack[key] = [
                _strip_ready_task_payload(record) for record in pack.get(key, [])
            ]
        pack["mode"] = "prime"
        return json.dumps(pack, ensure_ascii=False)

    if normalized_mode != "ready":
        return json.dumps(
            {
                "error": f"Invalid mode: {mode}",
                "valid_modes": ["ready", "suggested", "prime"],
            },
            ensure_ascii=False,
        )

    records = _ready_context(tasks, include_readings=include_readings)
    selected = [_strip_ready_task_payload(record) for record in records[:safe_limit]]
    return json.dumps(
        {
            "contract_version": _READY_CONTEXT_CONTRACT_VERSION,
            "mode": "ready",
            "count": len(selected),
            "truncated": len(records) > safe_limit,
            "items": selected,
        },
        ensure_ascii=False,
    )


# ── Entry point ──────────────────────────────────────────────────────────
def main() -> None:
    maybe_mount_premium_extensions(mcp, server_name="sqlite-tasks")
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
