#!/usr/bin/env python3
"""SQLite-backed MCP Memory Server.

Production-quality persistent memory with WAL concurrent safety,
FTS5 BM25-ranked search, session tracking, cross-machine bridge sync,
and structured task management.

Drop-in compatible with @modelcontextprotocol/server-memory (tools 1-9)
plus extended tools: session (10-12), task management (13-18), bridge (19-21),
multi-account knowledge collaboration (25-27).
"""

from __future__ import annotations

import json
import logging
import os
import socket
import sqlite3
import subprocess
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from db_utils import (
    TASK_ACTIVE_EXCLUSIONS as _TASK_ACTIVE_EXCLUSIONS,
    TASK_SECTIONS as _TASK_SECTIONS,
    TASK_PRIORITIES as _TASK_PRIORITIES,
    TASK_STATUSES as _TASK_STATUSES,
    TASK_TYPES as _TASK_TYPES,
    VISIBILITY_LEVELS as _VISIBILITY_LEVELS,
    PUBLISH_STANDBY_MINUTES as _PUBLISH_STANDBY_MINUTES,
    build_priority_order_sql,
    now_iso as _now,
)

# Pre-built SQL fragment for active-task exclusion filter
_EXCL_PH = ",".join("?" for _ in _TASK_ACTIVE_EXCLUSIONS)

# ── Recurring task validation ─────────────────────────────────────────
_RECURRING_EVERY = ("day", "week", "month")
_RECURRING_WEEKDAYS = frozenset(
    ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
)


def _validate_recurring(raw: str) -> str | None:
    """Validate recurring JSON config. Returns error message or None if valid."""
    try:
        config = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return f"Invalid JSON: {raw!r}"
    if not isinstance(config, dict):
        return "Recurring config must be a JSON object"
    every = config.get("every", "").lower()
    if every not in _RECURRING_EVERY:
        return f"Invalid 'every': {every}. Use: {_RECURRING_EVERY}"
    if every == "week":
        day = config.get("day", "").lower()
        if day not in _RECURRING_WEEKDAYS:
            return f"Weekly recurrence requires 'day' (weekday name). Got: {day!r}"
    if every == "month":
        day = config.get("day")
        if day is None:
            return "Monthly recurrence requires 'day' (1-31)"
        try:
            d = int(day)
            if not 1 <= d <= 31:
                return f"Monthly 'day' must be 1-31. Got: {d}"
        except (ValueError, TypeError):
            return f"Monthly 'day' must be an integer. Got: {day!r}"
    return None

# ── Logging setup (file-only, NEVER stdout — breaks MCP stdio) ──────────
LOG_PATH = Path.home() / ".claude" / "memory" / "server.log"
LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

logger = logging.getLogger("sqlite-memory")
logger.setLevel(logging.DEBUG)
_fh = logging.FileHandler(LOG_PATH, encoding="utf-8")
_fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
if not logger.handlers:
    logger.addHandler(_fh)

# ── FastMCP app ──────────────────────────────────────────────────────────
from fastmcp import FastMCP

mcp = FastMCP(
    "sqlite-memory",
    instructions=(
        "SQLite-backed persistent memory with WAL concurrent safety, "
        "FTS5 search, session tracking, structured task management, "
        "and cross-machine bridge sync"
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
    shared_by   TEXT    DEFAULT NULL,
    origin      TEXT    DEFAULT 'local',
    visibility           TEXT DEFAULT 'private',
    publish_requested_at TEXT DEFAULT NULL,
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

CREATE TABLE IF NOT EXISTS tasks (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    description TEXT DEFAULT NULL,
    status      TEXT NOT NULL DEFAULT 'not_started',
    priority    TEXT DEFAULT 'medium',
    section     TEXT DEFAULT 'inbox',
    due_date    TEXT DEFAULT NULL,
    project     TEXT DEFAULT NULL,
    parent_id   TEXT DEFAULT NULL REFERENCES tasks(id) ON DELETE SET NULL,  -- only affects fresh installs
    notes       TEXT DEFAULT NULL,
    recurring   TEXT DEFAULT NULL,
    type        TEXT NOT NULL DEFAULT 'task',
    assignee    TEXT DEFAULT NULL,
    shared_by   TEXT DEFAULT NULL,
    visibility           TEXT DEFAULT 'private',
    publish_requested_at TEXT DEFAULT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_entities_type    ON entities(entity_type);
CREATE INDEX IF NOT EXISTS idx_entities_project ON entities(project);
CREATE INDEX IF NOT EXISTS idx_obs_entity       ON observations(entity_id);
CREATE INDEX IF NOT EXISTS idx_sessions_started ON sessions(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_tasks_status     ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_section    ON tasks(section);
CREATE INDEX IF NOT EXISTS idx_tasks_due        ON tasks(due_date);
CREATE INDEX IF NOT EXISTS idx_tasks_project    ON tasks(project);
CREATE INDEX IF NOT EXISTS idx_tasks_parent     ON tasks(parent_id);
CREATE INDEX IF NOT EXISTS idx_tasks_type       ON tasks(type);
CREATE INDEX IF NOT EXISTS idx_tasks_assignee   ON tasks(assignee);

CREATE TABLE IF NOT EXISTS pending_shared_tasks (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    description TEXT DEFAULT NULL,
    status      TEXT NOT NULL DEFAULT 'not_started',
    priority    TEXT DEFAULT 'medium',
    section     TEXT DEFAULT 'inbox',
    due_date    TEXT DEFAULT NULL,
    project     TEXT DEFAULT NULL,
    parent_id   TEXT DEFAULT NULL,
    notes       TEXT DEFAULT NULL,
    recurring   TEXT DEFAULT NULL,
    type        TEXT NOT NULL DEFAULT 'task',
    assignee    TEXT DEFAULT NULL,
    shared_by   TEXT DEFAULT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    received_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS collaborators (
    github_user   TEXT PRIMARY KEY,
    display_name  TEXT DEFAULT NULL,
    trust_level   TEXT NOT NULL DEFAULT 'read_write',
    added_at      TEXT NOT NULL,
    last_sync_at  TEXT DEFAULT NULL,
    notes         TEXT DEFAULT NULL
);

CREATE TABLE IF NOT EXISTS pending_shared_entities (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    entity_type   TEXT NOT NULL,
    project       TEXT DEFAULT NULL,
    observations  TEXT NOT NULL,
    priority      TEXT NOT NULL DEFAULT 'medium',
    shared_by     TEXT NOT NULL,
    source_hash   TEXT NOT NULL,
    received_at   TEXT NOT NULL,
    UNIQUE(source_hash, shared_by)
);

CREATE TABLE IF NOT EXISTS pending_shared_relations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    from_entity     TEXT NOT NULL,
    to_entity       TEXT NOT NULL,
    relation_type   TEXT NOT NULL,
    shared_by       TEXT NOT NULL,
    received_at     TEXT NOT NULL,
    UNIQUE(from_entity, to_entity, relation_type, shared_by)
);

CREATE TABLE IF NOT EXISTS sharing_rules (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_name   TEXT NOT NULL,
    target_user   TEXT NOT NULL,
    share_type    TEXT NOT NULL DEFAULT 'entity',
    priority      TEXT NOT NULL DEFAULT 'medium',
    created_at    TEXT NOT NULL,
    UNIQUE(entity_name, target_user, share_type)
);

CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
    name, entity_type, observations_text,
    tokenize = "unicode61 remove_diacritics 2"
);
"""


# ── Connection helper ────────────────────────────────────────────────────
@contextmanager
def _get_conn():
    """Yield a SQLite connection with all PRAGMAs set, auto-commit/rollback."""
    conn = sqlite3.connect(DB_PATH, isolation_level=None)
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
_MIGRATIONS = [
    # (check_query, migration_sql, description)
    (
        "SELECT 1 FROM pragma_table_info('tasks') WHERE name='description'",
        "ALTER TABLE tasks ADD COLUMN description TEXT DEFAULT NULL",
        "tasks.description column",
    ),
    # v0.5.0: type column
    (
        "SELECT 1 FROM pragma_table_info('tasks') WHERE name='type'",
        "ALTER TABLE tasks ADD COLUMN type TEXT NOT NULL DEFAULT 'task'",
        "tasks.type column (task/note)",
    ),
    # v0.5.0: assignee column
    (
        "SELECT 1 FROM pragma_table_info('tasks') WHERE name='assignee'",
        "ALTER TABLE tasks ADD COLUMN assignee TEXT DEFAULT NULL",
        "tasks.assignee column",
    ),
    # v0.5.0: shared_by column
    (
        "SELECT 1 FROM pragma_table_info('tasks') WHERE name='shared_by'",
        "ALTER TABLE tasks ADD COLUMN shared_by TEXT DEFAULT NULL",
        "tasks.shared_by column",
    ),
    # v0.5.0: type index
    (
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_tasks_type'",
        "CREATE INDEX idx_tasks_type ON tasks(type)",
        "idx_tasks_type index",
    ),
    # v0.5.0: assignee index
    (
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_tasks_assignee'",
        "CREATE INDEX idx_tasks_assignee ON tasks(assignee)",
        "idx_tasks_assignee index",
    ),
    # v0.5.0: pending_shared_tasks staging table
    (
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='pending_shared_tasks'",
        "CREATE TABLE pending_shared_tasks ("
        "id TEXT PRIMARY KEY, title TEXT NOT NULL, description TEXT, "
        "status TEXT NOT NULL DEFAULT 'not_started', priority TEXT DEFAULT 'medium', "
        "section TEXT DEFAULT 'inbox', due_date TEXT, project TEXT, parent_id TEXT, "
        "notes TEXT, recurring TEXT, type TEXT NOT NULL DEFAULT 'task', "
        "assignee TEXT, shared_by TEXT, created_at TEXT NOT NULL, "
        "updated_at TEXT NOT NULL, received_at TEXT NOT NULL)",
        "pending_shared_tasks staging table",
    ),
    # v0.6.0: collaborators address book
    (
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='collaborators'",
        "CREATE TABLE collaborators ("
        "github_user TEXT PRIMARY KEY, display_name TEXT, "
        "trust_level TEXT NOT NULL DEFAULT 'read_write', "
        "added_at TEXT NOT NULL, last_sync_at TEXT, notes TEXT)",
        "collaborators table",
    ),
    # v0.6.0: pending_shared_entities staging
    (
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='pending_shared_entities'",
        "CREATE TABLE pending_shared_entities ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, "
        "entity_type TEXT NOT NULL, project TEXT, observations TEXT NOT NULL, "
        "priority TEXT NOT NULL DEFAULT 'medium', "
        "shared_by TEXT NOT NULL, source_hash TEXT NOT NULL, received_at TEXT NOT NULL, "
        "UNIQUE(source_hash, shared_by))",
        "pending_shared_entities staging table",
    ),
    # v0.6.0: pending_shared_relations staging
    (
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='pending_shared_relations'",
        "CREATE TABLE pending_shared_relations ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, from_entity TEXT NOT NULL, "
        "to_entity TEXT NOT NULL, relation_type TEXT NOT NULL, "
        "shared_by TEXT NOT NULL, received_at TEXT NOT NULL, "
        "UNIQUE(from_entity, to_entity, relation_type, shared_by))",
        "pending_shared_relations staging table",
    ),
    # v0.6.0: sharing_rules
    (
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sharing_rules'",
        "CREATE TABLE sharing_rules ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, entity_name TEXT NOT NULL, "
        "target_user TEXT NOT NULL, share_type TEXT NOT NULL DEFAULT 'entity', "
        "priority TEXT NOT NULL DEFAULT 'medium', "
        "created_at TEXT NOT NULL, UNIQUE(entity_name, target_user, share_type))",
        "sharing_rules table",
    ),
    # v0.6.0: entities.shared_by column
    (
        "SELECT 1 FROM pragma_table_info('entities') WHERE name='shared_by'",
        "ALTER TABLE entities ADD COLUMN shared_by TEXT DEFAULT NULL",
        "entities.shared_by column",
    ),
    # v0.6.0: entities.origin column
    (
        "SELECT 1 FROM pragma_table_info('entities') WHERE name='origin'",
        "ALTER TABLE entities ADD COLUMN origin TEXT DEFAULT 'local'",
        "entities.origin column",
    ),
    # v0.7.0: public knowledge — visibility columns
    (
        "SELECT 1 FROM pragma_table_info('entities') WHERE name='visibility'",
        "ALTER TABLE entities ADD COLUMN visibility TEXT DEFAULT 'private'",
        "entities.visibility column",
    ),
    (
        "SELECT 1 FROM pragma_table_info('entities') WHERE name='publish_requested_at'",
        "ALTER TABLE entities ADD COLUMN publish_requested_at TEXT DEFAULT NULL",
        "entities.publish_requested_at column",
    ),
    (
        "SELECT 1 FROM pragma_table_info('tasks') WHERE name='visibility'",
        "ALTER TABLE tasks ADD COLUMN visibility TEXT DEFAULT 'private'",
        "tasks.visibility column",
    ),
    (
        "SELECT 1 FROM pragma_table_info('tasks') WHERE name='publish_requested_at'",
        "ALTER TABLE tasks ADD COLUMN publish_requested_at TEXT DEFAULT NULL",
        "tasks.publish_requested_at column",
    ),
    # v0.7.0: visibility indexes
    (
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_entities_visibility'",
        "CREATE INDEX idx_entities_visibility ON entities(visibility)",
        "idx_entities_visibility index",
    ),
    (
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_tasks_visibility'",
        "CREATE INDEX idx_tasks_visibility ON tasks(visibility)",
        "idx_tasks_visibility index",
    ),
]


def _init_db() -> None:
    """Create tables if they don't exist, run migrations, set WAL mode."""
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with _get_conn() as conn:
        conn.executescript(_SCHEMA_SQL)
    # Migrations in separate transaction for proper rollback
    with _get_conn() as conn:
        for check_q, migrate_q, desc in _MIGRATIONS:
            if not conn.execute(check_q).fetchone():
                conn.execute(migrate_q)
                logger.info("Migration applied: %s", desc)
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
                    (
                        from_row["id"],
                        to_row["id"],
                        rel.get("relationType", "related_to"),
                        now,
                    ),
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
            # v0.7.0: visibility only 'private' at creation (no bypass)
            vis = ent.get("visibility", "private")
            if vis not in _VISIBILITY_LEVELS or vis != "private":
                vis = "private"

            cur = conn.execute(
                "INSERT OR IGNORE INTO entities "
                "(name, entity_type, project, visibility, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (name, etype, project, vis, now, now),
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

    logger.info(
        "create_entities: %d created out of %d requested", created, len(entities)
    )
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
            conn.execute("UPDATE entities SET updated_at = ? WHERE id = ?", (now, eid))
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
            {
                "from": r["from_name"],
                "to": r["to_name"],
                "relationType": r["relation_type"],
            }
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
                {
                    "from": r["from_name"],
                    "to": r["to_name"],
                    "relationType": r["relation_type"],
                }
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
            results.append(
                {
                    "name": r["name"],
                    "entityType": r["entity_type"],
                    "project": project,
                    "observations": [o["content"] for o in obs],
                }
            )

    logger.info(
        "search_by_project: query=%r project=%r matched=%d",
        query,
        project,
        len(results),
    )
    return json.dumps({"entities": results, "query": query, "project": project})


# ═══════════════════════════════════════════════════════════════════════════
# Tools 13-18: Task Management
# ═══════════════════════════════════════════════════════════════════════════


# _TASK_STATUSES, _TASK_PRIORITIES, _TASK_SECTIONS imported from db_utils


def _sanitize_task_enums(task: dict) -> None:
    """Clamp task enum fields to valid values in-place."""
    if task.get("status") not in _TASK_STATUSES:
        task["status"] = "not_started"
    if task.get("priority") not in _TASK_PRIORITIES:
        task["priority"] = "medium"
    if task.get("section") not in _TASK_SECTIONS:
        task["section"] = "inbox"
    if task.get("type") not in _TASK_TYPES:
        task["type"] = "task"


@mcp.tool()
def create_task(
    title: str,
    type: str = "task",
    description: str | None = None,
    section: str = "inbox",
    priority: str = "medium",
    due_date: str | None = None,
    project: str | None = None,
    parent_id: str | None = None,
    notes: str | None = None,
    recurring: str | None = None,
) -> str:
    """Create a new task or note. Returns the UUID.

    Args:
        title: Task title (required).
        type: task | note.
        description: Unlimited-length task description/details.
        section: inbox | today | next | someday | waiting.
        priority: low | medium | high | critical.
        due_date: YYYY-MM-DD format or None.
        project: Project tag for grouping.
        parent_id: UUID of parent task (for subtasks).
        notes: Freeform notes.
        recurring: JSON config for recurrence (e.g. '{"every":"week","day":"monday"}').
    """
    task_id = str(uuid.uuid4())
    now = _now()

    if section not in _TASK_SECTIONS:
        return json.dumps(
            {"error": f"Invalid section: {section}. Use: {_TASK_SECTIONS}"}
        )
    if priority not in _TASK_PRIORITIES:
        return json.dumps(
            {"error": f"Invalid priority: {priority}. Use: {_TASK_PRIORITIES}"}
        )
    if type not in _TASK_TYPES:
        return json.dumps({"error": f"Invalid type: {type}. Use: {_TASK_TYPES}"})
    if due_date:
        try:
            datetime.strptime(due_date, "%Y-%m-%d")
        except ValueError:
            return json.dumps(
                {"error": f"Invalid due_date: {due_date}. Use YYYY-MM-DD format"}
            )
    if recurring:
        err = _validate_recurring(recurring)
        if err:
            return json.dumps({"error": f"Invalid recurring config: {err}"})

    with _get_conn() as conn:
        if parent_id:
            parent = conn.execute(
                "SELECT id FROM tasks WHERE id = ?", (parent_id,)
            ).fetchone()
            if not parent:
                return json.dumps({"error": f"Parent task {parent_id} not found"})

        conn.execute(
            "INSERT INTO tasks (id, title, description, status, priority, section, "
            "due_date, project, parent_id, notes, recurring, type, "
            "created_at, updated_at) "
            "VALUES (?, ?, ?, 'not_started', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                task_id,
                title,
                description,
                priority,
                section,
                due_date,
                project,
                parent_id,
                notes,
                recurring,
                type,
                now,
                now,
            ),
        )

    logger.info("create_task: %s (%s)", title, task_id)
    return json.dumps(
        {"task_id": task_id, "title": title, "type": type, "status": "not_started"}
    )


@mcp.tool()
def update_task(
    task_id: str,
    title: str | None = None,
    description: str | None = None,
    status: str | None = None,
    priority: str | None = None,
    section: str | None = None,
    due_date: str | None = None,
    project: str | None = None,
    parent_id: str | None = None,
    notes: str | None = None,
    recurring: str | None = None,
    type: str | None = None,
) -> str:
    """Update a task's fields. Only provided fields are changed.

    Args:
        task_id: UUID of the task to update (required).
        title: New title.
        description: Unlimited-length task description/details.
        status: not_started | in_progress | done | archived | cancelled.
        priority: low | medium | high | critical.
        section: inbox | today | next | someday | waiting.
        due_date: YYYY-MM-DD or None.
        project: Project tag.
        parent_id: Parent task UUID.
        notes: Freeform notes.
        recurring: JSON recurrence config.
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
        "type": type,
    }
    updates = {}
    for k, v in fields.items():
        if v == "":
            updates[k] = None  # empty string = clear field to NULL
        elif v is not None:
            updates[k] = v
    if not updates:
        return json.dumps({"error": "No valid fields to update"})

    if "status" in updates and updates["status"] not in _TASK_STATUSES:
        return json.dumps(
            {"error": f"Invalid status: {updates['status']}. Use: {_TASK_STATUSES}"}
        )
    if "priority" in updates and updates["priority"] not in _TASK_PRIORITIES:
        return json.dumps(
            {
                "error": f"Invalid priority: {updates['priority']}. Use: {_TASK_PRIORITIES}"
            }
        )
    if "section" in updates and updates["section"] not in _TASK_SECTIONS:
        return json.dumps(
            {"error": f"Invalid section: {updates['section']}. Use: {_TASK_SECTIONS}"}
        )
    if "type" in updates and updates["type"] not in _TASK_TYPES:
        return json.dumps(
            {"error": f"Invalid type: {updates['type']}. Use: {_TASK_TYPES}"}
        )
    if "due_date" in updates and updates["due_date"] is not None:
        try:
            datetime.strptime(updates["due_date"], "%Y-%m-%d")
        except ValueError:
            return json.dumps(
                {"error": f"Invalid due_date: {updates['due_date']}. Use YYYY-MM-DD"}
            )
    if "recurring" in updates and updates["recurring"] is not None:
        err = _validate_recurring(updates["recurring"])
        if err:
            return json.dumps({"error": f"Invalid recurring config: {err}"})

    updates["updated_at"] = _now()
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [task_id]

    with _get_conn() as conn:
        cur = conn.execute(f"UPDATE tasks SET {set_clause} WHERE id = ?", values)
        if cur.rowcount == 0:
            return json.dumps({"error": f"Task {task_id} not found"})

    logger.info("update_task: %s updated %s", task_id, list(updates.keys()))
    return json.dumps({"updated": task_id, "fields": list(updates.keys())})


@mcp.tool()
def query_tasks(
    section: str | None = None,
    status: str | None = None,
    priority: str | None = None,
    project: str | None = None,
    parent_id: str | None = None,
    type: str | None = None,
    overdue_only: bool = False,
    limit: int = 50,
) -> str:
    """Query tasks with optional filters. Returns markdown table.

    Filters are combined with AND. Omit a filter to skip it.
    overdue_only=True shows only tasks past due_date that are not done/archived.
    """
    conditions: list[str] = []
    params: list[Any] = []

    if section:
        conditions.append("section = ?")
        params.append(section)
    if status:
        conditions.append("status = ?")
        params.append(status)
    if priority:
        conditions.append("priority = ?")
        params.append(priority)
    if project:
        conditions.append("project = ?")
        params.append(project)
    if parent_id:
        conditions.append("parent_id = ?")
        params.append(parent_id)
    if type:
        conditions.append("type = ?")
        params.append(type)
    if overdue_only:
        conditions.append("due_date < date('now')")
        conditions.append(f"status NOT IN ({_EXCL_PH})")
        params.extend(_TASK_ACTIVE_EXCLUSIONS)

    where = " AND ".join(conditions) if conditions else "1=1"
    params.append(limit)

    with _get_conn() as conn:
        rows = conn.execute(
            f"SELECT id, title, description, status, priority, section, due_date, project, parent_id "
            f"FROM tasks WHERE {where} "
            f"ORDER BY "
            f"  {build_priority_order_sql()}, "
            f"  due_date ASC NULLS LAST, created_at ASC "
            f"LIMIT ?",
            params,
        ).fetchall()

    if not rows:
        return json.dumps(
            {"tasks": [], "count": 0, "message": "No tasks match filters"}
        )

    # Build markdown table
    lines = [
        "| # | Title | Status | Priority | Section | Due | Project |",
        "|---|-------|--------|----------|---------|-----|---------|",
    ]
    for i, r in enumerate(rows, 1):
        due = r["due_date"] or "—"
        proj = r["project"] or "—"
        lines.append(
            f"| {i} | {r['title']} | {r['status']} | {r['priority']} "
            f"| {r['section']} | {due} | {proj} |"
        )

    tasks_json = [dict(r) for r in rows]
    return json.dumps(
        {
            "tasks": tasks_json,
            "count": len(rows),
            "markdown": "\n".join(lines),
        }
    )


@mcp.tool()
def task_digest(
    sections: list[str] | None = None,
    include_overdue: bool = True,
    limit: int = 20,
) -> str:
    """Generate a formatted task digest for session start.

    Shows pending/in-progress tasks grouped by section,
    plus overdue tasks highlighted separately.
    """
    target_sections = sections or ["today", "inbox", "next"]

    with _get_conn() as conn:
        # Active tasks by section
        ph = ",".join("?" * len(target_sections))
        active = conn.execute(
            f"SELECT id, title, status, priority, section, due_date, project "
            f"FROM tasks "
            f"WHERE section IN ({ph}) AND status IN ('not_started', 'in_progress') AND type = 'task' "
            f"ORDER BY "
            f"  CASE section WHEN 'today' THEN 0 WHEN 'inbox' THEN 1 "
            f"       WHEN 'next' THEN 2 WHEN 'waiting' THEN 3 WHEN 'someday' THEN 4 END, "
            f"  {build_priority_order_sql()} "
            f"LIMIT ?",
            target_sections + [limit],
        ).fetchall()

        # Overdue tasks
        overdue = []
        if include_overdue:
            overdue = conn.execute(
                "SELECT id, title, status, priority, section, due_date, project "
                "FROM tasks "
                f"WHERE due_date < date('now') AND status NOT IN ({_EXCL_PH}) AND type = 'task' "
                "ORDER BY due_date ASC LIMIT 10",
                list(_TASK_ACTIVE_EXCLUSIONS),
            ).fetchall()

        # Counts
        counts = conn.execute(
            "SELECT status, COUNT(*) as cnt FROM tasks "
            "WHERE status NOT IN ('archived', 'cancelled') GROUP BY status"
        ).fetchall()

    # Format digest
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
            lines.append(
                f"- [{t['priority'].upper()}] {t['title']} (due: {t['due_date']})"
            )
        lines.append("")

    # Group by section
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
                lines.append(f"- {prio}{t['title']}{due}")
            lines.append("")

    digest_text = "\n".join(lines)
    return json.dumps(
        {
            "digest": digest_text,
            "active_count": len(active),
            "overdue_count": len(overdue),
        }
    )


@mcp.tool()
def archive_done_tasks(older_than_days: int = 7) -> str:
    """Archive completed tasks older than N days.

    Moves tasks with status='done' and updated_at older than
    the threshold to status='archived'.
    """
    try:
        days = int(older_than_days)
    except (ValueError, TypeError):
        return json.dumps({"error": "older_than_days must be an integer"})
    if days < 0:
        return json.dumps({"error": "older_than_days must be non-negative"})

    with _get_conn() as conn:
        cur = conn.execute(
            "UPDATE tasks SET status = 'archived', updated_at = ? "
            "WHERE status = 'done' AND type = 'task' "
            "AND updated_at < datetime('now', ? || ' days')",
            (_now(), f"-{days}"),
        )
        archived = cur.rowcount

    logger.info(
        "archive_done_tasks: %d tasks archived (older than %d days)",
        archived,
        days,
    )
    return json.dumps({"archived": archived, "threshold_days": days})


@mcp.tool()
def bump_overdue_priority(target_priority: str = "high") -> str:
    """Bump priority of overdue tasks that are not done/archived.

    Only bumps tasks whose current priority is lower than target.
    """
    if target_priority not in _TASK_PRIORITIES:
        return json.dumps({"error": f"Invalid priority: {target_priority}"})

    priority_rank = {p: i for i, p in enumerate(_TASK_PRIORITIES)}
    target_rank = priority_rank[target_priority]

    # Only bump priorities lower than target
    lower_priorities = [p for p, r in priority_rank.items() if r < target_rank]
    if not lower_priorities:
        return json.dumps({"bumped": 0, "message": "No lower priorities to bump"})

    ph = ",".join("?" * len(lower_priorities))
    now = _now()

    with _get_conn() as conn:
        cur = conn.execute(
            f"UPDATE tasks SET priority = ?, updated_at = ? "
            f"WHERE due_date < date('now') "
            f"AND status NOT IN ({_EXCL_PH}) "
            f"AND priority IN ({ph})",
            [target_priority, now] + list(_TASK_ACTIVE_EXCLUSIONS) + lower_priorities,
        )
        bumped = cur.rowcount

    logger.info("bump_overdue_priority: %d tasks bumped to %s", bumped, target_priority)
    return json.dumps({"bumped": bumped, "target_priority": target_priority})


@mcp.tool()
def process_recurring_tasks(dry_run: bool = False) -> str:
    """Process recurring tasks: recreate done recurring tasks if schedule matches today.

    Finds tasks with status='done' and a recurring JSON config, checks if today
    matches the schedule, and creates a new not_started copy (idempotent — skips
    if an active task with the same title already exists).

    Args:
        dry_run: If True, show what would be created without inserting.
    """
    from recurring_tasks import process_recurring

    with _get_conn() as conn:
        created = process_recurring(conn, dry_run=dry_run)

    if not created:
        return json.dumps({"message": "No recurring tasks to process today.", "created": 0})

    titles = [t["title"] for t in created]
    prefix = "[dry-run] Would create" if dry_run else "Created"
    logger.info("process_recurring_tasks: %s %d task(s)", prefix.lower(), len(created))
    return json.dumps({
        "message": f"{prefix} {len(created)} recurring task(s)",
        "created": len(created),
        "tasks": titles,
    })


@mcp.tool()
def assign_task(task_id: str, assignee: str | None = None) -> str:
    """Assign a task or note to a GitHub user for collaboration.

    Sets assignee field. On next bridge_push, the item will be
    pushed to https://github.com/{assignee}/memory-bridge.
    Pass assignee=None to unassign.
    """
    now = _now()
    with _get_conn() as conn:
        existing = conn.execute(
            "SELECT id FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if not existing:
            return json.dumps({"error": f"Task {task_id} not found"})

        shared_by = None
        if assignee:
            try:
                result = subprocess.run(
                    ["git", "config", "--global", "user.name"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                shared_by = result.stdout.strip() or None
            except (subprocess.TimeoutExpired, OSError):
                pass

        conn.execute(
            "UPDATE tasks SET assignee = ?, shared_by = ?, updated_at = ? WHERE id = ?",
            (assignee, shared_by, now, task_id),
        )

    action = f"assigned to {assignee}" if assignee else "unassigned"
    logger.info("assign_task: %s %s", task_id, action)
    return json.dumps(
        {"task_id": task_id, "assignee": assignee, "shared_by": shared_by}
    )


@mcp.tool()
def review_shared_tasks(
    action: str = "list",
    task_ids: list[str] | None = None,
) -> str:
    """Review shared tasks pending approval from other users.

    Shared tasks from bridge_pull are staged — never auto-imported.
    Use this tool to list, approve, or reject them.

    Args:
        action: list | approve | reject.
        task_ids: UUIDs to approve/reject. If None with approve/reject, applies to ALL pending.
    """
    if action not in ("list", "approve", "reject"):
        return json.dumps({"error": "action must be: list, approve, reject"})

    with _get_conn() as conn:
        if action == "list":
            rows = conn.execute(
                "SELECT id, title, type, priority, shared_by, received_at "
                "FROM pending_shared_tasks ORDER BY received_at DESC"
            ).fetchall()
            if not rows:
                return json.dumps(
                    {"pending": [], "count": 0, "message": "No pending shared tasks"}
                )
            items = [dict(r) for r in rows]
            return json.dumps({"pending": items, "count": len(items)})

        # Build WHERE for specific IDs or all
        if task_ids:
            ph = ",".join("?" * len(task_ids))
            where = f"id IN ({ph})"
            params = list(task_ids)
        else:
            where = "1=1"
            params = []

        if action == "approve":
            rows = conn.execute(
                f"SELECT * FROM pending_shared_tasks WHERE {where}", params
            ).fetchall()
            imported = 0
            for row in rows:
                t = dict(row)
                _sanitize_task_enums(t)
                tid = t["id"]
                existing = conn.execute(
                    "SELECT updated_at FROM tasks WHERE id = ?", (tid,)
                ).fetchone()
                if existing:
                    if t.get("updated_at", "") > existing["updated_at"]:
                        conn.execute(
                            "UPDATE tasks SET title=?, description=?, status=?, priority=?, "
                            "section=?, due_date=?, project=?, parent_id=?, notes=?, "
                            "recurring=?, type=?, assignee=?, shared_by=?, updated_at=? "
                            "WHERE id=?",
                            (
                                t["title"],
                                t.get("description"),
                                t["status"],
                                t["priority"],
                                t["section"],
                                t.get("due_date"),
                                t.get("project"),
                                t.get("parent_id"),
                                t.get("notes"),
                                t.get("recurring"),
                                t.get("type", "task"),
                                t.get("assignee"),
                                t.get("shared_by"),
                                t["updated_at"],
                                tid,
                            ),
                        )
                        imported += 1
                else:
                    conn.execute(
                        "INSERT INTO tasks (id, title, description, status, priority, "
                        "section, due_date, project, parent_id, notes, recurring, "
                        "type, assignee, shared_by, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            tid,
                            t["title"],
                            t.get("description"),
                            t["status"],
                            t["priority"],
                            t["section"],
                            t.get("due_date"),
                            t.get("project"),
                            t.get("parent_id"),
                            t.get("notes"),
                            t.get("recurring"),
                            t.get("type", "task"),
                            t.get("assignee"),
                            t.get("shared_by"),
                            t.get("created_at"),
                            t["updated_at"],
                        ),
                    )
                    imported += 1
                conn.execute("DELETE FROM pending_shared_tasks WHERE id = ?", (tid,))
            logger.info("review_shared_tasks: approved %d tasks", imported)
            return json.dumps({"approved": imported, "imported": imported})

        # action == "reject"
        cur = conn.execute(f"DELETE FROM pending_shared_tasks WHERE {where}", params)
        rejected = cur.rowcount
        logger.info("review_shared_tasks: rejected %d tasks", rejected)
        return json.dumps({"rejected": rejected})


# ═══════════════════════════════════════════════════════════════════════════
# Tools 25-27: Multi-Account Knowledge Collaboration (v0.6.0)
# ═══════════════════════════════════════════════════════════════════════════


def _source_hash(name: str, entity_type: str, observations: list) -> str:
    """SHA256 hash for deduplication of shared entities."""
    raw = json.dumps({"n": name, "t": entity_type, "o": observations}, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()


@mcp.tool()
def manage_collaborators(
    action: str,
    github_user: str | None = None,
    display_name: str | None = None,
    trust_level: str | None = None,
    notes: str | None = None,
) -> str:
    """Manage the collaborator address book for P2P knowledge sharing.

    Each collaborator is a GitHub user whose memory-bridge repo you can
    push knowledge to and pull knowledge from.

    Args:
        action: add | remove | list | update.
        github_user: GitHub username (required for add/remove/update).
        display_name: Human-friendly name.
        trust_level: read_only (you push, they can't push back) | read_write (bidirectional).
        notes: Free-text notes about this collaborator.
    """
    if action not in ("add", "remove", "list", "update"):
        return json.dumps({"error": "action must be: add, remove, list, update"})

    with _get_conn() as conn:
        if action == "list":
            rows = conn.execute(
                "SELECT * FROM collaborators ORDER BY added_at"
            ).fetchall()
            items = [dict(r) for r in rows]
            return json.dumps({"collaborators": items, "count": len(items)})

        if not github_user:
            return json.dumps({"error": "github_user required for add/remove/update"})

        if action == "add":
            tl = trust_level or "read_write"
            if tl not in _TRUST_LEVELS:
                return json.dumps(
                    {"error": f"trust_level must be one of: {', '.join(_TRUST_LEVELS)}"}
                )
            now = _now()
            conn.execute(
                "INSERT OR REPLACE INTO collaborators "
                "(github_user, display_name, trust_level, added_at, notes) "
                "VALUES (?, ?, ?, ?, ?)",
                (github_user, display_name, tl, now, notes),
            )
            logger.info("manage_collaborators: added %s (trust=%s)", github_user, tl)
            return json.dumps(
                {"added": github_user, "trust_level": tl, "display_name": display_name}
            )

        if action == "remove":
            cur = conn.execute(
                "DELETE FROM collaborators WHERE github_user = ?", (github_user,)
            )
            # Also clean up sharing rules targeting this user
            conn.execute(
                "DELETE FROM sharing_rules WHERE target_user = ?", (github_user,)
            )
            if cur.rowcount == 0:
                return json.dumps({"error": f"Collaborator '{github_user}' not found"})
            logger.info("manage_collaborators: removed %s", github_user)
            return json.dumps({"removed": github_user})

        # action == "update"
        existing = conn.execute(
            "SELECT * FROM collaborators WHERE github_user = ?", (github_user,)
        ).fetchone()
        if not existing:
            return json.dumps({"error": f"Collaborator '{github_user}' not found"})

        updates = {}
        if display_name is not None:
            updates["display_name"] = display_name
        if trust_level is not None:
            if trust_level not in _TRUST_LEVELS:
                return json.dumps(
                    {"error": f"trust_level must be one of: {', '.join(_TRUST_LEVELS)}"}
                )
            updates["trust_level"] = trust_level
        if notes is not None:
            updates["notes"] = notes
        if not updates:
            return json.dumps({"error": "Nothing to update"})

        set_clause = ", ".join(f"{k} = ?" for k in updates)
        conn.execute(
            f"UPDATE collaborators SET {set_clause} WHERE github_user = ?",
            list(updates.values()) + [github_user],
        )
        logger.info("manage_collaborators: updated %s (%s)", github_user, list(updates))
        return json.dumps({"updated": github_user, "fields": list(updates.keys())})


@mcp.tool()
def share_knowledge(
    entity_names: list[str],
    target_users: list[str] | None = None,
    include_relations: bool = True,
    priority: str = "medium",
) -> str:
    """Queue entities for sharing with collaborators on next bridge_push.

    Creates sharing rules — does NOT push immediately.
    P2P priority signals how urgently the recipient should adopt this knowledge.

    Args:
        entity_names: Entity names to share (or ['*'] for all shared-tagged).
        target_users: GitHub usernames (or ['*'] for all collaborators). Defaults to all.
        include_relations: Also share inter-relations between the named entities.
        priority: critical | high | medium | low — urgency signal for recipients.
    """
    if priority not in _TASK_PRIORITIES:
        return json.dumps(
            {"error": f"priority must be one of: {', '.join(_TASK_PRIORITIES)}"}
        )

    with _get_conn() as conn:
        # Resolve target users
        if not target_users or target_users == ["*"]:
            collab_rows = conn.execute(
                "SELECT github_user FROM collaborators"
            ).fetchall()
            targets = [r["github_user"] for r in collab_rows]
        else:
            targets = target_users

        if not targets:
            return json.dumps(
                {
                    "error": "No collaborators found. Use manage_collaborators(action='add') first."
                }
            )

        # Validate entities exist (unless wildcard)
        if entity_names != ["*"]:
            for name in entity_names:
                row = conn.execute(
                    "SELECT 1 FROM entities WHERE name = ?", (name,)
                ).fetchone()
                if not row:
                    return json.dumps({"error": f"Entity '{name}' not found"})

        share_types = ["entity"]
        if include_relations:
            share_types.append("relation")

        created = 0
        now = _now()
        for ename in entity_names:
            for tuser in targets:
                for stype in share_types:
                    cur = conn.execute(
                        "INSERT OR REPLACE INTO sharing_rules "
                        "(entity_name, target_user, share_type, priority, created_at) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (ename, tuser, stype, priority, now),
                    )
                    created += cur.rowcount

        logger.info(
            "share_knowledge: %d rules created for %d entities → %d users (priority=%s)",
            created,
            len(entity_names),
            len(targets),
            priority,
        )
        return json.dumps(
            {
                "rules_created": created,
                "entities": entity_names,
                "targets": targets,
                "include_relations": include_relations,
                "priority": priority,
                "message": f"Queued for next bridge_push. {len(targets)} recipient(s).",
            }
        )


@mcp.tool()
def review_shared_knowledge(
    action: str = "list",
    item_ids: list[int] | None = None,
) -> str:
    """Review incoming shared knowledge from collaborators.

    All cross-account entities enter staging first — never auto-imported.
    P2P priority (critical/high/medium/low) indicates sender's urgency signal.

    Args:
        action: list | approve | reject | diff.
        item_ids: IDs from pending_shared_entities to act on. If None, applies to ALL.
    """
    if action not in ("list", "approve", "reject", "diff"):
        return json.dumps({"error": "action must be: list, approve, reject, diff"})

    with _get_conn() as conn:
        if action == "list":
            ent_rows = conn.execute(
                "SELECT id, name, entity_type, project, priority, shared_by, received_at "
                "FROM pending_shared_entities ORDER BY "
                "CASE priority WHEN 'critical' THEN 0 WHEN 'high' THEN 1 "
                "WHEN 'medium' THEN 2 WHEN 'low' THEN 3 END, received_at DESC"
            ).fetchall()
            rel_rows = conn.execute(
                "SELECT id, from_entity, to_entity, relation_type, shared_by, received_at "
                "FROM pending_shared_relations ORDER BY received_at DESC"
            ).fetchall()
            return json.dumps(
                {
                    "pending_entities": [dict(r) for r in ent_rows],
                    "pending_relations": [dict(r) for r in rel_rows],
                    "entity_count": len(ent_rows),
                    "relation_count": len(rel_rows),
                }
            )

        if action == "diff":
            if not item_ids:
                return json.dumps({"error": "item_ids required for diff"})
            diffs = []
            for iid in item_ids:
                pending = conn.execute(
                    "SELECT * FROM pending_shared_entities WHERE id = ?", (iid,)
                ).fetchone()
                if not pending:
                    diffs.append({"id": iid, "error": "not found"})
                    continue
                p = dict(pending)
                pending_obs = json.loads(p["observations"])
                local = conn.execute(
                    "SELECT id FROM entities WHERE name = ?", (p["name"],)
                ).fetchone()
                if not local:
                    diffs.append(
                        {
                            "id": iid,
                            "name": p["name"],
                            "status": "new_entity",
                            "remote_type": p["entity_type"],
                            "remote_observations": len(pending_obs),
                            "priority": p["priority"],
                        }
                    )
                else:
                    local_obs = conn.execute(
                        "SELECT content FROM observations WHERE entity_id = ?",
                        (local["id"],),
                    ).fetchall()
                    local_contents = {r["content"] for r in local_obs}
                    remote_contents = {o["content"] for o in pending_obs}
                    local_etype = conn.execute(
                        "SELECT entity_type FROM entities WHERE id = ?", (local["id"],)
                    ).fetchone()["entity_type"]
                    diffs.append(
                        {
                            "id": iid,
                            "name": p["name"],
                            "status": "type_conflict"
                            if local_etype != p["entity_type"]
                            else "merge",
                            "local_type": local_etype,
                            "remote_type": p["entity_type"],
                            "new_observations": list(remote_contents - local_contents),
                            "already_have": len(local_contents & remote_contents),
                            "priority": p["priority"],
                        }
                    )
            return json.dumps({"diffs": diffs})

        # Build WHERE for specific IDs or all
        if item_ids:
            ph = ",".join("?" * len(item_ids))
            ent_where = f"id IN ({ph})"
            ent_params: list = list(item_ids)
        else:
            ent_where = "1=1"
            ent_params = []

        if action == "approve":
            rows = conn.execute(
                f"SELECT * FROM pending_shared_entities WHERE {ent_where}", ent_params
            ).fetchall()
            imported_entities = 0
            imported_obs = 0
            now = _now()
            for row in rows:
                p = dict(row)
                pending_obs = json.loads(p["observations"])
                origin = f"shared:{p['shared_by']}"

                # Upsert entity (additive — never overwrites local)
                cur = conn.execute(
                    "INSERT OR IGNORE INTO entities "
                    "(name, entity_type, project, shared_by, origin, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        p["name"],
                        p["entity_type"],
                        p.get("project"),
                        p["shared_by"],
                        origin,
                        now,
                        now,
                    ),
                )
                imported_entities += cur.rowcount

                eid_row = conn.execute(
                    "SELECT id FROM entities WHERE name = ?", (p["name"],)
                ).fetchone()
                if eid_row:
                    eid = eid_row["id"]
                    for obs in pending_obs:
                        content = obs["content"] if isinstance(obs, dict) else obs
                        created = (
                            obs.get("createdAt", now) if isinstance(obs, dict) else now
                        )
                        cur2 = conn.execute(
                            "INSERT OR IGNORE INTO observations "
                            "(entity_id, content, created_at) VALUES (?, ?, ?)",
                            (eid, content, created),
                        )
                        imported_obs += cur2.rowcount
                    _fts_sync(conn, eid)

                conn.execute(
                    "DELETE FROM pending_shared_entities WHERE id = ?", (p["id"],)
                )

            # Also approve matching pending relations
            rel_rows = conn.execute("SELECT * FROM pending_shared_relations").fetchall()
            imported_rels = 0
            for rel in rel_rows:
                r = dict(rel)
                from_row = conn.execute(
                    "SELECT id FROM entities WHERE name = ?", (r["from_entity"],)
                ).fetchone()
                to_row = conn.execute(
                    "SELECT id FROM entities WHERE name = ?", (r["to_entity"],)
                ).fetchone()
                if from_row and to_row:
                    cur3 = conn.execute(
                        "INSERT OR IGNORE INTO relations "
                        "(from_id, to_id, relation_type, created_at) VALUES (?, ?, ?, ?)",
                        (from_row["id"], to_row["id"], r["relation_type"], now),
                    )
                    imported_rels += cur3.rowcount
                    conn.execute(
                        "DELETE FROM pending_shared_relations WHERE id = ?", (r["id"],)
                    )

            logger.info(
                "review_shared_knowledge: approved %d entities, %d obs, %d relations",
                imported_entities,
                imported_obs,
                imported_rels,
            )
            return json.dumps(
                {
                    "approved_entities": imported_entities,
                    "new_observations": imported_obs,
                    "approved_relations": imported_rels,
                }
            )

        # action == "reject"
        cur_e = conn.execute(
            f"DELETE FROM pending_shared_entities WHERE {ent_where}", ent_params
        )
        # If no specific IDs, also clear all pending relations
        if not item_ids:
            cur_r = conn.execute("DELETE FROM pending_shared_relations")
            rejected_rels = cur_r.rowcount
        else:
            rejected_rels = 0
        rejected = cur_e.rowcount
        logger.info(
            "review_shared_knowledge: rejected %d entities, %d relations",
            rejected,
            rejected_rels,
        )
        return json.dumps(
            {"rejected_entities": rejected, "rejected_relations": rejected_rels}
        )


# ═══════════════════════════════════════════════════════════════════════════
# Tools 28-30: Public Knowledge (v0.7.0)
# ═══════════════════════════════════════════════════════════════════════════


@mcp.tool()
def request_publish(
    entity_names: list[str] | None = None,
    task_ids: list[str] | None = None,
    safety_confirmed: bool = False,
) -> str:
    """Request to publish entities/tasks as public knowledge.

    ⚠️ WARNING 1: Publishing makes content visible to ALL instances.
    Default action is to NOT publish. You must explicitly set safety_confirmed=True.

    ⚠️ WARNING 2: Before confirming, verify the content will not harm,
    endanger, or compromise the safety of any person.

    After confirmation, content enters a standby period (default 15 min)
    before becoming truly public on next bridge_push.
    """
    if not entity_names and not task_ids:
        return json.dumps({"error": "Provide entity_names and/or task_ids"})

    if not safety_confirmed:
        return json.dumps({
            "status": "confirmation_required",
            "warning_1": (
                "⚠️ You are about to make content PUBLIC and visible to "
                "ALL Claude instances. Default: DO NOT publish."
            ),
            "warning_2": (
                "⚠️ Are you sure the content will NOT harm, endanger, "
                "or compromise the safety of any person?"
            ),
            "action": "Call request_publish again with safety_confirmed=True to proceed.",
            "standby_minutes": _PUBLISH_STANDBY_MINUTES,
        })

    now = _now()
    updated_entities = 0
    updated_tasks = 0
    not_found: list[str] = []

    with _get_conn() as conn:
        for name in (entity_names or []):
            cur = conn.execute(
                "UPDATE entities SET visibility='pending_public', "
                "publish_requested_at=?, updated_at=? "
                "WHERE name=? AND visibility='private'",
                (now, now, name),
            )
            if cur.rowcount:
                updated_entities += cur.rowcount
            else:
                # Check if it exists at all
                row = conn.execute(
                    "SELECT visibility FROM entities WHERE name=?", (name,)
                ).fetchone()
                if not row:
                    not_found.append(f"entity:{name}")
                # else already pending/public — skip silently

        for tid in (task_ids or []):
            cur = conn.execute(
                "UPDATE tasks SET visibility='pending_public', "
                "publish_requested_at=?, updated_at=? "
                "WHERE id=? AND visibility='private'",
                (now, now, tid),
            )
            if cur.rowcount:
                updated_tasks += cur.rowcount
            else:
                row = conn.execute(
                    "SELECT visibility FROM tasks WHERE id=?", (tid,)
                ).fetchone()
                if not row:
                    not_found.append(f"task:{tid}")

    logger.info(
        "request_publish: %d entities, %d tasks set to pending_public",
        updated_entities, updated_tasks,
    )
    result: dict[str, Any] = {
        "status": "pending_public",
        "entities_updated": updated_entities,
        "tasks_updated": updated_tasks,
        "standby_minutes": _PUBLISH_STANDBY_MINUTES,
        "message": (
            f"Content will become public after {_PUBLISH_STANDBY_MINUTES} min "
            "standby on next bridge_push."
        ),
    }
    if not_found:
        result["not_found"] = not_found
    return json.dumps(result)


@mcp.tool()
def cancel_publish(
    entity_names: list[str] | None = None,
    task_ids: list[str] | None = None,
) -> str:
    """Cancel a pending publish request. Reverts pending_public → private.

    Only works during the standby period (before content becomes truly public).
    """
    if not entity_names and not task_ids:
        return json.dumps({"error": "Provide entity_names and/or task_ids"})

    now = _now()
    reverted_entities = 0
    reverted_tasks = 0

    with _get_conn() as conn:
        for name in (entity_names or []):
            cur = conn.execute(
                "UPDATE entities SET visibility='private', "
                "publish_requested_at=NULL, updated_at=? "
                "WHERE name=? AND visibility='pending_public'",
                (now, name),
            )
            reverted_entities += cur.rowcount

        for tid in (task_ids or []):
            cur = conn.execute(
                "UPDATE tasks SET visibility='private', "
                "publish_requested_at=NULL, updated_at=? "
                "WHERE id=? AND visibility='pending_public'",
                (now, tid),
            )
            reverted_tasks += cur.rowcount

    logger.info(
        "cancel_publish: reverted %d entities, %d tasks to private",
        reverted_entities, reverted_tasks,
    )
    return json.dumps({
        "reverted_entities": reverted_entities,
        "reverted_tasks": reverted_tasks,
    })


@mcp.tool()
def search_public_knowledge(
    query: str,
    entity_type: str | None = None,
    limit: int = 50,
) -> str:
    """Search published public knowledge using FTS5 BM25-ranked search.

    Only returns entities with visibility='public'.
    """
    fts_q = _fts_query(query)
    with _get_conn() as conn:
        if entity_type:
            rows = conn.execute(
                "SELECT memory_fts.rowid, memory_fts.name, memory_fts.entity_type, "
                "memory_fts.observations_text, memory_fts.rank "
                "FROM memory_fts "
                "JOIN entities ON entities.id = memory_fts.rowid "
                "WHERE memory_fts MATCH ? AND entities.visibility = 'public' "
                "AND entities.entity_type = ? "
                "ORDER BY memory_fts.rank LIMIT ?",
                (fts_q, entity_type, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT memory_fts.rowid, memory_fts.name, memory_fts.entity_type, "
                "memory_fts.observations_text, memory_fts.rank "
                "FROM memory_fts "
                "JOIN entities ON entities.id = memory_fts.rowid "
                "WHERE memory_fts MATCH ? AND entities.visibility = 'public' "
                "ORDER BY memory_fts.rank LIMIT ?",
                (fts_q, limit),
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
                "observations": [o["content"] for o in obs],
            })

    logger.info("search_public_knowledge: query=%r matched=%d", query, len(results))
    return json.dumps({"entities": results, "query": query, "count": len(results)})


# ═══════════════════════════════════════════════════════════════════════════
# Bridge helper
# ═══════════════════════════════════════════════════════════════════════════


def _git(*args: str) -> subprocess.CompletedProcess:
    """Run a git command in the bridge repo. Never prints to stdout."""
    result = subprocess.run(
        ["git", "-C", BRIDGE_REPO, *args],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        logger.warning("git %s failed: %s", " ".join(args), result.stderr.strip())
    return result


def _push_to_assignee(assignee: str, tasks: list[dict]) -> None:
    """Push assigned tasks to another user's memory-bridge repo."""
    import tempfile

    repo_url = f"https://github.com/{assignee}/memory-bridge.git"
    with tempfile.TemporaryDirectory() as tmpdir:
        clone = subprocess.run(
            ["git", "clone", "--depth=1", repo_url, tmpdir],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if clone.returncode != 0:
            logger.warning(
                "_push_to_assignee: clone failed for %s: %s",
                assignee,
                clone.stderr.strip(),
            )
            return

        shared_path = Path(tmpdir) / "shared.json"
        existing: dict = {}
        if shared_path.exists():
            try:
                existing = json.loads(shared_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass

        # Merge into shared_tasks array (upsert by id, last-write-wins)
        shared_tasks = {t["id"]: t for t in existing.get("shared_tasks", [])}
        for t in tasks:
            if t.get("updated_at", "") >= shared_tasks.get(t["id"], {}).get(
                "updated_at", ""
            ):
                shared_tasks[t["id"]] = t
        existing["shared_tasks"] = list(shared_tasks.values())

        shared_path.write_text(
            json.dumps(existing, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        subprocess.run(
            ["git", "-C", tmpdir, "add", "shared.json"], capture_output=True, timeout=10
        )
        hostname = socket.gethostname()
        msg = f"bridge: shared {len(tasks)} tasks from {hostname} to {assignee}"
        commit = subprocess.run(
            ["git", "-C", tmpdir, "commit", "-m", msg],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if commit.returncode == 0:
            push = subprocess.run(
                ["git", "-C", tmpdir, "push"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if push.returncode == 0:
                logger.info(
                    "_push_to_assignee: pushed %d tasks to %s", len(tasks), assignee
                )
            else:
                logger.warning(
                    "_push_to_assignee: push failed for %s: %s",
                    assignee,
                    push.stderr.strip(),
                )


def _push_knowledge_to(conn: sqlite3.Connection, target_user: str) -> int:
    """Push shared knowledge (entities + relations) to a collaborator's repo."""
    import tempfile

    # Gather entities to share based on sharing_rules
    rules = conn.execute(
        "SELECT entity_name, share_type, priority FROM sharing_rules WHERE target_user IN (?, '*')",
        (target_user,),
    ).fetchall()
    if not rules:
        return 0

    entity_names: set[str] = set()
    include_relations = False
    priorities: dict[str, str] = {}  # entity_name → priority
    for r in rules:
        if r["share_type"] in ("entity", "all"):
            if r["entity_name"] == "*":
                # All shared-tagged entities
                rows = conn.execute(
                    "SELECT name FROM entities WHERE project LIKE 'shared%'"
                ).fetchall()
                for row in rows:
                    entity_names.add(row["name"])
                    priorities[row["name"]] = r["priority"]
            else:
                entity_names.add(r["entity_name"])
                priorities[r["entity_name"]] = r["priority"]
        if r["share_type"] in ("relation", "all"):
            include_relations = True

    if not entity_names:
        return 0

    # Build knowledge payload
    knowledge_out = []
    entity_ids = set()
    for ename in entity_names:
        erow = conn.execute(
            "SELECT id, name, entity_type, project FROM entities WHERE name = ?",
            (ename,),
        ).fetchone()
        if not erow:
            continue
        entity_ids.add(erow["id"])
        obs = conn.execute(
            "SELECT content, created_at FROM observations WHERE entity_id = ? ORDER BY id",
            (erow["id"],),
        ).fetchall()
        obs_list = [
            {"content": o["content"], "createdAt": o["created_at"]} for o in obs
        ]
        entry = {
            "name": erow["name"],
            "entityType": erow["entity_type"],
            "project": erow["project"],
            "observations": obs_list,
            "priority": priorities.get(ename, "medium"),
            "sharedBy": os.environ.get("GITHUB_USER", socket.gethostname()),
            "sharedAt": _now(),
            "sourceHash": _source_hash(erow["name"], erow["entity_type"], obs_list),
        }
        # Attach relations if requested
        if include_relations:
            rels = conn.execute(
                "SELECT et.name AS to_name, r.relation_type "
                "FROM relations r JOIN entities et ON r.to_id = et.id "
                "WHERE r.from_id = ?",
                (erow["id"],),
            ).fetchall()
            entry["relations"] = [
                {"to": r["to_name"], "relationType": r["relation_type"]}
                for r in rels
                if r["to_name"] in entity_names
            ]
        knowledge_out.append(entry)

    if not knowledge_out:
        return 0

    # Clone target repo, merge knowledge, push
    repo_url = f"https://github.com/{target_user}/memory-bridge.git"
    with tempfile.TemporaryDirectory() as tmpdir:
        clone = subprocess.run(
            ["git", "clone", "--depth=1", repo_url, tmpdir],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if clone.returncode != 0:
            logger.warning(
                "_push_knowledge_to: clone failed for %s: %s",
                target_user,
                clone.stderr.strip(),
            )
            return 0

        shared_path = Path(tmpdir) / "shared.json"
        existing: dict = {}
        if shared_path.exists():
            try:
                existing = json.loads(shared_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass

        # Merge into shared_knowledge (dedup by sourceHash)
        current = {e["sourceHash"]: e for e in existing.get("shared_knowledge", [])}
        for entry in knowledge_out:
            current[entry["sourceHash"]] = entry
        existing["shared_knowledge"] = list(current.values())

        shared_path.write_text(
            json.dumps(existing, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        subprocess.run(
            ["git", "-C", tmpdir, "add", "shared.json"],
            capture_output=True,
            timeout=10,
        )
        hostname = socket.gethostname()
        msg = f"bridge: shared {len(knowledge_out)} entities from {hostname} to {target_user}"
        commit = subprocess.run(
            ["git", "-C", tmpdir, "commit", "-m", msg],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if commit.returncode == 0:
            push = subprocess.run(
                ["git", "-C", tmpdir, "push"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if push.returncode == 0:
                logger.info(
                    "_push_knowledge_to: pushed %d entities to %s",
                    len(knowledge_out),
                    target_user,
                )
                return len(knowledge_out)
            else:
                logger.warning(
                    "_push_knowledge_to: push failed for %s: %s",
                    target_user,
                    push.stderr.strip(),
                )
        return 0


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
        return json.dumps(
            {
                "error": f"Bridge repo not found at {BRIDGE_REPO}. "
                "Run: mkdir -p {BRIDGE_REPO} && git -C {BRIDGE_REPO} init"
            }
        )

    with _get_conn() as conn:
        # v0.7.0: Promote pending_public → public if standby elapsed
        cutoff = (
            datetime.now(timezone.utc) - timedelta(minutes=_PUBLISH_STANDBY_MINUTES)
        ).isoformat()
        promoted_ent = conn.execute(
            "UPDATE entities SET visibility='public' "
            "WHERE visibility='pending_public' AND publish_requested_at <= ?",
            (cutoff,),
        ).rowcount
        promoted_tasks = conn.execute(
            "UPDATE tasks SET visibility='public' "
            "WHERE visibility='pending_public' AND publish_requested_at <= ?",
            (cutoff,),
        ).rowcount
        if promoted_ent or promoted_tasks:
            logger.info(
                "bridge_push: promoted %d entities, %d tasks to public",
                promoted_ent, promoted_tasks,
            )

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
                "WHERE entity_id = ? ORDER BY id",
                (e["id"],),
            ).fetchall()
            entities_out.append(
                {
                    "name": e["name"],
                    "entityType": e["entity_type"],
                    "project": e["project"],
                    "observations": [
                        {"content": o["content"], "createdAt": o["created_at"]}
                        for o in obs
                    ],
                    "createdAt": e["created_at"],
                    "updatedAt": e["updated_at"],
                }
            )

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
                {
                    "from": r["from_name"],
                    "to": r["to_name"],
                    "relationType": r["relation_type"],
                    "createdAt": r["created_at"],
                }
                for r in rel_rows
            ]

        # Export all non-archived tasks for cross-machine sync
        task_rows = conn.execute(
            "SELECT id, title, description, status, priority, section, due_date, "
            "project, parent_id, notes, recurring, type, assignee, shared_by, "
            "created_at, updated_at "
            "FROM tasks WHERE status != 'archived' ORDER BY created_at"
        ).fetchall()
        tasks_out = [dict(r) for r in task_rows]

        # v0.7.0: Export public entities + tasks as public_knowledge
        pub_ent_rows = conn.execute(
            "SELECT id, name, entity_type, project, created_at, updated_at "
            "FROM entities WHERE visibility='public' ORDER BY name"
        ).fetchall()
        public_entities_out = []
        for pe in pub_ent_rows:
            obs = conn.execute(
                "SELECT content, created_at FROM observations "
                "WHERE entity_id = ? ORDER BY id",
                (pe["id"],),
            ).fetchall()
            public_entities_out.append({
                "name": pe["name"],
                "entityType": pe["entity_type"],
                "project": pe["project"],
                "observations": [
                    {"content": o["content"], "createdAt": o["created_at"]}
                    for o in obs
                ],
                "createdAt": pe["created_at"],
                "updatedAt": pe["updated_at"],
            })
        pub_task_rows = conn.execute(
            "SELECT id, title, description, status, priority, section, "
            "due_date, project, created_at, updated_at "
            "FROM tasks WHERE visibility='public' ORDER BY created_at"
        ).fetchall()
        public_tasks_out = [dict(r) for r in pub_task_rows]

    hostname = socket.gethostname()

    # Build team_manifest from collaborators
    with _get_conn() as conn:
        collab_rows = conn.execute(
            "SELECT github_user FROM collaborators ORDER BY added_at"
        ).fetchall()
        collaborator_list = [r["github_user"] for r in collab_rows]

    owner = os.environ.get("GITHUB_USER", hostname)
    payload = {
        "version": 3,
        "pushed_at": _now(),
        "machine_id": hostname,
        "owner": owner,
        "entities": entities_out,
        "relations": relations_out,
        "tasks": tasks_out,
        "team_manifest": {
            "collaborators": collaborator_list,
            "display_name": owner,
        },
    }

    # v0.7.0: Add public_knowledge to payload
    if public_entities_out or public_tasks_out:
        payload["public_knowledge"] = {
            "entities": public_entities_out,
            "tasks": public_tasks_out,
        }

    # Merge remote tasks + preserve extra keys from remote
    shared_path = Path(BRIDGE_REPO) / "shared.json"
    if shared_path.exists():
        try:
            existing = json.loads(shared_path.read_text(encoding="utf-8"))

            # Merge: keep remote tasks that don't exist locally (by title)
            local_titles = {t["title"] for t in tasks_out}
            remote_tasks = existing.get("tasks", [])
            merged_count = 0
            for rt in remote_tasks:
                if rt.get("title") and rt["title"] not in local_titles:
                    tasks_out.append(rt)
                    local_titles.add(rt["title"])
                    merged_count += 1
            if merged_count:
                payload["tasks"] = tasks_out
                logger.info(
                    "bridge_push: merged %d remote-only tasks into payload",
                    merged_count,
                )

            # Update existing tasks where remote has newer updated_at
            local_by_title = {t["title"]: t for t in tasks_out}
            updated_count = 0
            for rt in remote_tasks:
                title = rt.get("title")
                if not title or title not in local_by_title:
                    continue
                lt = local_by_title[title]
                r_upd = rt.get("updated_at", "")
                l_upd = lt.get("updated_at", "")
                if r_upd > l_upd:
                    _sanitize_task_enums(rt)
                    for field in ("status", "section", "priority", "due_date",
                                  "notes", "description", "type"):
                        if rt.get(field) is not None:
                            lt[field] = rt[field]
                    lt["updated_at"] = r_upd
                    updated_count += 1
            if updated_count:
                logger.info(
                    "bridge_push: updated %d tasks from newer remote data",
                    updated_count,
                )

            # Preserve extra keys (e.g. reading_tasks, shared_knowledge)
            known_keys = {
                "version",
                "pushed_at",
                "machine_id",
                "owner",
                "entities",
                "relations",
                "tasks",
                "shared_tasks",
                "shared_knowledge",
                "public_knowledge",
                "team_manifest",
            }
            for key, val in existing.items():
                if key not in known_keys and isinstance(val, list):
                    payload[key] = val
                    logger.info(
                        "bridge_push: preserving extra key '%s' (%d items)",
                        key,
                        len(val),
                    )
        except (json.JSONDecodeError, OSError):
            pass

    shared_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    # Cross-account push: send assigned tasks to other users' repos
    by_assignee: dict[str, list] = {}
    for t in tasks_out:
        if t.get("assignee"):
            by_assignee.setdefault(t["assignee"], []).append(t)

    for target_user, assigned_tasks in by_assignee.items():
        try:
            _push_to_assignee(target_user, assigned_tasks)
        except Exception as exc:
            logger.warning("bridge_push: failed to push to %s: %s", target_user, exc)

    # Cross-account knowledge push: sharing_rules → collaborator repos
    knowledge_pushed = 0
    with _get_conn() as conn:
        rules = conn.execute(
            "SELECT DISTINCT target_user FROM sharing_rules"
        ).fetchall()
        for rule_row in rules:
            target = rule_row["target_user"]
            # Check trust level
            collab = conn.execute(
                "SELECT trust_level FROM collaborators WHERE github_user = ?",
                (target,),
            ).fetchone()
            if not collab:
                continue
            try:
                pushed_n = _push_knowledge_to(conn, target)
                knowledge_pushed += pushed_n
                # Update last_sync_at
                conn.execute(
                    "UPDATE collaborators SET last_sync_at = ? WHERE github_user = ?",
                    (_now(), target),
                )
            except Exception as exc:
                logger.warning(
                    "bridge_push: knowledge push to %s failed: %s", target, exc
                )

    n_obs = sum(len(e["observations"]) for e in entities_out)
    msg = (
        f"bridge: push {len(entities_out)} entities, "
        f"{len(tasks_out)} tasks from {hostname}"
    )

    _git("add", "shared.json")
    commit_result = _git("commit", "-m", msg)
    if commit_result.returncode != 0 and "nothing to commit" in commit_result.stdout:
        logger.info("bridge_push: no changes to commit")
        return json.dumps({"pushed": 0, "message": "No changes — already up to date"})

    push_result = _git("push")
    pushed = push_result.returncode == 0

    logger.info(
        "bridge_push: %d entities, %d observations, %d relations, %d tasks, push=%s",
        len(entities_out),
        n_obs,
        len(relations_out),
        len(tasks_out),
        pushed,
    )
    result: dict[str, Any] = {
        "entities": len(entities_out),
        "observations": n_obs,
        "relations": len(relations_out),
        "tasks": len(tasks_out),
        "pushed_to_remote": pushed,
        "message": msg,
    }
    if knowledge_pushed:
        result["knowledge_shared"] = knowledge_pushed
    if promoted_ent or promoted_tasks:
        result["promoted_to_public"] = {
            "entities": promoted_ent,
            "tasks": promoted_tasks,
        }

    # v0.7.0: Create GitHub release when public_knowledge is pushed
    has_public = bool(public_entities_out or public_tasks_out)
    if pushed and has_public:
        n_pub_ent = len(public_entities_out)
        n_pub_tasks = len(public_tasks_out)
        tag_name = f"public-v{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
        release_title = f"Public Knowledge: {n_pub_ent} entities, {n_pub_tasks} tasks"
        release_notes = (
            f"## Public Knowledge Release\n\n"
            f"- **{n_pub_ent}** public entities\n"
            f"- **{n_pub_tasks}** public tasks\n\n"
            f"Published from `{hostname}` at {_now()}"
        )
        try:
            rel_result = subprocess.run(
                [
                    "gh", "release", "create", tag_name,
                    "--repo", "RMANOV/sqlite-memory-mcp",
                    "--title", release_title,
                    "--notes", release_notes,
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if rel_result.returncode == 0:
                result["github_release"] = tag_name
                logger.info("bridge_push: created GitHub release %s", tag_name)
            else:
                logger.warning(
                    "bridge_push: GitHub release failed: %s", rel_result.stderr.strip()
                )
        except Exception as exc:
            logger.warning("bridge_push: GitHub release error: %s", exc)

    if has_public:
        result["public_knowledge"] = {
            "entities": len(public_entities_out),
            "tasks": len(public_tasks_out),
        }
    return json.dumps(result)


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
    # Collect tasks from all *_tasks keys (tasks, reading_tasks, etc.)
    tasks = list(payload.get("tasks", []))
    for key, val in payload.items():
        if (
            key.endswith("_tasks")
            and key != "tasks"
            and key != "shared_tasks"
            and isinstance(val, list)
        ):
            tasks.extend(val)
            logger.info("bridge_pull: merged %d tasks from '%s'", len(val), key)
    # Stage shared_tasks for review (never auto-import from other accounts)
    shared_tasks = payload.get("shared_tasks", [])
    staged_count = 0
    now = _now()
    new_entities = 0
    new_observations = 0
    new_relations = 0
    new_tasks = 0
    updated_tasks = 0

    with _get_conn() as conn:
        for ent in entities:
            cur = conn.execute(
                "INSERT OR IGNORE INTO entities "
                "(name, entity_type, project, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    ent["name"],
                    ent["entityType"],
                    ent.get("project"),
                    ent.get("createdAt", now),
                    ent.get("updatedAt", now),
                ),
            )
            new_entities += cur.rowcount

            row = conn.execute(
                "SELECT id FROM entities WHERE name = ?", (ent["name"],)
            ).fetchone()
            if row:
                eid = row["id"]
                for obs in ent.get("observations", []):
                    content = obs["content"] if isinstance(obs, dict) else obs
                    created = (
                        obs.get("createdAt", now) if isinstance(obs, dict) else now
                    )
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
                    (
                        from_row["id"],
                        to_row["id"],
                        rel["relationType"],
                        rel.get("createdAt", now),
                    ),
                )
                new_relations += cur3.rowcount

        # Import tasks (last-write-wins by updated_at)
        # Sort parents before children to avoid FK violations
        tasks_sorted = sorted(
            tasks,
            key=lambda t: (t.get("parent_id") is not None, t.get("created_at", "")),
        )
        for task in tasks_sorted:
            tid = task.get("id")
            if not tid:
                continue
            _sanitize_task_enums(task)
            existing = conn.execute(
                "SELECT updated_at FROM tasks WHERE id = ?", (tid,)
            ).fetchone()
            if existing:
                # Only overwrite if remote is newer
                if task.get("updated_at", "") > existing["updated_at"]:
                    conn.execute(
                        "UPDATE tasks SET title=?, description=?, status=?, priority=?, "
                        "section=?, due_date=?, project=?, parent_id=?, notes=?, "
                        "recurring=?, type=?, assignee=?, shared_by=?, updated_at=? WHERE id=?",
                        (
                            task["title"],
                            task.get("description"),
                            task["status"],
                            task["priority"],
                            task["section"],
                            task.get("due_date"),
                            task.get("project"),
                            task.get("parent_id"),
                            task.get("notes"),
                            task.get("recurring"),
                            task.get("type", "task"),
                            task.get("assignee"),
                            task.get("shared_by"),
                            task["updated_at"],
                            tid,
                        ),
                    )
                    updated_tasks += 1
            else:
                conn.execute(
                    "INSERT INTO tasks (id, title, description, status, priority, "
                    "section, due_date, project, parent_id, notes, recurring, "
                    "type, assignee, shared_by, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        tid,
                        task["title"],
                        task.get("description"),
                        task["status"],
                        task["priority"],
                        task["section"],
                        task.get("due_date"),
                        task.get("project"),
                        task.get("parent_id"),
                        task.get("notes"),
                        task.get("recurring"),
                        task.get("type", "task"),
                        task.get("assignee"),
                        task.get("shared_by"),
                        task.get("created_at", now),
                        task.get("updated_at", now),
                    ),
                )
                new_tasks += 1

        # Stage shared_tasks for manual review (security: never auto-import)
        for st in shared_tasks:
            sid = st.get("id")
            if not sid:
                continue
            _sanitize_task_enums(st)
            conn.execute(
                "INSERT OR REPLACE INTO pending_shared_tasks "
                "(id, title, description, status, priority, section, due_date, "
                "project, parent_id, notes, recurring, type, assignee, shared_by, "
                "created_at, updated_at, received_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    sid,
                    st.get("title", "Untitled"),
                    st.get("description"),
                    st.get("status", "not_started"),
                    st.get("priority", "medium"),
                    st.get("section", "inbox"),
                    st.get("due_date"),
                    st.get("project"),
                    st.get("parent_id"),
                    st.get("notes"),
                    st.get("recurring"),
                    st.get("type", "task"),
                    st.get("assignee"),
                    st.get("shared_by"),
                    st.get("created_at", now),
                    st.get("updated_at", now),
                    now,
                ),
            )
            staged_count += 1

        # Stage shared_knowledge for review (v0.6.0 P2P knowledge collaboration)
        shared_knowledge = payload.get("shared_knowledge", [])
        staged_knowledge = 0
        staged_relations = 0
        for sk in shared_knowledge:
            sname = sk.get("name")
            if not sname:
                continue
            obs_json = json.dumps(sk.get("observations", []), ensure_ascii=False)
            shash = sk.get("sourceHash") or _source_hash(
                sname, sk.get("entityType", ""), sk.get("observations", [])
            )
            sender = sk.get("sharedBy", "unknown")

            # Check trust: only accept from known read_write collaborators
            collab = conn.execute(
                "SELECT trust_level FROM collaborators WHERE github_user = ?",
                (sender,),
            ).fetchone()
            if not collab or collab["trust_level"] != "read_write":
                logger.info(
                    "bridge_pull: skipping knowledge from untrusted sender %s", sender
                )
                continue

            conn.execute(
                "INSERT OR IGNORE INTO pending_shared_entities "
                "(name, entity_type, project, observations, priority, "
                "shared_by, source_hash, received_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    sname,
                    sk.get("entityType", "unknown"),
                    sk.get("project"),
                    obs_json,
                    sk.get("priority", "medium"),
                    sender,
                    shash,
                    now,
                ),
            )
            staged_knowledge += 1

            # Stage relations if included
            for rel in sk.get("relations", []):
                conn.execute(
                    "INSERT OR IGNORE INTO pending_shared_relations "
                    "(from_entity, to_entity, relation_type, shared_by, received_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (sname, rel["to"], rel["relationType"], sender, now),
                )
                staged_relations += 1

        # v0.7.0: Stage incoming public_knowledge from collaborators
        staged_public = 0
        public_knowledge = payload.get("public_knowledge", {})
        pk_entities = (
            public_knowledge.get("entities", [])
            if isinstance(public_knowledge, dict)
            else []
        )
        source_owner = payload.get("owner", "unknown")
        for pk in pk_entities:
            pname = pk.get("name")
            if not pname:
                continue
            obs_json = json.dumps(pk.get("observations", []), ensure_ascii=False)
            phash = _source_hash(
                pname, pk.get("entityType", ""), pk.get("observations", [])
            )
            conn.execute(
                "INSERT OR IGNORE INTO pending_shared_entities "
                "(name, entity_type, project, observations, priority, "
                "shared_by, source_hash, received_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    pname,
                    pk.get("entityType", "unknown"),
                    pk.get("project"),
                    obs_json,
                    "medium",
                    f"public:{source_owner}",
                    phash,
                    now,
                ),
            )
            staged_public += 1
        if staged_public:
            logger.info(
                "bridge_pull: staged %d public knowledge entities for review",
                staged_public,
            )

    if staged_count:
        logger.info("bridge_pull: staged %d shared tasks for review", staged_count)
    if staged_knowledge:
        logger.info(
            "bridge_pull: staged %d shared entities, %d relations for knowledge review",
            staged_knowledge,
            staged_relations,
        )

    logger.info(
        "bridge_pull: %d new entities, %d new observations, %d new relations, "
        "%d new tasks, %d updated tasks, %d staged for review",
        new_entities,
        new_observations,
        new_relations,
        new_tasks,
        updated_tasks,
        staged_count,
    )
    result: dict[str, Any] = {
        "new_entities": new_entities,
        "new_observations": new_observations,
        "new_relations": new_relations,
        "new_tasks": new_tasks,
        "updated_tasks": updated_tasks,
        "source_machine": payload.get("machine_id", "unknown"),
        "pushed_at": payload.get("pushed_at", "unknown"),
    }
    if staged_count:
        result["staged_shared_tasks"] = staged_count
        result["review_required"] = (
            f"{staged_count} shared task(s) pending review. "
            "Use review_shared_tasks() to approve or reject."
        )
    if staged_knowledge:
        result["staged_shared_knowledge"] = staged_knowledge
        result["staged_shared_relations"] = staged_relations
        msg = f"{staged_knowledge} shared entit(ies) pending review"
        if staged_relations:
            msg += f" + {staged_relations} relation(s)"
        msg += ". Use review_shared_knowledge() to approve or reject."
        result["knowledge_review_required"] = msg
    if staged_public:
        result["staged_public_knowledge"] = staged_public
    return json.dumps(result)


@mcp.tool()
def bridge_status() -> str:
    """Show bridge sync status — local shared entities vs repo contents."""
    if not Path(BRIDGE_REPO).is_dir():
        return json.dumps({"error": f"Bridge repo not found at {BRIDGE_REPO}"})

    with _get_conn() as conn:
        local_rows = conn.execute(
            "SELECT name FROM entities WHERE project LIKE 'shared%' ORDER BY name"
        ).fetchall()
        local_task_count = conn.execute(
            "SELECT COUNT(*) as cnt FROM tasks WHERE status != 'archived'"
        ).fetchone()["cnt"]

        # v0.6.0: collaboration stats
        collab_rows = conn.execute(
            "SELECT github_user, display_name, trust_level, last_sync_at "
            "FROM collaborators ORDER BY added_at"
        ).fetchall()
        pending_knowledge = conn.execute(
            "SELECT COUNT(*) as cnt FROM pending_shared_entities"
        ).fetchone()["cnt"]
        pending_rels = conn.execute(
            "SELECT COUNT(*) as cnt FROM pending_shared_relations"
        ).fetchone()["cnt"]
        sharing_rule_count = conn.execute(
            "SELECT COUNT(*) as cnt FROM sharing_rules"
        ).fetchone()["cnt"]

        # v0.7.0: public knowledge counts
        public_ent_count = conn.execute(
            "SELECT COUNT(*) as cnt FROM entities WHERE visibility='public'"
        ).fetchone()["cnt"]
        pending_pub_ent_count = conn.execute(
            "SELECT COUNT(*) as cnt FROM entities WHERE visibility='pending_public'"
        ).fetchone()["cnt"]
        public_task_count = conn.execute(
            "SELECT COUNT(*) as cnt FROM tasks WHERE visibility='public'"
        ).fetchone()["cnt"]
        pending_pub_task_count = conn.execute(
            "SELECT COUNT(*) as cnt FROM tasks WHERE visibility='pending_public'"
        ).fetchone()["cnt"]
    local_names = {r["name"] for r in local_rows}

    shared_path = Path(BRIDGE_REPO) / "shared.json"
    remote_names: set[str] = set()
    remote_task_count = 0
    repo_meta = {}
    if shared_path.exists():
        try:
            payload = json.loads(shared_path.read_text(encoding="utf-8"))
            remote_names = {e["name"] for e in payload.get("entities", [])}
            remote_task_count = len(payload.get("tasks", []))
            repo_meta = {
                "pushed_at": payload.get("pushed_at"),
                "machine_id": payload.get("machine_id"),
                "version": payload.get("version"),
                "owner": payload.get("owner"),
            }
        except (json.JSONDecodeError, OSError):
            pass

    only_local = sorted(local_names - remote_names)
    only_remote = sorted(remote_names - local_names)
    in_sync = sorted(local_names & remote_names)

    # Git log for last push/pull timestamps
    log_result = _git("log", "-1", "--format=%ci %s")
    last_commit = log_result.stdout.strip() if log_result.returncode == 0 else None

    return json.dumps(
        {
            "local_shared_count": len(local_names),
            "remote_count": len(remote_names),
            "in_sync": len(in_sync),
            "only_local": only_local,
            "only_remote": only_remote,
            "local_tasks": local_task_count,
            "remote_tasks": remote_task_count,
            "last_commit": last_commit,
            "repo_meta": repo_meta,
            "collaborators": [dict(r) for r in collab_rows],
            "collaborator_count": len(collab_rows),
            "pending_shared_knowledge": pending_knowledge,
            "pending_shared_relations": pending_rels,
            "sharing_rules": sharing_rule_count,
            "public_entities": public_ent_count,
            "pending_public_entities": pending_pub_ent_count,
            "public_tasks": public_task_count,
            "pending_public_tasks": pending_pub_task_count,
        }
    )


# ═══════════════════════════════════════════════════════════════════════════
# Startup — always init DB on import (ensures tables exist for all callers)
# ═══════════════════════════════════════════════════════════════════════════

_init_db()

if __name__ == "__main__":
    _migrate_jsonl()
    mcp.run(transport="stdio")
