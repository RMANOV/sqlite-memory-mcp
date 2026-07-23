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
from importlib.util import find_spec
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sentence_transformers import SentenceTransformer

logger = logging.getLogger("sqlite-kb")

_VEC_LOAD_ERRORS = (AttributeError, OSError, sqlite3.Error)
_EMBEDDING_ERRORS = (AttributeError, OSError, RuntimeError, TypeError, ValueError)


# ── Availability check ─────────────────────────────────────────────────

try:
    import sqlite_vec

    _HAS_VEC = True
except ImportError:
    _HAS_VEC = False

_HAS_ST = find_spec("sentence_transformers") is not None

VEC_AVAILABLE: bool = _HAS_VEC and _HAS_ST


# ── Model singleton (lazy loaded) ─────────────────────────────────────

_model: SentenceTransformer | None = None
_model_lock = threading.Lock()
_MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"
EMBEDDING_DIM = 384
MAX_OBS_FOR_EMBEDDING = 20  # MiniLM-L6-v2 has 256-token limit; cap observations to fit


def _get_model() -> SentenceTransformer:
    """Lazy-load the embedding model on first use (thread-safe)."""
    global VEC_AVAILABLE, _HAS_ST, _model
    if _model is None:
        with _model_lock:
            if _model is None:
                try:
                    from sentence_transformers import SentenceTransformer
                except ImportError as exc:
                    _HAS_ST = False
                    VEC_AVAILABLE = False
                    raise RuntimeError("sentence-transformers is unavailable") from exc
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
    except sqlite3.Error:
        pass
    try:
        conn.enable_load_extension(True)
        try:
            sqlite_vec.load(conn)
        finally:
            try:
                conn.enable_load_extension(False)
            except _VEC_LOAD_ERRORS:
                pass  # don't mask the original load error
        return True
    except _VEC_LOAD_ERRORS as e:
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
    except sqlite3.Error as e:
        logger.warning("Failed to create %s: %s", table_name, e)
        return False


def _embed_text_or_none(text: str, *, context: str) -> bytes | None:
    try:
        return embed_text(text)
    except _EMBEDDING_ERRORS as exc:
        logger.warning("Embedding failed for %s: %s", context, exc)
        return None


def _existing_embedding_rowids(conn: sqlite3.Connection, table_name: str) -> set[int]:
    rowids: set[int] = set()
    try:
        for row in conn.execute(f"SELECT rowid FROM {table_name}"):
            rowids.add(row[0])
    except sqlite3.Error as exc:
        logger.debug("Failed to read %s rowids: %s", table_name, exc)
    return rowids


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


def vec_sync_entity(conn: sqlite3.Connection, entity_id: int) -> bool:
    """Update the embedding for an entity. Creates or replaces."""
    if not VEC_AVAILABLE or not load_vec(conn):
        return False
    try:
        row = conn.execute(
            "SELECT name, entity_type FROM entities WHERE id = ?",
            (entity_id,),
        ).fetchone()
        if row is None:
            vec_remove_entity(conn, entity_id)
            return False

        obs_rows = conn.execute(
            "SELECT content FROM observations WHERE entity_id = ? ORDER BY id",
            (entity_id,),
        ).fetchall()
        obs = [r["content"] for r in obs_rows]

        text = _entity_text(row["name"], row["entity_type"], obs)
        emb = _embed_text_or_none(text, context=f"entity:{entity_id}")
        if emb is None:
            return False

        conn.execute("DELETE FROM entity_embeddings WHERE rowid = ?", (entity_id,))
        conn.execute(
            "INSERT INTO entity_embeddings(rowid, embedding) VALUES (?, ?)",
            (entity_id, emb),
        )
        return True
    except sqlite3.Error as exc:
        logger.warning("vec_sync_entity(%d) failed: %s", entity_id, exc)
        return False


def vec_remove_entity(conn: sqlite3.Connection, entity_id: int) -> None:
    """Remove an entity's embedding."""
    if not VEC_AVAILABLE:
        return
    try:
        if load_vec(conn):
            conn.execute("DELETE FROM entity_embeddings WHERE rowid = ?", (entity_id,))
    except sqlite3.Error as e:
        logger.debug("vec_remove_entity(%d) failed: %s", entity_id, e)


# ── Vector search ──────────────────────────────────────────────────────


def vector_search(conn: sqlite3.Connection, query: str, limit: int = 50) -> list[dict]:
    """Perform KNN vector search.

    Returns list of dicts with: eid, name, entity_type, project, distance.
    """
    if not VEC_AVAILABLE or not load_vec(conn):
        return []
    emb = _embed_text_or_none(query, context="entity_search_query")
    if emb is None:
        return []
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
    except sqlite3.Error as e:
        logger.warning("vector_search failed: %s", e)
        return []


# ── Reciprocal Rank Fusion ─────────────────────────────────────────────


def _rrf_ranked_items(
    rankings: tuple[list[Any], ...],
    *,
    key_field: str,
    k: int,
) -> list[tuple[dict, float]]:
    """Fuse arbitrary ranked mappings and return copied rows with RRF scores."""
    scores: dict[Any, float] = {}
    item_data: dict[Any, dict] = {}
    for ranking in rankings:
        for rank, item in enumerate(ranking):
            key = item[key_field]
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank + 1)
            item_data.setdefault(key, dict(item))
    return [
        (item_data[key], score)
        for key, score in sorted(scores.items(), key=lambda pair: pair[1], reverse=True)
    ]


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
    results = []
    for item, rrf_score in _rrf_ranked_items(
        (fts_results, vec_results), key_field="eid", k=k
    ):
        # Use negative RRF score as rank (matches FTS5 convention: lower = better)
        results.append(
            {
                "eid": item["eid"],
                "name": item["name"],
                "entity_type": item["entity_type"],
                "project": item.get("project"),
                "rank": -rrf_score,
            }
        )
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


def vec_sync_task(conn: sqlite3.Connection, task_id: str) -> bool:
    """Update the embedding for a task. Creates or replaces."""
    if not VEC_AVAILABLE or not load_vec(conn):
        return False
    try:
        row = conn.execute(
            "SELECT rowid, title, description, notes FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        if row is None:
            return False

        text = _task_text(row["title"], row["description"], row["notes"])
        emb = _embed_text_or_none(text, context=f"task:{task_id}")
        if emb is None:
            return False
        rowid = row["rowid"]

        conn.execute("DELETE FROM task_embeddings WHERE rowid = ?", (rowid,))
        conn.execute(
            "INSERT INTO task_embeddings(rowid, embedding) VALUES (?, ?)",
            (rowid, emb),
        )
        return True
    except sqlite3.Error as exc:
        logger.warning("vec_sync_task(%s) failed: %s", task_id, exc)
        return False


def vec_remove_task_rowid(conn: sqlite3.Connection, rowid: int) -> bool:
    """Remove one task embedding by SQLite rowid.

    Deletion only needs sqlite-vec, not the transformer model.  Returning a
    boolean lets hard-delete paths distinguish an unavailable derived cache
    from a completed cleanup.
    """
    if not _HAS_VEC or not load_vec(conn):
        return False
    try:
        table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='task_embeddings'"
        ).fetchone()
        if table is None:
            return True
        conn.execute("DELETE FROM task_embeddings WHERE rowid = ?", (int(rowid),))
        return True
    except sqlite3.Error as e:
        logger.debug("vec_remove_task_rowid(%s) failed: %s", rowid, e)
        return False


def vec_remove_task(conn: sqlite3.Connection, task_id: str) -> bool:
    """Remove a task's embedding by UUID before the task row is deleted."""
    try:
        row = conn.execute(
            "SELECT rowid FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
    except sqlite3.Error as e:
        logger.debug("vec_remove_task(%s) lookup failed: %s", task_id, e)
        return False
    if row is None:
        return True
    return vec_remove_task_rowid(conn, int(row["rowid"]))


def prune_orphan_task_embeddings(conn: sqlite3.Connection) -> int:
    """Delete derived task embeddings whose task row no longer exists.

    This is safe to run repeatedly and never generates embeddings.  It repairs
    databases created before hard-delete paths cleaned the optional vec0 cache.
    """
    if not _HAS_VEC or not load_vec(conn):
        return 0
    try:
        table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='task_embeddings'"
        ).fetchone()
        if table is None:
            return 0
        orphan_rowids = [
            int(row[0])
            for row in conn.execute(
                "SELECT te.rowid FROM task_embeddings AS te "
                "LEFT JOIN tasks AS t ON t.rowid = te.rowid "
                "WHERE t.rowid IS NULL ORDER BY te.rowid"
            ).fetchall()
        ]
        for rowid in orphan_rowids:
            conn.execute("DELETE FROM task_embeddings WHERE rowid = ?", (rowid,))
        if orphan_rowids:
            logger.info("Pruned %d orphan task embeddings", len(orphan_rowids))
        return len(orphan_rowids)
    except sqlite3.Error as e:
        logger.warning("prune_orphan_task_embeddings failed: %s", e)
        return 0


def task_vector_search(
    conn: sqlite3.Connection, query: str, limit: int = 50
) -> list[dict]:
    """KNN vector search over task embeddings.

    Returns list of dicts with task fields + distance.
    """
    if not VEC_AVAILABLE or not load_vec(conn):
        return []
    emb = _embed_text_or_none(query, context="task_search_query")
    if emb is None:
        return []
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
    except sqlite3.Error as e:
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
    return [
        item
        for item, _score in _rrf_ranked_items(
            (fts_results, vec_results), key_field="id", k=k
        )
    ]


def backfill_task_embeddings(conn: sqlite3.Connection) -> int:
    """Generate embeddings for all tasks that don't have one yet.

    Returns the number of tasks backfilled.
    """
    if not VEC_AVAILABLE or not load_vec(conn):
        return 0
    existing = _existing_embedding_rowids(conn, "task_embeddings")

    all_tasks = conn.execute("SELECT id, rowid FROM tasks").fetchall()
    missing = [r["id"] for r in all_tasks if r["rowid"] not in existing]

    count = 0
    for tid in missing:
        try:
            if vec_sync_task(conn, tid):
                count += 1
        except _EMBEDDING_ERRORS as e:
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
    existing = _existing_embedding_rowids(conn, "entity_embeddings")

    all_entities = conn.execute("SELECT id FROM entities").fetchall()
    missing = [r["id"] for r in all_entities if r["id"] not in existing]

    count = 0
    for eid in missing:
        try:
            if vec_sync_entity(conn, eid):
                count += 1
        except _EMBEDDING_ERRORS as e:
            logger.warning("backfill failed for entity %d: %s", eid, e)

    if count:
        logger.info("Backfilled embeddings for %d entities", count)
    return count
