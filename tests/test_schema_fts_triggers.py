"""Regression tests for memory_fts entity triggers."""

import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from schema import init_db


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
