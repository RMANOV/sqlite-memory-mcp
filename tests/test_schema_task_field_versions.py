"""Task field-version referential-integrity and clock-clamp regressions."""

from __future__ import annotations

import sqlite3
import time

from db_utils import get_conn
from schema import init_db

_COUNTER_BITS = 16
_COUNTER_MASK = (1 << _COUNTER_BITS) - 1


def _insert_task(conn: sqlite3.Connection, task_id: str) -> None:
    conn.execute(
        "INSERT INTO tasks (id,title,created_at,updated_at) VALUES (?,?,?,?)",
        (task_id, task_id, "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
    )


def _insert_version(
    conn: sqlite3.Connection,
    task_id: str,
    field_name: str,
    updated_order: int = 0,
) -> None:
    conn.execute(
        "INSERT INTO task_field_versions "
        "(task_id,field_name,updated_at,updated_by,updated_order) VALUES (?,?,?,?,?)",
        (task_id, field_name, "2026-01-01T00:00:00Z", "test", updated_order),
    )


def _order_of(conn: sqlite3.Connection, task_id: str, field_name: str) -> int:
    return conn.execute(
        "SELECT updated_order FROM task_field_versions "
        "WHERE task_id=? AND field_name=?",
        (task_id, field_name),
    ).fetchone()[0]


def test_init_db_prunes_only_orphan_task_field_versions(tmp_path):
    db_path = tmp_path / "orphan-task-field-versions.db"
    init_db(str(db_path))

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys=OFF")
    _insert_task(conn, "live-task")
    _insert_version(conn, "live-task", "title")
    _insert_version(conn, "missing-task", "title")
    _insert_version(conn, "missing-task", "status")
    conn.commit()
    conn.close()

    init_db(str(db_path))
    init_db(str(db_path))

    conn = sqlite3.connect(db_path)
    missing_rows = conn.execute(
        "SELECT COUNT(*) FROM task_field_versions WHERE task_id='missing-task'"
    ).fetchone()[0]
    live_title_rows = conn.execute(
        "SELECT COUNT(*) FROM task_field_versions "
        "WHERE task_id='live-task' AND field_name='title'"
    ).fetchone()[0]
    violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    conn.close()

    assert missing_rows == 0
    assert live_title_rows == 1
    assert violations == []


def test_configured_connection_cascades_task_field_versions(tmp_path):
    db_path = tmp_path / "task-field-version-cascade.db"
    init_db(str(db_path))

    with get_conn(str(db_path)) as conn:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        _insert_task(conn, "deleted-task")
        _insert_version(conn, "deleted-task", "title")
        conn.execute("DELETE FROM tasks WHERE id='deleted-task'")
        remaining = conn.execute(
            "SELECT COUNT(*) FROM task_field_versions "
            "WHERE task_id='deleted-task'"
        ).fetchone()[0]

    assert remaining == 0


def test_init_db_clamps_future_field_version_clocks(tmp_path):
    """A future packed HLC must be pulled back to now, keeping its counter.

    Left alone it outranks every legitimate write forever and is re-exported
    to every peer, so `_clamp_field_version_clock`'s in-memory clamp never
    stops firing. The boundary row guards the +5s tolerance: if someone
    narrows it later, this test fails instead of silently clamping rows the
    runtime clamp still considers sane.
    """
    db_path = tmp_path / "future-field-version-clocks.db"
    init_db(str(db_path))

    now_ms = int(time.time() * 1000)
    future_order = ((now_ms + 30 * 365 * 24 * 3600 * 1000) << _COUNTER_BITS) | 7
    past_order = ((now_ms - 86_400_000) << _COUNTER_BITS) | 3
    boundary_order = ((now_ms + 3_000) << _COUNTER_BITS) | 11

    conn = sqlite3.connect(db_path)
    _insert_task(conn, "clock-task")
    _insert_version(conn, "clock-task", "title", future_order)
    _insert_version(conn, "clock-task", "status", past_order)
    _insert_version(conn, "clock-task", "notes", boundary_order)
    conn.commit()
    conn.close()

    init_db(str(db_path))

    conn = sqlite3.connect(db_path)
    clamped = _order_of(conn, "clock-task", "title")
    still_future = conn.execute(
        "SELECT COUNT(*) FROM task_field_versions WHERE updated_order > "
        "((CAST(strftime('%s','now') AS INTEGER) + 5) * 1000) * 65536"
    ).fetchone()[0]

    assert clamped < future_order
    assert (clamped >> _COUNTER_BITS) <= now_ms + 5_000
    assert clamped & _COUNTER_MASK == 7, "counter must survive the clamp"
    assert _order_of(conn, "clock-task", "status") == past_order
    assert _order_of(conn, "clock-task", "notes") == boundary_order, (
        "rows inside the +5s tolerance must be left alone"
    )
    assert still_future == 0
    conn.close()

    init_db(str(db_path))

    conn = sqlite3.connect(db_path)
    assert _order_of(conn, "clock-task", "title") == clamped, (
        "guard must stop firing once no future row remains"
    )
    conn.close()
