"""Regression tests for memory_fts entity triggers."""

import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from schema import _MIGRATIONS, init_db


def test_memory_fts_entity_update_and_delete_triggers_use_plain_delete(tmp_path):
    db_path = tmp_path / "fts.db"
    init_db(str(db_path))

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")

    conn.execute(
        "INSERT INTO entities (name, entity_type, created_at, updated_at) "
        "VALUES ('EntityA', 'concept', datetime('now'), datetime('now'))"
    )
    entity_id = conn.execute(
        "SELECT id FROM entities WHERE name = 'EntityA'"
    ).fetchone()[0]

    conn.execute(
        "UPDATE entities SET updated_at = datetime('now') WHERE id = ?", (entity_id,)
    )
    row = conn.execute(
        "SELECT rowid, name FROM memory_fts WHERE rowid = ?", (entity_id,)
    ).fetchone()
    assert row is not None
    assert row["name"] == "EntityA"

    conn.execute("DELETE FROM entities WHERE id = ?", (entity_id,))
    row = conn.execute(
        "SELECT rowid FROM memory_fts WHERE rowid = ?", (entity_id,)
    ).fetchone()
    assert row is None
    conn.close()


def test_memory_fts_entity_update_preserves_observation_text(tmp_path):
    db_path = tmp_path / "fts-observations.db"
    init_db(str(db_path))

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "INSERT INTO entities (name, entity_type, created_at, updated_at) "
        "VALUES ('EntityB', 'concept', datetime('now'), datetime('now'))"
    )
    entity_id = conn.execute(
        "SELECT id FROM entities WHERE name = 'EntityB'"
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO observations (entity_id, content, created_at) "
        "VALUES (?, 'critical observation needle', datetime('now'))",
        (entity_id,),
    )

    conn.execute(
        "UPDATE entities SET name = 'EntityB renamed' WHERE id = ?", (entity_id,)
    )

    row = conn.execute(
        "SELECT name, observations_text FROM memory_fts WHERE rowid = ?", (entity_id,)
    ).fetchone()
    assert row["name"] == "EntityB renamed"
    assert "critical observation needle" in row["observations_text"]
    conn.close()


def test_context_chunks_migration_defaults_language_to_null():
    _check, create_sql, _description = next(
        migration
        for migration in _MIGRATIONS
        if migration[2] == "context_chunks table (v3.0.0)"
    )
    conn = sqlite3.connect(":memory:")
    conn.execute(create_sql)

    language = next(
        row
        for row in conn.execute("PRAGMA table_info('context_chunks')")
        if row[1] == "language"
    )

    assert str(language[4]).upper() == "NULL"
    conn.close()


def test_init_db_configures_migration_busy_timeout(tmp_path, monkeypatch):
    real_connect = sqlite3.connect
    connect_timeouts: list[object] = []
    statements: list[str] = []

    def recording_connect(*args, **kwargs):
        connect_timeouts.append(kwargs.get("timeout"))
        conn = real_connect(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr("schema.sqlite3.connect", recording_connect)

    init_db(str(tmp_path / "busy-timeout.db"))

    assert connect_timeouts[0] == 30
    assert any(
        "".join(statement.casefold().split()) == "pragmabusy_timeout=30000"
        for statement in statements
    )


def test_lazy_claims_hit_count_migration_is_idempotent_and_backfills(tmp_path):
    db_path = tmp_path / "legacy-lazy-claims.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE lazy_claims (
            claim_id TEXT PRIMARY KEY,
            entity_id INTEGER NOT NULL,
            observation_id INTEGER NOT NULL,
            subject TEXT NOT NULL,
            predicate TEXT NOT NULL,
            object_text TEXT NOT NULL,
            confidence REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'candidate',
            promoted_to_fact_id TEXT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        INSERT INTO lazy_claims (
            claim_id, entity_id, observation_id, subject, predicate,
            object_text, confidence, created_at, updated_at
        ) VALUES (
            'legacy-claim', 1, 1, 'subject', 'uses', 'object', 0.5,
            '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00'
        );
        """
    )
    conn.commit()
    conn.close()

    init_db(str(db_path))
    init_db(str(db_path))

    conn = sqlite3.connect(db_path)
    hit_count = conn.execute(
        "SELECT hit_count FROM lazy_claims WHERE claim_id = 'legacy-claim'"
    ).fetchone()[0]
    columns = [
        row[1]
        for row in conn.execute("PRAGMA table_info('lazy_claims')")
        if row[1] == "hit_count"
    ]
    assert hit_count == 1
    assert columns == ["hit_count"]
    conn.close()
