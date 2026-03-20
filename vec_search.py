"""Optional vector search layer using sqlite-vec + sentence-transformers.

Adds semantic similarity search (cosine distance) alongside FTS5 BM25.
Gracefully degrades to FTS5-only when dependencies are not installed.

Architecture:
    embed_text() → float32 bytes (384-dim MiniLM-L6-v2)
    vec_sync_entity() → called after FTS sync on entity writes
    vector_search() → KNN query against entity_embeddings vec0 table
    rrf_merge() → Reciprocal Rank Fusion of FTS5 + vector results

The merged results feed into the existing 6-signal reranker (smart_retrieval.py)
without any changes to that module.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from typing import Any

logger = logging.getLogger("sqlite-kb")


# ── Availability check ─────────────────────────────────────────────────

try:
    import sqlite_vec

    _HAS_VEC = True
except ImportError:
    _HAS_VEC = False

try:
    from sentence_transformers import SentenceTransformer

    _HAS_ST = True
except ImportError:
    _HAS_ST = False

VEC_AVAILABLE: bool = _HAS_VEC and _HAS_ST


# ── Model singleton (lazy loaded) ─────────────────────────────────────

_model: SentenceTransformer | None = None
_model_lock = threading.Lock()
_MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"
EMBEDDING_DIM = 384
MAX_OBS_FOR_EMBEDDING = 20  # MiniLM-L6-v2 has 256-token limit; cap observations to fit


def _get_model() -> SentenceTransformer:
    """Lazy-load the embedding model on first use (thread-safe)."""
    global _model
    if _model is None:
        with _model_lock:
            if _model is None:
                _model = SentenceTransformer(_MODEL_NAME)
                logger.info("Loaded embedding model: %s", _MODEL_NAME)
    return _model


# ── Extension loader ───────────────────────────────────────────────────


def load_vec(conn: sqlite3.Connection) -> bool:
    """Load sqlite-vec extension on a connection.

    Uses a sentinel table query to detect if already loaded, avoiding
    redundant enable_load_extension cycles.
    """
    if not _HAS_VEC:
        return False
    # Fast check: if vec0 is already usable, skip reload
    try:
        conn.execute("SELECT vec_version()")
        return True
    except Exception:
        pass
    try:
        conn.enable_load_extension(True)
        try:
            sqlite_vec.load(conn)
        finally:
            try:
                conn.enable_load_extension(False)
            except Exception:
                pass  # don't mask the original load error
        return True
    except Exception as e:
        logger.debug("sqlite-vec load failed: %s", e)
        return False


# ── Table management ───────────────────────────────────────────────────


def _init_vec_table(conn: sqlite3.Connection, table_name: str) -> bool:
    """Create a vec0 virtual table if it doesn't exist."""
    if not load_vec(conn):
        return False
    try:
        conn.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS {table_name} "
            f"USING vec0(embedding float[{EMBEDDING_DIM}])"
        )
        logger.info("%s vec0 table ready (dim=%d)", table_name, EMBEDDING_DIM)
        return True
    except Exception as e:
        logger.warning("Failed to create %s: %s", table_name, e)
        return False


def init_vec_table(conn: sqlite3.Connection) -> bool:
    """Create the vec0 virtual table for entity embeddings."""
    return _init_vec_table(conn, "entity_embeddings")


# ── Embedding generation ───────────────────────────────────────────────


def _entity_text(name: str, entity_type: str, observations: list[str]) -> str:
    """Compose the text to embed for an entity."""
    obs_str = ". ".join(observations[:MAX_OBS_FOR_EMBEDDING])
    return f"{name} ({entity_type}): {obs_str}"


def embed_text(text: str) -> bytes:
    """Generate a 384-dim embedding and return as raw float32 bytes for vec0."""
    model = _get_model()
    vec = model.encode(text, normalize_embeddings=True)
    return vec.astype("float32").tobytes()


# ── Sync helpers (called after FTS sync on writes) ─────────────────────


def vec_sync_entity(conn: sqlite3.Connection, entity_id: int) -> None:
    """Update the embedding for an entity. Creates or replaces."""
    if not VEC_AVAILABLE or not load_vec(conn):
        return

    row = conn.execute(
        "SELECT name, entity_type FROM entities WHERE id = ?",
        (entity_id,),
    ).fetchone()
    if row is None:
        vec_remove_entity(conn, entity_id)
        return

    obs_rows = conn.execute(
        "SELECT content FROM observations WHERE entity_id = ? ORDER BY id",
        (entity_id,),
    ).fetchall()
    obs = [r["content"] for r in obs_rows]

    text = _entity_text(row["name"], row["entity_type"], obs)
    emb = embed_text(text)

    # vec0 doesn't support UPDATE — DELETE + INSERT
    conn.execute("DELETE FROM entity_embeddings WHERE rowid = ?", (entity_id,))
    conn.execute(
        "INSERT INTO entity_embeddings(rowid, embedding) VALUES (?, ?)",
        (entity_id, emb),
    )


def vec_remove_entity(conn: sqlite3.Connection, entity_id: int) -> None:
    """Remove an entity's embedding."""
    if not VEC_AVAILABLE:
        return
    try:
        if load_vec(conn):
            conn.execute("DELETE FROM entity_embeddings WHERE rowid = ?", (entity_id,))
    except Exception as e:
        logger.debug("vec_remove_entity(%d) failed: %s", entity_id, e)


# ── Vector search ──────────────────────────────────────────────────────


def vector_search(conn: sqlite3.Connection, query: str, limit: int = 50) -> list[dict]:
    """Perform KNN vector search.

    Returns list of dicts with: eid, name, entity_type, project, distance.
    """
    if not VEC_AVAILABLE or not load_vec(conn):
        return []

    emb = embed_text(query)
    try:
        rows = conn.execute(
            "SELECT ee.rowid AS eid, ee.distance, "
            "e.name, e.entity_type, e.project "
            "FROM entity_embeddings ee "
            "JOIN entities e ON e.id = ee.rowid "
            "WHERE ee.embedding MATCH ? AND k = ? "
            "ORDER BY ee.distance",
            (emb, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.warning("vector_search failed: %s", e)
        return []


# ── Reciprocal Rank Fusion ─────────────────────────────────────────────


def rrf_merge(
    fts_results: list[Any],
    vec_results: list[dict],
    k: int = 60,
) -> list[dict]:
    """Merge FTS5 and vector results using Reciprocal Rank Fusion.

    RRF(d) = sum(1 / (k + rank_i(d))) for each ranking source.

    Returns combined results ordered by RRF score (descending), formatted
    to match the FTS5 row format expected by rerank_entities().
    """
    scores: dict[int, float] = {}
    entity_data: dict[int, dict] = {}

    # FTS5 contributions (fts_results may be sqlite3.Row objects)
    for rank, item in enumerate(fts_results):
        eid = item["eid"]
        scores[eid] = scores.get(eid, 0.0) + 1.0 / (k + rank + 1)
        entity_data[eid] = {
            "eid": eid,
            "name": item["name"],
            "entity_type": item["entity_type"],
            "project": item["project"],
        }

    # Vector contributions
    for rank, item in enumerate(vec_results):
        eid = item["eid"]
        scores[eid] = scores.get(eid, 0.0) + 1.0 / (k + rank + 1)
        if eid not in entity_data:
            entity_data[eid] = {
                "eid": eid,
                "name": item["name"],
                "entity_type": item["entity_type"],
                "project": item.get("project"),
            }

    # Sort by RRF score descending
    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)

    results = []
    for eid, rrf_score in ranked:
        data = entity_data[eid]
        # Use negative RRF score as rank (matches FTS5 convention: lower = better)
        data["rank"] = -rrf_score
        results.append(data)
    return results


# ── Task vector search ─────────────────────────────────────────────────


def init_task_vec_table(conn: sqlite3.Connection) -> bool:
    """Create the vec0 virtual table for task embeddings."""
    return _init_vec_table(conn, "task_embeddings")


def _task_text(title: str, description: str | None, notes: str | None) -> str:
    """Compose the text to embed for a task."""
    parts = [title or ""]
    if description:
        parts.append(description[:500])
    if notes:
        parts.append(notes[:300])
    return ". ".join(parts)


def vec_sync_task(conn: sqlite3.Connection, task_id: str) -> None:
    """Update the embedding for a task. Creates or replaces."""
    if not VEC_AVAILABLE or not load_vec(conn):
        return

    row = conn.execute(
        "SELECT rowid, title, description, notes FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if row is None:
        return

    text = _task_text(row["title"], row["description"], row["notes"])
    emb = embed_text(text)
    rowid = row["rowid"]

    # vec0 doesn't support UPDATE — DELETE + INSERT
    conn.execute("DELETE FROM task_embeddings WHERE rowid = ?", (rowid,))
    conn.execute(
        "INSERT INTO task_embeddings(rowid, embedding) VALUES (?, ?)",
        (rowid, emb),
    )


def vec_remove_task(conn: sqlite3.Connection, task_id: str) -> None:
    """Remove a task's embedding by its UUID."""
    if not VEC_AVAILABLE:
        return
    try:
        if load_vec(conn):
            row = conn.execute(
                "SELECT rowid FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if row:
                conn.execute(
                    "DELETE FROM task_embeddings WHERE rowid = ?", (row["rowid"],)
                )
    except Exception as e:
        logger.debug("vec_remove_task(%s) failed: %s", task_id, e)


def task_vector_search(
    conn: sqlite3.Connection, query: str, limit: int = 50
) -> list[dict]:
    """KNN vector search over task embeddings.

    Returns list of dicts with task fields + distance.
    """
    if not VEC_AVAILABLE or not load_vec(conn):
        return []

    emb = embed_text(query)
    try:
        rows = conn.execute(
            "SELECT t.id, t.title, t.description, t.notes, t.status, "
            "t.priority, t.section, t.due_date, t.project, t.parent_id, "
            "t.type, t.updated_at, te.distance "
            "FROM task_embeddings te "
            "JOIN tasks t ON t.rowid = te.rowid "
            "WHERE te.embedding MATCH ? AND k = ? "
            "ORDER BY te.distance",
            (emb, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.warning("task_vector_search failed: %s", e)
        return []


def task_rrf_merge(
    fts_results: list[dict],
    vec_results: list[dict],
    k: int = 60,
) -> list[dict]:
    """Merge FTS5 and vector task results using Reciprocal Rank Fusion.

    Keyed by task UUID (id string), not integer eid.
    Returns combined results ordered by RRF score descending.
    """
    scores: dict[str, float] = {}
    task_data: dict[str, dict] = {}

    for rank, item in enumerate(fts_results):
        tid = item["id"]
        scores[tid] = scores.get(tid, 0.0) + 1.0 / (k + rank + 1)
        task_data[tid] = dict(item)

    for rank, item in enumerate(vec_results):
        tid = item["id"]
        scores[tid] = scores.get(tid, 0.0) + 1.0 / (k + rank + 1)
        if tid not in task_data:
            task_data[tid] = dict(item)

    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    return [task_data[tid] for tid, _ in ranked]


def backfill_task_embeddings(conn: sqlite3.Connection) -> int:
    """Generate embeddings for all tasks that don't have one yet.

    Returns the number of tasks backfilled.
    """
    if not VEC_AVAILABLE or not load_vec(conn):
        return 0

    existing = set()
    try:
        for row in conn.execute("SELECT rowid FROM task_embeddings"):
            existing.add(row[0])
    except Exception:
        pass

    all_tasks = conn.execute("SELECT id, rowid FROM tasks").fetchall()
    missing = [r["id"] for r in all_tasks if r["rowid"] not in existing]

    count = 0
    for tid in missing:
        try:
            vec_sync_task(conn, tid)
            count += 1
        except Exception as e:
            logger.warning("backfill failed for task %s: %s", tid, e)

    if count:
        logger.info("Backfilled embeddings for %d tasks", count)
    return count


# ── Backfill utility ───────────────────────────────────────────────────


def backfill_embeddings(conn: sqlite3.Connection) -> int:
    """Generate embeddings for all entities that don't have one yet.

    Returns the number of entities backfilled.
    """
    if not VEC_AVAILABLE or not load_vec(conn):
        return 0

    # Find entities without embeddings
    existing = set()
    try:
        for row in conn.execute("SELECT rowid FROM entity_embeddings"):
            existing.add(row[0])
    except Exception:
        pass

    all_entities = conn.execute("SELECT id FROM entities").fetchall()
    missing = [r["id"] for r in all_entities if r["id"] not in existing]

    count = 0
    for eid in missing:
        try:
            vec_sync_entity(conn, eid)
            count += 1
        except Exception as e:
            logger.warning("backfill failed for entity %d: %s", eid, e)

    if count:
        logger.info("Backfilled embeddings for %d entities", count)
    return count
