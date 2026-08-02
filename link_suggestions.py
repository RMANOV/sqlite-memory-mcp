"""Deterministic, explainable task↔entity link suggestions.

The module owns candidate generation, pairwise scoring, decision capture, and
evaluation.  Community membership is an optional *weak* derived signal; it can
never create a canonical link by itself.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import unicodedata
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from db_utils import TaskDAO, fts_query, now_iso, tokenize_for_similarity

LINK_MODEL_VERSION = "explainable-pairwise-v1"
AUTO_ACCEPT_SOURCE = "auto_high_confidence"
AUTO_ACCEPT_LINK_TYPE = "auto_high_confidence"
AUTO_ACCEPT_MIN_SCORE = 0.43
AUTO_ACCEPT_MIN_MARGIN = 0.08
AUTO_ACCEPT_DAILY_LIMIT = 3
AUTO_ACCEPT_SCAN_LIMIT = 30
AUTO_ACCEPT_REVIEW_HOURS = 24
# Fail-fast cold start: do not wait for a human-labelled corpus before testing
# whether the weak community signal adds operational value.  Zero labels do
# not make the model "validated"; that state is reported explicitly below.
# Safety remains elsewhere: community has only 0.05 weight and can never
# authorize an automatic canonical link on its own.
MIN_LABELS_FOR_COMMUNITIES = 0
MIN_ACCEPTED_FOR_COMMUNITIES = 0
MIN_REJECTED_FOR_COMMUNITIES = 0
_MAX_CANDIDATES = 100
_FTS_CANDIDATES = 80
_SIMILAR_TASKS = 20

# Fixed, versioned weights.  Missing optional signals contribute zero; scores
# are intentionally not renormalized so values remain comparable across calls.
SIGNAL_WEIGHTS: dict[str, float] = {
    "mention": 0.25,
    "fts": 0.20,
    "project": 0.10,
    "source": 0.10,
    "graph": 0.15,
    "temporal": 0.05,
    "vector": 0.10,
    "community": 0.05,
}


def normalize_phrase(value: str | None) -> str:
    """Return a stable Unicode/case/spacing key for names and aliases."""
    normalized = unicodedata.normalize("NFKC", value or "").casefold()
    return " ".join(re.findall(r"\w+", normalized, flags=re.UNICODE))


def _contains_phrase(haystack_key: str, needle_key: str) -> bool:
    if not haystack_key or not needle_key:
        return False
    return f" {needle_key} " in f" {haystack_key} "


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
            (table,),
        ).fetchone()
        is not None
    )


def _safe_fts_query(text: str) -> str:
    # Select the 64 *longest* tokens, not the 64 first by codepoint.  Plain
    # ``sorted`` puts every ASCII token ahead of every Cyrillic one, so on a
    # Bulgarian corpus the cap filled with digits and Latin noise and the query
    # reached ``memory_fts`` carrying no Cyrillic at all.  Length is the
    # available proxy for content words here; ``w`` keeps ties deterministic.
    tokens = sorted(tokenize_for_similarity(text), key=lambda w: (-len(w), w))[:64]
    return fts_query(" ".join(tokens)) if tokens else ""


def _bounded_limit(limit: int) -> int:
    return max(1, min(int(limit), _MAX_CANDIDATES))


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _temporal_score(left: str | None, right: str | None) -> float:
    left_dt = _parse_time(left)
    right_dt = _parse_time(right)
    if left_dt is None or right_dt is None:
        return 0.0
    days = abs((left_dt - right_dt).total_seconds()) / 86400.0
    return 1.0 / (1.0 + days / 30.0)


def _alias_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    if not _table_exists(conn, "entity_aliases"):
        return []
    return conn.execute(
        "SELECT entity_id, alias, normalized_alias FROM entity_aliases "
        "ORDER BY entity_id, normalized_alias"
    ).fetchall()


def add_entity_alias(
    conn: sqlite3.Connection, entity_id: int, alias: str
) -> dict[str, Any]:
    alias = " ".join((alias or "").split())
    normalized = normalize_phrase(alias)
    if len(normalized) < 2:
        raise ValueError("alias must contain at least two normalized characters")
    entity = conn.execute(
        "SELECT name FROM entities WHERE id = ?", (entity_id,)
    ).fetchone()
    if entity is None:
        raise ValueError(f"entity {entity_id} not found")
    if normalized == normalize_phrase(entity["name"]):
        raise ValueError("alias is identical to the canonical entity name")
    cur = conn.execute(
        "INSERT OR IGNORE INTO entity_aliases "
        "(entity_id, alias, normalized_alias, created_at) VALUES (?, ?, ?, ?)",
        (entity_id, alias, normalized, now_iso()),
    )
    return {
        "entity_id": entity_id,
        "entity_name": entity["name"],
        "alias": alias,
        "created": cur.rowcount == 1,
    }


def remove_entity_alias(
    conn: sqlite3.Connection, entity_id: int, alias: str
) -> dict[str, Any]:
    normalized = normalize_phrase(alias)
    cur = conn.execute(
        "DELETE FROM entity_aliases WHERE entity_id = ? AND normalized_alias = ?",
        (entity_id, normalized),
    )
    return {"entity_id": entity_id, "alias": alias, "removed": cur.rowcount == 1}


def list_entity_aliases(
    conn: sqlite3.Connection, entity_id: int
) -> list[dict[str, Any]]:
    if not _table_exists(conn, "entity_aliases"):
        return []
    return [
        dict(row)
        for row in conn.execute(
            "SELECT alias, normalized_alias, created_at FROM entity_aliases "
            "WHERE entity_id = ? ORDER BY normalized_alias",
            (entity_id,),
        ).fetchall()
    ]


def _provenance_keys(
    conn: sqlite3.Connection, subject_kind: str, subject_refs: list[str]
) -> dict[str, set[tuple[str, str]]]:
    if not subject_refs or not _table_exists(conn, "provenance_links"):
        return {}
    placeholders = ",".join("?" for _ in subject_refs)
    rows = conn.execute(
        "SELECT subject_ref, source_kind, source_ref FROM provenance_links "
        f"WHERE subject_kind = ? AND subject_ref IN ({placeholders})",
        [subject_kind, *subject_refs],
    ).fetchall()
    result: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for row in rows:
        result[str(row["subject_ref"])].add(
            (str(row["source_kind"]), str(row["source_ref"]))
        )
    return result


def _relation_scores(
    conn: sqlite3.Connection,
    *,
    linked_ids: set[int],
    fts_seed_ids: list[int],
) -> tuple[dict[int, float], dict[int, set[str]]]:
    scores: dict[int, float] = {}
    reasons: dict[int, set[str]] = defaultdict(set)

    def apply(seed_ids: set[int], one_hop_score: float, two_hop_score: float) -> None:
        if not seed_ids:
            return
        placeholders = ",".join("?" for _ in seed_ids)
        params = list(seed_ids)
        rows = conn.execute(
            "SELECT from_id, to_id, relation_type FROM relations "
            f"WHERE from_id IN ({placeholders}) OR to_id IN ({placeholders})",
            params + params,
        ).fetchall()
        one_hop: set[int] = set()
        for row in rows:
            left, right = int(row["from_id"]), int(row["to_id"])
            candidate = right if left in seed_ids else left
            if candidate in seed_ids:
                continue
            one_hop.add(candidate)
            scores[candidate] = max(scores.get(candidate, 0.0), one_hop_score)
            reasons[candidate].add(f"relation:{row['relation_type']}")
        if not one_hop:
            return
        hop_ph = ",".join("?" for _ in one_hop)
        hop_list = list(one_hop)
        second_rows = conn.execute(
            "SELECT from_id, to_id, relation_type FROM relations "
            f"WHERE from_id IN ({hop_ph}) OR to_id IN ({hop_ph})",
            hop_list + hop_list,
        ).fetchall()
        for row in second_rows:
            left, right = int(row["from_id"]), int(row["to_id"])
            candidate = right if left in one_hop else left
            if candidate in seed_ids or candidate in one_hop:
                continue
            scores[candidate] = max(scores.get(candidate, 0.0), two_hop_score)
            reasons[candidate].add(f"two_hop:{row['relation_type']}")

    apply(linked_ids, 1.0, 0.65)
    apply(set(fts_seed_ids), 0.45, 0.25)
    return scores, reasons


def _similar_task_entity_scores(
    conn: sqlite3.Connection, task_id: str, fts_q: str
) -> tuple[dict[int, float], dict[int, set[str]]]:
    if not fts_q:
        return {}, {}
    try:
        similar = conn.execute(
            "SELECT t.id, t.title, rank FROM tasks_fts "
            "JOIN tasks AS t ON t.rowid = tasks_fts.rowid "
            "WHERE tasks_fts MATCH ? AND t.id != ? "
            "ORDER BY rank, t.id LIMIT ?",
            (fts_q, task_id, _SIMILAR_TASKS),
        ).fetchall()
    except sqlite3.Error:
        return {}, {}
    if not similar:
        return {}, {}
    task_rank = {str(row["id"]): index for index, row in enumerate(similar, 1)}
    placeholders = ",".join("?" for _ in task_rank)
    links = conn.execute(
        "SELECT task_id, entity_id FROM task_entity_links "
        f"WHERE task_id IN ({placeholders})",
        list(task_rank),
    ).fetchall()
    scores: dict[int, float] = {}
    reasons: dict[int, set[str]] = defaultdict(set)
    for row in links:
        entity_id = int(row["entity_id"])
        rank = task_rank[str(row["task_id"])]
        score = 0.8 / math.sqrt(rank)
        scores[entity_id] = max(scores.get(entity_id, 0.0), score)
        reasons[entity_id].add(f"similar_task:{row['task_id']}")
    return scores, reasons


def _active_community_scores(
    conn: sqlite3.Connection, task_id: str
) -> tuple[dict[int, float], str | None]:
    if not _table_exists(conn, "link_community_runs"):
        return {}, None
    run = conn.execute(
        "SELECT run_id, primary_resolution FROM link_community_runs "
        "WHERE active = 1 ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    if run is None:
        return {}, None
    task_membership = conn.execute(
        "SELECT community_id FROM link_community_memberships "
        "WHERE run_id = ? AND resolution = ? "
        "AND node_kind = 'task' AND node_ref = ?",
        (run["run_id"], run["primary_resolution"], task_id),
    ).fetchone()
    if task_membership is None:
        return {}, str(run["run_id"])
    rows = conn.execute(
        "SELECT node_ref FROM link_community_memberships "
        "WHERE run_id = ? AND resolution = ? AND node_kind = 'entity' "
        "AND community_id = ? ORDER BY node_ref",
        (run["run_id"], run["primary_resolution"], task_membership["community_id"]),
    ).fetchall()
    return {int(row["node_ref"]): 1.0 for row in rows}, str(run["run_id"])


def _vector_scores(
    conn: sqlite3.Connection, search_text: str
) -> tuple[dict[int, float], dict[int, int]]:
    try:
        from vec_search import vector_search

        rows = vector_search(conn, search_text, limit=50)
    except (ImportError, RuntimeError, sqlite3.Error):
        return {}, {}
    scores: dict[int, float] = {}
    ranks: dict[int, int] = {}
    for index, row in enumerate(rows, 1):
        entity_id = int(row["eid"])
        distance = float(row.get("distance", 1.0))
        scores[entity_id] = max(0.0, min(1.0, 1.0 - distance))
        ranks[entity_id] = index
    return scores, ranks


def suggest_links(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    limit: int = 5,
    include_vector: bool = False,
    include_linked: bool = False,
    include_decided: bool = False,
) -> dict[str, Any]:
    """Return deterministic task→entity candidates with score receipts."""
    limit = _bounded_limit(limit)
    task = conn.execute(
        "SELECT id, title, description, notes, project, created_at, updated_at "
        "FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if task is None:
        raise ValueError(f"task {task_id} not found")

    search_text = " ".join(
        part for part in (task["title"], task["description"], task["notes"]) if part
    )
    search_key = normalize_phrase(search_text)
    task_tokens = set(tokenize_for_similarity(search_text))
    fts_q = _safe_fts_query(search_text)

    entities = conn.execute(
        "SELECT id, name, entity_type, project, created_at, updated_at "
        "FROM entities ORDER BY id"
    ).fetchall()
    entities_by_id = {int(row["id"]): row for row in entities}
    aliases_by_entity: dict[int, list[tuple[str, str]]] = defaultdict(list)
    for row in _alias_rows(conn):
        aliases_by_entity[int(row["entity_id"])].append(
            (str(row["alias"]), str(row["normalized_alias"]))
        )

    features: dict[int, dict[str, float]] = defaultdict(
        lambda: {key: 0.0 for key in SIGNAL_WEIGHTS}
    )
    reasons: dict[int, set[str]] = defaultdict(set)
    alias_hits: dict[int, list[str]] = defaultdict(list)

    for entity in entities:
        entity_id = int(entity["id"])
        name_key = normalize_phrase(entity["name"])
        if _contains_phrase(search_key, name_key):
            features[entity_id]["mention"] = 1.0
            reasons[entity_id].add("exact_entity_name")
        for alias, alias_key in aliases_by_entity.get(entity_id, []):
            if _contains_phrase(search_key, alias_key):
                features[entity_id]["mention"] = 1.0
                alias_hits[entity_id].append(alias)
                reasons[entity_id].add(f"exact_alias:{alias}")
        if task["project"] and entity["project"]:
            if normalize_phrase(task["project"]) == normalize_phrase(entity["project"]):
                features[entity_id]["project"] = 1.0
                reasons[entity_id].add("same_project")

    fts_rank: dict[int, int] = {}
    if fts_q:
        try:
            fts_rows = conn.execute(
                "SELECT rowid, rank FROM memory_fts WHERE memory_fts MATCH ? "
                "ORDER BY rank, rowid LIMIT ?",
                (fts_q, _FTS_CANDIDATES),
            ).fetchall()
        except sqlite3.Error:
            fts_rows = []
        for index, row in enumerate(fts_rows, 1):
            entity_id = int(row["rowid"])
            if entity_id not in entities_by_id:
                continue
            fts_rank[entity_id] = index
            features[entity_id]["fts"] = 1.0 / math.sqrt(index)
            reasons[entity_id].add(f"fts_rank:{index}")

    linked_ids = TaskDAO.get_linked_entity_ids(conn, task_id)
    graph_scores, graph_reasons = _relation_scores(
        conn,
        linked_ids=linked_ids,
        fts_seed_ids=list(fts_rank)[:5],
    )
    similar_scores, similar_reasons = _similar_task_entity_scores(conn, task_id, fts_q)
    for entity_id in set(graph_scores) | set(similar_scores):
        features[entity_id]["graph"] = max(
            graph_scores.get(entity_id, 0.0),
            similar_scores.get(entity_id, 0.0),
        )
        reasons[entity_id].update(graph_reasons.get(entity_id, set()))
        reasons[entity_id].update(similar_reasons.get(entity_id, set()))

    task_sources = _provenance_keys(conn, "task", [task_id]).get(task_id, set())
    if task_sources:
        entity_refs: list[str] = []
        ref_to_id: dict[str, int] = {}
        for entity in entities:
            entity_id = int(entity["id"])
            for ref in (str(entity_id), str(entity["name"])):
                entity_refs.append(ref)
                ref_to_id[ref] = entity_id
        entity_sources = _provenance_keys(conn, "entity", entity_refs)
        for ref, source_keys in entity_sources.items():
            shared = sorted(task_sources & source_keys)
            if not shared:
                continue
            entity_id = ref_to_id[ref]
            features[entity_id]["source"] = 1.0
            reasons[entity_id].update(
                f"shared_source:{kind}:{source}" for kind, source in shared[:3]
            )

    community_scores, community_run = _active_community_scores(conn, task_id)
    for entity_id, score in community_scores.items():
        if entity_id in entities_by_id:
            features[entity_id]["community"] = score
            reasons[entity_id].add(f"same_community:{community_run}")

    vector_rank: dict[int, int] = {}
    if include_vector:
        vector_scores, vector_rank = _vector_scores(conn, search_text)
        for entity_id, score in vector_scores.items():
            if entity_id not in entities_by_id:
                continue
            features[entity_id]["vector"] = score
            reasons[entity_id].add(f"vector_rank:{vector_rank[entity_id]}")

    decisions: dict[int, sqlite3.Row] = {}
    if _table_exists(conn, "link_suggestion_decisions"):
        decisions = {
            int(row["entity_id"]): row
            for row in conn.execute(
                "SELECT entity_id, decision, decision_source, updated_at "
                "FROM link_suggestion_decisions WHERE task_id = ?",
                (task_id,),
            ).fetchall()
        }

    # Evaluation must be able to rank known labels and links even when the
    # current candidate generators miss them.
    if include_linked:
        for entity_id in linked_ids:
            features[entity_id]
    if include_decided:
        for entity_id in decisions:
            features[entity_id]

    suggestions: list[dict[str, Any]] = []
    for entity_id, signal_values in features.items():
        entity = entities_by_id.get(entity_id)
        if entity is None:
            continue
        decision_row = decisions.get(entity_id)
        if not include_linked and entity_id in linked_ids:
            continue
        if not include_decided and decision_row is not None:
            continue
        if not reasons.get(entity_id) and not include_linked and not include_decided:
            continue

        signal_values["temporal"] = _temporal_score(
            task["updated_at"] or task["created_at"],
            entity["updated_at"] or entity["created_at"],
        )
        contributions = {
            signal: round(signal_values[signal] * weight, 6)
            for signal, weight in SIGNAL_WEIGHTS.items()
        }
        score = sum(contributions.values())
        entity_text = " ".join(
            [
                str(entity["name"]),
                *[alias for alias, _key in aliases_by_entity.get(entity_id, [])],
            ]
        )
        shared_keywords = sorted(
            task_tokens & set(tokenize_for_similarity(entity_text))
        )[:10]
        suggestions.append(
            {
                "entity_id": entity_id,
                "entity_name": entity["name"],
                "entity_type": entity["entity_type"],
                "score": round(score, 6),
                "signals": {
                    "raw": {
                        key: round(value, 6) for key, value in signal_values.items()
                    },
                    "weights": SIGNAL_WEIGHTS,
                    "contributions": contributions,
                },
                "reasons": sorted(reasons.get(entity_id, set())),
                "matched_aliases": sorted(alias_hits.get(entity_id, [])),
                "shared_keywords": shared_keywords,
                "fts_rank": fts_rank.get(entity_id),
                "vector_rank": vector_rank.get(entity_id),
                "existing_decision": (
                    {
                        "decision": decision_row["decision"],
                        "source": decision_row["decision_source"],
                        "updated_at": decision_row["updated_at"],
                    }
                    if decision_row is not None
                    else None
                ),
            }
        )

    suggestions.sort(
        key=lambda item: (
            -float(item["score"]),
            normalize_phrase(str(item["entity_name"])),
            int(item["entity_id"]),
        )
    )
    for rank, item in enumerate(suggestions, 1):
        item["rank"] = rank

    return {
        "task_id": task_id,
        "model_version": LINK_MODEL_VERSION,
        "include_vector": include_vector,
        "community_run": community_run,
        "suggestions": suggestions[:limit],
        "candidate_count": len(suggestions),
    }


def record_link_decision(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    entity_id: int,
    decision: str,
    decided_by: str,
    decision_source: str = "suggestion_review",
    accepted_link_type: str = "accepted_suggestion",
) -> dict[str, Any]:
    """Record one reviewed suggestion and synchronize only accepted links."""
    decision = (decision or "").strip().lower()
    if decision not in {"accepted", "rejected"}:
        raise ValueError("decision must be 'accepted' or 'rejected'")
    decided_by = (decided_by or "").strip()
    if not decided_by:
        raise ValueError("decided_by is required")
    if not TaskDAO.exists(conn, task_id):
        raise ValueError(f"task {task_id} not found")
    entity = conn.execute(
        "SELECT id, name FROM entities WHERE id = ?", (entity_id,)
    ).fetchone()
    if entity is None:
        raise ValueError(f"entity {entity_id} not found")

    existing_link = conn.execute(
        "SELECT link_type FROM task_entity_links WHERE task_id = ? AND entity_id = ?",
        (task_id, entity_id),
    ).fetchone()
    if decision == "rejected" and existing_link is not None:
        if existing_link["link_type"] == "manual":
            raise ValueError(
                "cannot reject an existing manual link; unlink it explicitly first"
            )
        TaskDAO.unlink_entity(conn, task_id, entity_id)

    snapshot = suggest_links(
        conn,
        task_id,
        limit=_MAX_CANDIDATES,
        include_vector=False,
        include_linked=True,
        include_decided=True,
    )
    suggestion = next(
        (
            item
            for item in snapshot["suggestions"]
            if int(item["entity_id"]) == entity_id
        ),
        None,
    )
    now = now_iso()
    existing_decision = conn.execute(
        "SELECT decision_id, created_at FROM link_suggestion_decisions "
        "WHERE task_id = ? AND entity_id = ?",
        (task_id, entity_id),
    ).fetchone()
    decision_id = (
        str(existing_decision["decision_id"])
        if existing_decision is not None
        else str(uuid.uuid4())
    )
    created_at = (
        str(existing_decision["created_at"]) if existing_decision is not None else now
    )
    score_receipt = (
        {
            "signals": suggestion["signals"],
            "reasons": suggestion["reasons"],
            "matched_aliases": suggestion["matched_aliases"],
            "shared_keywords": suggestion["shared_keywords"],
        }
        if suggestion
        else {}
    )
    signals_json = json.dumps(
        score_receipt,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    conn.execute(
        "INSERT INTO link_suggestion_decisions "
        "(decision_id, task_id, entity_id, decision, score, rank_at_decision, "
        "signals_json, model_version, decision_source, decided_by, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(task_id, entity_id) DO UPDATE SET "
        "decision = excluded.decision, score = excluded.score, "
        "rank_at_decision = excluded.rank_at_decision, "
        "signals_json = excluded.signals_json, "
        "model_version = excluded.model_version, "
        "decision_source = excluded.decision_source, "
        "decided_by = excluded.decided_by, updated_at = excluded.updated_at",
        (
            decision_id,
            task_id,
            entity_id,
            decision,
            suggestion["score"] if suggestion else None,
            suggestion["rank"] if suggestion else None,
            signals_json,
            LINK_MODEL_VERSION,
            decision_source,
            decided_by,
            created_at,
            now,
        ),
    )
    if decision == "accepted":
        if existing_link is None:
            TaskDAO.link_entity(
                conn,
                task_id,
                entity_id,
                link_type=accepted_link_type,
                score=suggestion["score"] if suggestion else None,
                created_at=now,
            )
        elif accepted_link_type == "manual" and existing_link["link_type"] != "manual":
            TaskDAO.link_entity(
                conn,
                task_id,
                entity_id,
                link_type="manual",
                score=suggestion["score"] if suggestion else None,
                created_at=now,
            )

    progress = decision_progress(conn)
    return {
        "decision_id": decision_id,
        "task_id": task_id,
        "entity_id": entity_id,
        "entity_name": entity["name"],
        "decision": decision,
        "score": suggestion["score"] if suggestion else None,
        "rank_at_decision": suggestion["rank"] if suggestion else None,
        "model_version": LINK_MODEL_VERSION,
        "label_progress": progress,
    }


def _auto_accept_receipt(
    suggestions: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Return a strict auto-accept receipt or ``None``.

    A scalar score is not enough.  The best candidate must contain an exact
    canonical-name/alias mention, strong contextual corroboration or a
    distinctive exact name with top FTS disambiguation, and a clear lead over
    the runner-up. Community membership and temporal proximity can increase a
    score but can never authorize auto-acceptance.
    """
    if not suggestions:
        return None
    winner = suggestions[0]
    raw = winner["signals"]["raw"]
    score = float(winner["score"])
    runner_up_score = float(suggestions[1]["score"]) if len(suggestions) > 1 else 0.0
    margin = score - runner_up_score
    contextual_corroborators = {
        "project": float(raw.get("project", 0.0)) >= 1.0,
        "source": float(raw.get("source", 0.0)) >= 1.0,
        "graph": float(raw.get("graph", 0.0)) >= 0.65,
    }
    name_key = normalize_phrase(str(winner["entity_name"]))
    distinctive_exact = (
        len(name_key.split()) >= 2
        and len(name_key.replace(" ", "")) >= 8
        and float(raw.get("fts", 0.0)) >= 0.70
    )
    if (
        float(raw.get("mention", 0.0)) < 1.0
        or score < AUTO_ACCEPT_MIN_SCORE
        or margin < AUTO_ACCEPT_MIN_MARGIN
        or not (any(contextual_corroborators.values()) or distinctive_exact)
    ):
        return None
    corroborators = [
        name for name, present in contextual_corroborators.items() if present
    ]
    if distinctive_exact:
        corroborators.append("distinctive_exact+fts")
    return {
        "winner": winner,
        "score": round(score, 6),
        "runner_up_score": round(runner_up_score, 6),
        "margin": round(margin, 6),
        "corroborators": sorted(corroborators),
        "policy": {
            "minimum_score": AUTO_ACCEPT_MIN_SCORE,
            "minimum_margin": AUTO_ACCEPT_MIN_MARGIN,
            "requires_exact_mention": True,
            "community_can_authorize": False,
            "vector_can_authorize": False,
        },
    }


def auto_accept_high_confidence_links(
    conn: sqlite3.Connection,
    *,
    daily_limit: int = AUTO_ACCEPT_DAILY_LIMIT,
    scan_limit: int = AUTO_ACCEPT_SCAN_LIMIT,
    as_of: datetime | None = None,
) -> dict[str, Any]:
    """Silently accept only the strongest reversible task→entity links.

    The scan is intentionally bounded and global: at most ``daily_limit`` links
    per UTC day, selected from recent active rows by score and winner margin.
    It never invokes vector models, never uses community membership as an
    authorization signal, and never adds a second automatic link to a task.
    """
    daily_limit = max(0, min(int(daily_limit), AUTO_ACCEPT_DAILY_LIMIT))
    scan_limit = max(1, min(int(scan_limit), AUTO_ACCEPT_SCAN_LIMIT))
    now = as_of or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    existing_today = int(
        conn.execute(
            "SELECT COUNT(*) FROM link_suggestion_decisions "
            "WHERE decision_source LIKE ? "
            "AND created_at >= ?",
            (f"{AUTO_ACCEPT_SOURCE}%", day_start.isoformat()),
        ).fetchone()[0]
    )
    remaining = max(0, daily_limit - existing_today)
    if remaining == 0:
        return {
            "accepted": [],
            "accepted_count": 0,
            "existing_today": existing_today,
            "daily_limit": daily_limit,
            "scan_count": 0,
        }

    task_rows = conn.execute(
        "SELECT t.id, t.title, t.priority, t.updated_at "
        "FROM tasks AS t "
        "WHERE t.status IN ('not_started', 'in_progress') "
        "AND NOT EXISTS ("
        "  SELECT 1 FROM task_entity_links AS tel WHERE tel.task_id = t.id"
        ") "
        "ORDER BY CASE COALESCE(t.priority, 'medium') "
        "  WHEN 'critical' THEN 0 WHEN 'high' THEN 1 "
        "  WHEN 'medium' THEN 2 WHEN 'low' THEN 3 ELSE 4 END, "
        "t.updated_at DESC, t.id "
        "LIMIT ?",
        (scan_limit,),
    ).fetchall()

    eligible: list[dict[str, Any]] = []
    for scan_rank, task in enumerate(task_rows):
        result = suggest_links(
            conn,
            str(task["id"]),
            limit=2,
            include_vector=False,
        )
        receipt = _auto_accept_receipt(result["suggestions"])
        if receipt is None:
            continue
        eligible.append(
            {
                "task_id": str(task["id"]),
                "task_title": str(task["title"]),
                "scan_rank": scan_rank,
                "receipt": receipt,
            }
        )
    eligible.sort(
        key=lambda item: (
            -float(item["receipt"]["score"]),
            -float(item["receipt"]["margin"]),
            int(item["scan_rank"]),
            item["task_id"],
            int(item["receipt"]["winner"]["entity_id"]),
        )
    )

    accepted: list[dict[str, Any]] = []
    conn.execute("SAVEPOINT link_auto_accept")
    try:
        for candidate in eligible[:remaining]:
            winner = candidate["receipt"]["winner"]
            decision = record_link_decision(
                conn,
                task_id=candidate["task_id"],
                entity_id=int(winner["entity_id"]),
                decision="accepted",
                decided_by=f"system/{LINK_MODEL_VERSION}",
                decision_source=AUTO_ACCEPT_SOURCE,
                accepted_link_type=AUTO_ACCEPT_LINK_TYPE,
            )
            accepted.append(
                {
                    **decision,
                    "task_title": candidate["task_title"],
                    "entity_name": winner["entity_name"],
                    "reasons": winner["reasons"],
                    "margin": candidate["receipt"]["margin"],
                    "corroborators": candidate["receipt"]["corroborators"],
                    "reversible": True,
                }
            )
        conn.execute("RELEASE SAVEPOINT link_auto_accept")
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT link_auto_accept")
        conn.execute("RELEASE SAVEPOINT link_auto_accept")
        raise

    return {
        "accepted": accepted,
        "accepted_count": len(accepted),
        "existing_today": existing_today,
        "daily_limit": daily_limit,
        "scan_count": len(task_rows),
        "eligible_count": len(eligible),
        "review_until": (now + timedelta(hours=AUTO_ACCEPT_REVIEW_HOURS)).isoformat(),
    }


def decision_progress(conn: sqlite3.Connection) -> dict[str, Any]:
    """Return label counts and the fail-fast Leiden eligibility state."""
    rows = conn.execute(
        "SELECT decision, decision_source, COUNT(*) AS n "
        "FROM link_suggestion_decisions GROUP BY decision, decision_source"
    ).fetchall()
    by_source: dict[str, dict[str, int]] = defaultdict(
        lambda: {"accepted": 0, "rejected": 0}
    )
    for row in rows:
        by_source[str(row["decision_source"])][str(row["decision"])] = int(row["n"])
    qualified = {
        key: value
        for key, value in by_source.items()
        if key not in {"legacy_manual_link", AUTO_ACCEPT_SOURCE}
    }
    accepted = sum(value["accepted"] for value in qualified.values())
    rejected = sum(value["rejected"] for value in qualified.values())
    total = accepted + rejected
    ready = (
        total >= MIN_LABELS_FOR_COMMUNITIES
        and accepted >= MIN_ACCEPTED_FOR_COMMUNITIES
        and rejected >= MIN_REJECTED_FOR_COMMUNITIES
    )
    return {
        "qualified_total": total,
        "qualified_accepted": accepted,
        "qualified_rejected": rejected,
        "legacy_manual_accepted": by_source["legacy_manual_link"]["accepted"],
        "by_source": dict(by_source),
        "gate": {
            "ready": ready,
            "mode": "zero_label_fail_fast",
            "unvalidated": total == 0,
            "minimum_total": MIN_LABELS_FOR_COMMUNITIES,
            "minimum_accepted": MIN_ACCEPTED_FOR_COMMUNITIES,
            "minimum_rejected": MIN_REJECTED_FOR_COMMUNITIES,
            "remaining_total": max(0, MIN_LABELS_FOR_COMMUNITIES - total),
            "remaining_accepted": max(0, MIN_ACCEPTED_FOR_COMMUNITIES - accepted),
            "remaining_rejected": max(0, MIN_REJECTED_FOR_COMMUNITIES - rejected),
        },
    }


def evaluate_link_suggestions(
    conn: sqlite3.Connection, *, k: int = 5
) -> dict[str, Any]:
    """Evaluate current ranking against reviewed labels without inventing negatives."""
    k = max(1, min(int(k), 20))
    decisions = conn.execute(
        "SELECT task_id, entity_id, decision, decision_source "
        "FROM link_suggestion_decisions "
        "WHERE decision_source NOT IN ('legacy_manual_link', ?) "
        "ORDER BY task_id, entity_id",
        (AUTO_ACCEPT_SOURCE,),
    ).fetchall()
    by_task: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in decisions:
        by_task[str(row["task_id"])].append(row)

    accepted_total = accepted_top_k = 0
    rejected_total = rejected_top_k = 0
    missing: list[dict[str, Any]] = []
    for task_id, labels in by_task.items():
        result = suggest_links(
            conn,
            task_id,
            limit=_MAX_CANDIDATES,
            include_vector=False,
            include_linked=True,
            include_decided=True,
        )
        ranks = {
            int(item["entity_id"]): int(item["rank"]) for item in result["suggestions"]
        }
        for label in labels:
            rank = ranks.get(int(label["entity_id"]))
            if label["decision"] == "accepted":
                accepted_total += 1
                accepted_top_k += int(rank is not None and rank <= k)
            else:
                rejected_total += 1
                rejected_top_k += int(rank is not None and rank <= k)
            if rank is None:
                missing.append(
                    {
                        "task_id": task_id,
                        "entity_id": int(label["entity_id"]),
                        "decision": label["decision"],
                    }
                )

    labeled_top_k = accepted_top_k + rejected_top_k
    progress = decision_progress(conn)
    return {
        "model_version": LINK_MODEL_VERSION,
        "k": k,
        "labels": {
            "accepted": accepted_total,
            "rejected": rejected_total,
            "total": accepted_total + rejected_total,
        },
        "positive_recall_at_k": (
            round(accepted_top_k / accepted_total, 6) if accepted_total else None
        ),
        "labeled_precision_at_k": (
            round(accepted_top_k / labeled_top_k, 6) if labeled_top_k else None
        ),
        "rejected_in_top_k_rate": (
            round(rejected_top_k / rejected_total, 6) if rejected_total else None
        ),
        "accepted_in_top_k": accepted_top_k,
        "rejected_in_top_k": rejected_top_k,
        "unranked_labels": missing,
        "label_progress": progress,
        "methodology": (
            "Precision is labeled-only until every displayed top-k candidate is reviewed; "
            "unlabeled candidates are never counted as false positives."
        ),
    }


__all__ = [
    "AUTO_ACCEPT_DAILY_LIMIT",
    "AUTO_ACCEPT_LINK_TYPE",
    "AUTO_ACCEPT_MIN_MARGIN",
    "AUTO_ACCEPT_MIN_SCORE",
    "AUTO_ACCEPT_REVIEW_HOURS",
    "AUTO_ACCEPT_SOURCE",
    "LINK_MODEL_VERSION",
    "SIGNAL_WEIGHTS",
    "add_entity_alias",
    "auto_accept_high_confidence_links",
    "decision_progress",
    "evaluate_link_suggestions",
    "list_entity_aliases",
    "normalize_phrase",
    "record_link_decision",
    "remove_entity_alias",
    "suggest_links",
]
