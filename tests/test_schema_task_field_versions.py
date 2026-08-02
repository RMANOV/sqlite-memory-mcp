"""Task field-version referential-integrity regressions."""

from __future__ import annotations

import sqlite3

from db_utils import get_conn
from schema import init_db


def _insert_task(conn: sqlite3.Connection, task_id: str) -> None:
    conn.execute(
        "INSERT INTO tasks (id,title,created_at,updated_at) VALUES (?,?,?,?)",
        (task_id, task_id, "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
    )


def _insert_version(conn: sqlite3.Connection, task_id: str, field_name: str) -> None:
    conn.execute(
        "INSERT INTO task_field_versions "
        "(task_id,field_name,updated_at,updated_by) VALUES (?,?,?,?)",
        (task_id, field_name, "2026-01-01T00:00:00Z", "test"),
    )


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
