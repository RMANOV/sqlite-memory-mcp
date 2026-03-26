"""Context Pack Compiler — Role-specific context compilation with token budgets.

Phase 3 of Intelligence v2:
- Pack types: planner, reviewer, executor, bridge_checker, handoff
- Greedy coverage algorithm: maximize relevance+novelty, minimize redundancy
- Token budget optimization
- Session continuity (resume_context) with handoff packs
"""

from __future__ import annotations

import logging
import math
import re
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from advanced_context import (
    _keyword_score,
    build_strategy as _build_advanced_strategy,
    compute_strategy_match as _compute_advanced_task_match,
    select_context_items as _select_advanced_items,
)
from db_utils import now_iso
from intelligence_v2 import (
    _new_id,
    load_config,
    log_enrichment_run,
)

logger = logging.getLogger("sqlite-kb")

# Minimum relevance score — fragments below this threshold are excluded
_MIN_RELEVANCE_THRESHOLD = 0.18
_TASK_TOKEN_RE = re.compile(r"[0-9A-Za-zА-Яа-я_./:-]{4,}")
_RECENCY_HALF_LIFE_DAYS = 21
_MAX_ITEMS_BY_TYPE = {
    "fact": 6,
    "claim": 5,
    "question": 4,
    "chunk": 6,
}
_MAX_CHUNKS_PER_GROUP = 3
_CHUNK_TRUST_BY_STATE = {
    "enrichable": 0.35,
    "uncertain": 0.22,
    "awaiting_human": 0.16,
}


# ── Task-Relevant Helpers ────────────────────────────────────────────────


def _format_ts(iso_str: str | None) -> str:
    """Format ISO timestamp as [YYYY-MM-DD HH:MM] prefix, or empty string."""
    if not iso_str:
        return ""
    try:
        return f"[{iso_str[:16].replace('T', ' ')}] "
    except (ValueError, TypeError):
        return ""


def _extract_task_keywords(text: str) -> list[str]:
    """Extract robust task keywords, preserving tool-like snake_case identifiers."""
    if not text:
        return []

    keywords: list[str] = []
    seen: set[str] = set()
    for raw in _TASK_TOKEN_RE.findall(text.lower()):
        token = raw.strip("._:/-")
        if len(token) >= 4 and token not in seen:
            keywords.append(token)
            seen.add(token)
        for part in re.split(r"[_./:-]+", token):
            part = part.strip()
            if len(part) >= 4 and part not in seen:
                keywords.append(part)
                seen.add(part)
    return keywords[:40]


def _entity_name_overlap_score(text: str, name_scores: dict[str, float]) -> float:
    """Return strongest relevant-entity name match found inside text."""
    if not text or not name_scores:
        return 0.0
    haystack = text.lower()
    best = 0.0
    for name, score in name_scores.items():
        if name and name.lower() in haystack:
            best = max(best, score)
    return best


def _compute_recency_score(
    iso_str: str | None,
    half_life_days: float = _RECENCY_HALF_LIFE_DAYS,
) -> float:
    """Exponential recency score: 1.0 for fresh content, decays with age."""
    if not iso_str:
        return 0.5
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age_days = max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 86400.0)
        return math.pow(2.0, -age_days / half_life_days)
    except (ValueError, TypeError):
        return 0.5


def _signal_floor(value: float | int | None, floor: float = 0.2) -> float:
    """Normalize noisy 0..1-ish signals so zero-ish rows can still participate."""
    if value is None:
        return floor
    try:
        return max(floor, min(1.0, float(value)))
    except (ValueError, TypeError):
        return floor


def _selection_score(
    base_signal: float,
    relevance: float,
    trust: float,
    freshness: float,
    role_weight: float,
) -> float:
    """Composite score used for greedy selection."""
    return (
        _signal_floor(base_signal)
        * role_weight
        * max(relevance, 0.01)
        * (0.45 + (trust * 0.55))
        * (0.75 + (freshness * 0.25))
    )


def _chunk_group_key(chunk: sqlite3.Row) -> str:
    """Group related chunk fragments so one source cannot dominate the pack."""
    for key in ("entity_id", "source_ref", "title"):
        value = chunk[key]
        if value:
            return str(value).lower()
    return chunk["chunk_id"]


def _make_coverage_keys(
    *texts: str | None,
    extras: list[str] | None = None,
    limit: int = 6,
) -> list[str]:
    """Build compact coverage keys for the optional advanced selector."""
    keys: list[str] = []
    seen: set[str] = set()
    for extra in extras or []:
        norm = str(extra).strip().lower()
        if norm and norm not in seen:
            keys.append(norm)
            seen.add(norm)
        if len(keys) >= limit:
            return keys[:limit]
    for text in texts:
        for keyword in _extract_task_keywords(text or ""):
            marker = f"kw:{keyword}"
            if marker not in seen:
                keys.append(marker)
                seen.add(marker)
            if len(keys) >= limit:
                return keys[:limit]
    return keys[:limit]


def _greedy_select_items(
    items: list[dict[str, Any]],
    budget: int,
) -> tuple[list[dict[str, Any]], int, dict[str, Any]]:
    """Baseline deterministic selector used when advanced mode is disabled."""
    items.sort(key=lambda x: x["score"], reverse=True)

    selected: list[dict[str, Any]] = []
    tokens_used = 0
    type_counts: dict[str, int] = defaultdict(int)
    chunk_group_counts: dict[str, int] = defaultdict(int)
    seen_texts: set[str] = set()
    for item in items:
        if tokens_used + item["tokens"] > budget:
            continue
        if type_counts[item["type"]] >= _MAX_ITEMS_BY_TYPE[item["type"]]:
            continue
        if item["type"] == "chunk":
            group_key = item.get("group_key")
            if group_key and chunk_group_counts[group_key] >= _MAX_CHUNKS_PER_GROUP:
                continue
        dedupe_key = " ".join(item["text"].lower().split())
        if dedupe_key in seen_texts:
            continue
        selected.append(item)
        tokens_used += item["tokens"]
        type_counts[item["type"]] += 1
        if item["type"] == "chunk" and item.get("group_key"):
            chunk_group_counts[item["group_key"]] += 1
        seen_texts.add(dedupe_key)

    return selected, tokens_used, {"selection_strategy": "greedy"}


def _prune_context_pack_history(
    conn: sqlite3.Connection,
    pack_type: str,
    target_ref: str | None,
    keep: int,
) -> None:
    """Keep only the most recent packs for a given pack_type/target_ref tuple."""
    if target_ref is None:
        rows = conn.execute(
            "SELECT pack_id FROM context_packs "
            "WHERE pack_type = ? AND target_ref IS NULL "
            "ORDER BY created_at DESC",
            (pack_type,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT pack_id FROM context_packs "
            "WHERE pack_type = ? AND target_ref = ? "
            "ORDER BY created_at DESC",
            (pack_type, target_ref),
        ).fetchall()
    stale_ids = [r["pack_id"] for r in rows[keep:]]
    if not stale_ids:
        return
    ph = ",".join("?" * len(stale_ids))
    conn.execute(f"DELETE FROM context_packs WHERE pack_id IN ({ph})", stale_ids)


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
) -> dict[str, dict[Any, float]]:
    """Return entity relevance maps keyed by both entity name and entity id."""
    # Tokenize, build FTS5 OR query
    words = [w for w in query.strip().split() if len(w) > 2]
    if not words:
        # Fallback: linked entities only
        name_scores: dict[str, float] = {}
        id_scores: dict[int, float] = {}
        for eid in linked_ids:
            row = conn.execute(
                "SELECT id, name FROM entities WHERE id = ?", (eid,)
            ).fetchone()
            if row:
                name_scores[row["name"]] = 1.0
                id_scores[row["id"]] = 1.0
        return {"by_name": name_scores, "by_id": id_scores}

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
    except sqlite3.OperationalError:
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
        except (ImportError, sqlite3.OperationalError) as exc:
            logger.warning("rerank_entities failed, using raw FTS: %s", exc)
            reranked = [
                {"eid": r["eid"], "name": r["name"], "_score": 0.1}
                for r in fts_rows[:limit]
            ]

    name_scores: dict[str, float] = {}
    id_scores: dict[int, float] = {}
    for item in reranked:
        score = item.get("_score", 0.1)
        name_scores[item["name"]] = score
        if item.get("eid") is not None:
            id_scores[int(item["eid"])] = score

    # Add linked entities not already in search results
    for eid in linked_ids:
        row = conn.execute(
            "SELECT id, name FROM entities WHERE id = ?", (eid,)
        ).fetchone()
        if row:
            name_scores[row["name"]] = max(name_scores.get(row["name"], 0.0), 0.75)
            id_scores[row["id"]] = max(id_scores.get(row["id"], 0.0), 0.75)

    return {"by_name": name_scores, "by_id": id_scores}


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
    persist: bool = True,
) -> dict[str, Any]:
    """Compile role-specific context pack with validated facts, provisional warnings.

    Uses greedy coverage algorithm:
    1. Score all available items by (relevance × novelty)
    2. Greedily add highest-scoring items until token budget exhausted
    3. Mark provisional items with warnings

    Returns dict with pack metadata, body, and relevance/trust/freshness scores.
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
    relevant_entity_ids: dict[int, float] = {}
    task_keywords: list[str] = []
    is_task_scoped = bool(target_ref)
    advanced_strategy: dict[str, Any] = {
        "enabled": False,
        "query_expansion_used": False,
        "submodular_enabled": False,
        "metadata": {
            "seed_entities": 0,
            "expanded_entities": 0,
            "expansion_keywords": 0,
        },
    }
    if target_ref:
        try:
            task_query, linked_ids = _build_task_query(conn, target_ref)
            task_keywords = _extract_task_keywords(task_query)
            if task_query or linked_ids:
                relevant_entities = _find_relevant_entities(
                    conn, task_query, linked_ids, session_id
                )
                relevant_names = relevant_entities["by_name"]
                relevant_entity_ids = relevant_entities["by_id"]
                if config.get("advanced_context_enabled"):
                    advanced_strategy = _build_advanced_strategy(
                        conn,
                        target_ref=target_ref,
                        task_query=task_query,
                        linked_ids=linked_ids,
                        task_keywords=task_keywords,
                        relevant_names=relevant_names,
                        relevant_entity_ids=relevant_entity_ids,
                        config=config,
                    )
        except (sqlite3.OperationalError, KeyError) as exc:
            logger.warning(
                "Task-scoped filtering failed, falling back to no-context: %s", exc
            )

    # Gather available items
    items: list[dict[str, Any]] = []

    def _task_match(
        *texts: str | None,
        entity_id: int | str | None = None,
        entity_name: str | None = None,
    ) -> float:
        if not is_task_scoped:
            return 1.0
        best = 0.0
        if entity_id is not None:
            try:
                best = max(best, relevant_entity_ids.get(int(entity_id), 0.0))
            except (ValueError, TypeError):
                pass
        if entity_name:
            best = max(best, relevant_names.get(entity_name, 0.0))
        joined = " ".join(t for t in texts if t)
        if joined:
            best = max(best, _keyword_score(joined, task_keywords))
            best = max(best, _entity_name_overlap_score(joined, relevant_names))
            best = max(
                best,
                _compute_advanced_task_match(
                    advanced_strategy,
                    *texts,
                    entity_id=entity_id,
                    entity_name=entity_name,
                ),
            )
        return best

    # 1. Canonical facts (highest trust)
    facts = conn.execute(
        "SELECT fact_id, subject, predicate, object_text, fact_scope, confidence, "
        "created_at FROM canonical_facts ORDER BY confidence DESC"
    ).fetchall()
    for f in facts:
        rel = _task_match(
            f["subject"],
            f["predicate"],
            f["object_text"],
            entity_name=f["subject"],
        )
        if is_task_scoped and rel < _MIN_RELEVANCE_THRESHOLD:
            continue
        trust = max(0.75, _signal_floor(f["confidence"], 0.75))
        freshness = _compute_recency_score(f["created_at"])
        ts = _format_ts(f["created_at"])
        text = f"{ts}[FACT] {f['subject']} {f['predicate']} {f['object_text']} (scope: {f['fact_scope']})"
        items.append(
            {
                "type": "fact",
                "id": f["fact_id"],
                "text": text,
                "tokens": _estimate_tokens(text),
                "relevance": rel,
                "trust": trust,
                "freshness": freshness,
                "score": _selection_score(
                    f["confidence"],
                    rel,
                    trust,
                    freshness,
                    priorities["fact_weight"],
                ),
                "provisional": False,
                "coverage_keys": _make_coverage_keys(
                    f["subject"],
                    f["predicate"],
                    f["object_text"],
                    extras=[
                        f"subject:{(f['subject'] or '').lower()}",
                        f"predicate:{(f['predicate'] or '').lower()}",
                        f"scope:{(f['fact_scope'] or '').lower()}",
                    ],
                ),
            }
        )

    # 2. Candidate claims (provisional)
    claims = conn.execute(
        "SELECT claim_id, subject, predicate, object_text, claim_scope, confidence, "
        "created_at FROM candidate_claims WHERE status = 'candidate' "
        "ORDER BY confidence DESC LIMIT 50"
    ).fetchall()
    for c in claims:
        rel = _task_match(
            c["subject"],
            c["predicate"],
            c["object_text"],
            entity_name=c["subject"],
        )
        if is_task_scoped and rel < _MIN_RELEVANCE_THRESHOLD:
            continue
        trust = min(0.7, 0.2 + (_signal_floor(c["confidence"], 0.2) * 0.5))
        freshness = _compute_recency_score(c["created_at"])
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
                "relevance": rel,
                "trust": trust,
                "freshness": freshness,
                "score": _selection_score(
                    c["confidence"],
                    rel,
                    trust,
                    freshness,
                    priorities["claim_weight"],
                ),
                "provisional": True,
                "coverage_keys": _make_coverage_keys(
                    c["subject"],
                    c["predicate"],
                    c["object_text"],
                    extras=[
                        f"subject:{(c['subject'] or '').lower()}",
                        f"predicate:{(c['predicate'] or '').lower()}",
                        f"scope:{(c['claim_scope'] or '').lower()}",
                    ],
                ),
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
            rel = _task_match(q["chunk_title"], q["question_text"])
            if is_task_scoped and rel < _MIN_RELEVANCE_THRESHOLD:
                continue
            trust = 0.55
            freshness = _compute_recency_score(q["created_at"])
            ts = _format_ts(q["created_at"])
            ctx = f" (re: {q['chunk_title']})" if q["chunk_title"] else ""
            text = f"{ts}[QUESTION] {q['question_text']}{ctx}"
            items.append(
                {
                    "type": "question",
                    "id": q["question_id"],
                    "text": text,
                    "tokens": _estimate_tokens(text),
                    "relevance": rel,
                    "trust": trust,
                    "freshness": freshness,
                    "score": _selection_score(
                        q["priority_score"],
                        rel,
                        trust,
                        freshness,
                        priorities["question_weight"],
                    ),
                    "provisional": False,
                    "coverage_keys": _make_coverage_keys(
                        q["question_text"],
                        q["chunk_title"],
                        extras=[f"question:{(q['question_type'] or '').lower()}"],
                    ),
                }
            )

    # 4. Enrichable/uncertain chunks (raw context)
    if priorities["chunk_weight"] > 0.2:
        chunk_limit = 100 if is_task_scoped else 30
        chunks = conn.execute(
            "SELECT chunk_id, entity_id, source_ref, title, body, materiality_score, "
            "state, created_at "
            "FROM context_chunks "
            "WHERE state IN ('enrichable', 'uncertain', 'awaiting_human') "
            "ORDER BY materiality_score DESC LIMIT ?",
            (chunk_limit,),
        ).fetchall()
        for ch in chunks:
            rel = _task_match(
                ch["source_ref"],
                ch["title"],
                ch["body"][:500],
                entity_id=ch["entity_id"],
                entity_name=ch["source_ref"] or ch["title"],
            )
            if is_task_scoped and rel < _MIN_RELEVANCE_THRESHOLD:
                continue
            trust = _CHUNK_TRUST_BY_STATE.get(ch["state"], 0.18)
            freshness = _compute_recency_score(ch["created_at"])
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
                    "group_key": _chunk_group_key(ch),
                    "relevance": rel,
                    "trust": trust,
                    "freshness": freshness,
                    "score": _selection_score(
                        ch["materiality_score"],
                        rel,
                        trust,
                        freshness,
                        priorities["chunk_weight"],
                    ),
                    "provisional": True,
                    "coverage_keys": _make_coverage_keys(
                        ch["title"],
                        ch["source_ref"],
                        ch["body"][:240],
                        extras=[
                            f"chunk_state:{(ch['state'] or '').lower()}",
                            f"source:{_chunk_group_key(ch)}",
                        ],
                    ),
                }
            )

    selection_meta = {"selection_strategy": "greedy"}
    if advanced_strategy.get("enabled") and advanced_strategy.get("submodular_enabled"):
        try:
            selection = _select_advanced_items(
                items,
                budget=budget,
                max_items_by_type=_MAX_ITEMS_BY_TYPE,
                max_chunks_per_group=_MAX_CHUNKS_PER_GROUP,
                strategy=advanced_strategy,
            )
            selected = selection["selected"]
            tokens_used = selection["tokens_used"]
            selection_meta = selection["metadata"]
        except (sqlite3.OperationalError, ValueError, KeyError) as exc:
            logger.warning(
                "Advanced context selector failed, using greedy fallback: %s", exc
            )
            selected, tokens_used, selection_meta = _greedy_select_items(items, budget)
            selection_meta["selector_error"] = str(exc)
    else:
        selected, tokens_used, selection_meta = _greedy_select_items(items, budget)

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

    pack_body = (
        "\n\n".join(body_parts)
        if body_parts
        else (
            "(no task-specific context found)"
            if is_task_scoped
            else "(no context available)"
        )
    )

    # Compute input signature for caching
    input_sig = f"{pack_type}:{target_ref}:{session_id}:{budget}"

    total_weight = sum(max(item["score"], 0.001) for item in selected) or 1.0
    relevance_score = (
        sum(item["relevance"] * max(item["score"], 0.001) for item in selected)
        / total_weight
    )
    quality_score = (
        sum(item["trust"] * max(item["score"], 0.001) for item in selected)
        / total_weight
    )
    freshness_score = (
        sum(item["freshness"] * max(item["score"], 0.001) for item in selected)
        / total_weight
    )

    pack_id = None
    if persist:
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
                freshness_score,
                now,
            ),
        )
        _prune_context_pack_history(
            conn,
            pack_type,
            target_ref,
            keep=4 if target_ref else 8,
        )

    if persist:
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
        "task_scoped": is_task_scoped,
        "persisted": persist,
        "freshness_score": round(freshness_score, 3),
        "relevance_score": round(relevance_score, 3),
        "quality_score": round(quality_score, 3),
        "advanced_context": {
            "enabled": bool(advanced_strategy.get("enabled")),
            "query_expansion_used": bool(advanced_strategy.get("query_expansion_used")),
            "submodular_used": selection_meta.get("selection_strategy") == "submodular",
            "selection_strategy": selection_meta.get("selection_strategy", "greedy"),
            "seed_entities": advanced_strategy.get("metadata", {}).get(
                "seed_entities", 0
            ),
            "expanded_entities": advanced_strategy.get("metadata", {}).get(
                "expanded_entities", 0
            ),
            "expansion_keywords": advanced_strategy.get("metadata", {}).get(
                "expansion_keywords", 0
            ),
        },
        "sections": {k: len(v) for k, v in sections.items()},
        "body": pack_body,
    }


def warm_recent_task_packs(
    conn: sqlite3.Connection,
    *,
    pack_type: str = "executor",
    session_id: str | None = None,
    token_budget: int | None = None,
    limit: int = 8,
) -> dict[str, int]:
    """Prebuild task-scoped packs for the most recent active, information-rich tasks."""
    rows = conn.execute(
        "SELECT t.id "
        "FROM tasks t "
        "LEFT JOIN task_entity_links tel ON tel.task_id = t.id "
        "WHERE t.status NOT IN ('archived', 'cancelled') "
        "AND (COALESCE(t.description, '') != '' "
        "     OR COALESCE(t.notes, '') != '' "
        "     OR COALESCE(t.project, '') != '' "
        "     OR tel.entity_id IS NOT NULL) "
        "GROUP BY t.id "
        "ORDER BY MAX(CASE WHEN tel.entity_id IS NOT NULL THEN 1 ELSE 0 END) DESC, "
        "         t.updated_at DESC "
        "LIMIT ?",
        (limit,),
    ).fetchall()

    built = 0
    with_context = 0
    for row in rows:
        result = build_context_pack(
            conn,
            pack_type=pack_type,
            target_ref=row["id"],
            session_id=session_id,
            token_budget=token_budget,
            persist=True,
        )
        built += 1
        if result.get("items_included", 0) > 0:
            with_context += 1

    return {"task_packs_built": built, "task_packs_with_context": with_context}


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
