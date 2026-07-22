import sqlite3

from server import _record_entity_access


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
