#!/usr/bin/env python3
"""Thin MCP server exposing only task management tools.

Shares the same SQLite database as the main sqlite-kb server.
Exists because Claude Code 2.x has a tool-count limit per MCP server
(~9 tools visible out of 50), so task tools are split into a separate server.
"""

from __future__ import annotations

import json
import re
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

# Pre-built SQL for active-task exclusion
_EXCL_PH = ",".join("?" for _ in _TASK_ACTIVE_EXCLUSIONS)

# ── Logging (file-only, NEVER stdout — breaks MCP stdio) ────────────────

logger = setup_logger("sqlite-tasks", "task_server.log")

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


_TITLE_LOOKUP_SPLIT_RE = re.compile(r"[\s\-_]+")


def _normalize_title_lookup(text: str | None) -> str:
    """Normalize title/name text for forgiving partial matching."""
    return " ".join(
        part for part in _TITLE_LOOKUP_SPLIT_RE.split((text or "").casefold()) if part
    )


def _title_lookup_score(query: str, candidate: str | None) -> float:
    """Score a candidate title/name against a partial user fragment."""
    raw_q = (query or "").casefold().strip()
    raw_c = (candidate or "").casefold().strip()
    if not raw_q or not raw_c:
        return 0.0

    norm_q = _normalize_title_lookup(raw_q)
    norm_c = _normalize_title_lookup(raw_c)

    if raw_q == raw_c or (norm_q and norm_q == norm_c):
        return 400.0
    if raw_q in raw_c:
        return 320.0 + min(len(raw_q), 80) / 100.0
    if norm_q and norm_q in norm_c:
        return 280.0 + min(len(norm_q), 80) / 100.0

    q_words = [w for w in norm_q.split() if w]
    if q_words and all(w in norm_c for w in q_words):
        return 220.0 + len(q_words)

    return 0.0


def _text_lookup_score(
    query: str,
    candidate: str | None,
    *,
    exact_score: float,
    normalized_score: float,
    all_words_score: float,
) -> float:
    """Score a non-title text field against a remembered phrase."""
    raw_q = (query or "").casefold().strip()
    raw_c = (candidate or "").casefold().strip()
    if not raw_q or not raw_c:
        return 0.0

    norm_q = _normalize_title_lookup(raw_q)
    norm_c = _normalize_title_lookup(raw_c)

    if raw_q in raw_c:
        return exact_score + min(len(raw_q), 80) / 100.0
    if norm_q and norm_q in norm_c:
        return normalized_score + min(len(norm_q), 80) / 100.0

    q_words = [w for w in norm_q.split() if w]
    if q_words and all(w in norm_c for w in q_words):
        return all_words_score + len(q_words)
    return 0.0


# ── FastMCP app ──────────────────────────────────────────────────────────

mcp = FastMCP(
    "sqlite-tasks",
    instructions=(
        "Task management tools for SQLite-backed persistent memory. "
        "Create, update, query, and digest tasks. "
        "Use find_by_title when only a remembered phrase is known: it searches tasks, notes, "
        "and entities across title/name, description, notes, observations, and project "
        "regardless of status, section, or project filters. "
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
        changed_keys = [k for k in updates if k != "updated_at"]
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
                    except Exception:
                        pass  # FTS5 failed — fall back to unfiltered scan

            sql = (
                f"SELECT {cols} FROM {from_clause} WHERE {fts_where} "
                f"ORDER BY {order_clause}"
            )
            all_rows = conn.execute(sql, fts_params).fetchall()
            results = _search_engine.search(
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
            matched_in: list[str] = []
            score = 0.0

            title_score = _title_lookup_score(query, title)
            if title_score > 0:
                score = max(score, title_score)
                matched_in.append("title")

            desc_score = _text_lookup_score(
                query,
                row["description"],
                exact_score=185.0,
                normalized_score=165.0,
                all_words_score=135.0,
            )
            if desc_score > 0:
                score = max(score, desc_score)
                matched_in.append("description")

            notes_score = _text_lookup_score(
                query,
                row["notes"],
                exact_score=165.0,
                normalized_score=145.0,
                all_words_score=120.0,
            )
            if notes_score > 0:
                score = max(score, notes_score)
                matched_in.append("notes")

            project_score = _text_lookup_score(
                query,
                row["project"],
                exact_score=150.0,
                normalized_score=130.0,
                all_words_score=110.0,
            )
            if project_score > 0:
                score = max(score, project_score)
                matched_in.append("project")

            if score <= 0:
                continue
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
            matched_in: list[str] = []
            score = 0.0

            name_score = _title_lookup_score(query, name)
            if name_score > 0:
                score = max(score, name_score)
                matched_in.append("name")

            obs_text = "\n".join(obs_by_entity.get(row["id"], []))
            obs_score = _text_lookup_score(
                query,
                obs_text,
                exact_score=180.0,
                normalized_score=160.0,
                all_words_score=130.0,
            )
            if obs_score > 0:
                score = max(score, obs_score)
                matched_in.append("observations")

            project_score = _text_lookup_score(
                query,
                row["project"],
                exact_score=150.0,
                normalized_score=130.0,
                all_words_score=110.0,
            )
            if project_score > 0:
                score = max(score, project_score)
                matched_in.append("project")

            type_score = _text_lookup_score(
                query,
                row["entity_type"],
                exact_score=120.0,
                normalized_score=110.0,
                all_words_score=95.0,
            )
            if type_score > 0:
                score = max(score, type_score)
                matched_in.append("entity_type")

            if score <= 0:
                continue
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
    matches = matches[:limit]

    lines = [
        "| # | Kind | Title | Status/Type | Section | Project | Matched In |",
        "|---|------|-------|-------------|---------|---------|------------|",
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
            f"{section} | {item.get('project') or '—'} | {', '.join(item.get('matched_in') or []) or '—'} |"
        )

    return json.dumps(
        {
            "matches": matches,
            "count": len(matches),
            "query": query,
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


# ── Entry point ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    mcp.run(transport="stdio")
