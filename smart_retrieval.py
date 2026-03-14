"""Smart Retrieval (Layer 1) — BM25 + multi-signal re-ranking.

Always-on, query-time enrichment: FTS5 top-N → Python re-rank with 6 signals → top-K.
Falls back gracefully to pure BM25 on any error.
"""

from __future__ import annotations

import math
import sqlite3
from datetime import datetime, timezone
from typing import Any

# ── Tunable constants ──────────────────────────────────────────────────────

RECENCY_HALF_LIFE_DAYS = 14
PROJECT_BOOST = 1.5
GRAPH_BOOST_1HOP = 1.8
GRAPH_BOOST_2HOP = 1.3
RICHNESS_DIVISOR = 3.0
FACT_BOOST = 1.4
SESSION_BOOST = 2.0
RERANKING_POOL_SIZE = 100


# ── Scoring helpers ────────────────────────────────────────────────────────


def compute_recency_decay(updated_at: str | None, half_life_days: float = RECENCY_HALF_LIFE_DAYS) -> float:
    """Exponential decay: 2^(-days / half_life). Returns 1.0 if unparseable."""
    if not updated_at:
        return 0.5
    try:
        dt = datetime.fromisoformat(updated_at)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        days = (datetime.now(timezone.utc) - dt).total_seconds() / 86400
        return math.pow(2, -days / half_life_days)
    except (ValueError, TypeError):
        return 0.5


def compute_composite_score(
    bm25_rank: float,
    updated_at: str | None,
    project: str | None,
    current_project: str | None,
    obs_count: int,
    relation_hops: int,
    has_canonical_facts: bool,
    in_active_session: bool,
) -> float:
    """Multiplicative composite score from 6 signals.

    bm25_rank is negative (lower = better match in FTS5), so we invert it.
    """
    # Base: invert BM25 rank (FTS5 rank is negative, closer to 0 = worse match)
    base = 1.0 / (1.0 + abs(bm25_rank))

    # Signal 1: recency
    recency = compute_recency_decay(updated_at)

    # Signal 2: project affinity
    proj = PROJECT_BOOST if (current_project and project == current_project) else 1.0

    # Signal 3: graph proximity
    if relation_hops == 1:
        graph = GRAPH_BOOST_1HOP
    elif relation_hops == 2:
        graph = GRAPH_BOOST_2HOP
    else:
        graph = 1.0

    # Signal 4: observation richness (log scale, capped)
    richness = 1.0 + math.log1p(obs_count) / RICHNESS_DIVISOR

    # Signal 5: canonical facts existence
    facts = FACT_BOOST if has_canonical_facts else 1.0

    # Signal 6: active session
    session = SESSION_BOOST if in_active_session else 1.0

    return base * recency * proj * graph * richness * facts * session


# ── Main re-ranking entry point ────────────────────────────────────────────


def rerank_entities(
    conn: sqlite3.Connection,
    fts_rows: list[Any],
    current_project: str | None,
    session_id: str | None,
    query_entity_ids: list[int] | None,
    limit: int = 50,
) -> list[dict]:
    """Re-rank FTS5 results using composite scoring.

    Args:
        conn: Active SQLite connection (within transaction).
        fts_rows: Rows from FTS5 query with eid, name, entity_type, project, rank.
        current_project: Current project for affinity boost.
        session_id: Current session ID for active-file boost.
        query_entity_ids: Entity IDs from the query itself (for graph proximity).
        limit: Max results to return.

    Returns:
        List of dicts with entity info + _meta scoring details.
    """
    if not fts_rows:
        return []

    eids = [r["eid"] for r in fts_rows]
    ph = ",".join("?" * len(eids))

    # Batch-fetch observation counts
    obs_counts: dict[int, int] = {}
    for row in conn.execute(
        f"SELECT entity_id, COUNT(*) AS cnt FROM observations "
        f"WHERE entity_id IN ({ph}) GROUP BY entity_id",
        eids,
    ):
        obs_counts[row["entity_id"]] = row["cnt"]

    # Batch-fetch updated_at timestamps
    updated_map: dict[int, str] = {}
    for row in conn.execute(
        f"SELECT id, updated_at FROM entities WHERE id IN ({ph})", eids
    ):
        updated_map[row["id"]] = row["updated_at"]

    # Batch-fetch 1-hop relations (entities connected to query entities)
    one_hop: set[int] = set()
    two_hop: set[int] = set()
    if query_entity_ids:
        q_ph = ",".join("?" * len(query_entity_ids))
        for row in conn.execute(
            f"SELECT DISTINCT to_id FROM relations WHERE from_id IN ({q_ph}) "
            f"UNION SELECT DISTINCT from_id FROM relations WHERE to_id IN ({q_ph})",
            query_entity_ids + query_entity_ids,
        ):
            one_hop.add(row[0])
        # Expand to 2-hop (only if manageable)
        if one_hop and len(one_hop) < 500:
            hop_ph = ",".join("?" * len(one_hop))
            hop_list = list(one_hop)
            for row in conn.execute(
                f"SELECT DISTINCT to_id FROM relations WHERE from_id IN ({hop_ph}) "
                f"UNION SELECT DISTINCT from_id FROM relations WHERE to_id IN ({hop_ph})",
                hop_list + hop_list,
            ):
                if row[0] not in one_hop:
                    two_hop.add(row[0])

    # Batch-check canonical_facts subjects
    facts_subjects: set[str] = set()
    eid_names = {r["eid"]: r["name"] for r in fts_rows}
    name_list = list(eid_names.values())
    if name_list:
        n_ph = ",".join("?" * len(name_list))
        try:
            for row in conn.execute(
                f"SELECT DISTINCT subject FROM canonical_facts WHERE subject IN ({n_ph})",
                name_list,
            ):
                facts_subjects.add(row["subject"])
        except Exception:
            pass  # canonical_facts may not exist yet

    # Session active files
    active_file_entities: set[str] = set()
    if session_id:
        try:
            srow = conn.execute(
                "SELECT active_files FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if srow and srow["active_files"]:
                import json
                files = json.loads(srow["active_files"])
                if isinstance(files, list):
                    active_file_entities = set(files)
        except Exception:
            pass

    # Compute scores and build results
    scored: list[tuple[float, dict]] = []
    for r in fts_rows:
        eid = r["eid"]
        name = r["name"]

        # Determine graph proximity
        if eid in one_hop:
            hops = 1
        elif eid in two_hop:
            hops = 2
        else:
            hops = 0

        score = compute_composite_score(
            bm25_rank=r["rank"],
            updated_at=updated_map.get(eid),
            project=r["project"],
            current_project=current_project,
            obs_count=obs_counts.get(eid, 0),
            relation_hops=hops,
            has_canonical_facts=name in facts_subjects,
            in_active_session=name in active_file_entities,
        )

        scored.append((score, {
            "eid": eid,
            "name": name,
            "entity_type": r["entity_type"],
            "project": r["project"],
            "_score": round(score, 6),
        }))

    # Sort descending by score, truncate
    scored.sort(key=lambda x: x[0], reverse=True)
    return [item for _, item in scored[:limit]]
