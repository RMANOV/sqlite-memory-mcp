#!/usr/bin/env python3
"""Thin MCP server exposing session management and knowledge health tools.

Shares the same SQLite database as the main sqlite-kb server.
Exists because Claude Code 2.x has a tool-count limit per MCP server
(~9 tools visible out of 50), so session tools are split into a separate server.
"""

from __future__ import annotations

import json
from typing import Any

from fastmcp_compat import FastMCP

from db_utils import (
    get_conn as _get_conn,
    fts_query as _fts_query,
    now_iso as _now,
    setup_logger,
)
from schema import init_db, error as _error

# ── Logging (file-only, NEVER stdout — breaks MCP stdio) ────────────────

logger = setup_logger("sqlite-session", "session_server.log")

# ── FastMCP app ──────────────────────────────────────────────────────────

mcp = FastMCP(
    "sqlite-session",
    instructions=(
        "Session management and knowledge health tools for SQLite-backed persistent memory. "
        "Shares DB with sqlite-kb."
    ),
)

# init DB
init_db()


# ═══════════════════════════════════════════════════════════════════════════
# Tool 1: session_save
# ═══════════════════════════════════════════════════════════════════════════
@mcp.tool()
def session_save(
    session_id: str,
    project: str | None = None,
    summary: str | None = None,
    active_files: list[str] | None = None,
) -> str:
    """Save or update a session snapshot.

    Creates a new session record or updates an existing one.
    Always sets ended_at to the current time.
    """
    now = _now()
    files_json = json.dumps(active_files) if active_files else None

    with _get_conn() as conn:
        existing = conn.execute(
            "SELECT started_at FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()

        if existing:
            conn.execute(
                "UPDATE sessions SET project = COALESCE(?, project), "
                "summary = COALESCE(?, summary), "
                "active_files = COALESCE(?, active_files), "
                "ended_at = ? WHERE session_id = ?",
                (project, summary, files_json, now, session_id),
            )
            action = "updated"
        else:
            conn.execute(
                "INSERT INTO sessions "
                "(session_id, project, summary, active_files, started_at, ended_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, project, summary, files_json, now, now),
            )
            action = "created"

    logger.info("session_save: %s session %s", action, session_id)
    return json.dumps({"action": action, "session_id": session_id})


# ═══════════════════════════════════════════════════════════════════════════
# Tool 2: session_recall
# ═══════════════════════════════════════════════════════════════════════════
@mcp.tool()
def session_recall(last_n: int = 5) -> str:
    """Recall the last N sessions, ordered by most recent first.

    Returns session metadata: session_id, project, summary, active_files,
    started_at, ended_at.
    """
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT session_id, project, summary, active_files, started_at, ended_at "
            "FROM sessions ORDER BY started_at DESC LIMIT ?",
            (last_n,),
        ).fetchall()

    sessions = []
    for r in rows:
        try:
            _af = json.loads(r["active_files"]) if r["active_files"] else None
        except (json.JSONDecodeError, TypeError):
            _af = None
        session = {
            "session_id": r["session_id"],
            "project": r["project"],
            "summary": r["summary"],
            "active_files": _af,
            "started_at": r["started_at"],
            "ended_at": r["ended_at"],
        }
        sessions.append(session)

    return json.dumps({"sessions": sessions, "count": len(sessions)})


# ═══════════════════════════════════════════════════════════════════════════
# Tool 3: search_by_project (FTS5 scoped)
# ═══════════════════════════════════════════════════════════════════════════
@mcp.tool()
def search_by_project(query: str, project: str) -> str:
    """Search the knowledge graph scoped to a specific project.

    Uses FTS5 BM25-ranked search filtered by project, then applies
    multi-signal re-ranking for improved relevance.
    """
    fts_q = _fts_query(query)
    with _get_conn() as conn:
        try:
            from smart_retrieval import RERANKING_POOL_SIZE

            pool_size = RERANKING_POOL_SIZE
        except Exception:
            pool_size = 50

        rows = conn.execute(
            "SELECT memory_fts.rowid AS eid, memory_fts.name, memory_fts.entity_type, "
            "entities.project, memory_fts.rank "
            "FROM memory_fts "
            "JOIN entities ON entities.id = memory_fts.rowid "
            "WHERE memory_fts MATCH ? AND entities.project = ? "
            "ORDER BY memory_fts.rank LIMIT ?",
            (fts_q, project, pool_size),
        ).fetchall()

        if not rows:
            results = []
        else:
            # Try smart re-ranking (L1)
            reranked = None
            try:
                from smart_retrieval import rerank_entities

                reranked = rerank_entities(
                    conn,
                    rows,
                    current_project=project,
                    session_id=None,
                    query_entity_ids=None,
                    limit=50,
                )
            except Exception:
                pass

            if reranked:
                eids = [r["eid"] for r in reranked]
            else:
                eids = [r["eid"] for r in rows[:50]]

            # Batch-fetch observations
            ph = ",".join("?" * len(eids))
            obs_rows = conn.execute(
                f"SELECT entity_id, content FROM observations "
                f"WHERE entity_id IN ({ph}) ORDER BY entity_id, id",
                eids,
            ).fetchall()
            obs_by_eid: dict[int, list[str]] = {}
            for o in obs_rows:
                obs_by_eid.setdefault(o["entity_id"], []).append(o["content"])

            if reranked:
                results = [
                    {
                        "name": r["name"],
                        "entityType": r["entity_type"],
                        "project": project,
                        "observations": obs_by_eid.get(r["eid"], []),
                        "_score": r["_score"],
                    }
                    for r in reranked
                ]
            else:
                results = [
                    {
                        "name": r["name"],
                        "entityType": r["entity_type"],
                        "project": project,
                        "observations": obs_by_eid.get(r["eid"], []),
                    }
                    for r in rows[:50]
                ]

    logger.info(
        "search_by_project: query=%r project=%r matched=%d",
        query,
        project,
        len(results),
    )
    return json.dumps({"entities": results, "query": query, "project": project})


# ═══════════════════════════════════════════════════════════════════════════
# Tool 4: knowledge_health (L2b health sweep)
# ═══════════════════════════════════════════════════════════════════════════
@mcp.tool()
def knowledge_health(
    include_duplicates: bool = True,
    include_contradictions: bool = True,
    include_stale: bool = True,
    auto_promote: bool = False,
) -> str:
    """Run observation health scan and return findings.

    Checks for near-duplicate observations, contradicting claims,
    stale entities, and optionally auto-promotes high-confidence claims.
    """
    with _get_conn() as conn:
        try:
            from lazy_enrichment import (
                detect_contradictions,
                detect_near_duplicates,
                detect_stale_entities,
                promote_ready_claims,
            )
        except ImportError:
            return _error("lazy_enrichment module not available")

        report: dict[str, Any] = {}
        if include_duplicates:
            report["near_duplicates"] = detect_near_duplicates(conn)
        if include_contradictions:
            report["contradictions"] = detect_contradictions(conn)
        if include_stale:
            report["stale_entities"] = detect_stale_entities(conn)
        if auto_promote:
            report["promoted"] = promote_ready_claims(conn)
        else:
            report["promoted"] = []

        report["summary"] = {
            "duplicates_found": len(report.get("near_duplicates", [])),
            "contradictions_found": len(report.get("contradictions", [])),
            "stale_entities_found": len(report.get("stale_entities", [])),
            "claims_promoted": len(report["promoted"]),
        }

    return json.dumps(report)


# ═══════════════════════════════════════════════════════════════════════════
# Tool 5: resume_context
# ═══════════════════════════════════════════════════════════════════════════
@mcp.tool()
def resume_context(
    session_id: str | None = None,
    include_open_questions: bool = True,
) -> str:
    """Session continuity: handoff pack + unresolved items + changed facts.

    Builds a handoff context pack and includes open questions, chunks
    awaiting human input, and recently changed canonical facts.

    Args:
        session_id: Optional session to resume from
        include_open_questions: Include open clarification questions (default True)
    """
    try:
        from context_packer import resume_context as _resume_context
    except ImportError:
        return _error("context_packer module not available")

    with _get_conn() as conn:
        result = _resume_context(conn, session_id, include_open_questions)
        return json.dumps(result)


# ── Entry point ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    mcp.run(transport="stdio")
