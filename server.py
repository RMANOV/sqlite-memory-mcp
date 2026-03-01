#!/usr/bin/env python3
"""SQLite-backed MCP Memory Server.

Production-quality persistent memory with WAL concurrent safety,
FTS5 BM25-ranked search, and session tracking.

Drop-in compatible with @modelcontextprotocol/server-memory (tools 1-9)
plus 3 additional tools: session_save, session_recall, search_by_project.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import sqlite3
import subprocess
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ── Logging setup (file-only, NEVER stdout — breaks MCP stdio) ──────────
LOG_PATH = Path.home() / ".claude" / "memory" / "server.log"
LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

logger = logging.getLogger("sqlite-memory")
logger.setLevel(logging.DEBUG)
_fh = logging.FileHandler(LOG_PATH, encoding="utf-8")
_fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
logger.addHandler(_fh)

# ── FastMCP app ──────────────────────────────────────────────────────────
from fastmcp import FastMCP

mcp = FastMCP(
    "sqlite-memory",
    instructions=(
        "SQLite-backed persistent memory with WAL concurrent safety, "
        "FTS5 search, and session tracking"
    ),
)

# ── Constants + DB path ──────────────────────────────────────────────────
DB_PATH = os.environ.get(
    "SQLITE_MEMORY_DB",
    os.path.expanduser("~/.claude/memory/memory.db"),
)

BRIDGE_REPO = os.environ.get(
    "BRIDGE_REPO",
    os.path.expanduser("~/.claude/memory/bridge"),
)

_PRAGMAS = (
    "PRAGMA journal_mode=WAL;",
    "PRAGMA foreign_keys=ON;",
    "PRAGMA busy_timeout=10000;",
    "PRAGMA wal_autocheckpoint=100;",
)

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS entities (
    id          INTEGER PRIMARY KEY,
    name        TEXT    UNIQUE NOT NULL,
    entity_type TEXT    NOT NULL,
    project     TEXT    DEFAULT NULL,
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS observations (
    id          INTEGER PRIMARY KEY,
    entity_id   INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    content     TEXT    NOT NULL,
    created_at  TEXT    NOT NULL,
    UNIQUE(entity_id, content)
);

CREATE TABLE IF NOT EXISTS relations (
    id            INTEGER PRIMARY KEY,
    from_id       INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    to_id         INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    relation_type TEXT    NOT NULL,
    created_at    TEXT    NOT NULL,
    UNIQUE(from_id, to_id, relation_type)
);

CREATE TABLE IF NOT EXISTS sessions (
    id           INTEGER PRIMARY KEY,
    session_id   TEXT    UNIQUE NOT NULL,
    project      TEXT    DEFAULT NULL,
    summary      TEXT    DEFAULT NULL,
    active_files TEXT    DEFAULT NULL,
    started_at   TEXT    NOT NULL,
    ended_at     TEXT    DEFAULT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
    name, entity_type, observations_text,
    tokenize = "unicode61 remove_diacritics 2"
);
"""


def _now() -> str:
    """ISO 8601 UTC timestamp."""
    return datetime.now(timezone.utc).isoformat()


# ── Connection helper ────────────────────────────────────────────────────
@contextmanager
def _get_conn():
    """Yield a SQLite connection with all PRAGMAs set, auto-commit/rollback."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    for pragma in _PRAGMAS:
        conn.execute(pragma)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── Schema init ──────────────────────────────────────────────────────────
def _init_db() -> None:
    """Create tables if they don't exist, set WAL mode."""
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with _get_conn() as conn:
        conn.executescript(_SCHEMA_SQL)
    logger.info("Database initialized at %s", DB_PATH)


# ── FTS sync helper ──────────────────────────────────────────────────────
def _fts_sync(conn: sqlite3.Connection, entity_id: int) -> None:
    """Rebuild the FTS entry for a given entity.

    Gathers all observations, concatenates them, and upserts into memory_fts.
    The FTS rowid is kept in sync with entities.id.
    """
    row = conn.execute(
        "SELECT id, name, entity_type FROM entities WHERE id = ?",
        (entity_id,),
    ).fetchone()
    if row is None:
        # Entity was deleted — remove from FTS
        conn.execute("DELETE FROM memory_fts WHERE rowid = ?", (entity_id,))
        return

    obs_rows = conn.execute(
        "SELECT content FROM observations WHERE entity_id = ? ORDER BY id",
        (entity_id,),
    ).fetchall()
    obs_text = "\n".join(r["content"] for r in obs_rows)

    # DELETE then INSERT to ensure idempotent upsert (FTS5 has no ON CONFLICT)
    conn.execute("DELETE FROM memory_fts WHERE rowid = ?", (entity_id,))
    conn.execute(
        "INSERT INTO memory_fts(rowid, name, entity_type, observations_text) "
        "VALUES (?, ?, ?, ?)",
        (row["id"], row["name"], row["entity_type"], obs_text),
    )


def _fts_sync_by_name(conn: sqlite3.Connection, entity_name: str) -> None:
    """FTS sync by entity name (convenience wrapper)."""
    row = conn.execute(
        "SELECT id FROM entities WHERE name = ?", (entity_name,)
    ).fetchone()
    if row:
        _fts_sync(conn, row["id"])


def _fts_remove(conn: sqlite3.Connection, entity_id: int) -> None:
    """Remove entity from FTS index."""
    conn.execute("DELETE FROM memory_fts WHERE rowid = ?", (entity_id,))


# ── Migration helper ─────────────────────────────────────────────────────
def _migrate_jsonl() -> None:
    """One-time migration from the old @modelcontextprotocol memory.json JSONL format.

    Expected format (one JSON object per line):
      {"type": "entity", "name": "...", "entityType": "...", "observations": [...]}
      {"type": "relation", "from": "...", "to": "...", "relationType": "..."}
    """
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
            row = conn.execute(
                "SELECT id FROM entities WHERE name = ?", (ent["name"],)
            ).fetchone()
            if row:
                for obs in ent.get("observations", []):
                    conn.execute(
                        "INSERT OR IGNORE INTO observations (entity_id, content, created_at) "
                        "VALUES (?, ?, ?)",
                        (row["id"], obs, now),
                    )
                _fts_sync(conn, row["id"])

        for rel in relations:
            from_row = conn.execute(
                "SELECT id FROM entities WHERE name = ?", (rel["from"],)
            ).fetchone()
            to_row = conn.execute(
                "SELECT id FROM entities WHERE name = ?", (rel["to"],)
            ).fetchone()
            if from_row and to_row:
                conn.execute(
                    "INSERT OR IGNORE INTO relations "
                    "(from_id, to_id, relation_type, created_at) VALUES (?, ?, ?, ?)",
                    (from_row["id"], to_row["id"], rel.get("relationType", "related_to"), now),
                )

    migrated_path = json_path.with_suffix(".json.migrated")
    json_path.rename(migrated_path)
    logger.info(
        "Migration complete: %d entities, %d relations. Old file → %s",
        len(entities), len(relations), migrated_path,
    )


# ═══════════════════════════════════════════════════════════════════════════
# Tools 1-3: Create
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

            cur = conn.execute(
                "INSERT OR IGNORE INTO entities "
                "(name, entity_type, project, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (name, etype, project, now, now),
            )
            if cur.rowcount > 0:
                created += 1

            row = conn.execute(
                "SELECT id FROM entities WHERE name = ?", (name,)
            ).fetchone()
            if row:
                eid = row["id"]
                # Update project if provided and entity already existed
                if project is not None and cur.rowcount == 0:
                    conn.execute(
                        "UPDATE entities SET project = ?, updated_at = ? "
                        "WHERE id = ? AND (project IS NULL OR project != ?)",
                        (project, now, eid, project),
                    )
                for obs in observations:
                    conn.execute(
                        "INSERT OR IGNORE INTO observations "
                        "(entity_id, content, created_at) VALUES (?, ?, ?)",
                        (eid, obs, now),
                    )
                _fts_sync(conn, eid)

    logger.info("create_entities: %d created out of %d requested", created, len(entities))
    return json.dumps({"created": created, "total_requested": len(entities)})


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
            row = conn.execute(
                "SELECT id FROM entities WHERE name = ?", (entity_name,)
            ).fetchone()
            if row is None:
                logger.warning("add_observations: entity %r not found", entity_name)
                continue
            eid = row["id"]
            for content in item.get("contents", []):
                cur = conn.execute(
                    "INSERT OR IGNORE INTO observations "
                    "(entity_id, content, created_at) VALUES (?, ?, ?)",
                    (eid, content, now),
                )
                added += cur.rowcount
            conn.execute(
                "UPDATE entities SET updated_at = ? WHERE id = ?", (now, eid)
            )
            _fts_sync(conn, eid)

    logger.info("add_observations: %d observations added", added)
    return json.dumps({"added": added})


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

            from_row = conn.execute(
                "SELECT id FROM entities WHERE name = ?", (from_name,)
            ).fetchone()
            to_row = conn.execute(
                "SELECT id FROM entities WHERE name = ?", (to_name,)
            ).fetchone()
            if from_row is None or to_row is None:
                logger.warning(
                    "create_relations: missing entity for %r -> %r", from_name, to_name
                )
                continue

            cur = conn.execute(
                "INSERT OR IGNORE INTO relations "
                "(from_id, to_id, relation_type, created_at) VALUES (?, ?, ?, ?)",
                (from_row["id"], to_row["id"], rel_type, now),
            )
            created += cur.rowcount

    logger.info("create_relations: %d created out of %d requested", created, len(relations))
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
    with _get_conn() as conn:
        for name in entityNames:
            row = conn.execute(
                "SELECT id FROM entities WHERE name = ?", (name,)
            ).fetchone()
            if row is None:
                continue
            eid = row["id"]
            _fts_remove(conn, eid)
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
    with _get_conn() as conn:
        for item in deletions:
            entity_name = item["entityName"]
            row = conn.execute(
                "SELECT id FROM entities WHERE name = ?", (entity_name,)
            ).fetchone()
            if row is None:
                continue
            eid = row["id"]
            for obs in item.get("observations", []):
                cur = conn.execute(
                    "DELETE FROM observations WHERE entity_id = ? AND content = ?",
                    (eid, obs),
                )
                deleted += cur.rowcount
            _fts_sync(conn, eid)

    logger.info("delete_observations: %d deleted", deleted)
    return json.dumps({"deleted": deleted})


@mcp.tool()
def delete_relations(relations: list[dict[str, Any]]) -> str:
    """Delete specific relations from the knowledge graph.

    Each dict has: from (str), to (str), relationType (str).
    """
    deleted = 0
    with _get_conn() as conn:
        for rel in relations:
            from_row = conn.execute(
                "SELECT id FROM entities WHERE name = ?", (rel["from"],)
            ).fetchone()
            to_row = conn.execute(
                "SELECT id FROM entities WHERE name = ?", (rel["to"],)
            ).fetchone()
            if from_row is None or to_row is None:
                continue
            cur = conn.execute(
                "DELETE FROM relations "
                "WHERE from_id = ? AND to_id = ? AND relation_type = ?",
                (from_row["id"], to_row["id"], rel["relationType"]),
            )
            deleted += cur.rowcount

    logger.info("delete_relations: %d deleted", deleted)
    return json.dumps({"deleted": deleted})


# ═══════════════════════════════════════════════════════════════════════════
# Tool 7: read_graph
# ═══════════════════════════════════════════════════════════════════════════

@mcp.tool()
def read_graph() -> str:
    """Read the full knowledge graph.

    Returns JSON: {entities: [{name, entityType, observations: [...]}],
                   relations: [{from, to, relationType}]}
    """
    with _get_conn() as conn:
        ent_rows = conn.execute(
            "SELECT id, name, entity_type, project FROM entities ORDER BY name"
        ).fetchall()

        entities_out = []
        for e in ent_rows:
            obs = conn.execute(
                "SELECT content FROM observations WHERE entity_id = ? ORDER BY id",
                (e["id"],),
            ).fetchall()
            entity = {
                "name": e["name"],
                "entityType": e["entity_type"],
                "observations": [o["content"] for o in obs],
            }
            if e["project"]:
                entity["project"] = e["project"]
            entities_out.append(entity)

        rel_rows = conn.execute(
            "SELECT r.relation_type, ef.name AS from_name, et.name AS to_name "
            "FROM relations r "
            "JOIN entities ef ON r.from_id = ef.id "
            "JOIN entities et ON r.to_id = et.id "
            "ORDER BY ef.name, et.name",
        ).fetchall()

        relations_out = [
            {"from": r["from_name"], "to": r["to_name"], "relationType": r["relation_type"]}
            for r in rel_rows
        ]

    return json.dumps({"entities": entities_out, "relations": relations_out})


# ═══════════════════════════════════════════════════════════════════════════
# Tool 8: search_nodes (FTS5 BM25)
# ═══════════════════════════════════════════════════════════════════════════

def _fts_query(raw: str) -> str:
    """Sanitize a user query for FTS5 MATCH.

    Wraps each token in double quotes to avoid FTS5 syntax errors
    from special characters, then joins with OR for broad matching.
    """
    tokens = raw.split()
    if not tokens:
        return '""'
    escaped = ['"' + t.replace('"', '""') + '"' for t in tokens]
    return " OR ".join(escaped)


@mcp.tool()
def search_nodes(query: str) -> str:
    """Search the knowledge graph using FTS5 BM25-ranked full-text search.

    Returns matching entities with their observations, ranked by relevance.
    """
    fts_q = _fts_query(query)
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT rowid, name, entity_type, observations_text, rank "
            "FROM memory_fts WHERE memory_fts MATCH ? ORDER BY rank "
            "LIMIT 50",
            (fts_q,),
        ).fetchall()

        results = []
        for r in rows:
            eid = r["rowid"]
            obs = conn.execute(
                "SELECT content FROM observations WHERE entity_id = ? ORDER BY id",
                (eid,),
            ).fetchall()
            ent = conn.execute(
                "SELECT project FROM entities WHERE id = ?", (eid,)
            ).fetchone()
            entity = {
                "name": r["name"],
                "entityType": r["entity_type"],
                "observations": [o["content"] for o in obs],
            }
            if ent and ent["project"]:
                entity["project"] = ent["project"]
            results.append(entity)

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
            obs = conn.execute(
                "SELECT content FROM observations WHERE entity_id = ? ORDER BY id",
                (row["id"],),
            ).fetchall()
            entity = {
                "name": row["name"],
                "entityType": row["entity_type"],
                "observations": [o["content"] for o in obs],
            }
            if row["project"]:
                entity["project"] = row["project"]
            entities_out.append(entity)

        # Inter-relations: relations where BOTH from and to are in the opened set
        relations_out = []
        if len(found_ids) >= 2:
            placeholders = ",".join("?" * len(found_ids))
            rel_rows = conn.execute(
                f"SELECT r.relation_type, ef.name AS from_name, et.name AS to_name "
                f"FROM relations r "
                f"JOIN entities ef ON r.from_id = ef.id "
                f"JOIN entities et ON r.to_id = et.id "
                f"WHERE r.from_id IN ({placeholders}) AND r.to_id IN ({placeholders})",
                found_ids + found_ids,
            ).fetchall()
            relations_out = [
                {"from": r["from_name"], "to": r["to_name"], "relationType": r["relation_type"]}
                for r in rel_rows
            ]

    return json.dumps({"entities": entities_out, "relations": relations_out})


# ═══════════════════════════════════════════════════════════════════════════
# Tool 10: session_save
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
# Tool 11: session_recall
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
        session = {
            "session_id": r["session_id"],
            "project": r["project"],
            "summary": r["summary"],
            "active_files": json.loads(r["active_files"]) if r["active_files"] else None,
            "started_at": r["started_at"],
            "ended_at": r["ended_at"],
        }
        sessions.append(session)

    return json.dumps({"sessions": sessions, "count": len(sessions)})


# ═══════════════════════════════════════════════════════════════════════════
# Tool 12: search_by_project (FTS5 scoped)
# ═══════════════════════════════════════════════════════════════════════════

@mcp.tool()
def search_by_project(query: str, project: str) -> str:
    """Search the knowledge graph scoped to a specific project.

    Uses FTS5 BM25-ranked search, then filters results to entities
    whose project field matches the given project.
    """
    fts_q = _fts_query(query)
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT memory_fts.rowid, memory_fts.name, memory_fts.entity_type, "
            "memory_fts.observations_text, memory_fts.rank "
            "FROM memory_fts "
            "JOIN entities ON entities.id = memory_fts.rowid "
            "WHERE memory_fts MATCH ? AND entities.project = ? "
            "ORDER BY memory_fts.rank LIMIT 50",
            (fts_q, project),
        ).fetchall()

        results = []
        for r in rows:
            eid = r["rowid"]
            obs = conn.execute(
                "SELECT content FROM observations WHERE entity_id = ? ORDER BY id",
                (eid,),
            ).fetchall()
            results.append({
                "name": r["name"],
                "entityType": r["entity_type"],
                "project": project,
                "observations": [o["content"] for o in obs],
            })

    logger.info(
        "search_by_project: query=%r project=%r matched=%d",
        query, project, len(results),
    )
    return json.dumps({"entities": results, "query": query, "project": project})


# ═══════════════════════════════════════════════════════════════════════════
# Bridge helper
# ═══════════════════════════════════════════════════════════════════════════

def _git(*args: str) -> subprocess.CompletedProcess:
    """Run a git command in the bridge repo. Never prints to stdout."""
    result = subprocess.run(
        ["git", "-C", BRIDGE_REPO, *args],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        logger.warning("git %s failed: %s", " ".join(args), result.stderr.strip())
    return result


# ═══════════════════════════════════════════════════════════════════════════
# Tools 13-15: Cross-Machine Bridge Sync
# ═══════════════════════════════════════════════════════════════════════════

@mcp.tool()
def bridge_push(tag: str = "shared") -> str:
    """Push tagged entities to the bridge git repo for cross-machine sync.

    Exports entities where project LIKE '{tag}%' with their observations
    and inter-relations to JSON. Git add, commit, push.
    """
    if not Path(BRIDGE_REPO).is_dir():
        return json.dumps({"error": f"Bridge repo not found at {BRIDGE_REPO}. "
                           "Run: mkdir -p {BRIDGE_REPO} && git -C {BRIDGE_REPO} init"})

    with _get_conn() as conn:
        ent_rows = conn.execute(
            "SELECT id, name, entity_type, project, created_at, updated_at "
            "FROM entities WHERE project LIKE ? ORDER BY name",
            (f"{tag}%",),
        ).fetchall()

        entities_out = []
        entity_ids = set()
        for e in ent_rows:
            entity_ids.add(e["id"])
            obs = conn.execute(
                "SELECT content, created_at FROM observations "
                "WHERE entity_id = ? ORDER BY id", (e["id"],),
            ).fetchall()
            entities_out.append({
                "name": e["name"],
                "entityType": e["entity_type"],
                "project": e["project"],
                "observations": [{"content": o["content"], "createdAt": o["created_at"]} for o in obs],
                "createdAt": e["created_at"],
                "updatedAt": e["updated_at"],
            })

        # Relations where BOTH endpoints are in the shared set
        relations_out = []
        if entity_ids:
            ph = ",".join("?" * len(entity_ids))
            ids = list(entity_ids)
            rel_rows = conn.execute(
                f"SELECT ef.name AS from_name, et.name AS to_name, r.relation_type, r.created_at "
                f"FROM relations r "
                f"JOIN entities ef ON r.from_id = ef.id "
                f"JOIN entities et ON r.to_id = et.id "
                f"WHERE r.from_id IN ({ph}) AND r.to_id IN ({ph})",
                ids + ids,
            ).fetchall()
            relations_out = [
                {"from": r["from_name"], "to": r["to_name"],
                 "relationType": r["relation_type"], "createdAt": r["created_at"]}
                for r in rel_rows
            ]

    hostname = socket.gethostname()
    payload = {
        "version": 1,
        "pushed_at": _now(),
        "machine_id": hostname,
        "entities": entities_out,
        "relations": relations_out,
    }

    shared_path = Path(BRIDGE_REPO) / "shared.json"
    shared_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    n_obs = sum(len(e["observations"]) for e in entities_out)
    msg = f"bridge: push {len(entities_out)} entities from {hostname}"

    _git("add", "shared.json")
    commit_result = _git("commit", "-m", msg)
    if commit_result.returncode != 0 and "nothing to commit" in commit_result.stdout:
        logger.info("bridge_push: no changes to commit")
        return json.dumps({"pushed": 0, "message": "No changes — already up to date"})

    push_result = _git("push")
    pushed = push_result.returncode == 0

    logger.info("bridge_push: %d entities, %d observations, %d relations, push=%s",
                len(entities_out), n_obs, len(relations_out), pushed)
    return json.dumps({
        "entities": len(entities_out),
        "observations": n_obs,
        "relations": len(relations_out),
        "pushed_to_remote": pushed,
        "message": msg,
    })


@mcp.tool()
def bridge_pull() -> str:
    """Pull shared entities from the bridge git repo into local memory.

    Git pull, read shared.json, import new entities/observations/relations.
    UNIQUE constraints handle deduplication automatically.
    """
    if not Path(BRIDGE_REPO).is_dir():
        return json.dumps({"error": f"Bridge repo not found at {BRIDGE_REPO}"})

    pull_result = _git("pull", "--rebase")
    if pull_result.returncode != 0:
        logger.warning("bridge_pull: git pull failed, proceeding with local copy")

    shared_path = Path(BRIDGE_REPO) / "shared.json"
    if not shared_path.exists():
        return json.dumps({"error": "shared.json not found in bridge repo"})

    try:
        payload = json.loads(shared_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return json.dumps({"error": f"Failed to read shared.json: {exc}"})

    entities = payload.get("entities", [])
    relations = payload.get("relations", [])
    now = _now()
    new_entities = 0
    new_observations = 0
    new_relations = 0

    with _get_conn() as conn:
        for ent in entities:
            cur = conn.execute(
                "INSERT OR IGNORE INTO entities "
                "(name, entity_type, project, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (ent["name"], ent["entityType"], ent.get("project"),
                 ent.get("createdAt", now), ent.get("updatedAt", now)),
            )
            new_entities += cur.rowcount

            row = conn.execute(
                "SELECT id FROM entities WHERE name = ?", (ent["name"],)
            ).fetchone()
            if row:
                eid = row["id"]
                for obs in ent.get("observations", []):
                    content = obs["content"] if isinstance(obs, dict) else obs
                    created = obs.get("createdAt", now) if isinstance(obs, dict) else now
                    cur2 = conn.execute(
                        "INSERT OR IGNORE INTO observations "
                        "(entity_id, content, created_at) VALUES (?, ?, ?)",
                        (eid, content, created),
                    )
                    new_observations += cur2.rowcount
                _fts_sync(conn, eid)

        for rel in relations:
            from_row = conn.execute(
                "SELECT id FROM entities WHERE name = ?", (rel["from"],)
            ).fetchone()
            to_row = conn.execute(
                "SELECT id FROM entities WHERE name = ?", (rel["to"],)
            ).fetchone()
            if from_row and to_row:
                cur3 = conn.execute(
                    "INSERT OR IGNORE INTO relations "
                    "(from_id, to_id, relation_type, created_at) VALUES (?, ?, ?, ?)",
                    (from_row["id"], to_row["id"], rel["relationType"],
                     rel.get("createdAt", now)),
                )
                new_relations += cur3.rowcount

    logger.info("bridge_pull: %d new entities, %d new observations, %d new relations",
                new_entities, new_observations, new_relations)
    return json.dumps({
        "new_entities": new_entities,
        "new_observations": new_observations,
        "new_relations": new_relations,
        "source_machine": payload.get("machine_id", "unknown"),
        "pushed_at": payload.get("pushed_at", "unknown"),
    })


@mcp.tool()
def bridge_status() -> str:
    """Show bridge sync status — local shared entities vs repo contents."""
    if not Path(BRIDGE_REPO).is_dir():
        return json.dumps({"error": f"Bridge repo not found at {BRIDGE_REPO}"})

    with _get_conn() as conn:
        local_rows = conn.execute(
            "SELECT name FROM entities WHERE project LIKE 'shared%' ORDER BY name"
        ).fetchall()
    local_names = {r["name"] for r in local_rows}

    shared_path = Path(BRIDGE_REPO) / "shared.json"
    remote_names: set[str] = set()
    repo_meta = {}
    if shared_path.exists():
        try:
            payload = json.loads(shared_path.read_text(encoding="utf-8"))
            remote_names = {e["name"] for e in payload.get("entities", [])}
            repo_meta = {
                "pushed_at": payload.get("pushed_at"),
                "machine_id": payload.get("machine_id"),
                "version": payload.get("version"),
            }
        except (json.JSONDecodeError, OSError):
            pass

    only_local = sorted(local_names - remote_names)
    only_remote = sorted(remote_names - local_names)
    in_sync = sorted(local_names & remote_names)

    # Git log for last push/pull timestamps
    log_result = _git("log", "-1", "--format=%ci %s")
    last_commit = log_result.stdout.strip() if log_result.returncode == 0 else None

    return json.dumps({
        "local_shared_count": len(local_names),
        "remote_count": len(remote_names),
        "in_sync": len(in_sync),
        "only_local": only_local,
        "only_remote": only_remote,
        "last_commit": last_commit,
        "repo_meta": repo_meta,
    })


# ═══════════════════════════════════════════════════════════════════════════
# Startup
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    _init_db()
    _migrate_jsonl()
    mcp.run(transport="stdio")
