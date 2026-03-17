# tests/test_bridge_export.py
"""Unit tests for bridge export functions: export_task_files, export_index_json."""

import json
import os
import sys
import sqlite3

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from db_utils import (
    export_index_json,
    export_task_files,
    json_loads,
    now_iso,
    upsert_field_versions,
)

# ── Schema helpers ────────────────────────────────────────────────────────

_TASKS_DDL = """
CREATE TABLE tasks (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    description TEXT DEFAULT NULL,
    status      TEXT NOT NULL DEFAULT 'not_started',
    priority    TEXT DEFAULT 'medium',
    section     TEXT DEFAULT 'inbox',
    due_date    TEXT DEFAULT NULL,
    project     TEXT DEFAULT NULL,
    parent_id   TEXT DEFAULT NULL REFERENCES tasks(id) ON DELETE SET NULL,
    notes       TEXT DEFAULT NULL,
    recurring   TEXT DEFAULT NULL,
    reminder_at TEXT DEFAULT NULL,
    type        TEXT NOT NULL DEFAULT 'task',
    assignee    TEXT DEFAULT NULL,
    shared_by   TEXT DEFAULT NULL,
    visibility           TEXT DEFAULT 'private',
    publish_requested_at TEXT DEFAULT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
"""

_FIELD_VERSIONS_DDL = """
CREATE TABLE task_field_versions (
    task_id    TEXT NOT NULL,
    field_name TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    updated_by TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (task_id, field_name),
    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
);
"""

_ENTITIES_DDL = """
CREATE TABLE entities (
    id         INTEGER PRIMARY KEY,
    name       TEXT UNIQUE NOT NULL,
    entity_type TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

_LINKS_DDL = """
CREATE TABLE task_entity_links (
    task_id   TEXT    NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    link_type TEXT    NOT NULL DEFAULT 'manual',
    score     REAL    DEFAULT NULL,
    created_at TEXT   NOT NULL,
    PRIMARY KEY (task_id, entity_id)
);
"""


def _insert_task(conn, task_id, title, *, status="not_started", description=None):
    now = now_iso()
    conn.execute(
        "INSERT INTO tasks (id, title, description, status, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (task_id, title, description, status, now, now),
    )
    return now


# ── Fixture ───────────────────────────────────────────────────────────────


@pytest.fixture
def setup(tmp_path):
    """Fresh SQLite DB + bridge dir for each test."""
    db_path = str(tmp_path / "test.db")
    bridge_dir = str(tmp_path / "bridge")
    os.makedirs(bridge_dir, exist_ok=True)

    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(_TASKS_DDL + _FIELD_VERSIONS_DDL + _ENTITIES_DDL + _LINKS_DDL)
    conn.execute("BEGIN")
    yield conn, bridge_dir
    conn.execute("COMMIT")
    conn.close()


# ── Tests: export_task_files ──────────────────────────────────────────────


class TestExportTaskFiles:
    def test_creates_per_task_json_file(self, setup):
        """Each active task produces its own <id>.json in tasks/."""
        conn, bridge_dir = setup
        _insert_task(conn, "task-001", "Buy milk")
        _insert_task(conn, "task-002", "Write tests")

        exported = export_task_files(conn, bridge_dir)

        assert set(exported) == {"task-001", "task-002"}
        tasks_dir = os.path.join(bridge_dir, "tasks")
        assert os.path.isfile(os.path.join(tasks_dir, "task-001.json"))
        assert os.path.isfile(os.path.join(tasks_dir, "task-002.json"))

    def test_file_contains_correct_fields(self, setup):
        """Exported JSON has id, title, status and _field_ts key."""
        conn, bridge_dir = setup
        _insert_task(conn, "task-001", "Buy milk")

        export_task_files(conn, bridge_dir)

        data = json_loads(
            open(
                os.path.join(bridge_dir, "tasks", "task-001.json"), encoding="utf-8"
            ).read()
        )
        assert data["id"] == "task-001"
        assert data["title"] == "Buy milk"
        assert data["status"] == "not_started"
        assert "_field_ts" in data
        assert "_links" in data

    def test_archived_cancelled_excluded(self, setup):
        """Archived and cancelled tasks are not exported."""
        conn, bridge_dir = setup
        _insert_task(conn, "task-active", "Active task")
        _insert_task(conn, "task-arch", "Archived task", status="archived")
        _insert_task(conn, "task-cancel", "Cancelled task", status="cancelled")

        exported = export_task_files(conn, bridge_dir)

        assert exported == ["task-active"]
        tasks_dir = os.path.join(bridge_dir, "tasks")
        assert not os.path.exists(os.path.join(tasks_dir, "task-arch.json"))
        assert not os.path.exists(os.path.join(tasks_dir, "task-cancel.json"))

    def test_content_aware_preserves_bridge_description(self, setup):
        """NULL local description is filled from existing bridge file, not overwritten."""
        conn, bridge_dir = setup
        _insert_task(conn, "task-001", "My task", description=None)

        # Pre-seed bridge file with a description
        tasks_dir = os.path.join(bridge_dir, "tasks")
        os.makedirs(tasks_dir, exist_ok=True)
        existing = {
            "id": "task-001",
            "description": "Bridge-written content",
            "notes": None,
        }
        open(os.path.join(tasks_dir, "task-001.json"), "w", encoding="utf-8").write(
            json.dumps(existing)
        )

        export_task_files(conn, bridge_dir)

        data = json_loads(
            open(os.path.join(tasks_dir, "task-001.json"), encoding="utf-8").read()
        )
        assert data["description"] == "Bridge-written content"

    def test_content_aware_does_not_override_local_description(self, setup):
        """Non-NULL local description is NOT replaced by bridge file value."""
        conn, bridge_dir = setup
        _insert_task(conn, "task-001", "My task", description="Local description")

        tasks_dir = os.path.join(bridge_dir, "tasks")
        os.makedirs(tasks_dir, exist_ok=True)
        existing = {
            "id": "task-001",
            "description": "Old bridge description",
            "notes": None,
        }
        open(os.path.join(tasks_dir, "task-001.json"), "w", encoding="utf-8").write(
            json.dumps(existing)
        )

        export_task_files(conn, bridge_dir)

        data = json_loads(
            open(os.path.join(tasks_dir, "task-001.json"), encoding="utf-8").read()
        )
        assert data["description"] == "Local description"

    def test_stale_file_cleanup_on_full_export(self, setup):
        """Full export (changed_since=None) removes files for tasks no longer active."""
        conn, bridge_dir = setup
        _insert_task(conn, "task-001", "Active")

        # Pre-create stale file for a deleted/archived task
        tasks_dir = os.path.join(bridge_dir, "tasks")
        os.makedirs(tasks_dir, exist_ok=True)
        stale = os.path.join(tasks_dir, "task-ghost.json")
        open(stale, "w").write("{}")

        export_task_files(conn, bridge_dir)

        assert not os.path.exists(stale)
        assert os.path.isfile(os.path.join(tasks_dir, "task-001.json"))

    def test_incremental_export_no_stale_cleanup(self, setup):
        """Incremental export (changed_since set) does NOT delete other task files."""
        conn, bridge_dir = setup
        _insert_task(conn, "task-001", "Active")

        # Pre-create file for another valid task (not in this incremental batch)
        tasks_dir = os.path.join(bridge_dir, "tasks")
        os.makedirs(tasks_dir, exist_ok=True)
        other = os.path.join(tasks_dir, "task-other.json")
        open(other, "w").write("{}")

        # Use a far-future changed_since so task-001 is NOT in the batch
        export_task_files(conn, bridge_dir, changed_since="2099-01-01T00:00:00+00:00")

        # The pre-existing file must survive — cleanup is full-export only
        assert os.path.exists(other)

    def test_incremental_export_returns_only_changed(self, setup):
        """Incremental export returns only tasks updated at or after changed_since."""
        conn, bridge_dir = setup
        now = now_iso()
        _insert_task(conn, "task-new", "New task")  # updated_at = now

        exported = export_task_files(conn, bridge_dir, changed_since=now)

        assert "task-new" in exported

    def test_field_versions_included_in_task_file(self, setup):
        """_field_ts in per-task file contains version entries for seeded fields."""
        conn, bridge_dir = setup
        _insert_task(conn, "task-001", "Task with versions")
        upsert_field_versions(
            conn,
            "task-001",
            ("title", "status"),
            timestamp="2026-01-01T00:00:00+00:00",
            machine_id="testmachine",
        )

        export_task_files(conn, bridge_dir)

        data = json_loads(
            open(
                os.path.join(bridge_dir, "tasks", "task-001.json"), encoding="utf-8"
            ).read()
        )
        fts = data["_field_ts"]
        assert "title" in fts
        assert fts["title"] == ["2026-01-01T00:00:00+00:00", "testmachine"]
        assert "status" in fts


# ── Tests: export_index_json ──────────────────────────────────────────────


class TestExportIndexJson:
    def test_creates_index_json(self, setup):
        """export_index_json writes index.json to bridge_dir root."""
        conn, bridge_dir = setup
        export_index_json(conn, bridge_dir)
        assert os.path.isfile(os.path.join(bridge_dir, "index.json"))

    def test_index_format_fields(self, setup):
        """index.json has version=4, format, machine_id, pushed_at, tasks."""
        conn, bridge_dir = setup
        export_index_json(conn, bridge_dir)

        idx = json_loads(
            open(os.path.join(bridge_dir, "index.json"), encoding="utf-8").read()
        )
        assert idx["version"] == 4
        assert idx["format"] == "bridge_v2"
        assert "pushed_at" in idx
        assert "machine_id" in idx
        assert "tasks" in idx

    def test_index_contains_active_tasks(self, setup):
        """Active tasks appear in index.json tasks list."""
        conn, bridge_dir = setup
        _insert_task(conn, "task-001", "Active task")

        count = export_index_json(conn, bridge_dir)

        assert count >= 1
        idx = json_loads(
            open(os.path.join(bridge_dir, "index.json"), encoding="utf-8").read()
        )
        ids = [t["id"] for t in idx["tasks"]]
        assert "task-001" in ids

    def test_index_excludes_archived_from_active_list(self, setup):
        """Archived tasks are not in the main tasks list (only as tombstones)."""
        conn, bridge_dir = setup
        _insert_task(conn, "task-active", "Active")
        _insert_task(conn, "task-arch", "Archived", status="archived")

        export_index_json(conn, bridge_dir)

        idx = json_loads(
            open(os.path.join(bridge_dir, "index.json"), encoding="utf-8").read()
        )
        # Active task present, archived only as tombstone
        non_tombstone_ids = [t["id"] for t in idx["tasks"] if not t.get("_tombstone")]
        assert "task-active" in non_tombstone_ids
        assert "task-arch" not in non_tombstone_ids

    def test_tombstones_for_recently_cancelled(self, setup):
        """Recently cancelled/archived tasks appear as tombstones (_tombstone=True)."""
        conn, bridge_dir = setup
        _insert_task(conn, "task-cancel", "Cancelled task", status="cancelled")

        export_index_json(conn, bridge_dir)

        idx = json_loads(
            open(os.path.join(bridge_dir, "index.json"), encoding="utf-8").read()
        )
        tombstones = [t for t in idx["tasks"] if t.get("_tombstone")]
        tombstone_ids = [t["id"] for t in tombstones]
        assert "task-cancel" in tombstone_ids

    def test_index_field_versions_populated(self, setup):
        """_field_ts present in index task entries for tasks with seeded versions."""
        conn, bridge_dir = setup
        _insert_task(conn, "task-001", "Versioned task")
        upsert_field_versions(
            conn,
            "task-001",
            ("title",),
            timestamp="2026-02-01T00:00:00+00:00",
            machine_id="machine-x",
        )

        export_index_json(conn, bridge_dir)

        idx = json_loads(
            open(os.path.join(bridge_dir, "index.json"), encoding="utf-8").read()
        )
        task_entry = next(t for t in idx["tasks"] if t["id"] == "task-001")
        assert "_field_ts" in task_entry
        assert task_entry["_field_ts"].get("title") == [
            "2026-02-01T00:00:00+00:00",
            "machine-x",
        ]

    def test_index_returns_correct_count(self, setup):
        """Return value equals total tasks in index (active + tombstones)."""
        conn, bridge_dir = setup
        _insert_task(conn, "task-a1", "Active 1")
        _insert_task(conn, "task-a2", "Active 2")
        _insert_task(conn, "task-c1", "Cancelled", status="cancelled")

        count = export_index_json(conn, bridge_dir)

        assert count == 3  # 2 active + 1 tombstone

    def test_empty_db_produces_valid_index(self, setup):
        """Empty DB writes a valid index.json with an empty tasks list."""
        conn, bridge_dir = setup
        count = export_index_json(conn, bridge_dir)

        assert count == 0
        idx = json_loads(
            open(os.path.join(bridge_dir, "index.json"), encoding="utf-8").read()
        )
        assert idx["tasks"] == []
        assert idx["version"] == 4
