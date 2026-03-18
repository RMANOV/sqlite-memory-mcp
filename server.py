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

import hashlib
import json
import logging
import math
import os
import re  # used by _tokenize() for Jaccard similarity
import socket
import sqlite3
import subprocess
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# Suppress console windows on Windows when spawning git/gh from GUI
import sys as _sys

_NOWIN: dict = (
    {"creationflags": subprocess.CREATE_NO_WINDOW} if _sys.platform == "win32" else {}
)

from db_utils import (
    json_dumps as _json_dumps,
    json_loads as _json_loads,
    get_conn as _get_conn,
    TaskDAO,
    TASK_ACTIVE_EXCLUSIONS as _TASK_ACTIVE_EXCLUSIONS,
    TASK_SECTIONS as _TASK_SECTIONS,
    TASK_PRIORITIES as _TASK_PRIORITIES,
    TASK_STATUSES as _TASK_STATUSES,
    TASK_TYPES as _TASK_TYPES,
    TRUST_LEVELS as _TRUST_LEVELS,
    VISIBILITY_LEVELS as _VISIBILITY_LEVELS,
    PUBLISH_STANDBY_MINUTES as _PUBLISH_STANDBY_MINUTES,
    IQ_WEIGHTS as _IQ_WEIGHTS,
    TIER_WEIGHTS as _TIER_WEIGHTS,
    VERIFICATION_OUTCOMES as _VERIFICATION_OUTCOMES,
    VERIFICATION_WEIGHTS as _VERIFICATION_WEIGHTS,
    RATING_BURST_THRESHOLD as _RATING_BURST_THRESHOLD,
    RATING_BURST_WINDOW_HOURS as _RATING_BURST_WINDOW_HOURS,
    MERGEABLE_FIELDS as _MERGEABLE_FIELDS,
    build_priority_order_sql,
    now_iso as _now,
    sanitize_task_enums as _sanitize_task_enums,
    upsert_field_versions as _upsert_field_versions,
    merge_import_tasks as _merge_import_tasks,
    export_task_files as _export_task_files,
    export_index_json as _export_index_json,
    migrate_to_per_task_files as _migrate_to_per_task_files,
    fts_sync_entity as _fts_sync,
    DB_PATH,
    BRIDGE_REPO,
)

# Pre-built SQL fragment for active-task exclusion filter
_EXCL_PH = ",".join("?" for _ in _TASK_ACTIVE_EXCLUSIONS)

# ── Recurring task validation ─────────────────────────────────────────
_RECURRING_EVERY = ("day", "week", "month", "year")
_RECURRING_WEEKDAYS = frozenset(
    ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
)


def _is_valid_timestamp(s: str) -> bool:
    """Validate ISO 8601 timestamp: parseable and not unreasonably in the future."""
    try:
        dt = datetime.fromisoformat(s)
        # Generous tolerance for clock drift; blocks obvious poisoning ("9999-...")
        return dt <= datetime.now(timezone.utc) + timedelta(hours=24)
    except (ValueError, TypeError):
        return False


def _clamp_score(val: Any, default: float = 0.0) -> float:
    """Clamp a score to [0.0, 1.0] range, returning default on invalid input."""
    try:
        return max(0.0, min(1.0, float(val)))
    except (TypeError, ValueError):
        return default


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
    # Optional interval (default 1)
    interval = config.get("interval")
    if interval is not None:
        try:
            iv = int(interval)
            if iv < 1:
                return f"'interval' must be >= 1. Got: {iv}"
        except (ValueError, TypeError):
            return f"'interval' must be an integer. Got: {interval!r}"
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
    if every == "year":
        month = config.get("month")
        if month is not None:
            try:
                m = int(month)
                if not 1 <= m <= 12:
                    return f"Yearly 'month' must be 1-12. Got: {m}"
            except (ValueError, TypeError):
                return f"Yearly 'month' must be an integer. Got: {month!r}"
        day = config.get("day")
        if day is not None:
            try:
                d = int(day)
                if not 1 <= d <= 31:
                    return f"Yearly 'day' must be 1-31. Got: {d}"
            except (ValueError, TypeError):
                return f"Yearly 'day' must be an integer. Got: {day!r}"
    return None


# ── Logging setup (file-only, NEVER stdout — breaks MCP stdio) ──────────
LOG_PATH = Path.home() / ".claude" / "memory" / "server.log"
LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

logger = logging.getLogger("sqlite-kb")
logger.setLevel(logging.DEBUG)
_fh = logging.FileHandler(LOG_PATH, encoding="utf-8")
_fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
if not logger.handlers:
    logger.addHandler(_fh)

# ── FastMCP app ──────────────────────────────────────────────────────────
from fastmcp import FastMCP

mcp = FastMCP(
    "sqlite-kb",
    instructions=(
        "SQLite-backed persistent memory with WAL concurrent safety, "
        "FTS5 search, session tracking, structured task management, "
        "and cross-machine bridge sync"
    ),
)

# DB_PATH and BRIDGE_REPO imported from db_utils (single source of truth)

# ── Debounced bridge auto-sync ──────────────────────────────────────
_bridge_sync_timer: threading.Timer | None = None
_bridge_sync_lock = threading.Lock()
_BRIDGE_SYNC_DELAY = 60  # seconds, matches task_tray.py


def _schedule_bridge_sync():
    """Schedule a debounced bridge sync. Resets timer on each call."""
    global _bridge_sync_timer
    with _bridge_sync_lock:
        if _bridge_sync_timer is not None:
            _bridge_sync_timer.cancel()
        _bridge_sync_timer = threading.Timer(_BRIDGE_SYNC_DELAY, _run_bridge_sync)
        _bridge_sync_timer.daemon = True  # don't block process exit
        _bridge_sync_timer.start()


def _run_bridge_sync():
    """Execute bridge sync in background thread."""
    global _bridge_sync_timer
    try:
        import bridge_sync_worker

        stats = bridge_sync_worker.main()
        logger.info("auto-sync: %s", stats)
    except Exception as exc:
        logger.warning("auto-sync failed: %s", exc)
    finally:
        with _bridge_sync_lock:
            _bridge_sync_timer = None


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
    reminder_at TEXT DEFAULT NULL,
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

CREATE TABLE IF NOT EXISTS knowledge_ratings (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_name          TEXT NOT NULL,
    rater_id             TEXT NOT NULL,
    content_hash         TEXT NOT NULL,
    specificity          REAL NOT NULL CHECK(specificity BETWEEN 0.0 AND 1.0),
    falsifiability       REAL NOT NULL CHECK(falsifiability BETWEEN 0.0 AND 1.0),
    internal_consistency REAL NOT NULL CHECK(internal_consistency BETWEEN 0.0 AND 1.0),
    novelty              REAL NOT NULL CHECK(novelty BETWEEN 0.0 AND 1.0),
    verification_outcome TEXT DEFAULT NULL CHECK(
        verification_outcome IS NULL
        OR verification_outcome IN ('confirmed', 'contradicted', 'inconclusive')
    ),
    usefulness           REAL DEFAULT NULL CHECK(usefulness IS NULL OR usefulness BETWEEN 0.0 AND 1.0),
    verification_context TEXT DEFAULT NULL,
    rated_at             TEXT NOT NULL,
    UNIQUE(entity_name, rater_id, content_hash)
);

CREATE TABLE IF NOT EXISTS rating_anomalies (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_name   TEXT NOT NULL,
    anomaly_type  TEXT NOT NULL,
    details       TEXT NOT NULL,
    detected_at   TEXT NOT NULL,
    resolved      INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS task_entity_links (
    task_id    TEXT    NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    entity_id  INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    link_type  TEXT    NOT NULL DEFAULT 'manual',
    score      REAL    DEFAULT NULL,
    created_at TEXT    NOT NULL,
    PRIMARY KEY (task_id, entity_id)
);
CREATE INDEX IF NOT EXISTS idx_tel_entity ON task_entity_links(entity_id);

CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
    name, entity_type, observations_text,
    tokenize = "unicode61 remove_diacritics 2"
);

CREATE VIRTUAL TABLE IF NOT EXISTS tasks_fts USING fts5(
    title, description, notes,
    content='tasks', content_rowid='rowid',
    tokenize = "unicode61 remove_diacritics 2"
);

-- Auto-sync triggers: keep tasks_fts in lockstep with tasks table
CREATE TRIGGER IF NOT EXISTS tasks_fts_ai AFTER INSERT ON tasks BEGIN
    INSERT INTO tasks_fts(rowid, title, description, notes)
    VALUES (new.rowid, new.title, new.description, new.notes);
END;

CREATE TRIGGER IF NOT EXISTS tasks_fts_ad AFTER DELETE ON tasks BEGIN
    INSERT INTO tasks_fts(tasks_fts, rowid, title, description, notes)
    VALUES ('delete', old.rowid, old.title, old.description, old.notes);
END;

CREATE TRIGGER IF NOT EXISTS tasks_fts_au AFTER UPDATE ON tasks BEGIN
    INSERT INTO tasks_fts(tasks_fts, rowid, title, description, notes)
    VALUES ('delete', old.rowid, old.title, old.description, old.notes);
    INSERT INTO tasks_fts(rowid, title, description, notes)
    VALUES (new.rowid, new.title, new.description, new.notes);
END;

-- ── Intelligence v2 tables (context state machine + knowledge tiers) ──

CREATE TABLE IF NOT EXISTS context_chunks (
    chunk_id            TEXT PRIMARY KEY,
    session_id          TEXT NULL,
    entity_id           TEXT NULL,
    source_type         TEXT NOT NULL,
    source_ref          TEXT NOT NULL,
    source_hash         TEXT NOT NULL,
    title               TEXT NULL,
    body                TEXT NOT NULL,
    language            TEXT DEFAULT 'bg',
    state               TEXT NOT NULL DEFAULT 'no_enrich',
    enrich_policy       TEXT NOT NULL DEFAULT 'manual',
    materiality_score   REAL DEFAULT 0.0,
    last_human_update_at TEXT NULL,
    last_ai_attempt_at  TEXT NULL,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cc_state ON context_chunks(state);
CREATE INDEX IF NOT EXISTS idx_cc_source_hash ON context_chunks(source_hash);

CREATE TABLE IF NOT EXISTS context_annotations (
    annotation_id       TEXT PRIMARY KEY,
    chunk_id            TEXT NOT NULL REFERENCES context_chunks(chunk_id) ON DELETE CASCADE,
    author_type         TEXT NOT NULL,
    annotation_type     TEXT NOT NULL,
    body                TEXT NOT NULL,
    source_hash_seen    TEXT NULL,
    created_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ca_chunk ON context_annotations(chunk_id, created_at DESC);

CREATE TABLE IF NOT EXISTS context_questions (
    question_id         TEXT PRIMARY KEY,
    chunk_id            TEXT NOT NULL REFERENCES context_chunks(chunk_id) ON DELETE CASCADE,
    question_text       TEXT NOT NULL,
    question_type       TEXT NOT NULL,
    priority_score      REAL NOT NULL,
    state               TEXT NOT NULL DEFAULT 'open',
    answered_by         TEXT NULL,
    answered_at         TEXT NULL,
    answer_text         TEXT NULL,
    created_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cq_open ON context_questions(state, priority_score DESC);

CREATE TABLE IF NOT EXISTS candidate_claims (
    claim_id            TEXT PRIMARY KEY,
    chunk_id            TEXT NOT NULL REFERENCES context_chunks(chunk_id) ON DELETE CASCADE,
    subject             TEXT NOT NULL,
    predicate           TEXT NOT NULL,
    object_text         TEXT NOT NULL,
    object_type         TEXT NOT NULL DEFAULT 'text',
    claim_scope         TEXT NOT NULL,
    confidence          REAL NOT NULL,
    status              TEXT NOT NULL DEFAULT 'candidate',
    requires_human      INTEGER NOT NULL DEFAULT 1,
    promoted_to_fact_id TEXT NULL,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ccm_status ON candidate_claims(status, confidence DESC);
CREATE INDEX IF NOT EXISTS idx_ccm_scope ON candidate_claims(claim_scope);

CREATE TABLE IF NOT EXISTS claim_evidence (
    evidence_id         TEXT PRIMARY KEY,
    claim_id            TEXT NOT NULL REFERENCES candidate_claims(claim_id) ON DELETE CASCADE,
    evidence_type       TEXT NOT NULL,
    evidence_ref        TEXT NOT NULL,
    weight              REAL NOT NULL,
    excerpt             TEXT NULL,
    created_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ce_claim ON claim_evidence(claim_id);

CREATE TABLE IF NOT EXISTS canonical_facts (
    fact_id             TEXT PRIMARY KEY,
    subject             TEXT NOT NULL,
    predicate           TEXT NOT NULL,
    object_text         TEXT NOT NULL,
    object_type         TEXT NOT NULL DEFAULT 'text',
    fact_scope          TEXT NOT NULL,
    provenance_summary  TEXT NOT NULL,
    confidence          REAL NOT NULL,
    validation_mode     TEXT NOT NULL,
    source_claim_id     TEXT NULL REFERENCES candidate_claims(claim_id),
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cf_scope ON canonical_facts(fact_scope);

CREATE TABLE IF NOT EXISTS context_packs (
    pack_id             TEXT PRIMARY KEY,
    session_id          TEXT NULL,
    entity_id           TEXT NULL,
    pack_type           TEXT NOT NULL,
    target_ref          TEXT NULL,
    input_signature     TEXT NOT NULL,
    token_budget        INTEGER NOT NULL,
    body                TEXT NOT NULL,
    freshness_score     REAL NOT NULL,
    created_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cp_target ON context_packs(pack_type, target_ref);

CREATE TABLE IF NOT EXISTS impact_edges (
    edge_id             TEXT PRIMARY KEY,
    source_kind         TEXT NOT NULL,
    source_ref          TEXT NOT NULL,
    target_kind         TEXT NOT NULL,
    target_ref          TEXT NOT NULL,
    impact_type         TEXT NOT NULL,
    impact_score        REAL NOT NULL,
    rationale           TEXT NULL,
    created_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ie_source ON impact_edges(source_kind, source_ref);
CREATE INDEX IF NOT EXISTS idx_ie_target ON impact_edges(target_kind, target_ref);

CREATE TABLE IF NOT EXISTS enrichment_runs (
    run_id              TEXT PRIMARY KEY,
    tool_name           TEXT NOT NULL,
    chunk_id            TEXT NULL,
    session_id          TEXT NULL,
    result_status       TEXT NOT NULL,
    reason_code         TEXT NULL,
    input_signature     TEXT NOT NULL,
    started_at          TEXT NOT NULL,
    finished_at         TEXT NOT NULL
);
"""


# _get_conn imported from db_utils (atomic BEGIN/COMMIT transactions)


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
    # v0.9.0: knowledge_ratings table
    (
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='knowledge_ratings'",
        "CREATE TABLE knowledge_ratings ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "entity_name TEXT NOT NULL, rater_id TEXT NOT NULL, content_hash TEXT NOT NULL, "
        "specificity REAL NOT NULL CHECK(specificity BETWEEN 0.0 AND 1.0), "
        "falsifiability REAL NOT NULL CHECK(falsifiability BETWEEN 0.0 AND 1.0), "
        "internal_consistency REAL NOT NULL CHECK(internal_consistency BETWEEN 0.0 AND 1.0), "
        "novelty REAL NOT NULL CHECK(novelty BETWEEN 0.0 AND 1.0), "
        "verification_outcome TEXT DEFAULT NULL CHECK("
        "verification_outcome IS NULL OR verification_outcome IN "
        "('confirmed','contradicted','inconclusive')), "
        "usefulness REAL DEFAULT NULL CHECK(usefulness IS NULL OR usefulness BETWEEN 0.0 AND 1.0), "
        "verification_context TEXT DEFAULT NULL, rated_at TEXT NOT NULL, "
        "UNIQUE(entity_name, rater_id, content_hash))",
        "knowledge_ratings table (v0.9.0)",
    ),
    # v0.9.0: rating_anomalies table
    (
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='rating_anomalies'",
        "CREATE TABLE rating_anomalies ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, entity_name TEXT NOT NULL, "
        "anomaly_type TEXT NOT NULL, details TEXT NOT NULL, "
        "detected_at TEXT NOT NULL, resolved INTEGER DEFAULT 0)",
        "rating_anomalies table (v0.9.0)",
    ),
    # v0.9.0: knowledge_ratings indexes
    (
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_kr_entity'",
        "CREATE INDEX idx_kr_entity ON knowledge_ratings(entity_name)",
        "idx_kr_entity index (v0.9.0)",
    ),
    (
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_kr_hash'",
        "CREATE INDEX idx_kr_hash ON knowledge_ratings(content_hash)",
        "idx_kr_hash index (v0.9.0)",
    ),
    (
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_kr_rater'",
        "CREATE INDEX idx_kr_rater ON knowledge_ratings(rater_id)",
        "idx_kr_rater index (v0.9.0)",
    ),
    (
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_anomaly_ent'",
        "CREATE INDEX idx_anomaly_ent ON rating_anomalies(entity_name)",
        "idx_anomaly_ent index (v0.9.0)",
    ),
    # v1.0.0: bridge sync metadata for incremental push
    (
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='bridge_meta'",
        "CREATE TABLE bridge_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
        "bridge_meta table (v1.0.0)",
    ),
    # v2.0.0: per-field LWW — task_field_versions table
    (
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='task_field_versions'",
        "CREATE TABLE task_field_versions ("
        "task_id TEXT NOT NULL, field_name TEXT NOT NULL, "
        "updated_at TEXT NOT NULL, updated_by TEXT NOT NULL DEFAULT '', "
        "PRIMARY KEY (task_id, field_name), "
        "FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE)",
        "task_field_versions table (v2.0.0 — per-field LWW)",
    ),
    # v2.0.0: seed field versions from existing tasks
    (
        "SELECT 1 FROM task_field_versions LIMIT 1",
        "INSERT OR IGNORE INTO task_field_versions (task_id, field_name, updated_at, updated_by) "
        "SELECT id, 'title', updated_at, '' FROM tasks "
        "UNION ALL SELECT id, 'status', updated_at, '' FROM tasks "
        "UNION ALL SELECT id, 'priority', updated_at, '' FROM tasks "
        "UNION ALL SELECT id, 'section', updated_at, '' FROM tasks "
        "UNION ALL SELECT id, 'due_date', updated_at, '' FROM tasks "
        "UNION ALL SELECT id, 'project', updated_at, '' FROM tasks "
        "UNION ALL SELECT id, 'parent_id', updated_at, '' FROM tasks "
        "UNION ALL SELECT id, 'recurring', updated_at, '' FROM tasks "
        "UNION ALL SELECT id, 'type', updated_at, '' FROM tasks "
        "UNION ALL SELECT id, 'assignee', updated_at, '' FROM tasks "
        "UNION ALL SELECT id, 'shared_by', updated_at, '' FROM tasks "
        "UNION ALL SELECT id, 'description', updated_at, '' FROM tasks "
        "UNION ALL SELECT id, 'notes', updated_at, '' FROM tasks",
        "seed task_field_versions from existing tasks (v2.0.0)",
    ),
    # v2.1.0: reminder_at column
    (
        "SELECT 1 FROM pragma_table_info('tasks') WHERE name='reminder_at'",
        "ALTER TABLE tasks ADD COLUMN reminder_at TEXT DEFAULT NULL",
        "tasks.reminder_at column (v2.1.0 — reminders)",
    ),
    # v2.1.0: partial index for reminder queries
    (
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_tasks_reminder_at'",
        "CREATE INDEX idx_tasks_reminder_at ON tasks(reminder_at) WHERE reminder_at IS NOT NULL",
        "idx_tasks_reminder_at partial index (v2.1.0)",
    ),
    # v2.1.0: seed reminder_at field version for existing tasks
    (
        "SELECT 1 FROM task_field_versions WHERE field_name = 'reminder_at' LIMIT 1",
        "INSERT OR IGNORE INTO task_field_versions (task_id, field_name, updated_at, updated_by) "
        "SELECT id, 'reminder_at', updated_at, '' FROM tasks",
        "seed task_field_versions.reminder_at for existing tasks (v2.1.0)",
    ),
    # v2.2.0: task_entity_links table
    (
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='task_entity_links'",
        "CREATE TABLE task_entity_links ("
        "task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE, "
        "entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE, "
        "link_type TEXT NOT NULL DEFAULT 'manual', "
        "score REAL DEFAULT NULL, "
        "created_at TEXT NOT NULL, "
        "PRIMARY KEY (task_id, entity_id))",
        "task_entity_links table (v2.2.0 — entity↔task links)",
    ),
    (
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_tel_entity'",
        "CREATE INDEX idx_tel_entity ON task_entity_links(entity_id)",
        "idx_tel_entity index (v2.2.0)",
    ),
    # ── v3.0.0: Intelligence v2 — context state machine + knowledge tiers ──
    (
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='context_chunks'",
        "CREATE TABLE context_chunks ("
        "chunk_id TEXT PRIMARY KEY, session_id TEXT NULL, entity_id TEXT NULL, "
        "source_type TEXT NOT NULL, source_ref TEXT NOT NULL, source_hash TEXT NOT NULL, "
        "title TEXT NULL, body TEXT NOT NULL, language TEXT DEFAULT 'bg', "
        "state TEXT NOT NULL DEFAULT 'no_enrich', enrich_policy TEXT NOT NULL DEFAULT 'manual', "
        "materiality_score REAL DEFAULT 0.0, last_human_update_at TEXT NULL, "
        "last_ai_attempt_at TEXT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)",
        "context_chunks table (v3.0.0 — intelligence v2)",
    ),
    (
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_cc_state'",
        "CREATE INDEX idx_cc_state ON context_chunks(state)",
        "idx_cc_state index (v3.0.0)",
    ),
    (
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_cc_source_hash'",
        "CREATE INDEX idx_cc_source_hash ON context_chunks(source_hash)",
        "idx_cc_source_hash index (v3.0.0)",
    ),
    (
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='context_annotations'",
        "CREATE TABLE context_annotations ("
        "annotation_id TEXT PRIMARY KEY, "
        "chunk_id TEXT NOT NULL REFERENCES context_chunks(chunk_id) ON DELETE CASCADE, "
        "author_type TEXT NOT NULL, annotation_type TEXT NOT NULL, "
        "body TEXT NOT NULL, source_hash_seen TEXT NULL, created_at TEXT NOT NULL)",
        "context_annotations table (v3.0.0)",
    ),
    (
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_ca_chunk'",
        "CREATE INDEX idx_ca_chunk ON context_annotations(chunk_id, created_at DESC)",
        "idx_ca_chunk index (v3.0.0)",
    ),
    (
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='context_questions'",
        "CREATE TABLE context_questions ("
        "question_id TEXT PRIMARY KEY, "
        "chunk_id TEXT NOT NULL REFERENCES context_chunks(chunk_id) ON DELETE CASCADE, "
        "question_text TEXT NOT NULL, question_type TEXT NOT NULL, "
        "priority_score REAL NOT NULL, state TEXT NOT NULL DEFAULT 'open', "
        "answered_by TEXT NULL, answered_at TEXT NULL, answer_text TEXT NULL, "
        "created_at TEXT NOT NULL)",
        "context_questions table (v3.0.0)",
    ),
    (
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_cq_open'",
        "CREATE INDEX idx_cq_open ON context_questions(state, priority_score DESC)",
        "idx_cq_open index (v3.0.0)",
    ),
    (
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='candidate_claims'",
        "CREATE TABLE candidate_claims ("
        "claim_id TEXT PRIMARY KEY, "
        "chunk_id TEXT NOT NULL REFERENCES context_chunks(chunk_id) ON DELETE CASCADE, "
        "subject TEXT NOT NULL, predicate TEXT NOT NULL, "
        "object_text TEXT NOT NULL, object_type TEXT NOT NULL DEFAULT 'text', "
        "claim_scope TEXT NOT NULL, confidence REAL NOT NULL, "
        "status TEXT NOT NULL DEFAULT 'candidate', requires_human INTEGER NOT NULL DEFAULT 1, "
        "promoted_to_fact_id TEXT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)",
        "candidate_claims table (v3.0.0)",
    ),
    (
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_ccm_status'",
        "CREATE INDEX idx_ccm_status ON candidate_claims(status, confidence DESC)",
        "idx_ccm_status index (v3.0.0)",
    ),
    (
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_ccm_scope'",
        "CREATE INDEX idx_ccm_scope ON candidate_claims(claim_scope)",
        "idx_ccm_scope index (v3.0.0)",
    ),
    (
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='claim_evidence'",
        "CREATE TABLE claim_evidence ("
        "evidence_id TEXT PRIMARY KEY, "
        "claim_id TEXT NOT NULL REFERENCES candidate_claims(claim_id) ON DELETE CASCADE, "
        "evidence_type TEXT NOT NULL, evidence_ref TEXT NOT NULL, "
        "weight REAL NOT NULL, excerpt TEXT NULL, created_at TEXT NOT NULL)",
        "claim_evidence table (v3.0.0)",
    ),
    (
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_ce_claim'",
        "CREATE INDEX idx_ce_claim ON claim_evidence(claim_id)",
        "idx_ce_claim index (v3.0.0)",
    ),
    (
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='canonical_facts'",
        "CREATE TABLE canonical_facts ("
        "fact_id TEXT PRIMARY KEY, subject TEXT NOT NULL, predicate TEXT NOT NULL, "
        "object_text TEXT NOT NULL, object_type TEXT NOT NULL DEFAULT 'text', "
        "fact_scope TEXT NOT NULL, provenance_summary TEXT NOT NULL, "
        "confidence REAL NOT NULL, validation_mode TEXT NOT NULL, "
        "source_claim_id TEXT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)",
        "canonical_facts table (v3.0.0)",
    ),
    (
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_cf_scope'",
        "CREATE INDEX idx_cf_scope ON canonical_facts(fact_scope)",
        "idx_cf_scope index (v3.0.0)",
    ),
    (
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='context_packs'",
        "CREATE TABLE context_packs ("
        "pack_id TEXT PRIMARY KEY, session_id TEXT NULL, entity_id TEXT NULL, "
        "pack_type TEXT NOT NULL, target_ref TEXT NULL, input_signature TEXT NOT NULL, "
        "token_budget INTEGER NOT NULL, body TEXT NOT NULL, "
        "freshness_score REAL NOT NULL, created_at TEXT NOT NULL)",
        "context_packs table (v3.0.0)",
    ),
    (
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_cp_target'",
        "CREATE INDEX idx_cp_target ON context_packs(pack_type, target_ref)",
        "idx_cp_target index (v3.0.0)",
    ),
    (
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='impact_edges'",
        "CREATE TABLE impact_edges ("
        "edge_id TEXT PRIMARY KEY, source_kind TEXT NOT NULL, source_ref TEXT NOT NULL, "
        "target_kind TEXT NOT NULL, target_ref TEXT NOT NULL, "
        "impact_type TEXT NOT NULL, impact_score REAL NOT NULL, "
        "rationale TEXT NULL, created_at TEXT NOT NULL)",
        "impact_edges table (v3.0.0)",
    ),
    (
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_ie_source'",
        "CREATE INDEX idx_ie_source ON impact_edges(source_kind, source_ref)",
        "idx_ie_source index (v3.0.0)",
    ),
    (
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_ie_target'",
        "CREATE INDEX idx_ie_target ON impact_edges(target_kind, target_ref)",
        "idx_ie_target index (v3.0.0)",
    ),
    (
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='enrichment_runs'",
        "CREATE TABLE enrichment_runs ("
        "run_id TEXT PRIMARY KEY, tool_name TEXT NOT NULL, "
        "chunk_id TEXT NULL, session_id TEXT NULL, "
        "result_status TEXT NOT NULL, reason_code TEXT NULL, "
        "input_signature TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT NOT NULL)",
        "enrichment_runs table (v3.0.0)",
    ),
    # ── v3.1.0: Layer 1+2 — smart retrieval + lazy enrichment ──
    (
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='entity_access_log'",
        "CREATE TABLE entity_access_log ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE, "
        "tool_name TEXT NOT NULL, "
        "accessed_at TEXT NOT NULL)",
        "entity_access_log table (v3.1.0 — smart retrieval)",
    ),
    (
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_eal_entity'",
        "CREATE INDEX idx_eal_entity ON entity_access_log(entity_id, accessed_at DESC)",
        "idx_eal_entity index (v3.1.0)",
    ),
    (
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='lazy_claims'",
        "CREATE TABLE lazy_claims ("
        "claim_id TEXT PRIMARY KEY, "
        "entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE, "
        "observation_id INTEGER NOT NULL REFERENCES observations(id) ON DELETE CASCADE, "
        "subject TEXT NOT NULL, predicate TEXT NOT NULL, "
        "object_text TEXT NOT NULL, confidence REAL NOT NULL, "
        "status TEXT NOT NULL DEFAULT 'candidate', "
        "promoted_to_fact_id TEXT NULL, "
        "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)",
        "lazy_claims table (v3.1.0 — lazy enrichment)",
    ),
    (
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_lc_entity'",
        "CREATE INDEX idx_lc_entity ON lazy_claims(entity_id, status)",
        "idx_lc_entity index (v3.1.0)",
    ),
    (
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_lc_obs'",
        "CREATE INDEX idx_lc_obs ON lazy_claims(observation_id)",
        "idx_lc_obs index (v3.1.0)",
    ),
    (
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_lc_status'",
        "CREATE INDEX idx_lc_status ON lazy_claims(status, confidence DESC)",
        "idx_lc_status index (v3.1.0)",
    ),
]


def _init_db() -> None:
    """Create tables if they don't exist, run migrations, set WAL mode."""
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    # executescript() auto-commits, incompatible with get_conn's BEGIN — use raw conn
    raw = sqlite3.connect(DB_PATH, isolation_level=None)
    raw.executescript(_SCHEMA_SQL)
    raw.close()
    # Migrations in separate transaction for proper rollback
    with _get_conn() as conn:
        for check_q, migrate_q, desc in _MIGRATIONS:
            if not conn.execute(check_q).fetchone():
                conn.execute(migrate_q)
                logger.info("Migration applied: %s", desc)

    # One-time FTS rebuild for tasks if tasks_fts index is stale or empty
    with _get_conn() as conn:
        task_count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        fts_count = conn.execute("SELECT COUNT(*) FROM tasks_fts").fetchone()[0]
        if task_count > 0 and fts_count == 0:
            conn.execute("INSERT INTO tasks_fts(tasks_fts) VALUES('rebuild')")
            logger.info(
                "tasks_fts: rebuilt FTS index for %d existing tasks", task_count
            )

    logger.info("Database initialized at %s", DB_PATH)


# ── FTS sync helper ──────────────────────────────────────────────────────
# _fts_sync is imported from db_utils.fts_sync_entity (single source of truth)


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
                new_obs_ids: list[tuple[int, str]] = []
                for obs in observations:
                    cur_obs = conn.execute(
                        "INSERT OR IGNORE INTO observations "
                        "(entity_id, content, created_at) VALUES (?, ?, ?)",
                        (eid, obs, now),
                    )
                    if cur_obs.rowcount > 0:
                        new_obs_ids.append((cur_obs.lastrowid, obs))
                _fts_sync(conn, eid)
                # L2 inline enrichment for newly inserted observations
                if new_obs_ids:
                    try:
                        from lazy_enrichment import extract_inline_claims

                        for obs_id, obs_text in new_obs_ids:
                            extract_inline_claims(conn, eid, obs_id, obs_text)
                    except Exception:
                        pass  # L2 enrichment is optional

    logger.info(
        "create_entities: %d created out of %d requested", created, len(entities)
    )
    _schedule_bridge_sync()
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
            contents = item.get("contents", [])
            for content in contents:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO observations "
                    "(entity_id, content, created_at) VALUES (?, ?, ?)",
                    (eid, content, now),
                )
                added += cur.rowcount
            conn.execute("UPDATE entities SET updated_at = ? WHERE id = ?", (now, eid))
            _fts_sync(conn, eid)
            # L2 inline enrichment: extract SPO claims from new observations
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
                except Exception:
                    pass  # L2 enrichment is optional — don't break core

    logger.info("add_observations: %d observations added", added)
    _schedule_bridge_sync()
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


_STOPWORDS = frozenset(
    "the a an is are was were be been being have has had do does did "
    "will would shall should may might can could and or but if then "
    "else for of in on at to from by with".split()
)


def _tokenize(text: str) -> set[str]:
    """Extract meaningful tokens from text for Jaccard similarity."""
    if not text:
        return set()
    words = re.findall(r"\w+", text.lower())
    return {w for w in words if len(w) >= 3 and w not in _STOPWORDS}


@mcp.tool()
def search_nodes(query: str, project: str | None = None) -> str:
    """Search the knowledge graph using FTS5 BM25-ranked full-text search.

    Returns matching entities with their observations, ranked by relevance.
    Applies multi-signal re-ranking (recency, project affinity, graph proximity,
    observation richness, canonical facts, session context).
    """
    fts_q = _fts_query(query)
    with _get_conn() as conn:
        # Empty query → return all entities (no FTS filter)
        if not query.strip():
            rows = conn.execute(
                "SELECT e.id AS eid, e.name, e.entity_type, e.project, 0 AS rank "
                "FROM entities e ORDER BY e.name LIMIT 50"
            ).fetchall()
        else:
            # Fetch larger pool for re-ranking
            try:
                from smart_retrieval import RERANKING_POOL_SIZE

                pool_size = RERANKING_POOL_SIZE
            except Exception:
                pool_size = 50

            rows = conn.execute(
                "SELECT memory_fts.rowid AS eid, memory_fts.name, "
                "memory_fts.entity_type, e.project, memory_fts.rank "
                "FROM memory_fts "
                "JOIN entities e ON e.id = memory_fts.rowid "
                "WHERE memory_fts MATCH ? ORDER BY memory_fts.rank LIMIT ?",
                (fts_q, pool_size),
            ).fetchall()

        if not rows:
            return json.dumps({"entities": [], "query": query})

        # Try smart re-ranking (L1), fallback to BM25 order
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
            pass  # L1 re-ranking is optional — fallback to BM25

        if reranked:
            eids = [r["eid"] for r in reranked]
        else:
            eids = [r["eid"] for r in rows[:50]]

        # Batch-fetch observations for all matched entities in one query
        ph = ",".join("?" * len(eids))
        obs_rows = conn.execute(
            f"SELECT entity_id, content FROM observations "
            f"WHERE entity_id IN ({ph}) ORDER BY entity_id, id",
            eids,
        ).fetchall()

        # Group observations by entity_id
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

        # Log entity access for staleness tracking
        now = _now()
        try:
            for eid in eids:
                conn.execute(
                    "INSERT INTO entity_access_log (entity_id, tool_name, accessed_at) "
                    "VALUES (?, 'search_nodes', ?)",
                    (eid, now),
                )
        except Exception:
            pass  # access logging is optional

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

        # Log entity access for staleness tracking
        if found_ids:
            now = _now()
            try:
                for eid in found_ids:
                    conn.execute(
                        "INSERT INTO entity_access_log (entity_id, tool_name, accessed_at) "
                        "VALUES (?, 'open_nodes', ?)",
                        (eid, now),
                    )
            except Exception:
                pass  # access logging is optional

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
# Tool: knowledge_health (L2b health sweep)
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
            return json.dumps({"error": "lazy_enrichment module not available"})

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
# Tools 13-18: Task Management
# ═══════════════════════════════════════════════════════════════════════════


# _TASK_STATUSES, _TASK_PRIORITIES, _TASK_SECTIONS imported from db_utils


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
    reminder_at: str | None = None,
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
        reminder_at: ISO datetime for one-time reminder (e.g. '2026-03-15T14:00:00').
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
    if reminder_at:
        try:
            datetime.fromisoformat(reminder_at)
        except ValueError:
            return json.dumps(
                {
                    "error": f"Invalid reminder_at: {reminder_at}. Use ISO datetime format (e.g. 2026-03-15T14:00:00)"
                }
            )

    with _get_conn() as conn:
        if parent_id:
            if not TaskDAO.exists(conn, parent_id):
                return json.dumps({"error": f"Parent task {parent_id} not found"})

        TaskDAO.create(
            conn,
            task_id,
            title,
            now,
            description=description,
            priority=priority,
            section=section,
            due_date=due_date,
            project=project,
            parent_id=parent_id,
            notes=notes,
            recurring=recurring,
            reminder_at=reminder_at,
            type=type,
        )
        # v2.0.0: Seed field versions for LWW sync
        _upsert_field_versions(conn, task_id, _MERGEABLE_FIELDS, now)

    logger.info("create_task: %s (%s)", title, task_id)
    _schedule_bridge_sync()
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
    reminder_at: str | None = None,
    type: str | None = None,
) -> str:
    """Update a task's fields. Only provided fields are changed.

    Pass empty string ("") to clear a field to NULL.

    Args:
        task_id: UUID of the task to update (required).
        title: New title.
        description: Unlimited-length task description/details.
        status: not_started | in_progress | done | archived | cancelled.
        priority: low | medium | high | critical.
        section: inbox | today | next | someday | waiting.
        due_date: YYYY-MM-DD or "" to clear.
        project: Project tag or "" to clear.
        parent_id: Parent task UUID or "" to clear.
        notes: Freeform notes or "" to clear.
        recurring: JSON recurrence config or "" to clear.
        reminder_at: ISO datetime for one-time reminder or "" to clear.
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
        "reminder_at": reminder_at,
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
    if "reminder_at" in updates and updates["reminder_at"] is not None:
        try:
            datetime.fromisoformat(updates["reminder_at"])
        except ValueError:
            return json.dumps(
                {
                    "error": f"Invalid reminder_at: {updates['reminder_at']}. Use ISO datetime format"
                }
            )

    updates["updated_at"] = _now()

    with _get_conn() as conn:
        if TaskDAO.update(conn, task_id, updates) == 0:
            return json.dumps({"error": f"Task {task_id} not found"})
        # v2.0.0: Track field versions for LWW sync
        changed = [k for k in updates if k != "updated_at"]
        _upsert_field_versions(conn, task_id, changed, updates["updated_at"])

    logger.info("update_task: %s updated %s", task_id, list(updates.keys()))
    _schedule_bridge_sync()
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
    search: str | None = None,
    summary_only: bool = False,
    offset: int = 0,
    limit: int = 50,
) -> str:
    """Query tasks with optional filters. Returns markdown table.

    Filters are combined with AND. Omit a filter to skip it.
    overdue_only=True shows only tasks past due_date that are not done/archived.
    search: FTS5 full-text search across title, description, and notes.
    summary_only=True omits description/notes from results (faster for large datasets).
    offset/limit for pagination.
    """
    conditions: list[str] = []
    params: list[Any] = []
    use_fts = bool(search and search.strip())

    if section:
        conditions.append("t.section = ?")
        params.append(section)
    if status:
        conditions.append("t.status = ?")
        params.append(status)
    if priority:
        conditions.append("t.priority = ?")
        params.append(priority)
    if project:
        conditions.append("t.project = ?")
        params.append(project)
    if parent_id:
        conditions.append("t.parent_id = ?")
        params.append(parent_id)
    if type:
        conditions.append("t.type = ?")
        params.append(type)
    if overdue_only:
        conditions.append("t.due_date < date('now')")
        conditions.append(f"t.status NOT IN ({_EXCL_PH})")
        params.extend(_TASK_ACTIVE_EXCLUSIONS)

    # Column selection based on summary_only
    if summary_only:
        cols = "t.id, t.title, t.status, t.priority, t.section, t.due_date, t.project, t.parent_id"
    else:
        cols = "t.id, t.title, t.description, t.status, t.priority, t.section, t.due_date, t.project, t.parent_id"

    if use_fts:
        fts_q = _fts_query(search)
        conditions.append("tasks_fts MATCH ?")
        params.append(fts_q)
        where = " AND ".join(conditions) if conditions else "1=1"
        # FTS snippet for search context (truncated to 64 tokens)
        if not summary_only:
            cols += ", snippet(tasks_fts, 1, '<b>', '</b>', '...', 64) AS match_snippet"
        sql = (
            f"SELECT {cols} FROM tasks t JOIN tasks_fts ON tasks_fts.rowid = t.rowid "
            f"WHERE {where} "
            f"ORDER BY tasks_fts.rank, "
            f"  {build_priority_order_sql('t.')}, "
            f"  t.due_date ASC NULLS LAST, t.created_at ASC "
            f"LIMIT ? OFFSET ?"
        )
    else:
        where = " AND ".join(conditions) if conditions else "1=1"
        sql = (
            f"SELECT {cols} FROM tasks t WHERE {where} "
            f"ORDER BY "
            f"  {build_priority_order_sql('t.')}, "
            f"  t.due_date ASC NULLS LAST, t.created_at ASC "
            f"LIMIT ? OFFSET ?"
        )

    params.extend([limit, offset])

    with _get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
        # Get total count for pagination info
        count_params = params[:-2]  # exclude limit/offset
        if use_fts:
            count_sql = f"SELECT COUNT(*) FROM tasks t, tasks_fts WHERE {where}"
        else:
            count_sql = f"SELECT COUNT(*) FROM tasks t WHERE {where}"
        total = conn.execute(count_sql, count_params).fetchone()[0]

    if not rows:
        return json.dumps(
            {
                "tasks": [],
                "count": 0,
                "total": total,
                "message": "No tasks match filters",
            }
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
            f"| {i + offset} | {r['title']} | {r['status']} | {r['priority']} "
            f"| {r['section']} | {due} | {proj} |"
        )

    tasks_json = [dict(r) for r in rows]
    result = {
        "tasks": tasks_json,
        "count": len(rows),
        "total": total,
        "offset": offset,
        "limit": limit,
        "markdown": "\n".join(lines),
    }
    if total > offset + limit:
        result["has_more"] = True
        result["next_offset"] = offset + limit
    return json.dumps(result)


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
        now = _now()
        affected_ids = TaskDAO.archive_done(conn, days)
        archived = len(affected_ids)
        for tid in affected_ids:
            _upsert_field_versions(conn, tid, ("status",), now)

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
        affected = conn.execute(
            f"SELECT id FROM tasks "
            f"WHERE due_date < date('now') "
            f"AND status NOT IN ({_EXCL_PH}) "
            f"AND priority IN ({ph})",
            list(_TASK_ACTIVE_EXCLUSIONS) + lower_priorities,
        ).fetchall()
        affected_ids = [r["id"] for r in affected]
        cur = conn.execute(
            f"UPDATE tasks SET priority = ?, updated_at = ? "
            f"WHERE due_date < date('now') "
            f"AND status NOT IN ({_EXCL_PH}) "
            f"AND priority IN ({ph})",
            [target_priority, now] + list(_TASK_ACTIVE_EXCLUSIONS) + lower_priorities,
        )
        bumped = cur.rowcount
        for tid in affected_ids:
            _upsert_field_versions(conn, tid, ("priority",), now)

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
        return json.dumps(
            {"message": "No recurring tasks to process today.", "created": 0}
        )

    titles = [t["title"] for t in created]
    prefix = "[dry-run] Would create" if dry_run else "Created"
    logger.info("process_recurring_tasks: %s %d task(s)", prefix.lower(), len(created))
    return json.dumps(
        {
            "message": f"{prefix} {len(created)} recurring task(s)",
            "created": len(created),
            "tasks": titles,
        }
    )


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
                    **_NOWIN,
                )
                shared_by = result.stdout.strip() or None
            except (subprocess.TimeoutExpired, OSError):
                pass

        conn.execute(
            "UPDATE tasks SET assignee = ?, shared_by = ?, updated_at = ? WHERE id = ?",
            (assignee, shared_by, now, task_id),
        )
        _upsert_field_versions(conn, task_id, ("assignee", "shared_by"), now)

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
                    remote_ts = t.get("updated_at", "")
                    if (
                        _is_valid_timestamp(remote_ts)
                        and remote_ts > existing["updated_at"]
                    ):
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
                        _upsert_field_versions(
                            conn, tid, _MERGEABLE_FIELDS, t.get("updated_at", _now())
                        )
                        imported += 1
                else:
                    TaskDAO.create(
                        conn,
                        tid,
                        t["title"],
                        t["updated_at"],
                        description=t.get("description"),
                        status=t["status"],
                        priority=t["priority"],
                        section=t["section"],
                        due_date=t.get("due_date"),
                        project=t.get("project"),
                        parent_id=t.get("parent_id"),
                        notes=t.get("notes"),
                        recurring=t.get("recurring"),
                        type=t.get("type", "task"),
                        assignee=t.get("assignee"),
                        shared_by=t.get("shared_by"),
                        created_at=t.get("created_at"),
                    )
                    _upsert_field_versions(
                        conn, tid, _MERGEABLE_FIELDS, t.get("updated_at", _now())
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


def _content_hash(entity_name: str, observations: list[str]) -> str:
    """Deterministic SHA256 bound to exact content version (order-independent)."""
    raw = json.dumps({"name": entity_name, "obs": sorted(observations)}, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()


def _entity_content_hash(conn, entity_name: str) -> tuple[str, list[str]] | None:
    """Fetch observations + compute content hash. Returns (hash, obs_list) or None."""
    obs_rows = conn.execute(
        "SELECT o.content FROM observations o "
        "JOIN entities e ON o.entity_id = e.id "
        "WHERE e.name = ? ORDER BY o.id",
        (entity_name,),
    ).fetchall()
    if not obs_rows:
        return None
    obs = [r["content"] for r in obs_rows]
    return _content_hash(entity_name, obs), obs


def _get_publisher_id(conn, entity_name: str) -> str:
    """Extract publisher identity for an entity."""
    row = conn.execute(
        "SELECT origin, shared_by FROM entities WHERE name = ?", (entity_name,)
    ).fetchone()
    if not row:
        return ""
    origin = row["origin"] or "local"
    if origin.startswith("shared:"):
        return origin.split(":", 1)[1]
    if row["shared_by"]:
        return row["shared_by"]
    return os.environ.get("GITHUB_USER", socket.gethostname())


def _compute_truth_score(entity_name: str, conn) -> dict[str, Any]:
    """Compute composite TruthScore for a public entity.

    Three tiers: IQ (content quality), Verification, Cross-validation.
    Returns dict with truth_score, confidence, rating_count, content_hash, dimensions.
    """
    # Get current content hash
    result = _entity_content_hash(conn, entity_name)
    observations = result[1] if result else []
    c_hash = result[0] if result else _content_hash(entity_name, [])

    # Get ratings for current content version
    ratings = conn.execute(
        "SELECT specificity, falsifiability, internal_consistency, novelty, "
        "verification_outcome, usefulness FROM knowledge_ratings "
        "WHERE entity_name = ? AND content_hash = ?",
        (entity_name, c_hash),
    ).fetchall()

    if not ratings:
        return {
            "truth_score": 0.0,
            "confidence": 0.0,
            "rating_count": 0,
            "content_hash": c_hash,
            "dimensions": {},
        }

    rater_count = len(ratings)

    # Tier 1: IQ — average of dimensional scores
    avg_spec = sum(r["specificity"] for r in ratings) / rater_count
    avg_fals = sum(r["falsifiability"] for r in ratings) / rater_count
    avg_cons = sum(r["internal_consistency"] for r in ratings) / rater_count
    avg_nov = sum(r["novelty"] for r in ratings) / rater_count

    iq = (
        _IQ_WEIGHTS["specificity"] * avg_spec
        + _IQ_WEIGHTS["falsifiability"] * avg_fals
        + _IQ_WEIGHTS["internal_consistency"] * avg_cons
        + _IQ_WEIGHTS["novelty"] * avg_nov
    )

    # Tier 2: Verification — avg(usefulness * weight) for verified ratings
    verified = [r for r in ratings if r["verification_outcome"] is not None]
    if verified:
        v_scores = []
        for r in verified:
            w = _VERIFICATION_WEIGHTS.get(r["verification_outcome"], 0.5)
            u = r["usefulness"] if r["usefulness"] is not None else 0.5
            v_scores.append(u * w)
        v = sum(v_scores) / len(v_scores)
    else:
        v = 0.5  # neutral if no verifications

    # Tier 3: Cross-validation — log-diminishing returns on confirmed count
    confirmed_count = sum(
        1 for r in ratings if r["verification_outcome"] == "confirmed"
    )
    cv = min(1.0, math.log2(confirmed_count + 1) / 4.0)

    # Confidence scales with rater count (log-diminishing)
    confidence = min(1.0, 0.5 + 0.15 * math.log2(rater_count + 1))

    # Adaptive weights: shift toward IQ if no verifications
    if not verified:
        iq_w, v_w, cv_w = 0.55, 0.20, 0.25
    else:
        iq_w = _TIER_WEIGHTS["iq"]
        v_w = _TIER_WEIGHTS["verification"]
        cv_w = _TIER_WEIGHTS["cross_validation"]

    truth_score = (iq_w * iq + v_w * v + cv_w * cv) * confidence

    return {
        "truth_score": round(truth_score, 4),
        "confidence": round(confidence, 4),
        "rating_count": rater_count,
        "content_hash": c_hash,
        "dimensions": {
            "specificity": round(avg_spec, 4),
            "falsifiability": round(avg_fals, 4),
            "internal_consistency": round(avg_cons, 4),
            "novelty": round(avg_nov, 4),
            "iq_composite": round(iq, 4),
            "verification": round(v, 4),
            "cross_validation": round(cv, 4),
        },
    }


def _check_rating_anomalies(conn, entity_name: str) -> None:
    """Detect rating burst anomalies (too many ratings in short window)."""
    cutoff = (
        datetime.now(timezone.utc) - timedelta(hours=_RATING_BURST_WINDOW_HOURS)
    ).isoformat()
    count = conn.execute(
        "SELECT COUNT(*) as cnt FROM knowledge_ratings "
        "WHERE entity_name = ? AND rated_at >= ?",
        (entity_name, cutoff),
    ).fetchone()["cnt"]

    if count > _RATING_BURST_THRESHOLD:
        conn.execute(
            "INSERT INTO rating_anomalies (entity_name, anomaly_type, details, detected_at) "
            "VALUES (?, ?, ?, ?)",
            (
                entity_name,
                "rating_burst",
                f"{count} ratings in {_RATING_BURST_WINDOW_HOURS}h (threshold: {_RATING_BURST_THRESHOLD})",
                _now(),
            ),
        )
        logger.warning(
            "Rating anomaly detected: %s has %d ratings in %dh",
            entity_name,
            count,
            _RATING_BURST_WINDOW_HOURS,
        )


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
                "INSERT INTO collaborators "
                "(github_user, display_name, trust_level, added_at, notes) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(github_user) DO UPDATE SET "
                "display_name=excluded.display_name, trust_level=excluded.trust_level, "
                "notes=excluded.notes",
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
                    remote_contents = {
                        o["content"] if isinstance(o, dict) else o for o in pending_obs
                    }
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
            approved_names: set[str] = set()
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
                approved_names.add(p["name"])

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

            # Also approve matching pending relations (only for approved entities)
            rel_rows = conn.execute("SELECT * FROM pending_shared_relations").fetchall()
            imported_rels = 0
            for rel in rel_rows:
                r = dict(rel)
                if (
                    r["from_entity"] not in approved_names
                    and r["to_entity"] not in approved_names
                ):
                    continue
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
        return json.dumps(
            {
                "status": "confirmation_required",
                "recommendation": (
                    "P2P Knowledge Sharing targets SPECIFIC technical "
                    "information useful to other machines/agents in the "
                    "network — not generic knowledge, but hard-won lessons. "
                    "Ideal candidates: verified gotchas, non-obvious patterns, "
                    "environment-specific bugs with confirmed workarounds. "
                    "Each item should be: specific (not generic), falsifiable "
                    "(can be tested), novel (hard to discover independently), "
                    "and universal (applies beyond one project)."
                ),
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
            }
        )

    now = _now()
    updated_entities = 0
    updated_tasks = 0
    not_found: list[str] = []

    with _get_conn() as conn:
        for name in entity_names or []:
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

        for tid in task_ids or []:
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
        updated_entities,
        updated_tasks,
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
        for name in entity_names or []:
            cur = conn.execute(
                "UPDATE entities SET visibility='private', "
                "publish_requested_at=NULL, updated_at=? "
                "WHERE name=? AND visibility='pending_public'",
                (now, name),
            )
            reverted_entities += cur.rowcount

        for tid in task_ids or []:
            cur = conn.execute(
                "UPDATE tasks SET visibility='private', "
                "publish_requested_at=NULL, updated_at=? "
                "WHERE id=? AND visibility='pending_public'",
                (now, tid),
            )
            reverted_tasks += cur.rowcount

    logger.info(
        "cancel_publish: reverted %d entities, %d tasks to private",
        reverted_entities,
        reverted_tasks,
    )
    return json.dumps(
        {
            "reverted_entities": reverted_entities,
            "reverted_tasks": reverted_tasks,
        }
    )


@mcp.tool()
def search_public_knowledge(
    query: str,
    entity_type: str | None = None,
    sort_by: str = "relevance",
    min_truth_score: float | None = None,
    limit: int = 50,
) -> str:
    """Search published public knowledge using FTS5 BM25-ranked search.

    Only returns entities with visibility='public'.

    Args:
        sort_by: "relevance" (BM25), "truth_score", or "rating_count"
        min_truth_score: Filter out entities below this TruthScore threshold
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

        # Batch-fetch observations (avoids N+1 per-entity queries)
        if rows:
            eids = [r["rowid"] for r in rows]
            ph = ",".join("?" * len(eids))
            obs_rows = conn.execute(
                f"SELECT entity_id, content FROM observations "
                f"WHERE entity_id IN ({ph}) ORDER BY entity_id, id",
                eids,
            ).fetchall()
            obs_by_eid: dict[int, list[str]] = {}
            for o in obs_rows:
                obs_by_eid.setdefault(o["entity_id"], []).append(o["content"])
        else:
            obs_by_eid = {}

        results = []
        for r in rows:
            score_info = _compute_truth_score(r["name"], conn)
            if (
                min_truth_score is not None
                and score_info["truth_score"] < min_truth_score
            ):
                continue
            results.append(
                {
                    "name": r["name"],
                    "entityType": r["entity_type"],
                    "observations": obs_by_eid.get(r["rowid"], []),
                    "truthScore": score_info["truth_score"],
                    "ratingCount": score_info["rating_count"],
                    "confidence": score_info["confidence"],
                }
            )

        # Sort results
        if sort_by == "truth_score":
            results.sort(key=lambda x: x["truthScore"], reverse=True)
        elif sort_by == "rating_count":
            results.sort(key=lambda x: x["ratingCount"], reverse=True)

    logger.info("search_public_knowledge: query=%r matched=%d", query, len(results))
    return json.dumps({"entities": results, "query": query, "count": len(results)})


@mcp.tool()
def rate_public_knowledge(
    entity_name: str,
    specificity: float,
    falsifiability: float,
    internal_consistency: float,
    novelty: float,
    verification_outcome: str | None = None,
    usefulness: float | None = None,
    verification_context: str | None = None,
) -> str:
    """Rate a public knowledge entity's quality (Claude-only structured analysis).

    Anti-gaming: rater_id set server-side, content_hash computed from DB,
    self-rating blocked, UNIQUE constraint prevents re-rating same version.

    Args:
        entity_name: Name of the public entity to rate
        specificity: How specific/precise the knowledge is (0.0-1.0)
        falsifiability: Can claims be tested/verified? (0.0-1.0)
        internal_consistency: Are observations consistent? (0.0-1.0)
        novelty: Does it add new information? (0.0-1.0)
        verification_outcome: "confirmed", "contradicted", or "inconclusive"
        usefulness: How useful was the knowledge in practice? (0.0-1.0)
        verification_context: Description of how verification was done
    """
    # Validate scores in [0.0, 1.0]
    for name, val in [
        ("specificity", specificity),
        ("falsifiability", falsifiability),
        ("internal_consistency", internal_consistency),
        ("novelty", novelty),
    ]:
        if not (0.0 <= val <= 1.0):
            return json.dumps(
                {"error": f"{name} must be between 0.0 and 1.0, got {val}"}
            )

    if verification_outcome is not None:
        if verification_outcome not in _VERIFICATION_OUTCOMES:
            return json.dumps(
                {
                    "error": f"verification_outcome must be one of {_VERIFICATION_OUTCOMES}"
                }
            )
        if usefulness is None:
            return json.dumps(
                {
                    "error": "usefulness is required when verification_outcome is provided"
                }
            )

    if usefulness is not None and not (0.0 <= usefulness <= 1.0):
        return json.dumps(
            {"error": f"usefulness must be between 0.0 and 1.0, got {usefulness}"}
        )

    # rater_id: server-side identity (never user input)
    rater_id = os.environ.get("GITHUB_USER", socket.gethostname())

    with _get_conn() as conn:
        # Entity must exist and be public
        entity = conn.execute(
            "SELECT name, visibility FROM entities WHERE name = ?", (entity_name,)
        ).fetchone()
        if not entity:
            return json.dumps({"error": f"Entity '{entity_name}' not found"})
        if entity["visibility"] != "public":
            return json.dumps(
                {
                    "error": f"Entity '{entity_name}' is not public (visibility={entity['visibility']})"
                }
            )

        # Anti-gaming: no self-rating
        publisher_id = _get_publisher_id(conn, entity_name)
        if rater_id == publisher_id:
            return json.dumps({"error": "Cannot rate your own published knowledge"})

        # Compute content hash from current DB content
        result = _entity_content_hash(conn, entity_name)
        c_hash = result[0] if result else _content_hash(entity_name, [])

        # Insert rating (UNIQUE constraint prevents re-rating same version)
        try:
            conn.execute(
                "INSERT INTO knowledge_ratings "
                "(entity_name, rater_id, content_hash, specificity, falsifiability, "
                "internal_consistency, novelty, verification_outcome, usefulness, "
                "verification_context, rated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    entity_name,
                    rater_id,
                    c_hash,
                    specificity,
                    falsifiability,
                    internal_consistency,
                    novelty,
                    verification_outcome,
                    usefulness,
                    verification_context,
                    _now(),
                ),
            )
        except sqlite3.IntegrityError:
            return json.dumps(
                {
                    "error": "Already rated this content version",
                    "hint": "Content must change before you can rate again",
                }
            )

        # Anomaly detection
        _check_rating_anomalies(conn, entity_name)

        # Compute updated TruthScore
        score_info = _compute_truth_score(entity_name, conn)

    logger.info(
        "rate_public_knowledge: %s rated by %s (score=%.4f)",
        entity_name,
        rater_id,
        score_info["truth_score"],
    )
    return json.dumps(
        {
            "status": "rated",
            "entity_name": entity_name,
            "rater_id": rater_id,
            "content_hash": c_hash,
            **score_info,
        }
    )


@mcp.tool()
def get_knowledge_ratings(
    entity_name: str,
    include_individual: bool = False,
) -> str:
    """Get computed TruthScore and dimensional breakdown for a public entity.

    Args:
        entity_name: Name of the entity to get ratings for
        include_individual: Include individual rating details
    """
    with _get_conn() as conn:
        entity = conn.execute(
            "SELECT name, visibility FROM entities WHERE name = ?", (entity_name,)
        ).fetchone()
        if not entity:
            return json.dumps({"error": f"Entity '{entity_name}' not found"})

        score_info = _compute_truth_score(entity_name, conn)

        # Anomaly status
        anomaly_count = conn.execute(
            "SELECT COUNT(*) as cnt FROM rating_anomalies "
            "WHERE entity_name = ? AND resolved = 0",
            (entity_name,),
        ).fetchone()["cnt"]
        score_info["unresolved_anomalies"] = anomaly_count

        if include_individual:
            ratings = conn.execute(
                "SELECT rater_id, specificity, falsifiability, internal_consistency, "
                "novelty, verification_outcome, usefulness, verification_context, rated_at "
                "FROM knowledge_ratings WHERE entity_name = ? AND content_hash = ? "
                "ORDER BY rated_at",
                (entity_name, score_info["content_hash"]),
            ).fetchall()
            score_info["individual_ratings"] = [dict(r) for r in ratings]

    return json.dumps(score_info)


@mcp.tool()
def update_verification(
    entity_name: str,
    verification_outcome: str,
    usefulness: float,
    verification_context: str | None = None,
) -> str:
    """Update verification fields on your existing rating for a public entity.

    Use after actually testing/applying the knowledge in practice.

    Args:
        entity_name: Name of the entity
        verification_outcome: "confirmed", "contradicted", or "inconclusive"
        usefulness: How useful was the knowledge in practice? (0.0-1.0)
        verification_context: Description of how verification was done
    """
    if verification_outcome not in _VERIFICATION_OUTCOMES:
        return json.dumps(
            {"error": f"verification_outcome must be one of {_VERIFICATION_OUTCOMES}"}
        )
    if not (0.0 <= usefulness <= 1.0):
        return json.dumps(
            {"error": f"usefulness must be between 0.0 and 1.0, got {usefulness}"}
        )

    rater_id = os.environ.get("GITHUB_USER", socket.gethostname())

    with _get_conn() as conn:
        # Get current content hash
        result = _entity_content_hash(conn, entity_name)
        if not result:
            return json.dumps(
                {"error": f"Entity '{entity_name}' not found or has no observations"}
            )
        c_hash = result[0]

        # Update existing rating
        cur = conn.execute(
            "UPDATE knowledge_ratings SET verification_outcome = ?, "
            "usefulness = ?, verification_context = ? "
            "WHERE entity_name = ? AND rater_id = ? AND content_hash = ?",
            (
                verification_outcome,
                usefulness,
                verification_context,
                entity_name,
                rater_id,
                c_hash,
            ),
        )
        if cur.rowcount == 0:
            return json.dumps(
                {
                    "error": "No existing rating found for this entity/version",
                    "hint": "You must rate_public_knowledge first before updating verification",
                }
            )

        score_info = _compute_truth_score(entity_name, conn)

    logger.info(
        "update_verification: %s by %s → %s (score=%.4f)",
        entity_name,
        rater_id,
        verification_outcome,
        score_info["truth_score"],
    )
    return json.dumps(
        {
            "status": "verification_updated",
            "entity_name": entity_name,
            "verification_outcome": verification_outcome,
            **score_info,
        }
    )


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
        **_NOWIN,
    )
    if result.returncode != 0:
        logger.warning("git %s failed: %s", " ".join(args), result.stderr.strip())
    return result


_GITHUB_USER_RE = re.compile(r"^[a-zA-Z0-9](?:[a-zA-Z0-9\-]{0,37}[a-zA-Z0-9])?$")


def _validate_github_user(username: str) -> None:
    """Raise ValueError if username is not a valid GitHub username."""
    if not _GITHUB_USER_RE.match(username):
        raise ValueError(f"Invalid GitHub username: {username!r}")


def _push_to_assignee(assignee: str, tasks: list[dict]) -> None:
    """Push assigned tasks to another user's memory-bridge repo."""
    import tempfile

    _validate_github_user(assignee)
    repo_url = f"https://github.com/{assignee}/memory-bridge.git"
    with tempfile.TemporaryDirectory() as tmpdir:
        clone = subprocess.run(
            ["git", "clone", "--depth=1", repo_url, tmpdir],
            capture_output=True,
            text=True,
            timeout=30,
            **_NOWIN,
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
            ["git", "-C", tmpdir, "add", "shared.json"],
            capture_output=True,
            timeout=10,
            **_NOWIN,
        )
        hostname = socket.gethostname()
        msg = f"bridge: shared {len(tasks)} tasks from {hostname} to {assignee}"
        commit = subprocess.run(
            ["git", "-C", tmpdir, "commit", "-m", msg],
            capture_output=True,
            text=True,
            timeout=10,
            **_NOWIN,
        )
        if commit.returncode == 0:
            push = subprocess.run(
                ["git", "-C", tmpdir, "push"],
                capture_output=True,
                text=True,
                timeout=30,
                **_NOWIN,
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
    _validate_github_user(target_user)
    repo_url = f"https://github.com/{target_user}/memory-bridge.git"
    with tempfile.TemporaryDirectory() as tmpdir:
        clone = subprocess.run(
            ["git", "clone", "--depth=1", repo_url, tmpdir],
            capture_output=True,
            text=True,
            timeout=30,
            **_NOWIN,
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
            **_NOWIN,
        )
        hostname = socket.gethostname()
        msg = f"bridge: shared {len(knowledge_out)} entities from {hostname} to {target_user}"
        commit = subprocess.run(
            ["git", "-C", tmpdir, "commit", "-m", msg],
            capture_output=True,
            text=True,
            timeout=10,
            **_NOWIN,
        )
        if commit.returncode == 0:
            push = subprocess.run(
                ["git", "-C", tmpdir, "push"],
                capture_output=True,
                text=True,
                timeout=30,
                **_NOWIN,
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
def bridge_push(tag: str = "shared", force: bool = False) -> str:
    """Push tagged entities to the bridge git repo for cross-machine sync.

    Exports entities where project LIKE '{tag}%' with their observations
    and inter-relations to JSON. Git add, commit, push.

    Incremental: skips full export if nothing changed since last push.
    Set force=True to push regardless.
    """
    if not Path(BRIDGE_REPO).is_dir():
        return json.dumps(
            {
                "error": f"Bridge repo not found at {BRIDGE_REPO}. "
                "Run: mkdir -p {BRIDGE_REPO} && git -C {BRIDGE_REPO} init"
            }
        )

    # v2.0.0: Pull before push (prevents overwriting remote changes)
    pull_result = _git("pull", "--rebase", "--autostash")
    if pull_result.returncode != 0:
        logger.warning("bridge_push: git pull failed: %s", pull_result.stderr.strip())

    # v2.0.0: One-time migration shared.json → per-task files
    _migrate_to_per_task_files(BRIDGE_REPO)

    with _get_conn() as conn:
        # v2.0.0: LWW merge remote index.json into local DB
        _bp_index_path = Path(BRIDGE_REPO) / "index.json"
        if _bp_index_path.exists():
            try:
                _remote_idx = _json_loads(_bp_index_path.read_text(encoding="utf-8"))
                _merge_import_tasks(conn, _remote_idx.get("tasks", []))
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("bridge_push: index.json merge failed: %s", exc)

        # Incremental check: skip if no changes since last push
        if not force:
            last_push_row = conn.execute(
                "SELECT value FROM bridge_meta WHERE key = 'last_push_at'"
            ).fetchone()
            if last_push_row:
                last_push_at = last_push_row["value"]
                row = conn.execute(
                    "SELECT "
                    "  (SELECT COUNT(*) FROM tasks WHERE updated_at > ?) AS changed_tasks, "
                    "  (SELECT COUNT(*) FROM entities WHERE updated_at > ?) AS changed_ents, "
                    "  (SELECT COUNT(*) FROM entities WHERE visibility = 'pending_public') AS pending_pub",
                    (last_push_at, last_push_at),
                ).fetchone()
                if row[0] == 0 and row[1] == 0 and row[2] == 0:
                    logger.info(
                        "bridge_push: no changes since %s, skipping", last_push_at
                    )
                    return json.dumps(
                        {
                            "pushed": 0,
                            "message": f"No changes since {last_push_at}. Use force=True to push anyway.",
                        }
                    )
        # v0.7.0: Promote pending_public → public if standby elapsed
        cutoff = (
            datetime.now(timezone.utc) - timedelta(minutes=_PUBLISH_STANDBY_MINUTES)
        ).isoformat()
        promoted_ent = conn.execute(
            "UPDATE entities SET visibility='public' "
            "WHERE visibility='pending_public' AND publish_requested_at <= ?",
            (cutoff,),
        ).rowcount
        promoted_tasks = TaskDAO.promote_pending_public(conn, cutoff)
        if promoted_ent or promoted_tasks:
            logger.info(
                "bridge_push: promoted %d entities, %d tasks to public",
                promoted_ent,
                promoted_tasks,
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
            "FROM tasks WHERE status NOT IN ('archived', 'cancelled') ORDER BY created_at"
        ).fetchall()
        tasks_out = [dict(r) for r in task_rows]

        # v2.0.0: Export per-task files + index.json
        last_push_at = None
        lp_row = conn.execute(
            "SELECT value FROM bridge_meta WHERE key = 'last_push_at'"
        ).fetchone()
        if lp_row:
            last_push_at = lp_row["value"]
        _export_task_files(conn, BRIDGE_REPO, changed_since=last_push_at)
        _export_index_json(conn, BRIDGE_REPO)

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
            public_entities_out.append(
                {
                    "name": pe["name"],
                    "entityType": pe["entity_type"],
                    "project": pe["project"],
                    "observations": [
                        {"content": o["content"], "createdAt": o["created_at"]}
                        for o in obs
                    ],
                    "createdAt": pe["created_at"],
                    "updatedAt": pe["updated_at"],
                }
            )
        pub_task_rows = conn.execute(
            "SELECT id, title, description, status, priority, section, "
            "due_date, project, created_at, updated_at "
            "FROM tasks WHERE visibility='public' ORDER BY created_at"
        ).fetchall()
        public_tasks_out = [dict(r) for r in pub_task_rows]

        # Build team_manifest from collaborators (same connection)
        collab_rows = conn.execute(
            "SELECT github_user FROM collaborators ORDER BY added_at"
        ).fetchall()
        collaborator_list = [r["github_user"] for r in collab_rows]

    hostname = socket.gethostname()
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

    # v0.9.0: Export knowledge_ratings
    with _get_conn() as conn:
        rating_rows = conn.execute(
            "SELECT entity_name, rater_id, content_hash, specificity, falsifiability, "
            "internal_consistency, novelty, verification_outcome, usefulness, "
            "verification_context, rated_at FROM knowledge_ratings ORDER BY rated_at"
        ).fetchall()
    if rating_rows:
        payload["knowledge_ratings"] = [dict(r) for r in rating_rows]

    # Merge remote tasks + preserve extra keys from remote
    shared_path = Path(BRIDGE_REPO) / "shared.json"
    index_exists = (Path(BRIDGE_REPO) / "index.json").exists()
    if shared_path.exists():
        try:
            existing = _json_loads(shared_path.read_text(encoding="utf-8"))

            if not index_exists:
                # Legacy merge: keep remote tasks that don't exist locally (by title)
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
                        for field in (
                            "status",
                            "section",
                            "priority",
                            "due_date",
                            "notes",
                            "description",
                            "type",
                        ):
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
                "knowledge_ratings",
                "team_manifest",
            }
            for key, val in existing.items():
                if key not in known_keys and isinstance(val, (list, dict)):
                    payload[key] = val
                    logger.info(
                        "bridge_push: preserving extra key '%s' (%s)",
                        key,
                        f"{len(val)} items" if isinstance(val, list) else "dict",
                    )
        except (json.JSONDecodeError, OSError):
            pass

    shared_path.write_text(_json_dumps(payload), encoding="utf-8")

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
    # Phase 1: collect targets inside short transaction (release WAL quickly)
    knowledge_pushed = 0
    push_targets: list[str] = []
    with _get_conn() as conn:
        rules = conn.execute(
            "SELECT DISTINCT target_user FROM sharing_rules"
        ).fetchall()
        for rule_row in rules:
            target = rule_row["target_user"]
            collab = conn.execute(
                "SELECT trust_level FROM collaborators WHERE github_user = ?",
                (target,),
            ).fetchone()
            if collab:
                push_targets.append(target)

    # Phase 2: git operations outside transaction (no WAL lock during network I/O)
    successful_targets: list[str] = []
    for target in push_targets:
        try:
            with _get_conn() as conn:
                pushed_n = _push_knowledge_to(conn, target)
            knowledge_pushed += pushed_n
            successful_targets.append(target)
        except Exception as exc:
            logger.warning("bridge_push: knowledge push to %s failed: %s", target, exc)

    # Phase 3: update sync timestamps in short transaction
    if successful_targets:
        with _get_conn() as conn:
            now = _now()
            for target in successful_targets:
                conn.execute(
                    "UPDATE collaborators SET last_sync_at = ? WHERE github_user = ?",
                    (now, target),
                )

    n_obs = sum(len(e["observations"]) for e in entities_out)
    msg = (
        f"bridge: push {len(entities_out)} entities, "
        f"{len(tasks_out)} tasks from {hostname}"
    )

    _git("add", "shared.json", "index.json", "tasks/")
    commit_result = _git("commit", "-m", msg)
    if commit_result.returncode != 0:
        if "nothing to commit" in (commit_result.stdout + commit_result.stderr):
            logger.info("bridge_push: no changes to commit")
            return json.dumps(
                {"pushed": 0, "message": "No changes — already up to date"}
            )
        logger.error("bridge_push: commit failed: %s", commit_result.stderr)
        return json.dumps(
            {"error": f"git commit failed: {commit_result.stderr.strip()}"}
        )

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
                    "gh",
                    "release",
                    "create",
                    tag_name,
                    "--repo",
                    os.environ.get("BRIDGE_GH_REPO", "RMANOV/sqlite-memory-mcp"),
                    "--title",
                    release_title,
                    "--notes",
                    release_notes,
                ],
                capture_output=True,
                text=True,
                timeout=30,
                **_NOWIN,
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

    if pushed:
        with _get_conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO bridge_meta(key, value) VALUES('last_push_at', ?)",
                (_now(),),
            )

    return json.dumps(result)


@mcp.tool()
def bridge_pull() -> str:
    """Pull shared entities from the bridge git repo into local memory.

    Git pull, read shared.json, import new entities/observations/relations.
    UNIQUE constraints handle deduplication automatically.
    """
    if not Path(BRIDGE_REPO).is_dir():
        return json.dumps({"error": f"Bridge repo not found at {BRIDGE_REPO}"})

    pull_result = _git("pull", "--rebase", "--autostash")
    if pull_result.returncode != 0:
        logger.warning("bridge_pull: git pull failed, proceeding with local copy")

    shared_path = Path(BRIDGE_REPO) / "shared.json"
    _pull_index_path = Path(BRIDGE_REPO) / "index.json"
    _has_index = _pull_index_path.exists()

    if not shared_path.exists() and not _has_index:
        return json.dumps({"error": "No sync data found in bridge repo"})

    # Read shared.json for entities/relations (and legacy task fallback)
    payload: dict = {}
    if shared_path.exists():
        try:
            payload = json.loads(shared_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            if not _has_index:
                return json.dumps({"error": f"Failed to read shared.json: {exc}"})
            logger.warning("bridge_pull: shared.json parse failed: %s", exc)

    entities = payload.get("entities", [])
    relations = payload.get("relations", [])
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

        # v2.0.0: Import tasks via per-field LWW merge from index.json
        if _has_index:
            try:
                _idx_data = _json_loads(_pull_index_path.read_text(encoding="utf-8"))
                new_tasks, updated_tasks = _merge_import_tasks(
                    conn, _idx_data.get("tasks", [])
                )
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("bridge_pull: index.json read failed: %s", exc)
                new_tasks, updated_tasks = 0, 0
        else:
            # Legacy fallback: task-level LWW from shared.json
            tasks = list(payload.get("tasks", []))
            for key, val in payload.items():
                if (
                    key.endswith("_tasks")
                    and key != "tasks"
                    and key != "shared_tasks"
                    and isinstance(val, list)
                ):
                    tasks.extend(val)
            tasks_sorted = sorted(
                tasks,
                key=lambda t: (
                    t.get("parent_id") is not None,
                    t.get("created_at", ""),
                ),
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
                    if task.get("updated_at", "") > existing["updated_at"]:
                        conn.execute(
                            "UPDATE tasks SET title=?, description=?, status=?, "
                            "priority=?, section=?, due_date=?, project=?, "
                            "parent_id=?, notes=?, recurring=?, type=?, "
                            "assignee=?, shared_by=?, updated_at=? WHERE id=?",
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
                        _upsert_field_versions(
                            conn, tid, _MERGEABLE_FIELDS, task.get("updated_at", now)
                        )
                        updated_tasks += 1
                else:
                    TaskDAO.create(
                        conn,
                        tid,
                        task["title"],
                        task.get("updated_at", now),
                        description=task.get("description"),
                        status=task["status"],
                        priority=task["priority"],
                        section=task["section"],
                        due_date=task.get("due_date"),
                        project=task.get("project"),
                        parent_id=task.get("parent_id"),
                        notes=task.get("notes"),
                        recurring=task.get("recurring"),
                        type=task.get("type", "task"),
                        assignee=task.get("assignee"),
                        shared_by=task.get("shared_by"),
                        created_at=task.get("created_at", now),
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

        # v0.9.0: Import knowledge ratings with anti-gaming validation
        imported_ratings = 0
        local_owner = os.environ.get("GITHUB_USER", socket.gethostname())
        for kr in payload.get("knowledge_ratings", []):
            kr_rater = kr.get("rater_id", "")
            kr_entity = kr.get("entity_name", "")
            # Skip own ratings (don't import back)
            if kr_rater == local_owner:
                continue
            # Skip if entity doesn't exist locally or isn't public
            ent = conn.execute(
                "SELECT visibility FROM entities WHERE name = ?", (kr_entity,)
            ).fetchone()
            if not ent or ent["visibility"] != "public":
                continue
            # Validate content_hash is non-empty (required for rating integrity)
            c_hash = kr.get("content_hash", "")
            if not c_hash:
                continue
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO knowledge_ratings "
                    "(entity_name, rater_id, content_hash, specificity, falsifiability, "
                    "internal_consistency, novelty, verification_outcome, usefulness, "
                    "verification_context, rated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        kr_entity,
                        kr_rater,
                        c_hash,
                        _clamp_score(kr.get("specificity", 0.0)),
                        _clamp_score(kr.get("falsifiability", 0.0)),
                        _clamp_score(kr.get("internal_consistency", 0.0)),
                        _clamp_score(kr.get("novelty", 0.0)),
                        kr.get("verification_outcome"),
                        kr.get("usefulness"),
                        kr.get("verification_context"),
                        kr.get("rated_at", now),
                    ),
                )
                imported_ratings += 1
            except (sqlite3.IntegrityError, sqlite3.OperationalError):
                continue
        if imported_ratings:
            logger.info("bridge_pull: imported %d knowledge ratings", imported_ratings)

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
    if imported_ratings:
        result["imported_ratings"] = imported_ratings
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
        local_task_count = TaskDAO.count_active(conn)

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
        public_task_count = TaskDAO.count_by_visibility(conn, "public")
        pending_pub_task_count = TaskDAO.count_by_visibility(conn, "pending_public")

        # v0.9.0: rating statistics
        total_ratings = conn.execute(
            "SELECT COUNT(*) as cnt FROM knowledge_ratings"
        ).fetchone()["cnt"]
        rated_entities = conn.execute(
            "SELECT COUNT(DISTINCT entity_name) as cnt FROM knowledge_ratings"
        ).fetchone()["cnt"]
        anomaly_count = conn.execute(
            "SELECT COUNT(*) as cnt FROM rating_anomalies WHERE resolved = 0"
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
            "total_ratings": total_ratings,
            "rated_entities": rated_entities,
            "anomalies": anomaly_count,
        }
    )


# ═══════════════════════════════════════════════════════════════════════════
# Tools 34-40: Entity↔Task Links + Cross-Entity Insights (v2.2.0)
# ═══════════════════════════════════════════════════════════════════════════


@mcp.tool()
def link_task_entity(task_id: str, entity_name: str) -> str:
    """Link a task to a knowledge graph entity.

    Creates a manual link between a task and an entity. If an auto-discovered
    link already exists, it upgrades to manual (manual always wins).
    """
    with _get_conn() as conn:
        if not TaskDAO.exists(conn, task_id):
            return json.dumps({"error": f"Task {task_id} not found"})

        entity = conn.execute(
            "SELECT id FROM entities WHERE name = ?", (entity_name,)
        ).fetchone()
        if not entity:
            return json.dumps({"error": f"Entity '{entity_name}' not found"})

        entity_id = entity["id"]
        now = datetime.now(timezone.utc).isoformat()

        TaskDAO.link_entity(
            conn, task_id, entity_id, link_type="manual", created_at=now
        )

        return json.dumps(
            {
                "task_id": task_id,
                "entity_name": entity_name,
                "entity_id": entity_id,
                "link_type": "manual",
                "created_at": now,
            }
        )


@mcp.tool()
def unlink_task_entity(task_id: str, entity_name: str) -> str:
    """Remove a link between a task and a knowledge graph entity."""
    with _get_conn() as conn:
        entity = conn.execute(
            "SELECT id FROM entities WHERE name = ?", (entity_name,)
        ).fetchone()
        if not entity:
            return json.dumps({"error": f"Entity '{entity_name}' not found"})

        removed = TaskDAO.unlink_entity(conn, task_id, entity["id"])

        return json.dumps({"removed": removed > 0})


@mcp.tool()
def get_task_links(task_id: str) -> str:
    """Get all knowledge graph entities linked to a task."""
    with _get_conn() as conn:
        links = TaskDAO.get_task_links(conn, task_id)
        return json.dumps({"task_id": task_id, "links": links})


@mcp.tool()
def get_entity_tasks(entity_name: str) -> str:
    """Get all tasks linked to a knowledge graph entity."""
    with _get_conn() as conn:
        entity = conn.execute(
            "SELECT id FROM entities WHERE name = ?", (entity_name,)
        ).fetchone()
        if not entity:
            return json.dumps({"error": f"Entity '{entity_name}' not found"})

        tasks = TaskDAO.get_entity_tasks(conn, entity["id"])
        return json.dumps({"entity_name": entity_name, "tasks": tasks})


@mcp.tool()
def suggest_task_links(task_id: str, limit: int = 5) -> str:
    """Suggest knowledge graph entities that may be related to a task.

    Uses FTS5 for candidate retrieval + Jaccard similarity for ranking.
    Does NOT auto-create links — returns suggestions for human/Claude review.
    """
    with _get_conn() as conn:
        task = TaskDAO.get_by_id(conn, task_id, "title, description")
        if not task:
            return json.dumps({"error": f"Task {task_id} not found"})

        search_text = f"{task['title'] or ''} {task['description'] or ''}"
        task_tokens = _tokenize(search_text)
        if not task_tokens:
            return json.dumps({"task_id": task_id, "suggestions": []})

        fts_q = _fts_query(search_text)
        if not fts_q:
            return json.dumps({"task_id": task_id, "suggestions": []})

        candidates = conn.execute(
            "SELECT rowid, name, entity_type, rank "
            "FROM memory_fts WHERE memory_fts MATCH ? "
            "ORDER BY rank LIMIT 50",
            (fts_q,),
        ).fetchall()

        linked_ids = TaskDAO.get_linked_entity_ids(conn, task_id)

        scored = []
        for c in candidates:
            if c["rowid"] in linked_ids:
                continue

            obs = conn.execute(
                "SELECT content FROM observations WHERE entity_id = ?",
                (c["rowid"],),
            ).fetchall()
            obs_text = " ".join(o["content"] for o in obs)
            entity_tokens = _tokenize(f"{c['name']} {obs_text}")

            if not entity_tokens:
                continue

            t_tok = set(list(task_tokens)[:500])
            e_tok = set(list(entity_tokens)[:500])
            intersection = t_tok & e_tok
            union = t_tok | e_tok
            jaccard = len(intersection) / len(union) if union else 0.0

            norm_rank = min(1.0, abs(c["rank"]) / 20.0)
            combined = 0.6 * norm_rank + 0.4 * jaccard

            scored.append(
                {
                    "entity_name": c["name"],
                    "entity_type": c["entity_type"],
                    "score": round(combined, 4),
                    "shared_keywords": sorted(intersection)[:10],
                }
            )

        scored.sort(key=lambda x: x["score"], reverse=True)

        return json.dumps({"task_id": task_id, "suggestions": scored[:limit]})


@mcp.tool()
def find_entity_overlaps(
    entity_name: str | None = None,
    min_score: float = 0.3,
    limit: int = 20,
) -> str:
    """Find overlapping/duplicate entities in the knowledge graph.

    Uses FTS5 + Jaccard similarity to detect entity pairs with significant
    observation overlap. Pairs with score >= 0.8 get a merge suggestion.
    """
    with _get_conn() as conn:
        if entity_name:
            sources = conn.execute(
                "SELECT id, name, entity_type FROM entities WHERE name = ?",
                (entity_name,),
            ).fetchall()
            if not sources:
                return json.dumps({"error": f"Entity '{entity_name}' not found"})
        else:
            sources = conn.execute(
                "SELECT id, name, entity_type FROM entities"
            ).fetchall()

        seen_pairs: set[tuple[int, int]] = set()
        overlaps = []

        for src in sources:
            src_obs = conn.execute(
                "SELECT content FROM observations WHERE entity_id = ?",
                (src["id"],),
            ).fetchall()
            src_text = " ".join(o["content"] for o in src_obs)
            src_tokens = _tokenize(f"{src['name']} {src_text}")

            if not src_tokens:
                continue

            fts_q = _fts_query(src_text or src["name"])
            if not fts_q:
                continue

            candidates = conn.execute(
                "SELECT rowid, name, entity_type "
                "FROM memory_fts WHERE memory_fts MATCH ? LIMIT 50",
                (fts_q,),
            ).fetchall()

            for cand in candidates:
                cand_id = cand["rowid"]
                if cand_id == src["id"]:
                    continue

                pair_key = (min(src["id"], cand_id), max(src["id"], cand_id))
                if pair_key in seen_pairs:
                    continue
                seen_pairs.add(pair_key)

                cand_obs = conn.execute(
                    "SELECT content FROM observations WHERE entity_id = ?",
                    (cand_id,),
                ).fetchall()
                cand_text = " ".join(o["content"] for o in cand_obs)
                cand_tokens = _tokenize(f"{cand['name']} {cand_text}")

                if not cand_tokens:
                    continue

                s_tok = set(list(src_tokens)[:500])
                c_tok = set(list(cand_tokens)[:500])
                intersection = s_tok & c_tok
                union_set = s_tok | c_tok
                jaccard = len(intersection) / len(union_set) if union_set else 0.0

                if jaccard < min_score:
                    continue

                overlaps.append(
                    {
                        "entity_a": src["name"],
                        "entity_b": cand["name"],
                        "score": round(jaccard, 4),
                        "shared_keywords": sorted(intersection)[:10],
                        "suggest_merge": jaccard >= 0.8,
                    }
                )

        overlaps.sort(key=lambda x: x["score"], reverse=True)

        return json.dumps({"overlaps": overlaps[:limit]})


@mcp.tool()
def merge_entities(source_name: str, target_name: str, dry_run: bool = True) -> str:
    """Merge one entity into another, combining observations, relations, and task links.

    The source entity is absorbed into the target. Use dry_run=True (default) to
    preview what will be moved before committing.

    Args:
        source_name: Entity to merge FROM (will be deleted)
        target_name: Entity to merge INTO (will receive all data)
        dry_run: If True, only show what would happen without making changes
    """
    with _get_conn() as conn:
        source = conn.execute(
            "SELECT id, name FROM entities WHERE name = ?", (source_name,)
        ).fetchone()
        if not source:
            return json.dumps({"error": f"Source entity '{source_name}' not found"})

        target = conn.execute(
            "SELECT id, name FROM entities WHERE name = ?", (target_name,)
        ).fetchone()
        if not target:
            return json.dumps({"error": f"Target entity '{target_name}' not found"})

        src_id, tgt_id = source["id"], target["id"]

        if src_id == tgt_id:
            return json.dumps({"error": "Source and target are the same entity"})

        # Count what will be moved
        unique_obs = conn.execute(
            "SELECT COUNT(*) AS cnt FROM observations "
            "WHERE entity_id = ? AND content NOT IN "
            "(SELECT content FROM observations WHERE entity_id = ?)",
            (src_id, tgt_id),
        ).fetchone()["cnt"]

        rel_from = conn.execute(
            "SELECT COUNT(*) AS cnt FROM relations WHERE from_id = ? AND to_id != ?",
            (src_id, tgt_id),
        ).fetchone()["cnt"]

        rel_to = conn.execute(
            "SELECT COUNT(*) AS cnt FROM relations WHERE to_id = ? AND from_id != ?",
            (src_id, tgt_id),
        ).fetchone()["cnt"]

        task_links = conn.execute(
            "SELECT COUNT(*) AS cnt FROM task_entity_links "
            "WHERE entity_id = ? AND task_id NOT IN "
            "(SELECT task_id FROM task_entity_links WHERE entity_id = ?)",
            (src_id, tgt_id),
        ).fetchone()["cnt"]

        preview = {
            "source": source_name,
            "target": target_name,
            "observations_to_move": unique_obs,
            "relations_to_move": rel_from + rel_to,
            "task_links_to_move": task_links,
            "dry_run": dry_run,
        }

        if dry_run:
            return json.dumps(preview)

        # 1. Move unique observations
        conn.execute(
            "INSERT INTO observations (entity_id, content, created_at) "
            "SELECT ?, content, created_at FROM observations "
            "WHERE entity_id = ? AND content NOT IN "
            "(SELECT content FROM observations WHERE entity_id = ?)",
            (tgt_id, src_id, tgt_id),
        )

        # 2. Reassign relations (from_id) — skip self-loops and dupes
        from_rels = conn.execute(
            "SELECT id, to_id, relation_type FROM relations "
            "WHERE from_id = ? AND to_id != ?",
            (src_id, tgt_id),
        ).fetchall()
        for rel in from_rels:
            existing = conn.execute(
                "SELECT 1 FROM relations "
                "WHERE from_id = ? AND to_id = ? AND relation_type = ?",
                (tgt_id, rel["to_id"], rel["relation_type"]),
            ).fetchone()
            if not existing:
                conn.execute(
                    "UPDATE relations SET from_id = ? WHERE id = ?",
                    (tgt_id, rel["id"]),
                )

        # Reassign relations (to_id)
        to_rels = conn.execute(
            "SELECT id, from_id, relation_type FROM relations "
            "WHERE to_id = ? AND from_id != ?",
            (src_id, tgt_id),
        ).fetchall()
        for rel in to_rels:
            existing = conn.execute(
                "SELECT 1 FROM relations "
                "WHERE from_id = ? AND to_id = ? AND relation_type = ?",
                (rel["from_id"], tgt_id, rel["relation_type"]),
            ).fetchone()
            if not existing:
                conn.execute(
                    "UPDATE relations SET to_id = ? WHERE id = ?",
                    (tgt_id, rel["id"]),
                )

        # 3. Reassign task links
        src_links = conn.execute(
            "SELECT task_id, link_type, score, created_at "
            "FROM task_entity_links WHERE entity_id = ?",
            (src_id,),
        ).fetchall()
        tgt_linked_task_ids = {
            r["task_id"]
            for r in conn.execute(
                "SELECT task_id FROM task_entity_links WHERE entity_id = ?", (tgt_id,)
            ).fetchall()
        }
        for link in src_links:
            if link["task_id"] not in tgt_linked_task_ids:
                TaskDAO.link_entity(
                    conn,
                    link["task_id"],
                    tgt_id,
                    link_type=link["link_type"],
                    score=link["score"],
                    created_at=link["created_at"],
                )

        # 4. Delete source entity (CASCADE cleans orphan observations/relations/links)
        conn.execute("DELETE FROM entities WHERE id = ?", (src_id,))

        # 5. Rebuild FTS5 for target + clean source
        _fts_sync(conn, tgt_id)
        conn.execute("DELETE FROM memory_fts WHERE rowid = ?", (src_id,))

        preview["merged"] = True
        preview["dry_run"] = False
        return json.dumps(preview)


# ═══════════════════════════════════════════════════════════════════════════
# Intelligence v2 — Context State Machine + Knowledge Tiers (tools 32-40)
# ═══════════════════════════════════════════════════════════════════════════

from intelligence_v2 import (
    assess_context as _assess_context,
    queue_clarification as _queue_clarification,
    record_human_answer as _record_human_answer,
    load_config as _load_intel_config,
)
from claim_graph import (
    extract_candidate_claims as _extract_claims,
    promote_candidate as _promote_candidate,
)
from context_packer import (
    build_context_pack as _build_pack,
    resume_context as _resume_context,
)
from impact_graph import (
    explain_impact as _explain_impact,
)


@mcp.tool()
def assess_context(
    chunk_ref: str,
    session_id: str | None = None,
    force: bool = False,
) -> str:
    """Classify context chunk, detect signals, determine state transition.

    Scans for signal phrases (ENRICH_OK, NO_ENRICH, WAIT_HUMAN, FREEZE_CONTEXT),
    computes materiality and uncertainty scores, and manages state transitions.
    Skips reprocessing if chunk is awaiting_human with unchanged source_hash.

    Args:
        chunk_ref: ID of the context chunk to assess
        session_id: Optional session context
        force: If True, bypass skip logic and frozen state
    """
    with _get_conn() as conn:
        result = _assess_context(conn, chunk_ref, session_id, force)
        return json.dumps(result)


@mcp.tool()
def queue_clarification(
    chunk_ref: str,
    max_questions: int = 5,
) -> str:
    """Generate AWAITING_HUMAN block with focused clarification questions.

    Analyzes the chunk content to produce typed questions (scope, semantics,
    time, action, downstream_use) and locks the chunk until human answers.

    Args:
        chunk_ref: ID of the context chunk
        max_questions: Maximum number of questions to generate (1-5)
    """
    with _get_conn() as conn:
        result = _queue_clarification(conn, chunk_ref, max_questions)
        return json.dumps(result)


@mcp.tool()
def record_human_answer(
    chunk_ref: str,
    answer_text: str,
    question_id: str | None = None,
) -> str:
    """Ingest human answer, update chunk state, resolve open questions.

    Transitions chunk from awaiting_human/uncertain back to enrichable,
    updates source_hash to reflect the new information.

    Args:
        chunk_ref: ID of the context chunk
        answer_text: Human's answer text
        question_id: Optional specific question to answer (answers all if omitted)
    """
    with _get_conn() as conn:
        result = _record_human_answer(conn, chunk_ref, answer_text, question_id)
        return json.dumps(result)


@mcp.tool()
def extract_candidate_claims(
    chunk_ref: str,
    scope_hint: str | None = None,
) -> str:
    """Extract typed (subject, predicate, object, scope) claims from a context chunk.

    Only works on enrichable or uncertain chunks. Creates candidate claims with
    evidence records linking back to the source. Claims require governance gate
    (promote_candidate) before becoming canonical facts.

    Args:
        chunk_ref: ID of the context chunk to extract from
        scope_hint: Optional scope override (memory|bridge|mapping|validation|export)
    """
    with _get_conn() as conn:
        result = _extract_claims(conn, chunk_ref, scope_hint)
        return json.dumps(result)


@mcp.tool()
def promote_candidate(
    claim_id: str,
    mode: str = "human_confirmed",
) -> str:
    """Governance gate: promote candidate claim to canonical fact.

    Modes:
    - human_confirmed: explicit human approval (always allowed)
    - multi_evidence: auto-promotion if enough independent evidence (policy-gated)
    - imported: bulk import from trusted source

    Sensitive scopes (mapping, validation, bridge, export) require human_confirmed.

    Args:
        claim_id: ID of the candidate claim
        mode: Promotion mode (human_confirmed|multi_evidence|imported)
    """
    with _get_conn() as conn:
        result = _promote_candidate(conn, claim_id, mode)
        return json.dumps(result)


@mcp.tool()
def build_context_pack(
    pack_type: str = "executor",
    target_ref: str | None = None,
    session_id: str | None = None,
    token_budget: int | None = None,
) -> str:
    """Compile role-specific context pack with token budget optimization.

    Greedy coverage algorithm: scores available facts, claims, questions, and
    chunks by relevance × role weight, then fills the token budget.

    Pack types:
    - planner: facts + questions (what do we know, what's uncertain)
    - reviewer: facts + claims (what to validate)
    - executor: facts + chunks (confirmed context for implementation)
    - bridge_checker: claims + questions (what needs bridge verification)
    - handoff: everything prioritized for session continuity

    Args:
        pack_type: Role-specific pack type
        target_ref: Optional target reference for context filtering
        session_id: Optional session context
        token_budget: Token limit (default from config, typically 4000)
    """
    with _get_conn() as conn:
        result = _build_pack(conn, pack_type, target_ref, session_id, token_budget)
        return json.dumps(result)


@mcp.tool()
def explain_impact(
    source_kind: str = "chunk",
    source_ref: str = "",
    depth: str = "standard",
) -> str:
    """Show downstream impact of a knowledge change via bounded BFS.

    Traverses impact_edges graph to find affected sessions, snapshots,
    mappings, validations, and exports. Results grouped and ranked by
    propagated impact score.

    Args:
        source_kind: Type of source (chunk|claim|fact)
        source_ref: ID of the source entity
        depth: Traversal depth (quick=1, standard=3, deep=5)
    """
    with _get_conn() as conn:
        result = _explain_impact(conn, source_kind, source_ref, depth)
        return json.dumps(result)


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
    with _get_conn() as conn:
        result = _resume_context(conn, session_id, include_open_questions)
        return json.dumps(result)


@mcp.tool()
def enrich_context(depth: str = "quick") -> str:
    """Compatibility wrapper: enriches context at different depth levels.

    Depth levels:
    - quick: assess all enrichable chunks + build executor pack
    - standard: + extract candidate claims
    - deep: + explain impact for all recent facts

    Args:
        depth: Enrichment depth (quick|standard|deep)
    """
    config = _load_intel_config()
    if not config["enabled"]:
        return json.dumps(
            {"status": "disabled", "message": "Intelligence v2 is disabled"}
        )

    results: dict = {"depth": depth, "steps": []}

    with _get_conn() as conn:
        # Step 1: Assess all enrichable chunks
        enrichable = conn.execute(
            "SELECT chunk_id FROM context_chunks WHERE state = 'enrichable' LIMIT 20"
        ).fetchall()
        assessed = []
        for row in enrichable:
            r = _assess_context(conn, row["chunk_id"])
            assessed.append(r.get("chunk_id", "?"))
        results["steps"].append({"assess": len(assessed)})

        # Step 2: Build executor pack
        pack = _build_pack(conn, "executor")
        results["steps"].append(
            {
                "pack": pack.get("pack_id"),
                "tokens": pack.get("token_usage", 0),
            }
        )
        results["pack_body"] = pack.get("body", "")

        if depth in ("standard", "deep"):
            # Step 3: Extract claims from enrichable chunks
            claims_total = 0
            for row in enrichable:
                cr = _extract_claims(conn, row["chunk_id"])
                claims_total += cr.get("claims_extracted", 0)
            results["steps"].append({"claims_extracted": claims_total})

        if depth == "deep":
            # Step 4: Explain impact for recent facts
            recent = conn.execute(
                "SELECT fact_id FROM canonical_facts "
                "WHERE updated_at >= datetime('now', '-7 days') LIMIT 10"
            ).fetchall()
            impacts = []
            for f in recent:
                imp = _explain_impact(conn, "fact", f["fact_id"])
                impacts.append(imp.get("total_impacts", 0))
            results["steps"].append({"impacts_analyzed": len(impacts)})

    return json.dumps(results)


# ═══════════════════════════════════════════════════════════════════════════
# Startup — always init DB on import (ensures tables exist for all callers)
# ═══════════════════════════════════════════════════════════════════════════

_init_db()

if __name__ == "__main__":
    _migrate_jsonl()
    mcp.run(transport="stdio")
