"""Shared schema, migrations, and DB initialization for sqlite-memory-mcp.

All micro-servers import init_db() from this module to ensure the database
schema is up-to-date. The function is idempotent (safe to call from multiple
concurrent processes thanks to WAL mode and IF NOT EXISTS guards).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from db_utils import get_conn as _get_conn, DB_PATH

logger = logging.getLogger("sqlite-kb")


# ── Response helpers (used by all micro-servers) ─────────────────────────


def error(msg: str) -> str:
    """Build a JSON error response string."""
    return json.dumps({"error": msg})


def is_valid_timestamp(s: str) -> bool:
    """Validate ISO 8601 timestamp: parseable and not unreasonably in the future.

    Accepts both timezone-aware and naive ISO timestamps. Naive timestamps are
    treated as UTC.
    """
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            # Naive ISO timestamp — treat as UTC
            return dt <= datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(
                hours=24
            )
        return dt <= datetime.now(timezone.utc) + timedelta(hours=24)
    except (ValueError, TypeError):
        return False


def clamp_score(val: Any, default: float = 0.0) -> float:
    """Clamp a score to [0.0, 1.0] range, returning default on invalid input."""
    try:
        return max(0.0, min(1.0, float(val)))
    except (TypeError, ValueError):
        return default


# ── Schema SQL ───────────────────────────────────────────────────────────

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
    parent_id   TEXT DEFAULT NULL REFERENCES tasks(id) ON DELETE SET NULL,
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
CREATE INDEX IF NOT EXISTS idx_tasks_updated_at    ON tasks(updated_at);
CREATE INDEX IF NOT EXISTS idx_entities_updated_at ON entities(updated_at);

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

-- Intelligence v2 tables (context state machine + knowledge tiers)

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

CREATE TABLE IF NOT EXISTS task_field_versions (
    task_id     TEXT NOT NULL,
    field_name  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    updated_by  TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (task_id, field_name),
    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS entity_access_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_id   INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    tool_name   TEXT    NOT NULL,
    accessed_at TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_eal_entity ON entity_access_log(entity_id, accessed_at DESC);

CREATE TABLE IF NOT EXISTS lazy_claims (
    claim_id            TEXT PRIMARY KEY,
    entity_id           INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    observation_id      INTEGER NOT NULL REFERENCES observations(id) ON DELETE CASCADE,
    subject             TEXT NOT NULL,
    predicate           TEXT NOT NULL,
    object_text         TEXT NOT NULL,
    confidence          REAL NOT NULL,
    status              TEXT NOT NULL DEFAULT 'candidate',
    promoted_to_fact_id TEXT NULL,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lc_entity ON lazy_claims(entity_id, status);
CREATE INDEX IF NOT EXISTS idx_lc_obs    ON lazy_claims(observation_id);
CREATE INDEX IF NOT EXISTS idx_lc_status ON lazy_claims(status, confidence DESC);

-- Auto-sync triggers: keep memory_fts in lockstep with entities table
CREATE TRIGGER IF NOT EXISTS memory_fts_ai AFTER INSERT ON entities BEGIN
    INSERT INTO memory_fts(rowid, name, entity_type, observations_text)
    VALUES (new.rowid, new.name, new.entity_type, '');
END;

CREATE TRIGGER IF NOT EXISTS memory_fts_ad AFTER DELETE ON entities BEGIN
    INSERT INTO memory_fts(memory_fts, rowid, name, entity_type, observations_text)
    VALUES ('delete', old.rowid, old.name, old.entity_type, '');
END;

-- NOTE: observations_text is always '' because triggers cannot aggregate child rows.
-- Full-text search on observations relies on name/entity_type columns only.
CREATE TRIGGER IF NOT EXISTS memory_fts_au AFTER UPDATE ON entities BEGIN
    INSERT INTO memory_fts(memory_fts, rowid, name, entity_type, observations_text)
    VALUES ('delete', old.rowid, old.name, old.entity_type, '');
    INSERT INTO memory_fts(rowid, name, entity_type, observations_text)
    VALUES (new.rowid, new.name, new.entity_type, '');
END;
"""


_MIGRATIONS = [
    (
        "SELECT 1 FROM pragma_table_info('tasks') WHERE name='description'",
        "ALTER TABLE tasks ADD COLUMN description TEXT DEFAULT NULL",
        "tasks.description column",
    ),
    (
        "SELECT 1 FROM pragma_table_info('tasks') WHERE name='type'",
        "ALTER TABLE tasks ADD COLUMN type TEXT NOT NULL DEFAULT 'task'",
        "tasks.type column (task/note)",
    ),
    (
        "SELECT 1 FROM pragma_table_info('tasks') WHERE name='assignee'",
        "ALTER TABLE tasks ADD COLUMN assignee TEXT DEFAULT NULL",
        "tasks.assignee column",
    ),
    (
        "SELECT 1 FROM pragma_table_info('tasks') WHERE name='shared_by'",
        "ALTER TABLE tasks ADD COLUMN shared_by TEXT DEFAULT NULL",
        "tasks.shared_by column",
    ),
    (
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_tasks_type'",
        "CREATE INDEX IF NOT EXISTS idx_tasks_type ON tasks(type)",
        "idx_tasks_type index",
    ),
    (
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_tasks_assignee'",
        "CREATE INDEX IF NOT EXISTS idx_tasks_assignee ON tasks(assignee)",
        "idx_tasks_assignee index",
    ),
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
    (
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='collaborators'",
        "CREATE TABLE collaborators ("
        "github_user TEXT PRIMARY KEY, display_name TEXT, "
        "trust_level TEXT NOT NULL DEFAULT 'read_write', "
        "added_at TEXT NOT NULL, last_sync_at TEXT, notes TEXT)",
        "collaborators table",
    ),
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
    (
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='pending_shared_relations'",
        "CREATE TABLE pending_shared_relations ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, from_entity TEXT NOT NULL, "
        "to_entity TEXT NOT NULL, relation_type TEXT NOT NULL, "
        "shared_by TEXT NOT NULL, received_at TEXT NOT NULL, "
        "UNIQUE(from_entity, to_entity, relation_type, shared_by))",
        "pending_shared_relations staging table",
    ),
    (
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sharing_rules'",
        "CREATE TABLE sharing_rules ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, entity_name TEXT NOT NULL, "
        "target_user TEXT NOT NULL, share_type TEXT NOT NULL DEFAULT 'entity', "
        "priority TEXT NOT NULL DEFAULT 'medium', "
        "created_at TEXT NOT NULL, UNIQUE(entity_name, target_user, share_type))",
        "sharing_rules table",
    ),
    (
        "SELECT 1 FROM pragma_table_info('entities') WHERE name='shared_by'",
        "ALTER TABLE entities ADD COLUMN shared_by TEXT DEFAULT NULL",
        "entities.shared_by column",
    ),
    (
        "SELECT 1 FROM pragma_table_info('entities') WHERE name='origin'",
        "ALTER TABLE entities ADD COLUMN origin TEXT DEFAULT 'local'",
        "entities.origin column",
    ),
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
    (
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='rating_anomalies'",
        "CREATE TABLE rating_anomalies ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, entity_name TEXT NOT NULL, "
        "anomaly_type TEXT NOT NULL, details TEXT NOT NULL, "
        "detected_at TEXT NOT NULL, resolved INTEGER DEFAULT 0)",
        "rating_anomalies table (v0.9.0)",
    ),
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
    (
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='bridge_meta'",
        "CREATE TABLE bridge_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
        "bridge_meta table (v1.0.0)",
    ),
    (
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='task_field_versions'",
        "CREATE TABLE task_field_versions ("
        "task_id TEXT NOT NULL, field_name TEXT NOT NULL, "
        "updated_at TEXT NOT NULL, updated_by TEXT NOT NULL DEFAULT '', "
        "PRIMARY KEY (task_id, field_name), "
        "FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE)",
        "task_field_versions table (v2.0.0)",
    ),
    (
        "SELECT 1 FROM tasks WHERE id NOT IN (SELECT task_id FROM task_field_versions) LIMIT 1",
        "INSERT OR IGNORE INTO task_field_versions (task_id, field_name, updated_at, updated_by) "
        "SELECT id, 'title', updated_at, '' FROM tasks WHERE id NOT IN (SELECT task_id FROM task_field_versions) "
        "UNION ALL SELECT id, 'status', updated_at, '' FROM tasks WHERE id NOT IN (SELECT task_id FROM task_field_versions) "
        "UNION ALL SELECT id, 'priority', updated_at, '' FROM tasks WHERE id NOT IN (SELECT task_id FROM task_field_versions) "
        "UNION ALL SELECT id, 'section', updated_at, '' FROM tasks WHERE id NOT IN (SELECT task_id FROM task_field_versions) "
        "UNION ALL SELECT id, 'due_date', updated_at, '' FROM tasks WHERE id NOT IN (SELECT task_id FROM task_field_versions) "
        "UNION ALL SELECT id, 'project', updated_at, '' FROM tasks WHERE id NOT IN (SELECT task_id FROM task_field_versions) "
        "UNION ALL SELECT id, 'parent_id', updated_at, '' FROM tasks WHERE id NOT IN (SELECT task_id FROM task_field_versions) "
        "UNION ALL SELECT id, 'recurring', updated_at, '' FROM tasks WHERE id NOT IN (SELECT task_id FROM task_field_versions) "
        "UNION ALL SELECT id, 'type', updated_at, '' FROM tasks WHERE id NOT IN (SELECT task_id FROM task_field_versions) "
        "UNION ALL SELECT id, 'assignee', updated_at, '' FROM tasks WHERE id NOT IN (SELECT task_id FROM task_field_versions) "
        "UNION ALL SELECT id, 'shared_by', updated_at, '' FROM tasks WHERE id NOT IN (SELECT task_id FROM task_field_versions) "
        "UNION ALL SELECT id, 'description', updated_at, '' FROM tasks WHERE id NOT IN (SELECT task_id FROM task_field_versions) "
        "UNION ALL SELECT id, 'notes', updated_at, '' FROM tasks WHERE id NOT IN (SELECT task_id FROM task_field_versions)",
        "seed task_field_versions from existing tasks (v2.0.0)",
    ),
    (
        "SELECT 1 FROM pragma_table_info('tasks') WHERE name='reminder_at'",
        "ALTER TABLE tasks ADD COLUMN reminder_at TEXT DEFAULT NULL",
        "tasks.reminder_at column (v2.1.0)",
    ),
    (
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_tasks_reminder_at'",
        "CREATE INDEX idx_tasks_reminder_at ON tasks(reminder_at) WHERE reminder_at IS NOT NULL",
        "idx_tasks_reminder_at partial index (v2.1.0)",
    ),
    (
        "SELECT 1 FROM task_field_versions WHERE field_name = 'reminder_at' LIMIT 1",
        "INSERT OR IGNORE INTO task_field_versions (task_id, field_name, updated_at, updated_by) "
        "SELECT id, 'reminder_at', updated_at, '' FROM tasks",
        "seed task_field_versions.reminder_at (v2.1.0)",
    ),
    (
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='task_entity_links'",
        "CREATE TABLE task_entity_links ("
        "task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE, "
        "entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE, "
        "link_type TEXT NOT NULL DEFAULT 'manual', "
        "score REAL DEFAULT NULL, "
        "created_at TEXT NOT NULL, "
        "PRIMARY KEY (task_id, entity_id))",
        "task_entity_links table (v2.2.0)",
    ),
    (
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_tel_entity'",
        "CREATE INDEX idx_tel_entity ON task_entity_links(entity_id)",
        "idx_tel_entity index (v2.2.0)",
    ),
    (
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='context_chunks'",
        "CREATE TABLE context_chunks ("
        "chunk_id TEXT PRIMARY KEY, session_id TEXT NULL, entity_id TEXT NULL, "
        "source_type TEXT NOT NULL, source_ref TEXT NOT NULL, source_hash TEXT NOT NULL, "
        "title TEXT NULL, body TEXT NOT NULL, language TEXT DEFAULT 'bg', "
        "state TEXT NOT NULL DEFAULT 'no_enrich', enrich_policy TEXT NOT NULL DEFAULT 'manual', "
        "materiality_score REAL DEFAULT 0.0, last_human_update_at TEXT NULL, "
        "last_ai_attempt_at TEXT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)",
        "context_chunks table (v3.0.0)",
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
    (
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='entity_access_log'",
        "CREATE TABLE entity_access_log ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE, "
        "tool_name TEXT NOT NULL, "
        "accessed_at TEXT NOT NULL)",
        "entity_access_log table (v3.1.0)",
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
        "lazy_claims table (v3.1.0)",
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
    (
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_tasks_updated_at'",
        "CREATE INDEX IF NOT EXISTS idx_tasks_updated_at ON tasks(updated_at)",
        "idx_tasks_updated_at index (F8)",
    ),
    (
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_entities_updated_at'",
        "CREATE INDEX IF NOT EXISTS idx_entities_updated_at ON entities(updated_at)",
        "idx_entities_updated_at index (F9)",
    ),
]


def init_db(db_path: str | None = None) -> None:
    """Create tables if they don't exist, run migrations, set WAL mode.

    Safe to call from multiple processes — all DDL uses IF NOT EXISTS.
    """
    _path = db_path or DB_PATH
    Path(_path).parent.mkdir(parents=True, exist_ok=True)
    raw = sqlite3.connect(_path, isolation_level=None)
    raw.execute("BEGIN EXCLUSIVE;")
    try:
        for stmt in _SCHEMA_SQL.split(";"):
            stmt = stmt.strip()
            if stmt:
                raw.execute(stmt)
        raw.execute("COMMIT;")
    except Exception:
        raw.execute("ROLLBACK;")
        raise
    finally:
        raw.close()

    with _get_conn(_path) as conn:
        for check_q, migrate_q, desc in _MIGRATIONS:
            if not conn.execute(check_q).fetchone():
                conn.execute(migrate_q)
                logger.info("Migration applied: %s", desc)

    with _get_conn(_path) as conn:
        task_count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        fts_count = conn.execute("SELECT COUNT(*) FROM tasks_fts").fetchone()[0]
        if task_count > 0 and fts_count == 0:
            conn.execute("INSERT INTO tasks_fts(tasks_fts) VALUES('rebuild')")
            logger.info(
                "tasks_fts: rebuilt FTS index for %d existing tasks", task_count
            )

    # Optional: initialize sqlite-vec virtual table for semantic search
    try:
        from vec_search import (
            VEC_AVAILABLE,
            init_vec_table,
            init_task_vec_table,
            backfill_task_embeddings,
        )

        if VEC_AVAILABLE:
            with _get_conn(_path) as conn:
                init_vec_table(conn)
                init_task_vec_table(conn)
                backfill_task_embeddings(conn)
    except Exception as e:
        logger.debug("sqlite-vec init skipped: %s", e)

    logger.info("Database initialized at %s", _path)
