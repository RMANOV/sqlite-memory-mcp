#!/usr/bin/env python3
"""SQLite-backed MCP Memory Server — Core Knowledge Graph.

Production-quality persistent memory with WAL concurrent safety,
FTS5 BM25-ranked search. Tools 1-9: entity/observation/relation CRUD,
read_graph, search_nodes, open_nodes.

Other tools remain split into domain servers for modular startup, ownership,
and fault isolation; ``unified_server.py`` mounts the complete surface.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from fastmcp_compat import FastMCP

from db_utils import (
    get_conn as _get_conn,
    get_entity_id as _get_entity_id,
    fts_query as _fts_query,
    VISIBILITY_LEVELS as _VISIBILITY_LEVELS,
    now_iso as _now,
    fts_sync_entity as _fts_sync,
    serialize_entity as _serialize_entity,
    setup_logger,
    export_relations as _export_relations,
    record_memory_event,
)
from premium_runtime import maybe_mount_premium_extensions

# Optional vector search (graceful fallback to FTS5-only)
try:
    from vec_search import (
        VEC_AVAILABLE as _VEC_AVAILABLE,
        vec_sync_entity as _vec_sync,
        vec_remove_entity as _vec_remove,
        vector_search as _vector_search,
        rrf_merge as _rrf_merge,
    )
except ImportError:
    _VEC_AVAILABLE = False


# ── Logging setup (file-only, NEVER stdout — breaks MCP stdio) ──────────

logger = setup_logger("sqlite-kb", "server.log")

# ── FastMCP app ──────────────────────────────────────────────────────────

mcp = FastMCP(
    "sqlite-kb",
    instructions=(
        "Core knowledge graph tools: create/read/delete entities, "
        "observations, relations. FTS5 BM25-ranked search."
    ),
)


# ── FTS helpers ──────────────────────────────────────────────────────────


def _fts_remove(conn, entity_id: int) -> None:
    """Remove entity from FTS index."""
    conn.execute("DELETE FROM memory_fts WHERE rowid = ?", (entity_id,))


def _record_entity_access(
    conn: sqlite3.Connection, entity_ids: list[int], tool_name: str
) -> None:
    """Best-effort batched access telemetry for read tools."""
    if not entity_ids:
        return
    now = _now()
    try:
        conn.executemany(
            "INSERT INTO entity_access_log (entity_id, tool_name, accessed_at) "
            "VALUES (?, ?, ?)",
            [(entity_id, tool_name, now) for entity_id in dict.fromkeys(entity_ids)],
        )
    except sqlite3.OperationalError as exc:
        logger.debug("Access log write failed: %s", exc)


# ── One-time JSONL migration ────────────────────────────────────────────


def _migrate_jsonl() -> None:
    """One-time migration from the old @modelcontextprotocol memory.json JSONL format."""
    json_path = Path.home() / ".claude" / "memory" / "memory.json"
    if not json_path.exists():
        return

    logger.info("Migrating from %s", json_path)
    entities: list[dict] = []
    relations: list[dict] = []

    try:
        with open(json_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                obj = json.loads(line)
                obj_type = obj.get("type", "")
                if obj_type == "entity":
                    entities.append(obj)
                elif obj_type == "relation":
                    relations.append(obj)
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("Migration parse error: %s", exc)
        return

    now = _now()
    with _get_conn() as conn:
        for ent in entities:
            conn.execute(
                "INSERT OR IGNORE INTO entities (name, entity_type, created_at, updated_at) "
                "VALUES (?, ?, ?, ?)",
                (ent["name"], ent.get("entityType", "unknown"), now, now),
            )
            eid = _get_entity_id(conn, ent["name"])
            if eid:
                for obs in ent.get("observations", []):
                    conn.execute(
                        "INSERT OR IGNORE INTO observations (entity_id, content, created_at) "
                        "VALUES (?, ?, ?)",
                        (eid, obs, now),
                    )
                _fts_sync(conn, eid)

        for rel in relations:
            from_id = _get_entity_id(conn, rel["from"])
            to_id = _get_entity_id(conn, rel["to"])
            if from_id and to_id:
                conn.execute(
                    "INSERT OR IGNORE INTO relations "
                    "(from_id, to_id, relation_type, created_at) VALUES (?, ?, ?, ?)",
                    (from_id, to_id, rel.get("relationType", "related_to"), now),
                )

    migrated_path = json_path.with_suffix(".json.migrated")
    json_path.rename(migrated_path)
    logger.info(
        "Migration complete: %d entities, %d relations. Old file → %s",
        len(entities),
        len(relations),
        migrated_path,
    )


# ═══════════════════════════════════════════════════════════════════════════
# Tool 1: create_entities
# ═══════════════════════════════════════════════════════════════════════════


@mcp.tool()
def create_entities(entities: list[dict[str, Any]]) -> str:
    """Create new entities in the knowledge graph.

    Each entity dict has: name (str), entityType (str), observations (list[str]).
    Optional: project (str). Duplicates are silently ignored.
    """
    now = _now()
    created = 0
    with _get_conn() as conn:
        for ent in entities:
            name = ent["name"]
            etype = ent["entityType"]
            project = ent.get("project")
            observations = ent.get("observations", [])
            vis = ent.get("visibility", "private")
            if vis not in _VISIBILITY_LEVELS:
                vis = "private"

            cur = conn.execute(
                "INSERT OR IGNORE INTO entities "
                "(name, entity_type, project, visibility, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (name, etype, project, vis, now, now),
            )
            if cur.rowcount > 0:
                created += 1

            eid = _get_entity_id(conn, name)
            if eid:
                if cur.rowcount > 0:
                    record_memory_event(
                        conn,
                        event_type="entity_create",
                        aggregate_kind="entity",
                        aggregate_id=str(eid),
                        tool_name="sqlite-kb.create_entities",
                        event_ts=now,
                        new_value={
                            "name": name,
                            "entity_type": etype,
                            "project": project,
                            "visibility": vis,
                        },
                        source_kind="entity",
                        source_ref=str(eid),
                    )
                if project is not None and cur.rowcount == 0:
                    conn.execute(
                        "UPDATE entities SET project = ?, updated_at = ? "
                        "WHERE id = ? AND (project IS NULL OR project != ?)",
                        (project, now, eid, project),
                    )
                new_obs_ids: list[tuple[int, str]] = []
                for obs in observations:
                    cur_obs = conn.execute(
                        "INSERT OR IGNORE INTO observations "
                        "(entity_id, content, created_at) VALUES (?, ?, ?)",
                        (eid, obs, now),
                    )
                    if cur_obs.rowcount > 0:
                        new_obs_ids.append((cur_obs.lastrowid, obs))
                        record_memory_event(
                            conn,
                            event_type="observation_add",
                            aggregate_kind="observation",
                            aggregate_id=str(cur_obs.lastrowid),
                            tool_name="sqlite-kb.create_entities",
                            event_ts=now,
                            new_value={"entity_id": eid, "content": obs},
                            source_kind="entity",
                            source_ref=str(eid),
                            source_excerpt=obs[:300],
                        )
                _fts_sync(conn, eid)
                if _VEC_AVAILABLE:
                    try:
                        _vec_sync(conn, eid)
                    except Exception as exc:
                        logger.debug(
                            "vec_sync(%s) skipped: %s", eid, exc, exc_info=True
                        )
                if new_obs_ids:
                    try:
                        from lazy_enrichment import extract_inline_claims

                        for obs_id, obs_text in new_obs_ids:
                            extract_inline_claims(conn, eid, obs_id, obs_text)
                    except (ImportError, sqlite3.OperationalError):
                        pass

    logger.info(
        "create_entities: %d created out of %d requested", created, len(entities)
    )
    return json.dumps({"created": created, "total_requested": len(entities)})


# ═══════════════════════════════════════════════════════════════════════════
# Tool 2: add_observations
# ═══════════════════════════════════════════════════════════════════════════


@mcp.tool()
def add_observations(observations: list[dict[str, Any]]) -> str:
    """Add new observations to existing entities.

    Each dict has: entityName (str), contents (list[str]).
    Duplicate observations are silently ignored.
    """
    now = _now()
    added = 0
    with _get_conn() as conn:
        for item in observations:
            entity_name = item["entityName"]
            eid = _get_entity_id(conn, entity_name)
            if eid is None:
                logger.warning("add_observations: entity %r not found", entity_name)
                continue
            contents = item.get("contents", [])
            for content in contents:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO observations "
                    "(entity_id, content, created_at) VALUES (?, ?, ?)",
                    (eid, content, now),
                )
                added += cur.rowcount
                if cur.rowcount > 0:
                    record_memory_event(
                        conn,
                        event_type="observation_add",
                        aggregate_kind="observation",
                        aggregate_id=str(cur.lastrowid),
                        tool_name="sqlite-kb.add_observations",
                        event_ts=now,
                        new_value={"entity_id": eid, "content": content},
                        source_kind="entity",
                        source_ref=str(eid),
                        source_excerpt=content[:300],
                    )
            conn.execute("UPDATE entities SET updated_at = ? WHERE id = ?", (now, eid))
            _fts_sync(conn, eid)
            if _VEC_AVAILABLE:
                try:
                    _vec_sync(conn, eid)
                except Exception as exc:
                    logger.debug("vec_sync(%s) skipped: %s", eid, exc, exc_info=True)
            if contents:
                try:
                    from lazy_enrichment import extract_inline_claims

                    for content in contents:
                        obs_row = conn.execute(
                            "SELECT id FROM observations WHERE entity_id = ? AND content = ?",
                            (eid, content),
                        ).fetchone()
                        if obs_row:
                            extract_inline_claims(conn, eid, obs_row["id"], content)
                except (ImportError, sqlite3.OperationalError):
                    pass

    logger.info("add_observations: %d observations added", added)
    return json.dumps({"added": added})


# ═══════════════════════════════════════════════════════════════════════════
# Tool 3: create_relations
# ═══════════════════════════════════════════════════════════════════════════


@mcp.tool()
def create_relations(relations: list[dict[str, Any]]) -> str:
    """Create relations between entities in the knowledge graph.

    Each dict has: from (str), to (str), relationType (str).
    Duplicate relations are silently ignored.
    """
    now = _now()
    created = 0
    with _get_conn() as conn:
        for rel in relations:
            from_name = rel["from"]
            to_name = rel["to"]
            rel_type = rel["relationType"]

            from_id = _get_entity_id(conn, from_name)
            to_id = _get_entity_id(conn, to_name)
            if from_id is None or to_id is None:
                logger.warning(
                    "create_relations: missing entity for %r -> %r", from_name, to_name
                )
                continue

            cur = conn.execute(
                "INSERT OR IGNORE INTO relations "
                "(from_id, to_id, relation_type, created_at) VALUES (?, ?, ?, ?)",
                (from_id, to_id, rel_type, now),
            )
            created += cur.rowcount
            if cur.rowcount > 0:
                record_memory_event(
                    conn,
                    event_type="relation_create",
                    aggregate_kind="relation",
                    aggregate_id=f"{from_id}:{rel_type}:{to_id}",
                    tool_name="sqlite-kb.create_relations",
                    event_ts=now,
                    new_value={
                        "from_id": from_id,
                        "to_id": to_id,
                        "relation_type": rel_type,
                    },
                    source_kind="entity",
                    source_ref=str(from_id),
                )

    logger.info(
        "create_relations: %d created out of %d requested", created, len(relations)
    )
    return json.dumps({"created": created, "total_requested": len(relations)})


# ═══════════════════════════════════════════════════════════════════════════
# Tools 4-6: Delete
# ═══════════════════════════════════════════════════════════════════════════


@mcp.tool()
def delete_entities(entityNames: list[str]) -> str:
    """Delete entities and their associated observations and relations (CASCADE).

    Also cleans up the FTS index.
    """
    deleted = 0
    now = _now()
    with _get_conn() as conn:
        for name in entityNames:
            eid = _get_entity_id(conn, name)
            if eid is None:
                continue
            record_memory_event(
                conn,
                event_type="entity_delete",
                aggregate_kind="entity",
                aggregate_id=str(eid),
                tool_name="sqlite-kb.delete_entities",
                event_ts=now,
                old_value={"name": name},
                source_kind="entity",
                source_ref=str(eid),
            )
            _fts_remove(conn, eid)
            if _VEC_AVAILABLE:
                try:
                    _vec_remove(conn, eid)
                except Exception as exc:
                    logger.debug("vec_remove(%s) skipped: %s", eid, exc, exc_info=True)
            conn.execute("DELETE FROM entities WHERE id = ?", (eid,))
            deleted += 1

    logger.info("delete_entities: %d deleted", deleted)
    return json.dumps({"deleted": deleted})


@mcp.tool()
def delete_observations(deletions: list[dict[str, Any]]) -> str:
    """Delete specific observations from entities.

    Each dict has: entityName (str), observations (list[str]).
    """
    deleted = 0
    now = _now()
    with _get_conn() as conn:
        for item in deletions:
            entity_name = item["entityName"]
            eid = _get_entity_id(conn, entity_name)
            if eid is None:
                continue
            for obs in item.get("observations", []):
                row = conn.execute(
                    "SELECT id FROM observations WHERE entity_id = ? AND content = ?",
                    (eid, obs),
                ).fetchone()
                cur = conn.execute(
                    "DELETE FROM observations WHERE entity_id = ? AND content = ?",
                    (eid, obs),
                )
                deleted += cur.rowcount
                if cur.rowcount > 0 and row:
                    record_memory_event(
                        conn,
                        event_type="observation_delete",
                        aggregate_kind="observation",
                        aggregate_id=str(row["id"]),
                        tool_name="sqlite-kb.delete_observations",
                        event_ts=now,
                        old_value={"entity_id": eid, "content": obs},
                        source_kind="entity",
                        source_ref=str(eid),
                        source_excerpt=obs[:300],
                    )
            _fts_sync(conn, eid)
            if _VEC_AVAILABLE:
                try:
                    _vec_sync(conn, eid)
                except Exception as exc:
                    logger.debug("vec_sync(%s) skipped: %s", eid, exc, exc_info=True)

    logger.info("delete_observations: %d deleted", deleted)
    return json.dumps({"deleted": deleted})


@mcp.tool()
def delete_relations(relations: list[dict[str, Any]]) -> str:
    """Delete specific relations from the knowledge graph.

    Each dict has: from (str), to (str), relationType (str).
    """
    deleted = 0
    now = _now()
    with _get_conn() as conn:
        for rel in relations:
            from_id = _get_entity_id(conn, rel["from"])
            to_id = _get_entity_id(conn, rel["to"])
            if from_id is None or to_id is None:
                continue
            cur = conn.execute(
                "DELETE FROM relations "
                "WHERE from_id = ? AND to_id = ? AND relation_type = ?",
                (from_id, to_id, rel["relationType"]),
            )
            deleted += cur.rowcount
            if cur.rowcount > 0:
                record_memory_event(
                    conn,
                    event_type="relation_delete",
                    aggregate_kind="relation",
                    aggregate_id=f"{from_id}:{rel['relationType']}:{to_id}",
                    tool_name="sqlite-kb.delete_relations",
                    event_ts=now,
                    old_value={
                        "from_id": from_id,
                        "to_id": to_id,
                        "relation_type": rel["relationType"],
                    },
                    source_kind="entity",
                    source_ref=str(from_id),
                )

    logger.info("delete_relations: %d deleted", deleted)
    return json.dumps({"deleted": deleted})


# ═══════════════════════════════════════════════════════════════════════════
# Tool 7: read_graph
# ═══════════════════════════════════════════════════════════════════════════


@mcp.tool()
def read_graph(offset: int = 0, limit: int = 500) -> str:
    """Read the full knowledge graph with pagination.

    Returns JSON: {entities: [{name, entityType, observations: [...]}],
                   relations: [{from, to, relationType}],
                   total: int, has_more: bool}
    """
    with _get_conn() as conn:
        total = (
            conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
            if offset == 0
            else -1
        )
        ent_rows = conn.execute(
            "SELECT id, name, entity_type, project FROM entities ORDER BY name LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()

        # Batch-fetch observations for all entities on this page in one query
        eids = [r[0] for r in ent_rows]
        obs_by_entity: dict[int, list[str]] = {r[0]: [] for r in ent_rows}
        if eids:
            ph = ",".join("?" * len(eids))
            obs_rows = conn.execute(
                f"SELECT entity_id, content FROM observations WHERE entity_id IN ({ph}) ORDER BY entity_id, id",
                eids,
            ).fetchall()
            for entity_id, content in obs_rows:
                obs_by_entity[entity_id].append(content)

        entities_out = [
            {
                "name": name,
                "entityType": entity_type,
                "project": project,
                "observations": obs_by_entity.get(eid, []),
            }
            for eid, name, entity_type, project in ent_rows
        ]
        relations_out = _export_relations(conn)

    return json.dumps(
        {
            "entities": entities_out,
            "relations": relations_out,
            "total": total,
            "has_more": offset + limit < total,
        }
    )


# ═══════════════════════════════════════════════════════════════════════════
# Tool 8: search_nodes (FTS5 BM25)
# ═══════════════════════════════════════════════════════════════════════════


@mcp.tool()
def search_nodes(query: str, project: str | None = None) -> str:
    """Search the knowledge graph using hybrid BM25 + semantic search.

    When sqlite-vec is installed, combines FTS5 keyword matching with vector
    cosine similarity via Reciprocal Rank Fusion. Falls back to FTS5-only
    otherwise. Results are re-ranked with 6 contextual signals (recency,
    project affinity, graph proximity, richness, canonical facts, session).
    """
    fts_q = _fts_query(query)
    with _get_conn() as conn:
        if not query.strip():
            rows = conn.execute(
                "SELECT e.id AS eid, e.name, e.entity_type, e.project, 0 AS rank "
                "FROM entities e ORDER BY e.name LIMIT 50"
            ).fetchall()
        else:
            try:
                from smart_retrieval import RERANKING_POOL_SIZE

                pool_size = RERANKING_POOL_SIZE
            except ImportError:
                pool_size = 50

            rows = conn.execute(
                "SELECT memory_fts.rowid AS eid, memory_fts.name, "
                "memory_fts.entity_type, e.project, memory_fts.rank "
                "FROM memory_fts "
                "JOIN entities e ON e.id = memory_fts.rowid "
                "WHERE memory_fts MATCH ? ORDER BY memory_fts.rank LIMIT ?",
                (fts_q, pool_size),
            ).fetchall()

        # Optional: parallel vector search + RRF merge
        if _VEC_AVAILABLE and query.strip():
            try:
                vec_rows = _vector_search(conn, query, pool_size)
                if vec_rows and rows:
                    rows = _rrf_merge(rows, vec_rows)
                elif vec_rows and not rows:
                    # Vector found results that FTS5 missed (semantic match)
                    rows = _rrf_merge([], vec_rows)
            except Exception as e:
                logger.debug("Vector search failed: %s", e, exc_info=True)

        if not rows:
            return json.dumps({"entities": [], "query": query})

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
        except (ImportError, sqlite3.OperationalError) as e:
            logger.warning("Rerank failed: %s", e)

        if reranked:
            eids = [r["eid"] for r in reranked]
        else:
            eids = [r["eid"] for r in rows[:50]]

        ph = ",".join("?" * len(eids))
        obs_rows = conn.execute(
            f"SELECT entity_id, content FROM observations "
            f"WHERE entity_id IN ({ph}) ORDER BY entity_id, id",
            eids,
        ).fetchall()

        obs_by_eid: dict[int, list[str]] = {}
        for o in obs_rows:
            obs_by_eid.setdefault(o["entity_id"], []).append(o["content"])

        results = []
        if reranked:
            for r in reranked:
                entity: dict[str, Any] = {
                    "name": r["name"],
                    "entityType": r["entity_type"],
                    "observations": obs_by_eid.get(r["eid"], []),
                }
                if r["project"]:
                    entity["project"] = r["project"]
                entity["_score"] = r["_score"]
                results.append(entity)
        else:
            for r in rows[:50]:
                entity = {
                    "name": r["name"],
                    "entityType": r["entity_type"],
                    "observations": obs_by_eid.get(r["eid"], []),
                }
                if r["project"]:
                    entity["project"] = r["project"]
                results.append(entity)

        _record_entity_access(conn, eids, "search_nodes")

    logger.info("search_nodes: query=%r matched=%d", query, len(results))
    return json.dumps({"entities": results, "query": query})


# ═══════════════════════════════════════════════════════════════════════════
# Tool 9: open_nodes
# ═══════════════════════════════════════════════════════════════════════════


@mcp.tool()
def open_nodes(names: list[str]) -> str:
    """Open specific entities and retrieve their inter-relations.

    Returns the requested entities with observations and all relations
    that exist between them.
    """
    with _get_conn() as conn:
        entities_out = []
        found_ids: list[int] = []

        for name in names:
            row = conn.execute(
                "SELECT id, name, entity_type, project FROM entities WHERE name = ?",
                (name,),
            ).fetchone()
            if row is None:
                continue
            found_ids.append(row["id"])
            entities_out.append(_serialize_entity(conn, row))

        relations_out = (
            _export_relations(conn, found_ids) if len(found_ids) >= 2 else []
        )

        _record_entity_access(conn, found_ids, "open_nodes")

    return json.dumps({"entities": entities_out, "relations": relations_out})


# ═══════════════════════════════════════════════════════════════════════════
# Startup
# ═══════════════════════════════════════════════════════════════════════════


def main() -> None:
    _migrate_jsonl()
    maybe_mount_premium_extensions(mcp, server_name="sqlite-kb")
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
