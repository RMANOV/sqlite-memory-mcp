import sqlite3

from entity_server import _observations_by_entity


def test_observations_by_entity_batches_and_preserves_row_order():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        "CREATE TABLE observations ("
        "id INTEGER PRIMARY KEY, entity_id INTEGER, content TEXT);"
        "INSERT INTO observations VALUES (2, 10, 'second');"
        "INSERT INTO observations VALUES (1, 10, 'first');"
        "INSERT INTO observations VALUES (3, 20, 'other');"
    )

    assert _observations_by_entity(conn, [20, 10, 20]) == {
        10: ["first", "second"],
        20: ["other"],
    }
