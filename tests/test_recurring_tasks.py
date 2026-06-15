import os
import sqlite3
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from recurring_tasks import process_recurring


@pytest.fixture
def conn(tmp_path):
    db_path = str(tmp_path / "test.db")
    connection = sqlite3.connect(db_path, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(
        """
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            description TEXT DEFAULT NULL,
            status TEXT DEFAULT 'not_started',
            section TEXT DEFAULT 'inbox',
            priority TEXT DEFAULT 'medium',
            due_date TEXT,
            project TEXT,
            parent_id TEXT,
            notes TEXT,
            recurring TEXT,
            reminder_at TEXT,
            type TEXT NOT NULL DEFAULT 'task',
            assignee TEXT,
            shared_by TEXT,
            visibility TEXT DEFAULT 'private',
            publish_requested_at TEXT,
            created_at TEXT,
            updated_at TEXT,
            tombstone_pushed_at TEXT DEFAULT NULL
        );

        CREATE TABLE task_field_versions (
            task_id TEXT NOT NULL,
            field_name TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            updated_by TEXT NOT NULL DEFAULT '',
            old_value TEXT DEFAULT NULL,
            new_value TEXT DEFAULT NULL,
            PRIMARY KEY (task_id, field_name),
            FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
        );
        """
    )
    yield connection
    connection.close()


def _insert_task(
    conn,
    task_id,
    *,
    title,
    status,
    recurring,
    project=None,
    parent_id=None,
    task_type="task",
    section="today",
):
    conn.execute(
        "INSERT INTO tasks (id, title, status, section, priority, recurring, project, "
        "parent_id, type, visibility, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, 'medium', ?, ?, ?, ?, 'private', datetime('now'), datetime('now'))",
        (task_id, title, status, section, recurring, project, parent_id, task_type),
    )


def test_process_recurring_scopes_duplicates_to_same_project(conn):
    _insert_task(
        conn,
        "source-done",
        title="Weekly Review",
        status="done",
        recurring='{"every":"day"}',
        project="client-b",
    )
    _insert_task(
        conn,
        "other-active",
        title="Weekly Review",
        status="not_started",
        recurring='{"every":"day"}',
        project="client-a",
    )

    created = process_recurring(conn, dry_run=False)

    assert len(created) == 1
    created_row = conn.execute(
        "SELECT title, project, status FROM tasks WHERE id = ?", (created[0]["id"],)
    ).fetchone()
    assert created_row["title"] == "Weekly Review"
    assert created_row["project"] == "client-b"
    assert created_row["status"] == "not_started"


def test_process_recurring_spawns_visible_task_from_done_section_source(conn):
    _insert_task(
        conn,
        "source-done",
        title="Monthly KPI Update",
        status="done",
        section="done",
        recurring='{"every":"day"}',
        project="work",
    )

    created = process_recurring(conn, dry_run=False)

    assert len(created) == 1
    created_row = conn.execute(
        "SELECT status, section FROM tasks WHERE id = ?", (created[0]["id"],)
    ).fetchone()
    assert created_row["status"] == "not_started"
    assert created_row["section"] == "next"


def test_process_recurring_creates_one_child_for_duplicate_done_series(conn):
    _insert_task(
        conn,
        "source-done-a",
        title="Monthly KPI Update",
        status="done",
        section="done",
        recurring='{"every":"day"}',
        project="work",
    )
    _insert_task(
        conn,
        "source-done-b",
        title="Monthly KPI Update",
        status="done",
        section="someday",
        recurring='{"every":"day"}',
        project="work",
    )

    created = process_recurring(conn, dry_run=False)

    assert len(created) == 1
    active_count = conn.execute(
        "SELECT COUNT(*) FROM tasks "
        "WHERE title = 'Monthly KPI Update' "
        "AND project = 'work' "
        "AND status IN ('not_started', 'in_progress')"
    ).fetchone()[0]
    assert active_count == 1


def test_process_recurring_uses_shared_timestamp_for_new_task_versions(conn):
    _insert_task(
        conn,
        "source-done",
        title="Daily Standup",
        status="done",
        recurring='{"every":"day"}',
        project="ops",
    )

    created = process_recurring(conn, dry_run=False)

    new_task_id = created[0]["id"]
    task_row = conn.execute(
        "SELECT updated_at FROM tasks WHERE id = ?", (new_task_id,)
    ).fetchone()
    field_row = conn.execute(
        "SELECT updated_at FROM task_field_versions WHERE task_id = ? AND field_name = 'title'",
        (new_task_id,),
    ).fetchone()
    assert field_row is not None
    assert task_row["updated_at"] == field_row["updated_at"]


def test_process_recurring_updates_source_recurring_version_and_timestamp(conn):
    raw = '{"every":"day"}'
    _insert_task(
        conn,
        "source-done",
        title="Backup",
        status="done",
        recurring=raw,
        project="infra",
    )

    process_recurring(conn, dry_run=False)

    source_row = conn.execute(
        "SELECT recurring, updated_at FROM tasks WHERE id = 'source-done'"
    ).fetchone()
    version_row = conn.execute(
        "SELECT updated_at, old_value, new_value FROM task_field_versions "
        "WHERE task_id = 'source-done' AND field_name = 'recurring'"
    ).fetchone()
    assert version_row is not None
    assert version_row["old_value"] == raw
    assert version_row["new_value"] == source_row["recurring"]
    assert source_row["updated_at"] == version_row["updated_at"]
    assert date.today().isoformat() in source_row["recurring"]
