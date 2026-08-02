"""Regression coverage for legacy duplicate role/session bindings."""

from __future__ import annotations

import json
import sqlite3

import pytest

from schema import _migrate_debate_messages_v1, init_db


def test_init_db_retires_older_duplicate_session_before_unique_index(tmp_path):
    db_path = tmp_path / "legacy-duplicate-role-session.db"
    init_db(str(db_path))

    conn = sqlite3.connect(db_path)
    conn.execute("DROP INDEX idx_drb_one_active_session")
    conn.execute(
        "INSERT INTO debates "
        "(topic_id,title,state,created_at,created_by_role,roles_json,metadata_json) "
        "VALUES (?,?,?,?,?,?,?)",
        (
            "LEGACY_DUPLICATE",
            "Legacy duplicate role/session",
            "ACTIVE",
            "2026-07-19T10:45:26.000000+00:00",
            "CONDUCTOR",
            json.dumps(
                [
                    {"role": "EXECUTOR", "session_id": "codex-shared"},
                    {"role": "ADVOCATE_CODEX", "session_id": "codex-shared"},
                    {"role": "CONDUCTOR", "session_id": "cc-conductor"},
                ]
            ),
            "{}",
        ),
    )
    binding_sql = (
        "INSERT INTO debate_role_bindings "
        "(topic_id,role,session_id,runtime,state,generation,created_at,updated_at,"
        "retired_at,reason,bound_by_role,bound_by_msg_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"
    )
    conn.execute(
        binding_sql,
        (
            "LEGACY_DUPLICATE",
            "EXECUTOR",
            "codex-shared",
            "codex",
            "active",
            2,
            "2026-07-19T10:45:26.100000+00:00",
            "2026-07-19T10:45:26.100000+00:00",
            None,
            "legacy executor binding",
            "CONDUCTOR",
            None,
        ),
    )
    conn.execute(
        binding_sql,
        (
            "LEGACY_DUPLICATE",
            "ADVOCATE_CODEX",
            "codex-shared",
            "codex",
            "active",
            1,
            "2026-07-19T10:45:26.200000+00:00",
            "2026-07-20T08:00:00.000000+00:00",
            None,
            "operator correction keeps advocate online",
            "ADVOCATE_CODEX",
            None,
        ),
    )
    conn.execute(
        binding_sql,
        (
            "LEGACY_DUPLICATE",
            "CONDUCTOR",
            "cc-conductor",
            "cc",
            "active",
            1,
            "2026-07-19T10:45:26.000000+00:00",
            "2026-07-19T10:45:26.000000+00:00",
            None,
            "unrelated binding",
            "CONDUCTOR",
            None,
        ),
    )
    conn.commit()
    conn.close()

    init_db(str(db_path))
    init_db(str(db_path))

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT role,state,retired_at,reason FROM debate_role_bindings "
        "WHERE topic_id='LEGACY_DUPLICATE' ORDER BY role"
    ).fetchall()
    by_role = {row["role"]: dict(row) for row in rows}

    assert by_role["ADVOCATE_CODEX"]["state"] == "active"
    assert by_role["ADVOCATE_CODEX"]["reason"] == (
        "operator correction keeps advocate online"
    )
    assert by_role["EXECUTOR"]["state"] == "retired"
    assert by_role["EXECUTOR"]["retired_at"] is not None
    assert by_role["EXECUTOR"]["reason"].count(
        "migration: retired legacy duplicate active session"
    ) == 1
    assert by_role["CONDUCTOR"]["state"] == "active"

    index = conn.execute(
        "SELECT sql FROM sqlite_master "
        "WHERE type='index' AND name='idx_drb_one_active_session'"
    ).fetchone()
    assert index is not None
    assert "UNIQUE INDEX" in index["sql"]

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            binding_sql,
            (
                "LEGACY_DUPLICATE",
                "REVIEWER",
                "codex-shared",
                "codex",
                "active",
                1,
                "2026-07-21T00:00:00.000000+00:00",
                "2026-07-21T00:00:00.000000+00:00",
                None,
                "must be rejected by unique session index",
                "CONDUCTOR",
                None,
            ),
        )
    conn.close()


def test_debate_message_rebuild_preserves_unrelated_fk_violation_baseline():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE debates (topic_id TEXT PRIMARY KEY);
        INSERT INTO debates VALUES ('LEGACY_TOPIC');

        CREATE TABLE debate_messages (
            msg_id TEXT PRIMARY KEY,
            topic_id TEXT NOT NULL REFERENCES debates(topic_id) ON DELETE CASCADE,
            role TEXT NOT NULL,
            ts TEXT NOT NULL,
            priority TEXT NOT NULL,
            kind TEXT NOT NULL,
            standing INTEGER,
            vehicle TEXT,
            reply_to TEXT REFERENCES debate_messages(msg_id) ON DELETE SET NULL,
            body TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        INSERT INTO debate_messages VALUES (
            'legacy-msg', 'LEGACY_TOPIC', 'CONDUCTOR',
            '2026-07-01T00:00:00Z', 'INFO', 'STATUS', NULL, NULL, NULL,
            'legacy body', '2026-07-01T00:00:00Z'
        );
        CREATE VIRTUAL TABLE debate_messages_fts USING fts5(
            msg_id UNINDEXED, topic_id UNINDEXED, role, kind, body
        );

        CREATE TABLE tasks (id TEXT PRIMARY KEY);
        CREATE TABLE task_field_versions (
            task_id TEXT NOT NULL REFERENCES tasks(id),
            field_name TEXT NOT NULL
        );
        INSERT INTO task_field_versions VALUES ('missing-task', 'title');
        """
    )
    before = {
        tuple(row) for row in conn.execute("PRAGMA foreign_key_check").fetchall()
    }
    assert before == {("task_field_versions", 1, "tasks", 0)}

    _migrate_debate_messages_v1(conn)

    after = {
        tuple(row) for row in conn.execute("PRAGMA foreign_key_check").fetchall()
    }
    columns = {
        row["name"] for row in conn.execute("PRAGMA table_info('debate_messages')")
    }
    message = conn.execute(
        "SELECT msg_id,body FROM debate_messages WHERE msg_id='legacy-msg'"
    ).fetchone()
    assert after == before
    assert {"protocol_version", "round_no", "body_mode", "payload_json"} <= columns
    assert dict(message) == {"msg_id": "legacy-msg", "body": "legacy body"}
    conn.close()
