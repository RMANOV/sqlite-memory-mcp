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
CREATE INDEX IF NOT EXISTS idx_tasks_title      ON tasks(title);
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

CREATE TABLE IF NOT EXISTS entity_aliases (
    entity_id        INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    alias            TEXT    NOT NULL,
    normalized_alias TEXT    NOT NULL,
    created_at       TEXT    NOT NULL,
    PRIMARY KEY (entity_id, normalized_alias)
);
CREATE INDEX IF NOT EXISTS idx_entity_aliases_normalized
    ON entity_aliases(normalized_alias);

CREATE TABLE IF NOT EXISTS link_suggestion_decisions (
    decision_id       TEXT PRIMARY KEY,
    task_id           TEXT    NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    entity_id         INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    decision          TEXT    NOT NULL CHECK (decision IN ('accepted', 'rejected')),
    score             REAL    DEFAULT NULL,
    rank_at_decision  INTEGER DEFAULT NULL,
    signals_json      TEXT    NOT NULL DEFAULT '{}',
    model_version     TEXT    NOT NULL,
    decision_source   TEXT    NOT NULL,
    decided_by        TEXT    NOT NULL,
    created_at        TEXT    NOT NULL,
    updated_at        TEXT    NOT NULL,
    UNIQUE(task_id, entity_id)
);
CREATE INDEX IF NOT EXISTS idx_link_decisions_decision
    ON link_suggestion_decisions(decision, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_link_decisions_model
    ON link_suggestion_decisions(model_version, decision);

CREATE TABLE IF NOT EXISTS link_community_runs (
    run_id              TEXT PRIMARY KEY,
    model_version       TEXT    NOT NULL,
    algorithm           TEXT    NOT NULL CHECK (algorithm = 'leiden'),
    seed                INTEGER NOT NULL,
    resolutions_json    TEXT    NOT NULL,
    primary_resolution  TEXT    NOT NULL,
    stability_json      TEXT    NOT NULL,
    label_count         INTEGER NOT NULL,
    node_count          INTEGER NOT NULL,
    edge_count          INTEGER NOT NULL,
    created_at          TEXT    NOT NULL,
    active              INTEGER NOT NULL DEFAULT 0 CHECK (active IN (0, 1))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_link_community_one_active
    ON link_community_runs(active) WHERE active = 1;

CREATE TABLE IF NOT EXISTS link_community_memberships (
    run_id           TEXT NOT NULL REFERENCES link_community_runs(run_id) ON DELETE CASCADE,
    resolution       TEXT NOT NULL,
    node_kind        TEXT NOT NULL CHECK (node_kind IN ('task', 'entity', 'project', 'source')),
    node_ref         TEXT NOT NULL,
    community_id     INTEGER NOT NULL,
    stability_score  REAL NOT NULL,
    PRIMARY KEY (run_id, resolution, node_kind, node_ref)
);
CREATE INDEX IF NOT EXISTS idx_link_community_lookup
    ON link_community_memberships(run_id, resolution, node_kind, node_ref);

CREATE TABLE IF NOT EXISTS task_attachments (
    attachment_id  TEXT PRIMARY KEY,
    task_id        TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    file_name      TEXT NOT NULL,
    stored_relpath TEXT NOT NULL UNIQUE,
    media_type     TEXT DEFAULT NULL,
    file_size      INTEGER NOT NULL DEFAULT 0,
    status         TEXT NOT NULL DEFAULT 'active',
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_task_attachments_task ON task_attachments(task_id, status, created_at);

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
    language            TEXT DEFAULT NULL,
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
    source_start        INTEGER NULL,
    source_end          INTEGER NULL,
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
    valid_from          TEXT NULL,
    valid_to            TEXT NULL,
    superseded_by_fact_id TEXT NULL REFERENCES canonical_facts(fact_id),
    contradiction_count INTEGER NOT NULL DEFAULT 0,
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
    contract_version    TEXT NOT NULL DEFAULT 'legacy',
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

CREATE TABLE IF NOT EXISTS memory_cursors (
    machine_id          TEXT PRIMARY KEY,
    last_clock          INTEGER NOT NULL DEFAULT 0,
    updated_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memory_events (
    event_id            TEXT PRIMARY KEY,
    event_type          TEXT NOT NULL,
    aggregate_kind      TEXT NOT NULL,
    aggregate_id        TEXT NOT NULL,
    field_name          TEXT NULL,
    actor_type          TEXT NOT NULL DEFAULT 'system',
    actor_id            TEXT NULL,
    machine_id          TEXT NOT NULL,
    tool_name           TEXT NOT NULL,
    logical_clock       INTEGER NOT NULL,
    event_ts            TEXT NOT NULL,
    old_value           TEXT NULL,
    new_value           TEXT NULL,
    payload_json        TEXT NULL,
    parent_event_id     TEXT NULL,
    source_kind         TEXT NULL,
    source_ref          TEXT NULL,
    source_excerpt      TEXT NULL,
    source_start        INTEGER NULL,
    source_end          INTEGER NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_events_clock
    ON memory_events(machine_id, logical_clock);
CREATE INDEX IF NOT EXISTS idx_memory_events_aggregate
    ON memory_events(aggregate_kind, aggregate_id, logical_clock DESC);

CREATE TABLE IF NOT EXISTS provenance_links (
    provenance_id       TEXT PRIMARY KEY,
    subject_kind        TEXT NOT NULL,
    subject_ref         TEXT NOT NULL,
    source_kind         TEXT NOT NULL,
    source_ref          TEXT NOT NULL,
    span_start          INTEGER NULL,
    span_end            INTEGER NULL,
    excerpt             TEXT NULL,
    confidence          REAL NOT NULL DEFAULT 1.0,
    created_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_provenance_subject
    ON provenance_links(subject_kind, subject_ref);

CREATE TABLE IF NOT EXISTS knowledge_links (
    link_id             TEXT PRIMARY KEY,
    subject_kind        TEXT NOT NULL,
    subject_ref         TEXT NOT NULL,
    relation_type       TEXT NOT NULL,
    object_kind         TEXT NOT NULL,
    object_ref          TEXT NOT NULL,
    rationale           TEXT NULL,
    created_at          TEXT NOT NULL,
    active              INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_knowledge_links_subject
    ON knowledge_links(subject_kind, subject_ref, relation_type, active);
CREATE INDEX IF NOT EXISTS idx_knowledge_links_object
    ON knowledge_links(object_kind, object_ref, relation_type, active);

CREATE TABLE IF NOT EXISTS memory_audit_issues (
    issue_id            TEXT PRIMARY KEY,
    issue_type          TEXT NOT NULL,
    severity            TEXT NOT NULL,
    subject_kind        TEXT NOT NULL,
    subject_ref         TEXT NOT NULL,
    details_json        TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'open',
    first_detected_at   TEXT NOT NULL,
    last_detected_at    TEXT NOT NULL,
    resolved_at         TEXT NULL
);
CREATE INDEX IF NOT EXISTS idx_memory_audit_status
    ON memory_audit_issues(status, severity, issue_type);

CREATE TABLE IF NOT EXISTS memory_artifacts (
    artifact_id          TEXT PRIMARY KEY,
    artifact_key         TEXT NOT NULL UNIQUE,
    artifact_kind        TEXT NOT NULL,
    scope_kind           TEXT NOT NULL,
    scope_ref            TEXT NOT NULL,
    title                TEXT NULL,
    body                 TEXT NOT NULL,
    confidence           REAL NOT NULL DEFAULT 1.0,
    status               TEXT NOT NULL DEFAULT 'active',
    valid_from           TEXT NULL,
    valid_to             TEXT NULL,
    source_event_id      TEXT NULL,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memory_artifacts_scope
    ON memory_artifacts(artifact_kind, scope_kind, scope_ref, status);

CREATE TABLE IF NOT EXISTS memory_conflicts (
    conflict_id          TEXT PRIMARY KEY,
    conflict_key         TEXT NOT NULL UNIQUE,
    aggregate_kind       TEXT NOT NULL,
    aggregate_id         TEXT NOT NULL,
    field_name           TEXT NULL,
    local_value          TEXT NULL,
    remote_value         TEXT NULL,
    local_updated_at     TEXT NULL,
    remote_updated_at    TEXT NULL,
    local_updated_order  INTEGER NOT NULL DEFAULT 0,
    remote_updated_order INTEGER NOT NULL DEFAULT 0,
    local_source_event_id TEXT NULL,
    remote_source_event_id TEXT NULL,
    winner               TEXT NOT NULL,
    status               TEXT NOT NULL DEFAULT 'open',
    rationale            TEXT NULL,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL,
    resolved_at          TEXT NULL
);
CREATE INDEX IF NOT EXISTS idx_memory_conflicts_status
    ON memory_conflicts(status, aggregate_kind, aggregate_id, field_name);

CREATE TABLE IF NOT EXISTS memory_audit_state (
    runner_name          TEXT PRIMARY KEY,
    cadence_minutes      INTEGER NOT NULL DEFAULT 60,
    last_started_at      TEXT NULL,
    last_finished_at     TEXT NULL,
    next_run_after       TEXT NULL,
    last_status          TEXT NOT NULL DEFAULT 'never',
    last_summary_json    TEXT NULL,
    updated_at           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_field_versions (
    task_id     TEXT NOT NULL,
    field_name  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    updated_by  TEXT NOT NULL DEFAULT '',
    updated_order INTEGER NOT NULL DEFAULT 0,
    source_event_id TEXT DEFAULT NULL,
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
    hit_count           INTEGER NOT NULL DEFAULT 1,
    status              TEXT NOT NULL DEFAULT 'candidate',
    promoted_to_fact_id TEXT NULL,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lc_entity ON lazy_claims(entity_id, status);
CREATE INDEX IF NOT EXISTS idx_lc_obs    ON lazy_claims(observation_id);
CREATE INDEX IF NOT EXISTS idx_lc_status ON lazy_claims(status, confidence DESC);

CREATE TABLE IF NOT EXISTS premium_gate_audit (
    audit_id            TEXT PRIMARY KEY,
    feature_id          TEXT NOT NULL,
    decision            TEXT NOT NULL,
    reason              TEXT NOT NULL,
    entitlement_id      TEXT DEFAULT NULL,
    customer_id         TEXT DEFAULT NULL,
    server_name         TEXT DEFAULT NULL,
    tool_name           TEXT DEFAULT NULL,
    actor_id            TEXT DEFAULT NULL,
    machine_id          TEXT NOT NULL,
    checked_at          TEXT NOT NULL,
    payload_json        TEXT DEFAULT NULL
);
CREATE INDEX IF NOT EXISTS idx_premium_gate_audit_feature
    ON premium_gate_audit(feature_id, checked_at DESC);

CREATE TABLE IF NOT EXISTS premium_revocations (
    revocation_id       TEXT PRIMARY KEY,
    entitlement_id      TEXT NOT NULL,
    feature_id          TEXT DEFAULT NULL,
    customer_id         TEXT DEFAULT NULL,
    reason              TEXT DEFAULT NULL,
    revoked_at          TEXT NOT NULL,
    revoked_by          TEXT DEFAULT NULL,
    active              INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_premium_revocations_lookup
    ON premium_revocations(entitlement_id, feature_id, active);

CREATE TABLE IF NOT EXISTS premium_artifact_manifests (
    manifest_id          TEXT PRIMARY KEY,
    extension_name       TEXT NOT NULL,
    entrypoint_ref       TEXT NOT NULL,
    entrypoint_sha256    TEXT NOT NULL,
    contract_version     TEXT NOT NULL,
    build_id             TEXT DEFAULT NULL,
    customer_id          TEXT DEFAULT NULL,
    protection_phase     INTEGER NOT NULL DEFAULT 1,
    minimum_host_version TEXT DEFAULT NULL,
    maximum_host_version TEXT DEFAULT NULL,
    issued_at            TEXT DEFAULT NULL,
    expires_at           TEXT DEFAULT NULL,
    verified_at          TEXT NOT NULL,
    payload_json         TEXT DEFAULT NULL
);
CREATE INDEX IF NOT EXISTS idx_premium_artifact_manifests_entrypoint
    ON premium_artifact_manifests(entrypoint_ref, verified_at DESC);

CREATE TABLE IF NOT EXISTS premium_control_plane_cache (
    scope_key            TEXT PRIMARY KEY,
    policy_id            TEXT NOT NULL,
    source_ref           TEXT DEFAULT NULL,
    fetched_at           TEXT NOT NULL,
    expires_at           TEXT DEFAULT NULL,
    cache_deadline       TEXT DEFAULT NULL,
    payload_json         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_premium_control_plane_cache_deadline
    ON premium_control_plane_cache(cache_deadline, fetched_at DESC);

-- Auto-sync triggers: keep memory_fts in lockstep with entities table
CREATE TRIGGER IF NOT EXISTS memory_fts_ai AFTER INSERT ON entities BEGIN
    INSERT INTO memory_fts(rowid, name, entity_type, observations_text)
    VALUES (new.rowid, new.name, new.entity_type, '');
END;

CREATE TRIGGER IF NOT EXISTS memory_fts_ad AFTER DELETE ON entities BEGIN
    DELETE FROM memory_fts WHERE rowid = old.rowid;
END;

CREATE TRIGGER IF NOT EXISTS memory_fts_au AFTER UPDATE ON entities BEGIN
    DELETE FROM memory_fts WHERE rowid = old.rowid;
    INSERT INTO memory_fts(rowid, name, entity_type, observations_text)
    SELECT new.rowid, new.name, new.entity_type,
           COALESCE(GROUP_CONCAT(o.content, ' '), '')
    FROM (SELECT 1) LEFT JOIN observations o ON o.entity_id = new.id;
END;

-- FTS sync triggers on observations table (FT-01/FT-02 fix)
CREATE TRIGGER IF NOT EXISTS memory_fts_obs_ai AFTER INSERT ON observations BEGIN
    DELETE FROM memory_fts WHERE rowid = new.entity_id;
    INSERT INTO memory_fts(rowid, name, entity_type, observations_text)
    SELECT e.id, e.name, e.entity_type,
           COALESCE(GROUP_CONCAT(o.content, ' '), '')
    FROM entities e LEFT JOIN observations o ON o.entity_id = e.id
    WHERE e.id = new.entity_id GROUP BY e.id;
END;

CREATE TRIGGER IF NOT EXISTS memory_fts_obs_ad AFTER DELETE ON observations BEGIN
    DELETE FROM memory_fts WHERE rowid = old.entity_id;
    INSERT INTO memory_fts(rowid, name, entity_type, observations_text)
    SELECT e.id, e.name, e.entity_type,
           COALESCE(GROUP_CONCAT(o.content, ' '), '')
    FROM entities e LEFT JOIN observations o ON o.entity_id = e.id
    WHERE e.id = old.entity_id GROUP BY e.id;
END;

-- ── Debate Protocol v2 (Tier S #0, conductor 2026-05-09T16:35 EEST) ─────
-- Productized inter-session coordination. Replaces ad-hoc observations on
-- a single KG entity with a structured channel: (debates) topic record,
-- (debate_messages) append-only log with role + priority + kind enums,
-- (debate_watermarks) per-role read cursors. Lifecycle state machine
-- INIT→ACTIVE→RESOLVED→ARCHIVED enforced in DAO + STATE messages.
-- COMPACTION kind added per conductor 16:55 EEST (recursive bloat fix —
-- readers bootstrap from latest compaction snapshot + incremental tail).
-- topic_id regex validation lives in pure Python (debate.py) since
-- SQLite lacks native REGEXP; CHECK constraint omitted intentionally.

CREATE TABLE IF NOT EXISTS debates (
    topic_id          TEXT PRIMARY KEY,
    title             TEXT NOT NULL,
    state             TEXT NOT NULL DEFAULT 'INIT'
        CHECK (state IN ('INIT', 'ACTIVE', 'RESOLVED', 'ARCHIVED')),
    created_at        TEXT NOT NULL,
    created_by_role   TEXT NOT NULL,
    resolve_by        TEXT,
    archived_at       TEXT,
    roles_json        TEXT NOT NULL,
    metadata_json     TEXT
);
CREATE INDEX IF NOT EXISTS idx_debates_state
    ON debates(state, created_at);
CREATE INDEX IF NOT EXISTS idx_debates_resolve_by
    ON debates(resolve_by) WHERE resolve_by IS NOT NULL;

CREATE TABLE IF NOT EXISTS debate_messages (
    msg_id     TEXT PRIMARY KEY,
    topic_id   TEXT NOT NULL REFERENCES debates(topic_id) ON DELETE CASCADE,
    role       TEXT NOT NULL,
    ts         TEXT NOT NULL,
    priority   TEXT NOT NULL
        CHECK (priority IN ('H', 'M', 'L', 'INFO')),
    kind       TEXT NOT NULL
        CHECK (kind IN ('Q', 'A', 'STATUS', 'DECISION', 'PING',
                        'WATERMARK', 'STATE', 'COMPACTION',
                        'CLAIM', 'CHALLENGE', 'EVIDENCE', 'REBUT',
                        'CONCEDE', 'VERIFY', 'DISSENT', 'ESCALATE')),
    standing   INTEGER DEFAULT NULL
        CHECK (standing IS NULL OR standing IN (0, 1)),
    vehicle    TEXT DEFAULT NULL
        CHECK (vehicle IS NULL
               OR vehicle IN ('analysis', 'review', 'implementation')),
    reply_to   TEXT REFERENCES debate_messages(msg_id) ON DELETE SET NULL,
    body       TEXT NOT NULL,
    protocol_version TEXT DEFAULT NULL
        CHECK (protocol_version IS NULL OR protocol_version = 'debate/v1'),
    round_no   INTEGER DEFAULT NULL
        CHECK (round_no IS NULL OR round_no >= 1),
    body_mode  TEXT DEFAULT NULL
        CHECK (body_mode IS NULL OR body_mode IN ('structured', 'live_text')),
    payload_json TEXT DEFAULT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_debmsg_topic_ts
    ON debate_messages(topic_id, ts);
CREATE INDEX IF NOT EXISTS idx_debmsg_topic_kind
    ON debate_messages(topic_id, kind);
CREATE INDEX IF NOT EXISTS idx_debmsg_topic_priority
    ON debate_messages(topic_id, priority);
CREATE INDEX IF NOT EXISTS idx_debmsg_topic_role_ts
    ON debate_messages(topic_id, role, ts);
CREATE INDEX IF NOT EXISTS idx_debmsg_reply_to
    ON debate_messages(reply_to) WHERE reply_to IS NOT NULL;
-- Native, dependency-free debate retrieval.  The prompt/watcher path used to
-- scan recent rows and inject FIFO bodies, which made a long-lived DAILY topic
-- both noisy and expensive.  Keep a dedicated FTS5 index so callers can pair
-- BM25 token search with the literal/metadata path in debate_retrieval.py.
-- msg_id/topic_id remain stored but unindexed metadata; role/kind/body are
-- searchable.  A standalone table (rather than external-content FTS) keeps
-- the TEXT primary key contract simple and makes legacy backfill explicit.
CREATE VIRTUAL TABLE IF NOT EXISTS debate_messages_fts USING fts5(
    msg_id UNINDEXED,
    topic_id UNINDEXED,
    role,
    kind,
    body,
    tokenize = 'unicode61 remove_diacritics 2'
);

CREATE TRIGGER IF NOT EXISTS debate_messages_fts_ai
AFTER INSERT ON debate_messages BEGIN
    INSERT INTO debate_messages_fts(msg_id, topic_id, role, kind, body)
    VALUES (new.msg_id, new.topic_id, new.role, new.kind, new.body);
END;

CREATE TRIGGER IF NOT EXISTS debate_messages_fts_ad
AFTER DELETE ON debate_messages BEGIN
    DELETE FROM debate_messages_fts WHERE msg_id = old.msg_id;
END;

CREATE TRIGGER IF NOT EXISTS debate_messages_fts_au
AFTER UPDATE OF topic_id, role, kind, body ON debate_messages BEGIN
    DELETE FROM debate_messages_fts WHERE msg_id = old.msg_id;
    INSERT INTO debate_messages_fts(msg_id, topic_id, role, kind, body)
    VALUES (new.msg_id, new.topic_id, new.role, new.kind, new.body);
END;

CREATE TABLE IF NOT EXISTS debate_watermarks (
    topic_id              TEXT NOT NULL REFERENCES debates(topic_id) ON DELETE CASCADE,
    role                  TEXT NOT NULL,
    last_processed_msg_id TEXT REFERENCES debate_messages(msg_id) ON DELETE SET NULL,
    last_processed_ts     TEXT,
    updated_at            TEXT NOT NULL,
    PRIMARY KEY (topic_id, role)
);

-- ── v3.9.2: prompt-time inbox signaling ─────────────────────────────────
-- Two-table model per CONDUCTOR canonical msg:b3a87f15 (replaces
-- deprecated msg:f3a72c84). debate_message_recipients carries WHO is
-- addressed (intent, normalized — replaces rejected CSV addressed_to
-- column anti-pattern from turn 7). debate_signal_state carries WHERE
-- each per-session read cursor sits (compound (ts, msg_id) per turn 2).
-- ARCHIVED retention preserved: ON DELETE CASCADE only fires on actual
-- DELETE FROM debates / debate_messages, NOT on state transitions.
-- last_processed_msg_id has NO FK so cursor history survives potential
-- message hard-deletes; advance is gated by recipient match in DAO
-- (msg:5e2d1c89 turn-12 fix).

CREATE TABLE IF NOT EXISTS debate_message_recipients (
    msg_id    TEXT NOT NULL REFERENCES debate_messages(msg_id) ON DELETE CASCADE,
    recipient TEXT NOT NULL,
    recipient_mode TEXT NOT NULL DEFAULT 'normal'
        CHECK (recipient_mode IN ('normal', 'diagnostic')),
    PRIMARY KEY (msg_id, recipient)
);
CREATE INDEX IF NOT EXISTS idx_dmr_recipient
    ON debate_message_recipients(recipient);

-- Durable targeted-delivery queue.  Kept separate from
-- debate_message_recipients so pre-event clients that use positional
-- INSERT(msg_id, recipient, recipient_mode) remain wire-compatible.
CREATE TABLE IF NOT EXISTS debate_delivery_queue (
    msg_id       TEXT NOT NULL REFERENCES debate_messages(msg_id) ON DELETE CASCADE,
    recipient    TEXT NOT NULL,
    enqueued_at  TEXT NOT NULL,
    completed_at TEXT,
    PRIMARY KEY (msg_id, recipient)
);
CREATE INDEX IF NOT EXISTS idx_ddq_pending
    ON debate_delivery_queue(enqueued_at, msg_id)
    WHERE completed_at IS NULL;

CREATE TABLE IF NOT EXISTS debate_signal_state (
    session_id            TEXT NOT NULL,
    role                  TEXT NOT NULL,
    topic_id              TEXT NOT NULL REFERENCES debates(topic_id) ON DELETE CASCADE,
    last_processed_msg_id TEXT,
    last_processed_ts     TEXT,
    last_check_at         TEXT NOT NULL,
    PRIMARY KEY (session_id, role, topic_id)
);
CREATE INDEX IF NOT EXISTS idx_dss_role_topic
    ON debate_signal_state(role, topic_id);
CREATE INDEX IF NOT EXISTS idx_dss_last_check
    ON debate_signal_state(last_check_at);

-- Two-phase read receipt for debate/v1: signal_check records the greatest
-- cursor actually delivered by the server; signal_advance cannot acknowledge
-- a message that was never returned to that session.
CREATE TABLE IF NOT EXISTS debate_signal_deliveries (
    session_id              TEXT NOT NULL,
    role                    TEXT NOT NULL,
    topic_id                TEXT NOT NULL REFERENCES debates(topic_id) ON DELETE CASCADE,
    delivered_up_to_msg_id  TEXT NOT NULL,
    delivered_up_to_ts      TEXT NOT NULL,
    delivered_at            TEXT NOT NULL,
    PRIMARY KEY (session_id, role, topic_id)
);

-- ── v3.10: role/session lifecycle authority + dry-run wake audit ───────
-- roles_json remains the declared debate roster. debate_role_bindings is the
-- runtime authority for which concrete session owns or diagnoses a role now.

CREATE TABLE IF NOT EXISTS debate_role_bindings (
    topic_id        TEXT NOT NULL REFERENCES debates(topic_id) ON DELETE CASCADE,
    role            TEXT NOT NULL,
    session_id      TEXT NOT NULL,
    runtime         TEXT NOT NULL,
    state           TEXT NOT NULL
        CHECK (state IN ('active', 'retired', 'diagnostic')),
    generation      INTEGER NOT NULL,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    retired_at      TEXT,
    reason          TEXT NOT NULL,
    bound_by_role   TEXT,
    bound_by_msg_id TEXT,
    PRIMARY KEY (topic_id, role, session_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_drb_one_active
    ON debate_role_bindings(topic_id, role)
    WHERE state = 'active';
-- idx_drb_one_active_session is created by the migration below only after
-- legacy duplicate active bindings have been reconciled deterministically.
CREATE INDEX IF NOT EXISTS idx_drb_session
    ON debate_role_bindings(session_id);
CREATE INDEX IF NOT EXISTS idx_drb_topic_state
    ON debate_role_bindings(topic_id, state);

CREATE TABLE IF NOT EXISTS debate_wake_log (
    wake_id             TEXT PRIMARY KEY,
    trigger_msg_id      TEXT NOT NULL,
    topic_id            TEXT NOT NULL,
    recipient           TEXT NOT NULL,
    target_role         TEXT,
    target_session_id   TEXT,
    target_runtime      TEXT,
    binding_generation  INTEGER,
    action              TEXT NOT NULL,
    result              TEXT NOT NULL,
    schema_version      TEXT NOT NULL,
    details_json        TEXT,
    created_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dwl_trigger
    ON debate_wake_log(trigger_msg_id);
CREATE INDEX IF NOT EXISTS idx_dwl_topic_created
    ON debate_wake_log(topic_id, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_dwl_once
    ON debate_wake_log(trigger_msg_id, target_session_id, action)
    WHERE target_session_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS debate_worker_counters (
    topic_id          TEXT NOT NULL REFERENCES debates(topic_id) ON DELETE CASCADE,
    role              TEXT NOT NULL,
    parent_session_id TEXT NOT NULL,
    next_worker_n     INTEGER NOT NULL DEFAULT 1,
    updated_at        TEXT NOT NULL,
    PRIMARY KEY (topic_id, role, parent_session_id)
);

CREATE TABLE IF NOT EXISTS debate_worker_claims (
    topic_id             TEXT NOT NULL REFERENCES debates(topic_id) ON DELETE CASCADE,
    role                 TEXT NOT NULL,
    parent_session_id    TEXT NOT NULL,
    trigger_msg_id       TEXT NOT NULL REFERENCES debate_messages(msg_id) ON DELETE CASCADE,
    worker_session_id    TEXT NOT NULL,
    state                TEXT NOT NULL
        CHECK (state IN ('active', 'completed', 'retired')),
    parent_cursor_msg_id TEXT,
    parent_cursor_ts     TEXT,
    claimed_at           TEXT NOT NULL,
    heartbeat_at         TEXT NOT NULL,
    completed_at         TEXT,
    ack_msg_id           TEXT,
    details_json         TEXT,
    PRIMARY KEY (topic_id, role, parent_session_id, trigger_msg_id),
    UNIQUE (topic_id, role, worker_session_id)
);
CREATE INDEX IF NOT EXISTS idx_dwc_worker
    ON debate_worker_claims(worker_session_id);
CREATE INDEX IF NOT EXISTS idx_dwc_trigger
    ON debate_worker_claims(trigger_msg_id);
CREATE INDEX IF NOT EXISTS idx_dwc_state
    ON debate_worker_claims(topic_id, role, state);

CREATE TABLE IF NOT EXISTS debate_worker_recovery_log (
    recovery_id       TEXT PRIMARY KEY,
    topic_id          TEXT NOT NULL,
    role              TEXT NOT NULL,
    parent_session_id TEXT NOT NULL,
    worker_session_id TEXT NOT NULL,
    trigger_msg_id    TEXT NOT NULL,
    previous_state    TEXT NOT NULL,
    result            TEXT NOT NULL,
    details_json      TEXT,
    created_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dwrl_topic_created
    ON debate_worker_recovery_log(topic_id, created_at);

CREATE TABLE IF NOT EXISTS debate_message_claims (
    msg_id           TEXT NOT NULL REFERENCES debate_messages(msg_id) ON DELETE CASCADE,
    role             TEXT NOT NULL,
    owner_session_id TEXT,
    state            TEXT NOT NULL
        CHECK (state IN ('active', 'done')),
    claimed_at       TEXT NOT NULL,
    heartbeat_at     TEXT NOT NULL,
    completed_at     TEXT,
    ack_msg_id       TEXT,
    PRIMARY KEY (msg_id, role)
);
CREATE INDEX IF NOT EXISTS idx_dmc_owner
    ON debate_message_claims(owner_session_id);
CREATE INDEX IF NOT EXISTS idx_dmc_state
    ON debate_message_claims(state);

CREATE TABLE IF NOT EXISTS debate_message_claim_reclaim_log (
    reclaim_id       TEXT PRIMARY KEY,
    msg_id           TEXT NOT NULL,
    topic_id         TEXT NOT NULL,
    role             TEXT NOT NULL,
    owner_session_id TEXT,
    result           TEXT NOT NULL,
    details_json     TEXT,
    created_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dmcrl_topic_created
    ON debate_message_claim_reclaim_log(topic_id, created_at);

CREATE TABLE IF NOT EXISTS debate_worker_reap_log (
    reap_id           TEXT PRIMARY KEY,
    topic_id          TEXT NOT NULL,
    role              TEXT NOT NULL,
    parent_session_id TEXT NOT NULL,
    worker_session_id TEXT NOT NULL,
    trigger_msg_id    TEXT NOT NULL,
    result            TEXT NOT NULL,
    details_json      TEXT,
    created_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dwrl_topic_created
    ON debate_worker_reap_log(topic_id, created_at);

-- ── debate/v1 §7 deterministic server invariants (2026-07-22) ───────
-- This is protocol micro-state, intentionally separate from the durable
-- topic lifecycle in debates.state.  Every machine-relevant transition is
-- represented by typed columns/tables; prose is never parsed for control.

CREATE TABLE IF NOT EXISTS debate_protocol_state (
    topic_id              TEXT PRIMARY KEY REFERENCES debates(topic_id) ON DELETE CASCADE,
    protocol_version      TEXT NOT NULL CHECK (protocol_version = 'debate/v1'),
    phase                 TEXT NOT NULL CHECK (phase IN (
        'BLIND_CLAIM','DEBATE','ADJUDICATE','STALEMATE','ESCALATED','STOPPED')),
    round_no              INTEGER NOT NULL CHECK (round_no >= 1),
    max_rounds            INTEGER NOT NULL CHECK (max_rounds BETWEEN 1 AND 10),
    blind_barrier_state   TEXT NOT NULL CHECK (blind_barrier_state IN (
        'not_required','waiting','released')),
    blind_roles_json      TEXT NOT NULL,
    stalemate_reason      TEXT,
    transition_version    INTEGER NOT NULL CHECK (transition_version >= 1),
    phase_deadline_at     TEXT,
    phase_timeout_seconds INTEGER NOT NULL CHECK (phase_timeout_seconds >= 30),
    updated_at            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dps_phase_deadline
    ON debate_protocol_state(phase, phase_deadline_at);

CREATE TABLE IF NOT EXISTS debate_blind_commits (
    topic_id     TEXT NOT NULL REFERENCES debates(topic_id) ON DELETE CASCADE,
    role         TEXT NOT NULL,
    msg_id       TEXT NOT NULL UNIQUE REFERENCES debate_messages(msg_id) ON DELETE CASCADE,
    round_no     INTEGER NOT NULL CHECK (round_no >= 1),
    committed_at TEXT NOT NULL,
    released_at  TEXT,
    PRIMARY KEY (topic_id, role)
);
CREATE INDEX IF NOT EXISTS idx_dbc_unreleased
    ON debate_blind_commits(topic_id, released_at);

CREATE TABLE IF NOT EXISTS debate_judge_projections (
    projection_id  TEXT PRIMARY KEY,
    topic_id       TEXT NOT NULL REFERENCES debates(topic_id) ON DELETE CASCADE,
    round_no       INTEGER NOT NULL CHECK (round_no >= 1),
    order_key      TEXT NOT NULL CHECK (order_key IN ('AB','BA')),
    left_msg_id    TEXT NOT NULL REFERENCES debate_messages(msg_id) ON DELETE CASCADE,
    right_msg_id   TEXT NOT NULL REFERENCES debate_messages(msg_id) ON DELETE CASCADE,
    normalized_json TEXT NOT NULL,
    verdict_json   TEXT,
    judge_role     TEXT,
    created_at     TEXT NOT NULL,
    decided_at     TEXT,
    UNIQUE (topic_id, round_no, order_key)
);

CREATE TABLE IF NOT EXISTS debate_human_packets (
    topic_id            TEXT NOT NULL REFERENCES debates(topic_id) ON DELETE CASCADE,
    protocol_generation INTEGER NOT NULL CHECK (protocol_generation >= 1),
    msg_id              TEXT NOT NULL UNIQUE REFERENCES debate_messages(msg_id) ON DELETE CASCADE,
    state               TEXT NOT NULL CHECK (state IN ('open','resolved')),
    exact_human_action  TEXT NOT NULL,
    payload_json        TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    resolved_at         TEXT,
    PRIMARY KEY (topic_id, protocol_generation)
);
CREATE INDEX IF NOT EXISTS idx_dhp_open
    ON debate_human_packets(state, created_at);

CREATE TABLE IF NOT EXISTS debate_role_recovery_log (
    recovery_id    TEXT PRIMARY KEY,
    topic_id       TEXT NOT NULL REFERENCES debates(topic_id) ON DELETE CASCADE,
    role           TEXT NOT NULL,
    old_session_id TEXT,
    new_session_id TEXT NOT NULL,
    generation     INTEGER NOT NULL CHECK (generation >= 1),
    reason         TEXT NOT NULL,
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_drrl_topic_created
    ON debate_role_recovery_log(topic_id, created_at);

CREATE TABLE IF NOT EXISTS debate_scheduler_decisions (
    decision_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    decided_at      TEXT NOT NULL,
    interval_seconds REAL NOT NULL CHECK (interval_seconds >= 0),
    reason          TEXT NOT NULL CHECK (reason IN (
        'event_signal','eligible_backlog','capacity_wait','active_worker_lease',
        'persisted_retry_backoff','resource_blocked','idle_crash_replay_sweep')),
    queue_depth     INTEGER NOT NULL CHECK (queue_depth >= 0),
    live_workers    INTEGER NOT NULL CHECK (live_workers >= 0),
    resource_tier   TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_dsd_decided_at
    ON debate_scheduler_decisions(decided_at);

-- ── Memory Reflection (Phase 1) ─────────────────────────────────────────
-- Reviewable memory consolidation runs. Async job model with state machine
-- pending → running → completed/failed/canceled (C1). Lifecycle ops cancel +
-- archive (C2 enforced in MCP tool layer). Resource-based inputs (C3) via
-- reflection_inputs.input_type. Free-form instructions (C4). Structured
-- error taxonomy (C5). Per-run usage tracking (C6). Per-candidate granularity
-- preserved (C8 default); atomic-apply mode opt-in via reflection_candidates
-- batch decisions. Apply snapshots never mutate in-place (C9). Pagination
-- via archived_at index (C10). Per-run version pin for API evolution (C11).
-- Per-run model selection with BYOK/Ollama support (C13).
-- Source: notes 0ea75f2a + 5a4be019; corrections C1-C14 in entity
-- MemoryReflection_DreamsAlignmentCorrections (approved subset 2026-05-09).

CREATE TABLE IF NOT EXISTS reflection_runs (
    run_id              TEXT PRIMARY KEY,
    version             TEXT NOT NULL DEFAULT 'reflect_v1.0',
    status              TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','running','completed','failed','canceled')),
    model               TEXT NULL,
    instructions        TEXT NULL,
    error_type          TEXT NULL
        CHECK (error_type IS NULL OR error_type IN (
            'timeout','internal_error',
            'input_session_unavailable','input_too_large',
            'instructions_too_long','candidate_limit_exceeded'
        )),
    error_message       TEXT NULL,
    usage_json          TEXT NULL,
    created_by          TEXT NOT NULL DEFAULT 'system',
    created_at          TEXT NOT NULL,
    started_at          TEXT NULL,
    ended_at            TEXT NULL,
    archived_at         TEXT NULL
);
CREATE INDEX IF NOT EXISTS idx_reflection_runs_status_created
    ON reflection_runs(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_reflection_runs_archived
    ON reflection_runs(archived_at, created_at DESC);

CREATE TABLE IF NOT EXISTS reflection_inputs (
    input_id            TEXT PRIMARY KEY,
    run_id              TEXT NOT NULL REFERENCES reflection_runs(run_id) ON DELETE CASCADE,
    input_type          TEXT NOT NULL
        CHECK (input_type IN ('tasks','sessions','entities','notes')),
    input_ref_json      TEXT NOT NULL,
    created_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reflection_inputs_run
    ON reflection_inputs(run_id, input_type);

CREATE TABLE IF NOT EXISTS reflection_candidates (
    candidate_id        TEXT PRIMARY KEY,
    run_id              TEXT NOT NULL REFERENCES reflection_runs(run_id) ON DELETE CASCADE,
    candidate_type      TEXT NOT NULL,
    suggested_action    TEXT NOT NULL,
    target_kind         TEXT NOT NULL
        CHECK (target_kind IN ('task','entity','note','observation')),
    target_ref          TEXT NOT NULL,
    evidence_json       TEXT NOT NULL,
    proposed_state_json TEXT NULL,
    confidence          REAL NULL,
    human_decision      TEXT NULL
        CHECK (human_decision IS NULL OR human_decision IN ('accept','reject','defer')),
    decided_by          TEXT NULL,
    decided_at          TEXT NULL,
    created_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reflection_candidates_run
    ON reflection_candidates(run_id, candidate_type);
CREATE INDEX IF NOT EXISTS idx_reflection_candidates_decision
    ON reflection_candidates(run_id, human_decision);
CREATE INDEX IF NOT EXISTS idx_reflection_candidates_target
    ON reflection_candidates(target_kind, target_ref);

CREATE TABLE IF NOT EXISTS reflection_apply_snapshots (
    snapshot_id         TEXT PRIMARY KEY,
    run_id              TEXT NOT NULL REFERENCES reflection_runs(run_id) ON DELETE CASCADE,
    candidate_id        TEXT NOT NULL REFERENCES reflection_candidates(candidate_id) ON DELETE CASCADE,
    target_kind         TEXT NOT NULL,
    target_ref          TEXT NOT NULL,
    before_state_json   TEXT NOT NULL,
    after_state_json    TEXT NOT NULL,
    applied_by          TEXT NOT NULL,
    applied_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reflection_apply_run
    ON reflection_apply_snapshots(run_id, applied_at);
CREATE INDEX IF NOT EXISTS idx_reflection_apply_target
    ON reflection_apply_snapshots(target_kind, target_ref, applied_at DESC);
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
        "SELECT 1 WHERE NOT EXISTS ("
        "SELECT 1 FROM task_field_versions versions "
        "WHERE NOT EXISTS ("
        "SELECT 1 FROM tasks WHERE tasks.id = versions.task_id"
        ") LIMIT 1)",
        "DELETE FROM task_field_versions "
        "WHERE NOT EXISTS ("
        "SELECT 1 FROM tasks WHERE tasks.id = task_field_versions.task_id"
        ")",
        "prune orphan task_field_versions rows (v3.13.2)",
    ),
    (
        # A future-dated packed HLC outranks every legitimate write forever,
        # and _clamp_field_version_clock only clamps in memory for one merge,
        # so the row stays poisoned on disk and is re-exported to every peer.
        # 65536 == 1 << _HLC_COUNTER_BITS; the modulo keeps the counter, so
        # intra-machine ordering survives. The +5s tolerance mirrors
        # _clamp_field_version_clock exactly -- one rule, no threshold drift.
        "SELECT 1 WHERE NOT EXISTS ("
        "SELECT 1 FROM task_field_versions "
        "WHERE updated_order > "
        "((CAST(strftime('%s','now') AS INTEGER) + 5) * 1000) * 65536)",
        "UPDATE task_field_versions "
        "SET updated_order = (CAST(strftime('%s','now') AS INTEGER) * 1000) * 65536 "
        "+ (updated_order % 65536) "
        "WHERE updated_order > "
        "((CAST(strftime('%s','now') AS INTEGER) + 5) * 1000) * 65536",
        "clamp future task_field_versions.updated_order clocks (v3.13.3)",
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
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='task_attachments'",
        "CREATE TABLE task_attachments ("
        "attachment_id TEXT PRIMARY KEY, "
        "task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE, "
        "file_name TEXT NOT NULL, stored_relpath TEXT NOT NULL UNIQUE, "
        "media_type TEXT DEFAULT NULL, file_size INTEGER NOT NULL DEFAULT 0, "
        "status TEXT NOT NULL DEFAULT 'active', created_at TEXT NOT NULL, updated_at TEXT NOT NULL)",
        "task_attachments table (v7.0.0)",
    ),
    (
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_task_attachments_task'",
        "CREATE INDEX idx_task_attachments_task ON task_attachments(task_id, status, created_at)",
        "idx_task_attachments_task index (v7.0.0)",
    ),
    (
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='context_chunks'",
        "CREATE TABLE context_chunks ("
        "chunk_id TEXT PRIMARY KEY, session_id TEXT NULL, entity_id TEXT NULL, "
        "source_type TEXT NOT NULL, source_ref TEXT NOT NULL, source_hash TEXT NOT NULL, "
        "title TEXT NULL, body TEXT NOT NULL, language TEXT DEFAULT NULL, "
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
        "hit_count INTEGER NOT NULL DEFAULT 1, "
        "status TEXT NOT NULL DEFAULT 'candidate', "
        "promoted_to_fact_id TEXT NULL, "
        "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)",
        "lazy_claims table (v3.1.0)",
    ),
    (
        "SELECT 1 FROM pragma_table_info('lazy_claims') WHERE name='hit_count'",
        "ALTER TABLE lazy_claims ADD COLUMN hit_count INTEGER NOT NULL DEFAULT 1",
        "lazy_claims.hit_count column (evidence accumulation)",
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
    (
        "SELECT 1 FROM pragma_table_info('task_field_versions') WHERE name='old_value'",
        "ALTER TABLE task_field_versions ADD COLUMN old_value TEXT DEFAULT NULL",
        "task_field_versions.old_value column (v3.2.0)",
    ),
    (
        "SELECT 1 FROM pragma_table_info('task_field_versions') WHERE name='new_value'",
        "ALTER TABLE task_field_versions ADD COLUMN new_value TEXT DEFAULT NULL",
        "task_field_versions.new_value column (v3.2.0)",
    ),
    (
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_relations_to'",
        "CREATE INDEX IF NOT EXISTS idx_relations_to ON relations(to_id)",
        "idx_relations_to index for reverse lookups (HIGH-1)",
    ),
    (
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_enrichment_runs_chunk'",
        "CREATE INDEX IF NOT EXISTS idx_enrichment_runs_chunk ON enrichment_runs(chunk_id)",
        "idx_enrichment_runs_chunk index (HIGH-3)",
    ),
    (
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_cursors'",
        "CREATE TABLE memory_cursors ("
        "machine_id TEXT PRIMARY KEY, "
        "last_clock INTEGER NOT NULL DEFAULT 0, "
        "updated_at TEXT NOT NULL)",
        "memory_cursors table (v3.4.0)",
    ),
    (
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_events'",
        "CREATE TABLE memory_events ("
        "event_id TEXT PRIMARY KEY, "
        "event_type TEXT NOT NULL, "
        "aggregate_kind TEXT NOT NULL, "
        "aggregate_id TEXT NOT NULL, "
        "field_name TEXT NULL, "
        "actor_type TEXT NOT NULL DEFAULT 'system', "
        "actor_id TEXT NULL, "
        "machine_id TEXT NOT NULL, "
        "tool_name TEXT NOT NULL, "
        "logical_clock INTEGER NOT NULL, "
        "event_ts TEXT NOT NULL, "
        "old_value TEXT NULL, "
        "new_value TEXT NULL, "
        "payload_json TEXT NULL, "
        "parent_event_id TEXT NULL, "
        "source_kind TEXT NULL, "
        "source_ref TEXT NULL, "
        "source_excerpt TEXT NULL, "
        "source_start INTEGER NULL, "
        "source_end INTEGER NULL)",
        "memory_events table (v3.4.0)",
    ),
    (
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_memory_events_clock'",
        "CREATE UNIQUE INDEX idx_memory_events_clock ON memory_events(machine_id, logical_clock)",
        "idx_memory_events_clock index (v3.4.0)",
    ),
    (
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_memory_events_aggregate'",
        "CREATE INDEX idx_memory_events_aggregate ON memory_events(aggregate_kind, aggregate_id, logical_clock DESC)",
        "idx_memory_events_aggregate index (v3.4.0)",
    ),
    (
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='provenance_links'",
        "CREATE TABLE provenance_links ("
        "provenance_id TEXT PRIMARY KEY, "
        "subject_kind TEXT NOT NULL, "
        "subject_ref TEXT NOT NULL, "
        "source_kind TEXT NOT NULL, "
        "source_ref TEXT NOT NULL, "
        "span_start INTEGER NULL, "
        "span_end INTEGER NULL, "
        "excerpt TEXT NULL, "
        "confidence REAL NOT NULL DEFAULT 1.0, "
        "created_at TEXT NOT NULL)",
        "provenance_links table (v3.4.0)",
    ),
    (
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_provenance_subject'",
        "CREATE INDEX idx_provenance_subject ON provenance_links(subject_kind, subject_ref)",
        "idx_provenance_subject index (v3.4.0)",
    ),
    (
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='knowledge_links'",
        "CREATE TABLE knowledge_links ("
        "link_id TEXT PRIMARY KEY, "
        "subject_kind TEXT NOT NULL, "
        "subject_ref TEXT NOT NULL, "
        "relation_type TEXT NOT NULL, "
        "object_kind TEXT NOT NULL, "
        "object_ref TEXT NOT NULL, "
        "rationale TEXT NULL, "
        "created_at TEXT NOT NULL, "
        "active INTEGER NOT NULL DEFAULT 1)",
        "knowledge_links table (v3.4.0)",
    ),
    (
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_knowledge_links_subject'",
        "CREATE INDEX idx_knowledge_links_subject ON knowledge_links(subject_kind, subject_ref, relation_type, active)",
        "idx_knowledge_links_subject index (v3.4.0)",
    ),
    (
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_knowledge_links_object'",
        "CREATE INDEX idx_knowledge_links_object ON knowledge_links(object_kind, object_ref, relation_type, active)",
        "idx_knowledge_links_object index (v3.4.0)",
    ),
    (
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_audit_issues'",
        "CREATE TABLE memory_audit_issues ("
        "issue_id TEXT PRIMARY KEY, "
        "issue_type TEXT NOT NULL, "
        "severity TEXT NOT NULL, "
        "subject_kind TEXT NOT NULL, "
        "subject_ref TEXT NOT NULL, "
        "details_json TEXT NOT NULL, "
        "status TEXT NOT NULL DEFAULT 'open', "
        "first_detected_at TEXT NOT NULL, "
        "last_detected_at TEXT NOT NULL, "
        "resolved_at TEXT NULL)",
        "memory_audit_issues table (v3.4.0)",
    ),
    (
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_memory_audit_status'",
        "CREATE INDEX idx_memory_audit_status ON memory_audit_issues(status, severity, issue_type)",
        "idx_memory_audit_status index (v3.4.0)",
    ),
    (
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_artifacts'",
        "CREATE TABLE memory_artifacts ("
        "artifact_id TEXT PRIMARY KEY, "
        "artifact_key TEXT NOT NULL UNIQUE, "
        "artifact_kind TEXT NOT NULL, "
        "scope_kind TEXT NOT NULL, "
        "scope_ref TEXT NOT NULL, "
        "title TEXT NULL, "
        "body TEXT NOT NULL, "
        "confidence REAL NOT NULL DEFAULT 1.0, "
        "status TEXT NOT NULL DEFAULT 'active', "
        "valid_from TEXT NULL, "
        "valid_to TEXT NULL, "
        "source_event_id TEXT NULL, "
        "created_at TEXT NOT NULL, "
        "updated_at TEXT NOT NULL)",
        "memory_artifacts table (v3.5.0)",
    ),
    (
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_memory_artifacts_scope'",
        "CREATE INDEX idx_memory_artifacts_scope ON memory_artifacts(artifact_kind, scope_kind, scope_ref, status)",
        "idx_memory_artifacts_scope index (v3.5.0)",
    ),
    (
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_conflicts'",
        "CREATE TABLE memory_conflicts ("
        "conflict_id TEXT PRIMARY KEY, "
        "conflict_key TEXT NOT NULL UNIQUE, "
        "aggregate_kind TEXT NOT NULL, "
        "aggregate_id TEXT NOT NULL, "
        "field_name TEXT NULL, "
        "local_value TEXT NULL, "
        "remote_value TEXT NULL, "
        "local_updated_at TEXT NULL, "
        "remote_updated_at TEXT NULL, "
        "local_updated_order INTEGER NOT NULL DEFAULT 0, "
        "remote_updated_order INTEGER NOT NULL DEFAULT 0, "
        "local_source_event_id TEXT NULL, "
        "remote_source_event_id TEXT NULL, "
        "winner TEXT NOT NULL, "
        "status TEXT NOT NULL DEFAULT 'open', "
        "rationale TEXT NULL, "
        "created_at TEXT NOT NULL, "
        "updated_at TEXT NOT NULL, "
        "resolved_at TEXT NULL)",
        "memory_conflicts table (v3.5.0)",
    ),
    (
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_memory_conflicts_status'",
        "CREATE INDEX idx_memory_conflicts_status ON memory_conflicts(status, aggregate_kind, aggregate_id, field_name)",
        "idx_memory_conflicts_status index (v3.5.0)",
    ),
    (
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_audit_state'",
        "CREATE TABLE memory_audit_state ("
        "runner_name TEXT PRIMARY KEY, "
        "cadence_minutes INTEGER NOT NULL DEFAULT 60, "
        "last_started_at TEXT NULL, "
        "last_finished_at TEXT NULL, "
        "next_run_after TEXT NULL, "
        "last_status TEXT NOT NULL DEFAULT 'never', "
        "last_summary_json TEXT NULL, "
        "updated_at TEXT NOT NULL)",
        "memory_audit_state table (v3.5.0)",
    ),
    (
        "SELECT 1 FROM pragma_table_info('task_field_versions') WHERE name='updated_order'",
        "ALTER TABLE task_field_versions ADD COLUMN updated_order INTEGER NOT NULL DEFAULT 0",
        "task_field_versions.updated_order column (v3.4.0)",
    ),
    (
        "SELECT 1 FROM pragma_table_info('task_field_versions') WHERE name='source_event_id'",
        "ALTER TABLE task_field_versions ADD COLUMN source_event_id TEXT DEFAULT NULL",
        "task_field_versions.source_event_id column (v3.4.0)",
    ),
    (
        "SELECT 1 FROM pragma_table_info('claim_evidence') WHERE name='source_start'",
        "ALTER TABLE claim_evidence ADD COLUMN source_start INTEGER DEFAULT NULL",
        "claim_evidence.source_start column (v3.4.0)",
    ),
    (
        "SELECT 1 FROM pragma_table_info('claim_evidence') WHERE name='source_end'",
        "ALTER TABLE claim_evidence ADD COLUMN source_end INTEGER DEFAULT NULL",
        "claim_evidence.source_end column (v3.4.0)",
    ),
    (
        "SELECT 1 FROM pragma_table_info('canonical_facts') WHERE name='valid_from'",
        "ALTER TABLE canonical_facts ADD COLUMN valid_from TEXT DEFAULT NULL",
        "canonical_facts.valid_from column (v3.4.0)",
    ),
    (
        "SELECT 1 FROM pragma_table_info('canonical_facts') WHERE name='valid_to'",
        "ALTER TABLE canonical_facts ADD COLUMN valid_to TEXT DEFAULT NULL",
        "canonical_facts.valid_to column (v3.4.0)",
    ),
    (
        "SELECT 1 FROM pragma_table_info('canonical_facts') WHERE name='superseded_by_fact_id'",
        "ALTER TABLE canonical_facts ADD COLUMN superseded_by_fact_id TEXT DEFAULT NULL",
        "canonical_facts.superseded_by_fact_id column (v3.4.0)",
    ),
    (
        "SELECT 1 FROM pragma_table_info('canonical_facts') WHERE name='contradiction_count'",
        "ALTER TABLE canonical_facts ADD COLUMN contradiction_count INTEGER NOT NULL DEFAULT 0",
        "canonical_facts.contradiction_count column (v3.4.0)",
    ),
    (
        "SELECT 1 FROM pragma_table_info('context_packs') WHERE name='contract_version'",
        "ALTER TABLE context_packs ADD COLUMN contract_version TEXT NOT NULL DEFAULT 'legacy'",
        "context_packs.contract_version column (v3.4.0)",
    ),
    (
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='premium_gate_audit'",
        """
        CREATE TABLE premium_gate_audit (
            audit_id            TEXT PRIMARY KEY,
            feature_id          TEXT NOT NULL,
            decision            TEXT NOT NULL,
            reason              TEXT NOT NULL,
            entitlement_id      TEXT DEFAULT NULL,
            customer_id         TEXT DEFAULT NULL,
            server_name         TEXT DEFAULT NULL,
            tool_name           TEXT DEFAULT NULL,
            actor_id            TEXT DEFAULT NULL,
            machine_id          TEXT NOT NULL,
            checked_at          TEXT NOT NULL,
            payload_json        TEXT DEFAULT NULL
        );
        CREATE INDEX idx_premium_gate_audit_feature
            ON premium_gate_audit(feature_id, checked_at DESC);
        """,
        "premium_gate_audit table (v3.5.0)",
    ),
    (
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='premium_revocations'",
        """
        CREATE TABLE premium_revocations (
            revocation_id       TEXT PRIMARY KEY,
            entitlement_id      TEXT NOT NULL,
            feature_id          TEXT DEFAULT NULL,
            customer_id         TEXT DEFAULT NULL,
            reason              TEXT DEFAULT NULL,
            revoked_at          TEXT NOT NULL,
            revoked_by          TEXT DEFAULT NULL,
            active              INTEGER NOT NULL DEFAULT 1
        );
        CREATE INDEX idx_premium_revocations_lookup
            ON premium_revocations(entitlement_id, feature_id, active);
        """,
        "premium_revocations table (v3.5.0)",
    ),
    (
        "SELECT 1 FROM pragma_table_info('debate_message_recipients') "
        "WHERE name='recipient_mode'",
        "ALTER TABLE debate_message_recipients "
        "ADD COLUMN recipient_mode TEXT NOT NULL DEFAULT 'normal'",
        "debate_message_recipients.recipient_mode column (v3.10)",
    ),
    (
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='debate_delivery_queue'",
        "CREATE TABLE debate_delivery_queue ("
        "msg_id TEXT NOT NULL REFERENCES debate_messages(msg_id) ON DELETE CASCADE, "
        "recipient TEXT NOT NULL, enqueued_at TEXT NOT NULL, completed_at TEXT, "
        "PRIMARY KEY (msg_id, recipient)); "
        "CREATE INDEX idx_ddq_pending "
        "ON debate_delivery_queue(enqueued_at, msg_id) "
        "WHERE completed_at IS NULL",
        "debate_delivery_queue table and pending index (event delivery)",
    ),
    (
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_dmr_mode'",
        "CREATE INDEX idx_dmr_mode ON debate_message_recipients(recipient_mode)",
        "idx_dmr_mode index (v3.10)",
    ),
    (
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='debate_role_bindings'",
        """
        CREATE TABLE debate_role_bindings (
            topic_id        TEXT NOT NULL REFERENCES debates(topic_id) ON DELETE CASCADE,
            role            TEXT NOT NULL,
            session_id      TEXT NOT NULL,
            runtime         TEXT NOT NULL,
            state           TEXT NOT NULL
                CHECK (state IN ('active', 'retired', 'diagnostic')),
            generation      INTEGER NOT NULL,
            created_at      TEXT NOT NULL,
            updated_at      TEXT NOT NULL,
            retired_at      TEXT,
            reason          TEXT NOT NULL,
            bound_by_role   TEXT,
            bound_by_msg_id TEXT,
            PRIMARY KEY (topic_id, role, session_id)
        );
        CREATE UNIQUE INDEX idx_drb_one_active
            ON debate_role_bindings(topic_id, role)
            WHERE state = 'active';
        CREATE INDEX idx_drb_session
            ON debate_role_bindings(session_id);
        CREATE INDEX idx_drb_topic_state
            ON debate_role_bindings(topic_id, state);
        """,
        "debate_role_bindings table and indexes (v3.10)",
    ),
    (
        "SELECT 1 FROM sqlite_master WHERE type='index' "
        "AND name='idx_drb_one_active_session'",
        """
        WITH ranked_active_bindings AS (
            SELECT rowid AS binding_rowid,
                   ROW_NUMBER() OVER (
                       PARTITION BY topic_id, session_id
                       ORDER BY updated_at DESC, created_at DESC,
                                generation DESC, role ASC
                   ) AS keep_rank
            FROM debate_role_bindings
            WHERE state = 'active'
        )
        UPDATE debate_role_bindings
        SET state = 'retired',
            updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
            retired_at = COALESCE(
                retired_at,
                strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            ),
            reason = reason ||
                ' | migration: retired legacy duplicate active session, '
                || 'kept most-recent binding'
        WHERE rowid IN (
            SELECT binding_rowid
            FROM ranked_active_bindings
            WHERE keep_rank > 1
        );
        CREATE UNIQUE INDEX idx_drb_one_active_session
            ON debate_role_bindings(topic_id, session_id)
            WHERE state = 'active';
        """,
        "reconcile legacy duplicate sessions and enforce one active role",
    ),
    (
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='debate_wake_log'",
        """
        CREATE TABLE debate_wake_log (
            wake_id             TEXT PRIMARY KEY,
            trigger_msg_id      TEXT NOT NULL,
            topic_id            TEXT NOT NULL,
            recipient           TEXT NOT NULL,
            target_role         TEXT,
            target_session_id   TEXT,
            target_runtime      TEXT,
            binding_generation  INTEGER,
            action              TEXT NOT NULL,
            result              TEXT NOT NULL,
            schema_version      TEXT NOT NULL,
            details_json        TEXT,
            created_at          TEXT NOT NULL
        );
        CREATE INDEX idx_dwl_trigger
            ON debate_wake_log(trigger_msg_id);
        CREATE INDEX idx_dwl_topic_created
            ON debate_wake_log(topic_id, created_at);
        CREATE UNIQUE INDEX idx_dwl_once
            ON debate_wake_log(trigger_msg_id, target_session_id, action)
            WHERE target_session_id IS NOT NULL;
        """,
        "debate_wake_log table and indexes (v3.10)",
    ),
    (
        "SELECT 1 FROM pragma_table_info('debate_messages') WHERE name='standing'",
        "ALTER TABLE debate_messages "
        "ADD COLUMN standing INTEGER DEFAULT NULL "
        "CHECK (standing IS NULL OR standing IN (0, 1))",
        "debate_messages.standing column (v3.11)",
    ),
    (
        "SELECT 1 FROM pragma_table_info('debate_messages') WHERE name='vehicle'",
        "ALTER TABLE debate_messages "
        "ADD COLUMN vehicle TEXT DEFAULT NULL "
        "CHECK (vehicle IS NULL "
        "OR vehicle IN ('analysis', 'review', 'implementation'))",
        "debate_messages.vehicle column (v3.12 — vehicle-tagging router)",
    ),
    (
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='debate_worker_counters'",
        """
        CREATE TABLE debate_worker_counters (
            topic_id          TEXT NOT NULL REFERENCES debates(topic_id) ON DELETE CASCADE,
            role              TEXT NOT NULL,
            parent_session_id TEXT NOT NULL,
            next_worker_n     INTEGER NOT NULL DEFAULT 1,
            updated_at        TEXT NOT NULL,
            PRIMARY KEY (topic_id, role, parent_session_id)
        );
        """,
        "debate_worker_counters table (v3.11)",
    ),
    (
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='debate_worker_claims'",
        """
        CREATE TABLE debate_worker_claims (
            topic_id             TEXT NOT NULL REFERENCES debates(topic_id) ON DELETE CASCADE,
            role                 TEXT NOT NULL,
            parent_session_id    TEXT NOT NULL,
            trigger_msg_id       TEXT NOT NULL REFERENCES debate_messages(msg_id) ON DELETE CASCADE,
            worker_session_id    TEXT NOT NULL,
            state                TEXT NOT NULL
                CHECK (state IN ('active', 'completed', 'retired')),
            parent_cursor_msg_id TEXT,
            parent_cursor_ts     TEXT,
            claimed_at           TEXT NOT NULL,
            heartbeat_at         TEXT NOT NULL,
            completed_at         TEXT,
            ack_msg_id           TEXT,
            details_json         TEXT,
            PRIMARY KEY (topic_id, role, parent_session_id, trigger_msg_id),
            UNIQUE (topic_id, role, worker_session_id)
        );
        CREATE INDEX idx_dwc_worker
            ON debate_worker_claims(worker_session_id);
        CREATE INDEX idx_dwc_trigger
            ON debate_worker_claims(trigger_msg_id);
        CREATE INDEX idx_dwc_state
            ON debate_worker_claims(topic_id, role, state);
        """,
        "debate_worker_claims table and indexes (v3.11)",
    ),
    (
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='debate_message_claims'",
        """
        CREATE TABLE debate_message_claims (
            msg_id           TEXT NOT NULL REFERENCES debate_messages(msg_id) ON DELETE CASCADE,
            role             TEXT NOT NULL,
            owner_session_id TEXT,
            state            TEXT NOT NULL
                CHECK (state IN ('active', 'done')),
            claimed_at       TEXT NOT NULL,
            heartbeat_at     TEXT NOT NULL,
            completed_at     TEXT,
            ack_msg_id       TEXT,
            PRIMARY KEY (msg_id, role)
        );
        CREATE INDEX idx_dmc_owner
            ON debate_message_claims(owner_session_id);
        CREATE INDEX idx_dmc_state
            ON debate_message_claims(state);
        """,
        "debate_message_claims table and indexes (v3.11)",
    ),
    (
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='debate_message_claim_reclaim_log'",
        """
        CREATE TABLE debate_message_claim_reclaim_log (
            reclaim_id       TEXT PRIMARY KEY,
            msg_id           TEXT NOT NULL,
            topic_id         TEXT NOT NULL,
            role             TEXT NOT NULL,
            owner_session_id TEXT,
            result           TEXT NOT NULL,
            details_json     TEXT,
            created_at       TEXT NOT NULL
        );
        CREATE INDEX idx_dmcrl_topic_created
            ON debate_message_claim_reclaim_log(topic_id, created_at);
        """,
        "debate_message_claim_reclaim_log table and index (v3.11.2)",
    ),
    (
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='debate_worker_reap_log'",
        """
        CREATE TABLE debate_worker_reap_log (
            reap_id           TEXT PRIMARY KEY,
            topic_id          TEXT NOT NULL,
            role              TEXT NOT NULL,
            parent_session_id TEXT NOT NULL,
            worker_session_id TEXT NOT NULL,
            trigger_msg_id    TEXT NOT NULL,
            result            TEXT NOT NULL,
            details_json      TEXT,
            created_at        TEXT NOT NULL
        );
        CREATE INDEX idx_dwrl_topic_created
            ON debate_worker_reap_log(topic_id, created_at);
        """,
        "debate_worker_reap_log table and indexes (v3.11)",
    ),
    (
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='premium_artifact_manifests'",
        """
        CREATE TABLE premium_artifact_manifests (
            manifest_id          TEXT PRIMARY KEY,
            extension_name       TEXT NOT NULL,
            entrypoint_ref       TEXT NOT NULL,
            entrypoint_sha256    TEXT NOT NULL,
            contract_version     TEXT NOT NULL,
            build_id             TEXT DEFAULT NULL,
            customer_id          TEXT DEFAULT NULL,
            protection_phase     INTEGER NOT NULL DEFAULT 1,
            minimum_host_version TEXT DEFAULT NULL,
            maximum_host_version TEXT DEFAULT NULL,
            issued_at            TEXT DEFAULT NULL,
            expires_at           TEXT DEFAULT NULL,
            verified_at          TEXT NOT NULL,
            payload_json         TEXT DEFAULT NULL
        );
        CREATE INDEX idx_premium_artifact_manifests_entrypoint
            ON premium_artifact_manifests(entrypoint_ref, verified_at DESC);
        """,
        "premium_artifact_manifests table (v3.6.0)",
    ),
    (
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='premium_control_plane_cache'",
        """
        CREATE TABLE premium_control_plane_cache (
            scope_key            TEXT PRIMARY KEY,
            policy_id            TEXT NOT NULL,
            source_ref           TEXT DEFAULT NULL,
            fetched_at           TEXT NOT NULL,
            expires_at           TEXT DEFAULT NULL,
            cache_deadline       TEXT DEFAULT NULL,
            payload_json         TEXT NOT NULL
        );
        CREATE INDEX idx_premium_control_plane_cache_deadline
            ON premium_control_plane_cache(cache_deadline, fetched_at DESC);
        """,
        "premium_control_plane_cache table (v3.6.0)",
    ),
    (
        "SELECT 1 FROM sqlite_master WHERE type='trigger' AND name='memory_fts_obs_ai'",
        """
        CREATE TRIGGER IF NOT EXISTS memory_fts_obs_ai AFTER INSERT ON observations BEGIN
            DELETE FROM memory_fts WHERE rowid = new.entity_id;
            INSERT INTO memory_fts(rowid, name, entity_type, observations_text)
            SELECT e.id, e.name, e.entity_type,
                   COALESCE(GROUP_CONCAT(o.content, ' '), '')
            FROM entities e LEFT JOIN observations o ON o.entity_id = e.id
            WHERE e.id = new.entity_id GROUP BY e.id;
        END;
        CREATE TRIGGER IF NOT EXISTS memory_fts_obs_ad AFTER DELETE ON observations BEGIN
            DELETE FROM memory_fts WHERE rowid = old.entity_id;
            INSERT INTO memory_fts(rowid, name, entity_type, observations_text)
            SELECT e.id, e.name, e.entity_type,
                   COALESCE(GROUP_CONCAT(o.content, ' '), '')
            FROM entities e LEFT JOIN observations o ON o.entity_id = e.id
            WHERE e.id = old.entity_id GROUP BY e.id;
        END;
        DROP TRIGGER IF EXISTS memory_fts_au;
        CREATE TRIGGER memory_fts_au AFTER UPDATE ON entities BEGIN
            DELETE FROM memory_fts WHERE rowid = old.rowid;
            INSERT INTO memory_fts(rowid, name, entity_type, observations_text)
            SELECT new.rowid, new.name, new.entity_type,
                   COALESCE(GROUP_CONCAT(o.content, ' '), '')
            FROM (SELECT 1) LEFT JOIN observations o ON o.entity_id = new.id;
        END
        """,
        "FTS observation triggers + entity trigger update (FT-01/FT-02 fix)",
    ),
    (
        # Tier-A #5 typed predicates vocabulary seed (canonical_facts as
        # data-not-code per coordinator advice 2026-05-09). Seven predicates
        # cover the structural relations the export/import bridge produces
        # (mentions / references / depends_on / related_to / supersedes /
        # implements / contradicts) plus the GBrain-style typed edges
        # (works_at / attended / invested_in / founded / advises). Future
        # code reads vocabulary from DB, not from source.
        "SELECT 1 FROM canonical_facts WHERE fact_scope = 'predicate_vocabulary' LIMIT 1",
        """
        INSERT OR IGNORE INTO canonical_facts (
            fact_id, subject, predicate, object_text, object_type,
            fact_scope, provenance_summary, confidence, validation_mode,
            valid_from, created_at, updated_at
        ) VALUES
        ('pred-mentions',    'predicate', 'name', 'mentions',    'text', 'predicate_vocabulary', 'seed-2026-05-09', 1.0, 'multi_evidence', '2026-05-09T00:00:00+00:00', '2026-05-09T00:00:00+00:00', '2026-05-09T00:00:00+00:00'),
        ('pred-references',  'predicate', 'name', 'references',  'text', 'predicate_vocabulary', 'seed-2026-05-09', 1.0, 'multi_evidence', '2026-05-09T00:00:00+00:00', '2026-05-09T00:00:00+00:00', '2026-05-09T00:00:00+00:00'),
        ('pred-depends-on',  'predicate', 'name', 'depends_on',  'text', 'predicate_vocabulary', 'seed-2026-05-09', 1.0, 'multi_evidence', '2026-05-09T00:00:00+00:00', '2026-05-09T00:00:00+00:00', '2026-05-09T00:00:00+00:00'),
        ('pred-related-to',  'predicate', 'name', 'related_to',  'text', 'predicate_vocabulary', 'seed-2026-05-09', 1.0, 'multi_evidence', '2026-05-09T00:00:00+00:00', '2026-05-09T00:00:00+00:00', '2026-05-09T00:00:00+00:00'),
        ('pred-supersedes',  'predicate', 'name', 'supersedes',  'text', 'predicate_vocabulary', 'seed-2026-05-09', 1.0, 'multi_evidence', '2026-05-09T00:00:00+00:00', '2026-05-09T00:00:00+00:00', '2026-05-09T00:00:00+00:00'),
        ('pred-implements',  'predicate', 'name', 'implements',  'text', 'predicate_vocabulary', 'seed-2026-05-09', 1.0, 'multi_evidence', '2026-05-09T00:00:00+00:00', '2026-05-09T00:00:00+00:00', '2026-05-09T00:00:00+00:00'),
        ('pred-contradicts', 'predicate', 'name', 'contradicts', 'text', 'predicate_vocabulary', 'seed-2026-05-09', 1.0, 'multi_evidence', '2026-05-09T00:00:00+00:00', '2026-05-09T00:00:00+00:00', '2026-05-09T00:00:00+00:00'),
        ('pred-works-at',    'predicate', 'name', 'works_at',    'text', 'predicate_vocabulary', 'seed-2026-05-09', 1.0, 'multi_evidence', '2026-05-09T00:00:00+00:00', '2026-05-09T00:00:00+00:00', '2026-05-09T00:00:00+00:00'),
        ('pred-attended',    'predicate', 'name', 'attended',    'text', 'predicate_vocabulary', 'seed-2026-05-09', 1.0, 'multi_evidence', '2026-05-09T00:00:00+00:00', '2026-05-09T00:00:00+00:00', '2026-05-09T00:00:00+00:00'),
        ('pred-invested-in', 'predicate', 'name', 'invested_in', 'text', 'predicate_vocabulary', 'seed-2026-05-09', 1.0, 'multi_evidence', '2026-05-09T00:00:00+00:00', '2026-05-09T00:00:00+00:00', '2026-05-09T00:00:00+00:00'),
        ('pred-founded',     'predicate', 'name', 'founded',     'text', 'predicate_vocabulary', 'seed-2026-05-09', 1.0, 'multi_evidence', '2026-05-09T00:00:00+00:00', '2026-05-09T00:00:00+00:00', '2026-05-09T00:00:00+00:00'),
        ('pred-advises',     'predicate', 'name', 'advises',     'text', 'predicate_vocabulary', 'seed-2026-05-09', 1.0, 'multi_evidence', '2026-05-09T00:00:00+00:00', '2026-05-09T00:00:00+00:00', '2026-05-09T00:00:00+00:00')
        """,
        "Tier-A #5 typed predicate vocabulary seed (12 predicates)",
    ),
    (
        # Companion provenance_links for the vocabulary seed. memory_audit
        # backfill flags any canonical_fact lacking a provenance_links row,
        # so each vocabulary fact gets a self-referential 'seed' link.
        # Idempotent — keyed off the existence of pred-mentions provenance.
        "SELECT 1 FROM provenance_links WHERE subject_kind = 'fact' "
        "AND subject_ref = 'pred-mentions' LIMIT 1",
        """
        INSERT OR IGNORE INTO provenance_links (
            provenance_id, subject_kind, subject_ref, source_kind, source_ref,
            confidence, created_at
        ) VALUES
        ('prov-pred-mentions',    'fact', 'pred-mentions',    'seed', 'predicate_vocabulary', 1.0, '2026-05-09T00:00:00+00:00'),
        ('prov-pred-references',  'fact', 'pred-references',  'seed', 'predicate_vocabulary', 1.0, '2026-05-09T00:00:00+00:00'),
        ('prov-pred-depends-on',  'fact', 'pred-depends-on',  'seed', 'predicate_vocabulary', 1.0, '2026-05-09T00:00:00+00:00'),
        ('prov-pred-related-to',  'fact', 'pred-related-to',  'seed', 'predicate_vocabulary', 1.0, '2026-05-09T00:00:00+00:00'),
        ('prov-pred-supersedes',  'fact', 'pred-supersedes',  'seed', 'predicate_vocabulary', 1.0, '2026-05-09T00:00:00+00:00'),
        ('prov-pred-implements',  'fact', 'pred-implements',  'seed', 'predicate_vocabulary', 1.0, '2026-05-09T00:00:00+00:00'),
        ('prov-pred-contradicts', 'fact', 'pred-contradicts', 'seed', 'predicate_vocabulary', 1.0, '2026-05-09T00:00:00+00:00'),
        ('prov-pred-works-at',    'fact', 'pred-works-at',    'seed', 'predicate_vocabulary', 1.0, '2026-05-09T00:00:00+00:00'),
        ('prov-pred-attended',    'fact', 'pred-attended',    'seed', 'predicate_vocabulary', 1.0, '2026-05-09T00:00:00+00:00'),
        ('prov-pred-invested-in', 'fact', 'pred-invested-in', 'seed', 'predicate_vocabulary', 1.0, '2026-05-09T00:00:00+00:00'),
        ('prov-pred-founded',     'fact', 'pred-founded',     'seed', 'predicate_vocabulary', 1.0, '2026-05-09T00:00:00+00:00'),
        ('prov-pred-advises',     'fact', 'pred-advises',     'seed', 'predicate_vocabulary', 1.0, '2026-05-09T00:00:00+00:00')
        """,
        "Tier-A #5 typed predicate vocabulary provenance backfill",
    ),
    (
        "SELECT 1 FROM pragma_table_info('tasks') WHERE name='tombstone_pushed_at'",
        "ALTER TABLE tasks ADD COLUMN tombstone_pushed_at TEXT DEFAULT NULL",
        "tasks.tombstone_pushed_at column (push-aware tombstone retention)",
    ),
]


def _repair_memory_fts_triggers(conn: sqlite3.Connection) -> None:
    """Replace legacy FTS triggers that used invalid delete syntax."""
    expected = {
        "memory_fts_ad": (
            "CREATE TRIGGER memory_fts_ad AFTER DELETE ON entities BEGIN "
            "DELETE FROM memory_fts WHERE rowid = old.rowid; END"
        ),
        "memory_fts_au": (
            "CREATE TRIGGER memory_fts_au AFTER UPDATE ON entities BEGIN "
            "DELETE FROM memory_fts WHERE rowid = old.rowid; "
            "INSERT INTO memory_fts(rowid, name, entity_type, observations_text) "
            "SELECT new.rowid, new.name, new.entity_type, "
            "COALESCE(GROUP_CONCAT(o.content, ' '), '') "
            "FROM (SELECT 1) LEFT JOIN observations o ON o.entity_id = new.id; END"
        ),
    }
    for name, expected_sql in expected.items():
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name = ?", (name,)
        ).fetchone()
        sql = " ".join((row[0] or "").split()) if row and row[0] else ""
        if sql == expected_sql:
            continue
        conn.execute(f"DROP TRIGGER IF EXISTS {name}")
        if name == "memory_fts_ad":
            conn.execute(
                """
                CREATE TRIGGER memory_fts_ad AFTER DELETE ON entities BEGIN
                    DELETE FROM memory_fts WHERE rowid = old.rowid;
                END
                """
            )
        else:
            conn.execute(
                """
                CREATE TRIGGER memory_fts_au AFTER UPDATE ON entities BEGIN
                    DELETE FROM memory_fts WHERE rowid = old.rowid;
                    INSERT INTO memory_fts(rowid, name, entity_type, observations_text)
                    SELECT new.rowid, new.name, new.entity_type,
                           COALESCE(GROUP_CONCAT(o.content, ' '), '')
                    FROM (SELECT 1) LEFT JOIN observations o ON o.entity_id = new.id;
                END
                """
            )
        # DEBUG for the same reason as the migration loop in init_db: trigger
        # repair is routine start-up maintenance, not an operational event.
        logger.debug("Migration applied: repaired %s trigger", name)


def _backfill_manual_link_decisions(conn: sqlite3.Connection) -> None:
    """Treat existing manual task↔entity links as explicit positive labels.

    The identifiers are deterministic, so the backfill is idempotent.  These
    rows remain distinguishable from decisions captured by the suggestion UI
    and must not be mistaken for negative-label coverage.
    """
    conn.execute(
        """
        INSERT OR IGNORE INTO link_suggestion_decisions (
            decision_id, task_id, entity_id, decision, score, rank_at_decision,
            signals_json, model_version, decision_source, decided_by,
            created_at, updated_at
        )
        SELECT
            'legacy-manual:' || tel.task_id || ':' || tel.entity_id,
            tel.task_id,
            tel.entity_id,
            'accepted',
            tel.score,
            NULL,
            '{"legacy_manual_link":true}',
            'legacy/manual',
            'legacy_manual_link',
            'legacy',
            tel.created_at,
            tel.created_at
        FROM task_entity_links AS tel
        WHERE tel.link_type = 'manual'
        """
    )


def _split_schema_sql(sql: str) -> list[str]:
    """Split SQL schema into statements, respecting BEGIN...END trigger blocks.

    Naive split(";") breaks CREATE TRIGGER bodies that contain semicolons
    inside BEGIN...END.  This function accumulates lines and only emits a
    statement when a top-level semicolon is reached (i.e. NOT inside a
    BEGIN...END block).
    """
    stmts: list[str] = []
    current: list[str] = []
    in_trigger = False
    for line in sql.split("\n"):
        stripped = line.strip().upper()
        if stripped.startswith("CREATE TRIGGER"):
            in_trigger = True
        current.append(line)
        if in_trigger and stripped == "END;":
            stmts.append("\n".join(current))
            current = []
            in_trigger = False
        elif not in_trigger and ";" in line:
            stmts.append("\n".join(current))
            current = []
    if current:
        remaining = "\n".join(current).strip()
        if remaining:
            stmts.append(remaining)
    return [s.strip() for s in stmts if s.strip()]


_DEBATE_MESSAGE_KINDS_V1 = (
    "'Q', 'A', 'STATUS', 'DECISION', 'PING', 'WATERMARK', 'STATE', "
    "'COMPACTION', 'CLAIM', 'CHALLENGE', 'EVIDENCE', 'REBUT', "
    "'CONCEDE', 'VERIFY', 'DISSENT', 'ESCALATE'"
)


def _create_debate_message_indexes_and_triggers(conn: sqlite3.Connection) -> None:
    """Restore every object owned by a rebuilt ``debate_messages`` table."""
    statements = (
        "CREATE INDEX IF NOT EXISTS idx_debmsg_topic_ts "
        "ON debate_messages(topic_id, ts)",
        "CREATE INDEX IF NOT EXISTS idx_debmsg_topic_kind "
        "ON debate_messages(topic_id, kind)",
        "CREATE INDEX IF NOT EXISTS idx_debmsg_topic_priority "
        "ON debate_messages(topic_id, priority)",
        "CREATE INDEX IF NOT EXISTS idx_debmsg_topic_role_ts "
        "ON debate_messages(topic_id, role, ts)",
        "CREATE INDEX IF NOT EXISTS idx_debmsg_reply_to "
        "ON debate_messages(reply_to) WHERE reply_to IS NOT NULL",
        "CREATE INDEX IF NOT EXISTS idx_debmsg_protocol_round "
        "ON debate_messages(topic_id, protocol_version, round_no, kind)",
        """
        CREATE TRIGGER IF NOT EXISTS debate_messages_fts_ai
        AFTER INSERT ON debate_messages BEGIN
            INSERT INTO debate_messages_fts(msg_id, topic_id, role, kind, body)
            VALUES (new.msg_id, new.topic_id, new.role, new.kind, new.body);
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS debate_messages_fts_ad
        AFTER DELETE ON debate_messages BEGIN
            DELETE FROM debate_messages_fts WHERE msg_id = old.msg_id;
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS debate_messages_fts_au
        AFTER UPDATE OF topic_id, role, kind, body ON debate_messages BEGIN
            DELETE FROM debate_messages_fts WHERE msg_id = old.msg_id;
            INSERT INTO debate_messages_fts(msg_id, topic_id, role, kind, body)
            VALUES (new.msg_id, new.topic_id, new.role, new.kind, new.body);
        END
        """,
    )
    for statement in statements:
        conn.execute(statement)


def _migrate_debate_messages_v1(conn: sqlite3.Connection) -> None:
    """Losslessly widen the append-only debate envelope for ``debate/v1``.

    SQLite cannot ALTER a CHECK constraint.  The migration therefore rebuilds
    the table inside ``init_db``'s existing EXCLUSIVE transaction, verifies the
    row count and exact msg_id set before dropping the old table, then restores
    all indexes and FTS triggers.  A failure rolls the whole transaction back.
    """
    table_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='debate_messages'"
    ).fetchone()
    if table_row is None:
        raise RuntimeError("debate_messages missing after base schema creation")
    columns = {
        str(row["name"])
        for row in conn.execute("PRAGMA table_info('debate_messages')").fetchall()
    }
    required = {"protocol_version", "round_no", "body_mode", "payload_json"}
    table_sql = str(table_row["sql"] or "")
    needs_rebuild = not required.issubset(columns) or "'CLAIM'" not in table_sql
    if not needs_rebuild:
        _create_debate_message_indexes_and_triggers(conn)
        return

    foreign_key_baseline = {
        tuple(row) for row in conn.execute("PRAGMA foreign_key_check").fetchall()
    }
    old_count = int(conn.execute("SELECT COUNT(*) FROM debate_messages").fetchone()[0])
    conn.execute("DROP TABLE IF EXISTS debate_messages_v1_new")
    conn.execute(
        f"""
        CREATE TABLE debate_messages_v1_new (
            msg_id     TEXT PRIMARY KEY,
            topic_id   TEXT NOT NULL REFERENCES debates(topic_id) ON DELETE CASCADE,
            role       TEXT NOT NULL,
            ts         TEXT NOT NULL,
            priority   TEXT NOT NULL
                CHECK (priority IN ('H', 'M', 'L', 'INFO')),
            kind       TEXT NOT NULL CHECK (kind IN ({_DEBATE_MESSAGE_KINDS_V1})),
            standing   INTEGER DEFAULT NULL
                CHECK (standing IS NULL OR standing IN (0, 1)),
            vehicle    TEXT DEFAULT NULL
                CHECK (vehicle IS NULL OR vehicle IN
                       ('analysis', 'review', 'implementation')),
            reply_to   TEXT REFERENCES debate_messages(msg_id) ON DELETE SET NULL,
            body       TEXT NOT NULL,
            protocol_version TEXT DEFAULT NULL
                CHECK (protocol_version IS NULL OR protocol_version = 'debate/v1'),
            round_no   INTEGER DEFAULT NULL
                CHECK (round_no IS NULL OR round_no >= 1),
            body_mode  TEXT DEFAULT NULL
                CHECK (body_mode IS NULL OR body_mode IN ('structured', 'live_text')),
            payload_json TEXT DEFAULT NULL,
            created_at TEXT NOT NULL
        )
        """
    )

    def source(column: str) -> str:
        return column if column in columns else "NULL"

    conn.execute(
        "INSERT INTO debate_messages_v1_new "
        "(msg_id,topic_id,role,ts,priority,kind,standing,vehicle,reply_to,body,"
        " protocol_version,round_no,body_mode,payload_json,created_at) "
        "SELECT msg_id,topic_id,role,ts,priority,kind,"
        f"{source('standing')},{source('vehicle')},reply_to,body,"
        f"{source('protocol_version')},{source('round_no')},"
        f"{source('body_mode')},{source('payload_json')},created_at "
        "FROM debate_messages ORDER BY ts,msg_id"
    )
    new_count = int(
        conn.execute("SELECT COUNT(*) FROM debate_messages_v1_new").fetchone()[0]
    )
    missing = conn.execute(
        "SELECT msg_id FROM debate_messages EXCEPT "
        "SELECT msg_id FROM debate_messages_v1_new LIMIT 1"
    ).fetchone()
    extra = conn.execute(
        "SELECT msg_id FROM debate_messages_v1_new EXCEPT "
        "SELECT msg_id FROM debate_messages LIMIT 1"
    ).fetchone()
    if new_count != old_count or missing is not None or extra is not None:
        raise RuntimeError(
            "debate_messages debate/v1 migration failed lossless-copy check: "
            f"old={old_count} new={new_count} missing={missing} extra={extra}"
        )

    for trigger in (
        "debate_messages_fts_ai",
        "debate_messages_fts_ad",
        "debate_messages_fts_au",
    ):
        conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    conn.execute("DROP TABLE debate_messages")
    conn.execute("ALTER TABLE debate_messages_v1_new RENAME TO debate_messages")
    _create_debate_message_indexes_and_triggers(conn)
    foreign_keys_after = {
        tuple(row) for row in conn.execute("PRAGMA foreign_key_check").fetchall()
    }
    new_foreign_key_problems = foreign_keys_after - foreign_key_baseline
    if new_foreign_key_problems:
        fk_problem = sorted(new_foreign_key_problems, key=repr)[0]
        raise RuntimeError(
            "debate_messages debate/v1 migration introduced foreign-key violation: "
            f"{fk_problem}"
        )
    logger.info(
        "Migration applied: debate_messages debate/v1 envelope (%d rows preserved)",
        old_count,
    )


def init_db(db_path: str | None = None) -> None:
    """Create tables if they don't exist, run migrations, set WAL mode.

    Safe to call from multiple processes — all DDL uses IF NOT EXISTS.
    """
    _path = db_path or DB_PATH
    Path(_path).parent.mkdir(parents=True, exist_ok=True)
    raw = sqlite3.connect(_path, isolation_level=None, timeout=30)
    raw.row_factory = sqlite3.Row
    raw.execute("PRAGMA busy_timeout=30000")
    raw.execute("BEGIN EXCLUSIVE;")
    try:
        for stmt in _split_schema_sql(_SCHEMA_SQL):
            raw.execute(stmt)
        # Run migrations under same EXCLUSIVE lock (SM-01 fix: prevents race)
        #
        # Per-migration lines are DEBUG. With the full migration list re-checked
        # on every process start across ~10 server processes, this loop was
        # measured as 39% of the shared log window. The aggregate below carries
        # the INFO signal: whether anything changed at all, which is the
        # operational question. The individual descriptions stay available at
        # DEBUG for diagnosis.
        applied: list[str] = []
        for check_q, migrate_q, desc in _MIGRATIONS:
            if not raw.execute(check_q).fetchone():
                for stmt in _split_schema_sql(migrate_q):
                    raw.execute(stmt)
                logger.debug("Migration applied: %s", desc)
                applied.append(desc)
        if applied:
            shown = ", ".join(applied[:3]) + ("…" if len(applied) > 3 else "")
            logger.info("Migrations applied: %d (%s)", len(applied), shown)
        _migrate_debate_messages_v1(raw)
        _repair_memory_fts_triggers(raw)
        _backfill_manual_link_decisions(raw)
        raw.execute("COMMIT;")
    except Exception:
        raw.execute("ROLLBACK;")
        raise
    finally:
        raw.close()

    with _get_conn(_path) as conn:
        # Index + prune access log entries older than 30 days
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_access_log_accessed ON entity_access_log(accessed_at)"
        )
        conn.execute(
            "DELETE FROM entity_access_log WHERE accessed_at < datetime('now', '-30 days')"
        )

    with _get_conn(_path) as conn:
        task_count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        fts_count = conn.execute("SELECT COUNT(*) FROM tasks_fts").fetchone()[0]
        if task_count > 0 and fts_count == 0:
            conn.execute("INSERT INTO tasks_fts(tasks_fts) VALUES('rebuild')")
            logger.info(
                "tasks_fts: rebuilt FTS index for %d existing tasks", task_count
            )

    with _get_conn(_path) as conn:
        debate_count = conn.execute("SELECT COUNT(*) FROM debate_messages").fetchone()[
            0
        ]
        debate_fts_count = conn.execute(
            "SELECT COUNT(*) FROM debate_messages_fts"
        ).fetchone()[0]
        if debate_count != debate_fts_count:
            # Legacy databases predate the triggers.  Rebuild only when the
            # row counts disagree; normal startups stay O(1), while a partial
            # or interrupted backfill repairs deterministically.
            conn.execute("DELETE FROM debate_messages_fts")
            conn.execute(
                "INSERT INTO debate_messages_fts "
                "(msg_id, topic_id, role, kind, body) "
                "SELECT msg_id, topic_id, role, kind, body "
                "FROM debate_messages ORDER BY ts, msg_id"
            )
            logger.info(
                "debate_messages_fts: rebuilt FTS index for %d messages",
                debate_count,
            )

    # Optional: initialize sqlite-vec virtual table for semantic search
    try:
        import vec_search as _vec_search

        with _get_conn(_path) as conn:
            if _vec_search.VEC_AVAILABLE:
                _vec_search.init_vec_table(conn)
                _vec_search.init_task_vec_table(conn)
            prune = getattr(_vec_search, "prune_orphan_task_embeddings", None)
            if callable(prune):
                prune(conn)
    except Exception as e:
        logger.debug("sqlite-vec init skipped: %s", e)

    logger.info("Database initialized at %s", _path)
