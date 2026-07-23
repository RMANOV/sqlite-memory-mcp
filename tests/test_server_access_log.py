import sqlite3
import time

import server
from server import _record_entity_access, _record_entity_access_best_effort


def test_record_entity_access_batches_and_deduplicates_rows():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE entity_access_log ("
        "entity_id INTEGER, tool_name TEXT, accessed_at TEXT)"
    )

    _record_entity_access(conn, [3, 1, 3, 2], "search_nodes")

    assert conn.execute(
        "SELECT entity_id,tool_name FROM entity_access_log ORDER BY rowid"
    ).fetchall() == [
        (3, "search_nodes"),
        (1, "search_nodes"),
        (2, "search_nodes"),
    ]


def test_best_effort_access_log_does_not_block_read_path(tmp_path, monkeypatch):
    db_path = tmp_path / "access-log.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE entity_access_log ("
            "entity_id INTEGER, tool_name TEXT, accessed_at TEXT)"
        )

    monkeypatch.setattr(server, "_DB_PATH", str(db_path))
    writer = sqlite3.connect(db_path, isolation_level=None)
    writer.execute("BEGIN IMMEDIATE")
    try:
        started = time.monotonic()
        _record_entity_access_best_effort([1], "search_nodes")
        elapsed = time.monotonic() - started
    finally:
        writer.execute("ROLLBACK")
        writer.close()

    assert elapsed < 0.5
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM entity_access_log").fetchone()[0] == 0

    _record_entity_access_best_effort([1], "search_nodes")
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT entity_id, tool_name FROM entity_access_log"
        ).fetchall() == [(1, "search_nodes")]
