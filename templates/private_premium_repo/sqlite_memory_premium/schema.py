"""Template schema for premium-only runtime features."""

from __future__ import annotations

from . import host_api

_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS premium_acl_roles (
        role_id TEXT PRIMARY KEY,
        role_name TEXT NOT NULL,
        description TEXT DEFAULT NULL,
        scope_kind TEXT NOT NULL DEFAULT 'global',
        scope_ref TEXT DEFAULT NULL,
        permissions_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(role_name, scope_kind, scope_ref)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS premium_acl_assignments (
        assignment_id TEXT PRIMARY KEY,
        principal_type TEXT NOT NULL,
        principal_id TEXT NOT NULL,
        role_id TEXT NOT NULL REFERENCES premium_acl_roles(role_id) ON DELETE CASCADE,
        scope_kind TEXT NOT NULL DEFAULT 'global',
        scope_ref TEXT DEFAULT NULL,
        granted_by TEXT DEFAULT NULL,
        granted_at TEXT NOT NULL,
        expires_at TEXT DEFAULT NULL,
        active INTEGER NOT NULL DEFAULT 1
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS premium_governance_decisions (
        decision_id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        decision TEXT NOT NULL,
        subject_kind TEXT NOT NULL,
        subject_id TEXT DEFAULT NULL,
        scope_kind TEXT NOT NULL DEFAULT 'global',
        scope_ref TEXT DEFAULT NULL,
        rationale TEXT DEFAULT NULL,
        evidence_json TEXT DEFAULT NULL,
        risk_level TEXT NOT NULL DEFAULT 'medium',
        created_at TEXT NOT NULL,
        created_by TEXT DEFAULT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS premium_mailboxes (
        mailbox_id TEXT PRIMARY KEY,
        mailbox_key TEXT NOT NULL UNIQUE,
        mailbox_type TEXT NOT NULL DEFAULT 'email',
        owner_label TEXT NOT NULL,
        project TEXT DEFAULT NULL,
        client_scope TEXT DEFAULT NULL,
        config_json TEXT DEFAULT NULL,
        active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS premium_mailbox_threads (
        thread_id TEXT PRIMARY KEY,
        mailbox_id TEXT NOT NULL REFERENCES premium_mailboxes(mailbox_id) ON DELETE CASCADE,
        external_thread_ref TEXT NOT NULL,
        subject TEXT DEFAULT NULL,
        client_ref TEXT DEFAULT NULL,
        participants_json TEXT DEFAULT NULL,
        tags_json TEXT DEFAULT NULL,
        message_count INTEGER NOT NULL DEFAULT 0,
        last_message_at TEXT DEFAULT NULL,
        last_direction TEXT DEFAULT NULL,
        open_followup INTEGER NOT NULL DEFAULT 0,
        metadata_json TEXT DEFAULT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(mailbox_id, external_thread_ref)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS premium_mailbox_messages (
        message_id TEXT PRIMARY KEY,
        thread_id TEXT NOT NULL REFERENCES premium_mailbox_threads(thread_id) ON DELETE CASCADE,
        mailbox_id TEXT NOT NULL REFERENCES premium_mailboxes(mailbox_id) ON DELETE CASCADE,
        external_message_ref TEXT NOT NULL,
        direction TEXT NOT NULL,
        sender TEXT NOT NULL,
        recipients_json TEXT DEFAULT NULL,
        subject TEXT DEFAULT NULL,
        body_text TEXT NOT NULL,
        sent_at TEXT NOT NULL,
        ingest_source TEXT DEFAULT NULL,
        metadata_json TEXT DEFAULT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(mailbox_id, external_message_ref)
    )
    """,
)


def ensure_private_schema(conn) -> None:
    """Create all premium-private tables if they do not already exist."""
    for statement in _SCHEMA_STATEMENTS:
        conn.execute(statement)


def init_private_schema() -> None:
    """Initialize the template schema using the host DB connection helper."""
    with host_api.get_conn() as conn:
        ensure_private_schema(conn)
