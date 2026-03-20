"""Context Pack Compiler — Role-specific context compilation with token budgets.

Phase 3 of Intelligence v2:
- Pack types: planner, reviewer, executor, bridge_checker, handoff
- Greedy coverage algorithm: maximize relevance+novelty, minimize redundancy
- Token budget optimization
- Session continuity (resume_context) with handoff packs
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

from db_utils import now_iso
from intelligence_v2 import (
    _new_id,
    load_config,
    log_enrichment_run,
)

logger = logging.getLogger("sqlite-kb")


# ── Task-Relevant Helpers ────────────────────────────────────────────────


def _format_ts(iso_str: str | None) -> str:
    """Format ISO timestamp as [YYYY-MM-DD HH:MM] prefix, or empty string."""
    if not iso_str:
        return ""
    try:
        return f"[{iso_str[:16].replace('T', ' ')}] "
    except (ValueError, TypeError):
        return ""


def _build_task_query(conn: sqlite3.Connection, task_id: str) -> tuple[str, list[int]]:
    """Extract search query text and linked entity IDs from a task."""
    task = conn.execute(
        "SELECT title, project, description, notes FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if not task:
        return "", []
    parts = [task["title"] or ""]
    if task["project"]:
        parts.append(task["project"])
    if task["description"]:
        parts.append(task["description"][:500])
    if task["notes"]:
        parts.append(task["notes"][:300])
    query = " ".join(parts)

    linked = conn.execute(
        "SELECT entity_id FROM task_entity_links WHERE task_id = ?", (task_id,)
    ).fetchall()
    return query, [r["entity_id"] for r in linked]


def _find_relevant_entities(
    conn: sqlite3.Connection,
    query: str,
    linked_ids: list[int],
    session_id: str | None,
    limit: int = 50,
) -> dict[str, float]:
    """Return {entity_name: relevance_score} for entities relevant to query."""
    # Tokenize, build FTS5 OR query
    words = [w for w in query.strip().split() if len(w) > 2]
    if not words:
        # Fallback: linked entities only
        scores: dict[str, float] = {}
        for eid in linked_ids:
            row = conn.execute(
                "SELECT name FROM entities WHERE id = ?", (eid,)
            ).fetchone()
            if row:
                scores[row["name"]] = 1.0
        return scores

    escaped = ['"' + w.replace('"', '""') + '"' for w in words[:20]]
    fts_q = " OR ".join(escaped)

    try:
        fts_rows = conn.execute(
            "SELECT memory_fts.rowid AS eid, memory_fts.name, "
            "memory_fts.entity_type, e.project, memory_fts.rank "
            "FROM memory_fts JOIN entities e ON e.id = memory_fts.rowid "
            "WHERE memory_fts MATCH ? ORDER BY memory_fts.rank LIMIT ?",
            (fts_q, 100),
        ).fetchall()
    except Exception:
        fts_rows = []

    # Optional vector search + RRF merge
    try:
        from vec_search import VEC_AVAILABLE, rrf_merge, vector_search

        if VEC_AVAILABLE:
            vec_rows = vector_search(conn, query, 100)
            if fts_rows and vec_rows:
                fts_rows = rrf_merge(fts_rows, vec_rows)
            elif vec_rows:
                fts_rows = vec_rows
    except ImportError:
        pass

    if not fts_rows and not linked_ids:
        return {}

    # 6-signal reranking
    reranked: list[dict] = []
    if fts_rows:
        try:
            from smart_retrieval import rerank_entities

            reranked = rerank_entities(
                conn, fts_rows, None, session_id, linked_ids, limit
            )
        except Exception as exc:
            logger.warning("rerank_entities failed, using raw FTS: %s", exc)
            reranked = [{"name": r["name"], "_score": 0.1} for r in fts_rows[:limit]]

    scores = {}
    for item in reranked:
        scores[item["name"]] = item.get("_score", 0.1)

    # Add linked entities not already in search results
    for eid in linked_ids:
        row = conn.execute("SELECT name FROM entities WHERE id = ?", (eid,)).fetchone()
        if row and row["name"] not in scores:
            scores[row["name"]] = 0.5

    return scores


# ── Pack Type Definitions ────────────────────────────────────────────────

PACK_TYPES = ("planner", "reviewer", "executor", "bridge_checker", "handoff")

# What each pack type prioritizes
_PACK_PRIORITIES = {
    "planner": {
        "fact_weight": 1.0,  # canonical facts are critical for planning
        "claim_weight": 0.7,  # provisional claims are useful context
        "question_weight": 0.9,  # open questions shape the plan
        "chunk_weight": 0.3,  # raw chunks less useful
    },
    "reviewer": {
        "fact_weight": 1.0,
        "claim_weight": 0.8,  # reviewer needs to validate claims
        "question_weight": 0.5,
        "chunk_weight": 0.4,
    },
    "executor": {
        "fact_weight": 1.0,
        "claim_weight": 0.5,  # executor uses confirmed facts
        "question_weight": 0.3,
        "chunk_weight": 0.6,  # raw context for implementation
    },
    "bridge_checker": {
        "fact_weight": 0.8,
        "claim_weight": 0.9,  # bridge needs to check claims
        "question_weight": 0.7,
        "chunk_weight": 0.5,
    },
    "handoff": {
        "fact_weight": 1.0,
        "claim_weight": 0.8,
        "question_weight": 1.0,  # handoff must include open questions
        "chunk_weight": 0.2,
    },
}

# Approximate tokens per character (conservative estimate)
_CHARS_PER_TOKEN = 4


def _estimate_tokens(text: str) -> int:
    """Estimate token count from text length."""
    return max(1, len(text) // _CHARS_PER_TOKEN)


# ── Core: Build Context Pack ─────────────────────────────────────────────


def build_context_pack(
    conn: sqlite3.Connection,
    pack_type: str = "executor",
    target_ref: str | None = None,
    session_id: str | None = None,
    token_budget: int | None = None,
) -> dict[str, Any]:
    """Compile role-specific context pack with validated facts, provisional warnings.

    Uses greedy coverage algorithm:
    1. Score all available items by (relevance × novelty)
    2. Greedily add highest-scoring items until token budget exhausted
    3. Mark provisional items with warnings

    Returns dict with: pack_id, pack_type, sections, token_usage, freshness_score.
    """
    config = load_config()
    started = now_iso()

    if not config["enabled"]:
        return {"status": "disabled"}

    if pack_type not in PACK_TYPES:
        return {"error": f"Invalid pack type: {pack_type}. Use one of: {PACK_TYPES}"}

    budget = token_budget or config.get("context_pack_token_budget_default", 4000)
    priorities = _PACK_PRIORITIES[pack_type]

    # Task-relevant filtering: when target_ref is a task ID, scope to relevant entities
    relevant_names: dict[str, float] = {}
    is_task_scoped = False
    if target_ref:
        try:
            task_query, linked_ids = _build_task_query(conn, target_ref)
            if task_query:
                relevant_names = _find_relevant_entities(
                    conn, task_query, linked_ids, session_id
                )
                is_task_scoped = bool(relevant_names)
        except Exception as exc:
            logger.warning(
                "Task-scoped filtering failed, falling back to global: %s", exc
            )

    # Gather available items
    items: list[dict[str, Any]] = []

    # 1. Canonical facts (highest trust)
    facts = conn.execute(
        "SELECT fact_id, subject, predicate, object_text, fact_scope, confidence, "
        "created_at FROM canonical_facts ORDER BY confidence DESC"
    ).fetchall()
    for f in facts:
        rel = 1.0
        if is_task_scoped:
            rel = relevant_names.get(f["subject"], 0.0)
            if rel < 0.01:
                continue
        ts = _format_ts(f["created_at"])
        text = f"{ts}[FACT] {f['subject']} {f['predicate']} {f['object_text']} (scope: {f['fact_scope']})"
        items.append(
            {
                "type": "fact",
                "id": f["fact_id"],
                "text": text,
                "tokens": _estimate_tokens(text),
                "score": f["confidence"] * priorities["fact_weight"] * rel,
                "provisional": False,
            }
        )

    # 2. Candidate claims (provisional)
    claims = conn.execute(
        "SELECT claim_id, subject, predicate, object_text, claim_scope, confidence, "
        "created_at FROM candidate_claims WHERE status = 'candidate' "
        "ORDER BY confidence DESC LIMIT 50"
    ).fetchall()
    for c in claims:
        rel = 1.0
        if is_task_scoped:
            rel = relevant_names.get(c["subject"], 0.0)
            if rel < 0.01:
                continue
        ts = _format_ts(c["created_at"])
        text = (
            f"{ts}[PROVISIONAL] {c['subject']} {c['predicate']} {c['object_text']} "
            f"(scope: {c['claim_scope']}, confidence: {c['confidence']:.2f})"
        )
        items.append(
            {
                "type": "claim",
                "id": c["claim_id"],
                "text": text,
                "tokens": _estimate_tokens(text),
                "score": c["confidence"] * priorities["claim_weight"] * rel,
                "provisional": True,
            }
        )

    # 3. Open questions (for planner/handoff)
    if priorities["question_weight"] > 0.3:
        questions = conn.execute(
            "SELECT q.question_id, q.question_text, q.question_type, q.priority_score, "
            "q.created_at, c.title AS chunk_title "
            "FROM context_questions q "
            "LEFT JOIN context_chunks c ON q.chunk_id = c.chunk_id "
            "WHERE q.state = 'open' "
            "ORDER BY q.priority_score DESC LIMIT 20"
        ).fetchall()
        for q in questions:
            if is_task_scoped:
                # Filter: keep question if its chunk title overlaps with relevant entities
                title = (q["chunk_title"] or "") + " " + q["question_text"]
                max_rel = max(
                    (
                        sc
                        for name, sc in relevant_names.items()
                        if name.lower() in title.lower()
                    ),
                    default=0.0,
                )
                if max_rel < 0.01:
                    continue
            else:
                max_rel = 1.0
            ts = _format_ts(q["created_at"])
            ctx = f" (re: {q['chunk_title']})" if q["chunk_title"] else ""
            text = f"{ts}[QUESTION] {q['question_text']}{ctx}"
            items.append(
                {
                    "type": "question",
                    "id": q["question_id"],
                    "text": text,
                    "tokens": _estimate_tokens(text),
                    "score": q["priority_score"]
                    * priorities["question_weight"]
                    * max_rel,
                    "provisional": False,
                }
            )

    # 4. Enrichable/uncertain chunks (raw context)
    if priorities["chunk_weight"] > 0.2:
        chunk_limit = 100 if is_task_scoped else 30
        chunks = conn.execute(
            "SELECT chunk_id, title, body, materiality_score, state, created_at "
            "FROM context_chunks "
            "WHERE state IN ('enrichable', 'uncertain', 'awaiting_human') "
            "ORDER BY materiality_score DESC LIMIT ?",
            (chunk_limit,),
        ).fetchall()
        for ch in chunks:
            if is_task_scoped:
                haystack = ((ch["title"] or "") + " " + ch["body"][:500]).lower()
                max_rel = max(
                    (
                        sc
                        for name, sc in relevant_names.items()
                        if name.lower() in haystack
                    ),
                    default=0.0,
                )
                if max_rel < 0.01:
                    continue
            else:
                max_rel = 1.0
            ts = _format_ts(ch["created_at"])
            label = f"[CONTEXT:{ch['state'].upper()}]"
            title_part = f" {ch['title']} —" if ch["title"] else ""
            body_preview = ch["body"][:300] + ("..." if len(ch["body"]) > 300 else "")
            text = f"{ts}{label}{title_part} {body_preview}"
            items.append(
                {
                    "type": "chunk",
                    "id": ch["chunk_id"],
                    "text": text,
                    "tokens": _estimate_tokens(text),
                    "score": ch["materiality_score"]
                    * priorities["chunk_weight"]
                    * max_rel,
                    "provisional": ch["state"] != "enrichable",
                }
            )

    # Greedy coverage: sort by score, fill budget
    items.sort(key=lambda x: x["score"], reverse=True)

    selected: list[dict[str, Any]] = []
    tokens_used = 0
    for item in items:
        if tokens_used + item["tokens"] > budget:
            continue
        selected.append(item)
        tokens_used += item["tokens"]

    # Build pack body
    sections: dict[str, list[str]] = {
        "facts": [],
        "claims": [],
        "questions": [],
        "chunks": [],
    }
    for item in selected:
        sections[item["type"] + "s" if item["type"] != "chunk" else "chunks"].append(
            item["text"]
        )

    body_parts = []
    if sections["facts"]:
        body_parts.append("## Canonical Facts\n" + "\n".join(sections["facts"]))
    if sections["claims"]:
        body_parts.append(
            "## Provisional Claims (⚠ unverified)\n" + "\n".join(sections["claims"])
        )
    if sections["questions"]:
        body_parts.append("## Open Questions\n" + "\n".join(sections["questions"]))
    if sections["chunks"]:
        body_parts.append("## Context Fragments\n" + "\n".join(sections["chunks"]))

    pack_body = "\n\n".join(body_parts) if body_parts else "(no context available)"

    # Compute input signature for caching
    input_sig = f"{pack_type}:{target_ref}:{session_id}:{budget}"

    # Freshness: ratio of canonical vs provisional items
    total = len(selected) or 1
    canonical_count = sum(1 for s in selected if not s["provisional"])
    freshness = canonical_count / total

    # Store pack
    pack_id = _new_id()
    now = now_iso()
    conn.execute(
        "INSERT INTO context_packs "
        "(pack_id, session_id, entity_id, pack_type, target_ref, input_signature, "
        "token_budget, body, freshness_score, created_at) "
        "VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?)",
        (
            pack_id,
            session_id,
            pack_type,
            target_ref,
            input_sig,
            budget,
            pack_body,
            freshness,
            now,
        ),
    )

    log_enrichment_run(
        conn,
        "build_context_pack",
        "success",
        input_sig,
        session_id=session_id,
        started_at=started,
    )

    return {
        "pack_id": pack_id,
        "pack_type": pack_type,
        "token_budget": budget,
        "token_usage": tokens_used,
        "items_included": len(selected),
        "freshness_score": round(freshness, 3),
        "sections": {k: len(v) for k, v in sections.items()},
        "body": pack_body,
    }


# ── Core: Resume Context ─────────────────────────────────────────────────


def resume_context(
    conn: sqlite3.Connection,
    session_id: str | None = None,
    include_open_questions: bool = True,
) -> dict[str, Any]:
    """Session continuity: handoff pack + unresolved items + changed facts.

    Returns dict with: pack (handoff context), open_questions, changed_facts,
    impacted_artifacts, summary.
    """
    config = load_config()
    started = now_iso()

    if not config["enabled"]:
        return {"status": "disabled"}

    result: dict[str, Any] = {
        "session_id": session_id,
        "open_questions": [],
        "changed_facts_since_last_session": [],
        "chunks_awaiting_human": [],
    }

    # Build a handoff pack
    pack_result = build_context_pack(
        conn,
        pack_type="handoff",
        session_id=session_id,
        token_budget=config.get("context_pack_token_budget_default", 4000),
    )
    if "error" in pack_result or pack_result.get("status") == "disabled":
        result["pack"] = pack_result
    else:
        result["pack"] = {
            "pack_id": pack_result["pack_id"],
            "token_usage": pack_result["token_usage"],
            "freshness_score": pack_result["freshness_score"],
            "body": pack_result["body"],
        }

    # Open questions
    if include_open_questions:
        questions = conn.execute(
            "SELECT q.question_id, q.question_text, q.question_type, "
            "q.priority_score, c.title AS chunk_title "
            "FROM context_questions q "
            "LEFT JOIN context_chunks c ON q.chunk_id = c.chunk_id "
            "WHERE q.state = 'open' "
            "ORDER BY q.priority_score DESC LIMIT 20"
        ).fetchall()
        result["open_questions"] = [
            {
                "question_id": q["question_id"],
                "text": q["question_text"],
                "type": q["question_type"],
                "priority": q["priority_score"],
                "chunk_title": q["chunk_title"],
            }
            for q in questions
        ]

    # Chunks awaiting human input
    awaiting = conn.execute(
        "SELECT chunk_id, title, state, source_type, materiality_score "
        "FROM context_chunks WHERE state = 'awaiting_human' "
        "ORDER BY materiality_score DESC LIMIT 10"
    ).fetchall()
    result["chunks_awaiting_human"] = [
        {
            "chunk_id": a["chunk_id"],
            "title": a["title"],
            "source_type": a["source_type"],
            "materiality": a["materiality_score"],
        }
        for a in awaiting
    ]

    # Recently changed canonical facts (last 7 days)
    recent_facts = conn.execute(
        "SELECT fact_id, subject, predicate, object_text, fact_scope, "
        "validation_mode, updated_at "
        "FROM canonical_facts "
        "WHERE updated_at >= datetime('now', '-7 days') "
        "ORDER BY updated_at DESC LIMIT 20"
    ).fetchall()
    result["changed_facts_since_last_session"] = [
        {
            "fact_id": f["fact_id"],
            "subject": f["subject"],
            "predicate": f["predicate"],
            "object": f["object_text"],
            "scope": f["fact_scope"],
            "validation_mode": f["validation_mode"],
            "updated_at": f["updated_at"],
        }
        for f in recent_facts
    ]

    # Summary
    total_open = len(result["open_questions"])
    total_awaiting = len(result["chunks_awaiting_human"])
    total_changed = len(result["changed_facts_since_last_session"])

    result["summary"] = (
        f"Session resume: {total_open} open questions, "
        f"{total_awaiting} chunks awaiting human, "
        f"{total_changed} recently changed facts."
    )

    log_enrichment_run(
        conn,
        "resume_context",
        "success",
        f"session:{session_id}",
        session_id=session_id,
        started_at=started,
    )

    return result
