"""Unit tests for LWW (Last-Write-Wins) merge logic in merge_import_tasks().

Covers:
  1. Normal merge       — new remote task inserted locally
  2. Field update       — remote newer field wins
  3. Local wins         — local newer field kept
  4. Conflict tie-break — same timestamp, lexicographic machine_id winner
  5. Tombstone merge    — remote tombstone updates local status via LWW
  6. Clock skew         — remote ts far ahead still processed (warning only)
  7. NULL-fill          — local NULL description adopted from remote regardless of import_content
  8. import_content     — content fields included/excluded per flag
  9. New task field_ts  — new task seeds field_versions from remote _field_ts
"""

import os
import sys
import sqlite3

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from db_utils import (
    _field_version_sort_key,
    _pack_logical_clock,
    merge_import_tasks,
    now_iso,
    upsert_field_versions,
)

# ── Fixture ───────────────────────────────────────────────────────────────


@pytest.fixture
def conn(tmp_path):
    db_path = str(tmp_path / "test.db")
    c = sqlite3.connect(db_path, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    c.executescript("""
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY, title TEXT NOT NULL, description TEXT,
            status TEXT DEFAULT 'not_started', section TEXT DEFAULT 'inbox',
            priority TEXT DEFAULT 'medium', due_date TEXT, project TEXT,
            parent_id TEXT, notes TEXT, recurring TEXT, reminder_at TEXT,
            type TEXT NOT NULL DEFAULT 'task', assignee TEXT, shared_by TEXT,
            visibility TEXT DEFAULT 'private', publish_requested_at TEXT,
            created_at TEXT, updated_at TEXT
        );
        CREATE TABLE task_field_versions (
            task_id TEXT NOT NULL, field_name TEXT NOT NULL,
            updated_at TEXT NOT NULL, updated_by TEXT NOT NULL DEFAULT '',
            old_value TEXT DEFAULT NULL, new_value TEXT DEFAULT NULL,
            PRIMARY KEY (task_id, field_name),
            FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
        );
        CREATE TABLE entities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE, entity_type TEXT NOT NULL,
            project TEXT, created_at TEXT, updated_at TEXT
        );
        CREATE TABLE task_entity_links (
            task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            entity_id INTEGER NOT NULL,
            link_type TEXT NOT NULL DEFAULT 'manual',
            score REAL DEFAULT NULL, created_at TEXT NOT NULL,
            PRIMARY KEY (task_id, entity_id)
        );
        CREATE TABLE IF NOT EXISTS memory_events (
            event_id TEXT PRIMARY KEY, event_type TEXT NOT NULL,
            aggregate_kind TEXT NOT NULL, aggregate_id TEXT NOT NULL,
            field_name TEXT NULL, actor_type TEXT NOT NULL DEFAULT 'system',
            actor_id TEXT NULL, machine_id TEXT NOT NULL DEFAULT '',
            tool_name TEXT NOT NULL DEFAULT 'merge',
            logical_clock INTEGER NOT NULL DEFAULT 0, event_ts TEXT NOT NULL DEFAULT '',
            old_value TEXT NULL, new_value TEXT NULL, payload_json TEXT NULL,
            parent_event_id TEXT NULL, source_kind TEXT NULL, source_ref TEXT NULL,
            source_excerpt TEXT NULL, source_start INTEGER NULL, source_end INTEGER NULL
        );
        CREATE TABLE IF NOT EXISTS memory_cursors (
            machine_id TEXT PRIMARY KEY, last_clock INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        );
    """)
    yield c
    c.close()


# ── Helpers ───────────────────────────────────────────────────────────────


def _insert_task(
    conn,
    tid,
    title="Test",
    status="not_started",
    priority="medium",
    description=None,
    updated_at=None,
):
    ts = updated_at or now_iso()
    conn.execute(
        "INSERT INTO tasks (id, title, status, priority, description, "
        "section, type, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (tid, title, status, priority, description, "inbox", "task", ts, ts),
    )
    return ts


def _fv(conn, tid, field):
    """Return (updated_at, updated_by) for a task field version, or None."""
    row = conn.execute(
        "SELECT updated_at, updated_by FROM task_field_versions "
        "WHERE task_id=? AND field_name=?",
        (tid, field),
    ).fetchone()
    return (row["updated_at"], row["updated_by"]) if row else None


def _task(conn, tid):
    return conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()


# ── Test 1: Normal merge — new task ──────────────────────────────────────


def test_new_task_inserted(conn):
    remote = [
        {
            "id": "task-aaa",
            "title": "New from remote",
            "status": "in_progress",
            "priority": "high",
            "section": "inbox",
            "type": "task",
            "created_at": "2026-01-01T10:00:00",
            "updated_at": "2026-01-01T10:00:00",
        }
    ]
    new_count, updated = merge_import_tasks(conn, remote)

    assert new_count == 1
    row = _task(conn, "task-aaa")
    assert row is not None
    assert row["title"] == "New from remote"
    assert row["status"] == "in_progress"
    assert row["priority"] == "high"


# ── Test 2: Field update — remote newer wins ──────────────────────────────


def test_remote_newer_field_wins(conn):
    tid = "task-bbb"
    old_ts = "2026-01-01T09:00:00"
    new_ts = "2026-01-01T10:00:00"

    _insert_task(conn, tid, title="Old title", updated_at=old_ts)
    upsert_field_versions(
        conn, tid, ["title"], timestamp=old_ts, machine_id="machine-A"
    )

    remote = [
        {
            "id": tid,
            "title": "Updated title",
            "updated_at": new_ts,
            "_field_ts": {"title": [new_ts, "machine-B"]},
        }
    ]
    _, updated = merge_import_tasks(conn, remote)

    assert updated >= 1
    assert _task(conn, tid)["title"] == "Updated title"
    fv = _fv(conn, tid, "title")
    assert fv is not None
    assert fv[0] == new_ts
    assert fv[1] == "machine-B"


# ── Test 3: Local wins — local has newer timestamp ────────────────────────


def test_local_newer_field_kept(conn):
    tid = "task-ccc"
    local_ts = "2026-01-01T12:00:00"
    remote_ts = "2026-01-01T10:00:00"

    _insert_task(conn, tid, title="Local title", updated_at=local_ts)
    upsert_field_versions(
        conn, tid, ["title"], timestamp=local_ts, machine_id="machine-A"
    )

    remote = [
        {
            "id": tid,
            "title": "Remote stale title",
            "updated_at": remote_ts,
            "_field_ts": {"title": [remote_ts, "machine-B"]},
        }
    ]
    _, updated = merge_import_tasks(conn, remote)

    # The title field should not have been overwritten
    assert _task(conn, tid)["title"] == "Local title"


# ── Test 4: Tie-break — same timestamp, lexicographic machine_id ──────────


def test_conflict_tie_break_machine_id(conn):
    tid = "task-ddd"
    ts = "2026-01-01T10:00:00"

    _insert_task(conn, tid, title="Local title", updated_at=ts)
    # machine-A < machine-Z lexicographically
    upsert_field_versions(conn, tid, ["title"], timestamp=ts, machine_id="machine-A")

    remote = [
        {
            "id": tid,
            "title": "Remote title",
            "updated_at": ts,
            "_field_ts": {"title": [ts, "machine-Z"]},
        }
    ]
    _, updated = merge_import_tasks(conn, remote)

    # machine-Z > machine-A → remote wins
    assert _task(conn, tid)["title"] == "Remote title"


def test_conflict_tie_break_local_higher_machine_id(conn):
    tid = "task-eee"
    ts = "2026-01-01T10:00:00"

    _insert_task(conn, tid, title="Local title", updated_at=ts)
    # machine-Z > machine-A → local keeps its value
    upsert_field_versions(conn, tid, ["title"], timestamp=ts, machine_id="machine-Z")

    remote = [
        {
            "id": tid,
            "title": "Remote title",
            "updated_at": ts,
            "_field_ts": {"title": [ts, "machine-A"]},
        }
    ]
    merge_import_tasks(conn, remote)

    assert _task(conn, tid)["title"] == "Local title"


# ── Test 5: Tombstone merge ───────────────────────────────────────────────


def test_tombstone_newer_updates_status(conn):
    tid = "task-fff"
    old_ts = "2026-01-01T09:00:00"
    new_ts = "2026-01-01T11:00:00"

    _insert_task(conn, tid, status="in_progress", updated_at=old_ts)
    upsert_field_versions(
        conn, tid, ["status"], timestamp=old_ts, machine_id="machine-A"
    )

    remote = [
        {
            "id": tid,
            "_tombstone": True,
            "status": "cancelled",
            "updated_at": new_ts,
            "_field_ts": {"status": [new_ts, "machine-B"]},
        }
    ]
    _, updated = merge_import_tasks(conn, remote)

    assert updated == 1
    assert _task(conn, tid)["status"] == "cancelled"


def test_tombstone_older_does_not_overwrite_local(conn):
    tid = "task-ggg"
    local_ts = "2026-01-01T12:00:00"
    remote_ts = "2026-01-01T09:00:00"

    _insert_task(conn, tid, status="done", updated_at=local_ts)
    upsert_field_versions(
        conn, tid, ["status"], timestamp=local_ts, machine_id="machine-A"
    )

    remote = [
        {
            "id": tid,
            "_tombstone": True,
            "status": "cancelled",
            "updated_at": remote_ts,
            "_field_ts": {"status": [remote_ts, "machine-B"]},
        }
    ]
    merge_import_tasks(conn, remote)

    assert _task(conn, tid)["status"] == "done"


def test_tombstone_wins_when_field_ts_equal_but_updated_at_newer(conn):
    """Tombstone with equal _field_ts but newer updated_at should win.

    This covers the blind-audit-trail bug: archival updates updated_at but
    not field_versions, so _field_ts[status] is stale. Fallback to updated_at.
    """
    tid = "tomb-fallback"
    same_ts = "2026-03-09T08:00:00"
    _insert_task(conn, tid, title="Old task", status="done", updated_at=same_ts)
    upsert_field_versions(conn, tid, ["status"], timestamp=same_ts, machine_id="fedora")

    remote = [
        {
            "id": tid,
            "title": "Old task",
            "status": "archived",
            "updated_at": "2026-03-19T06:00:00",  # Much newer
            "_tombstone": True,
            "_field_ts": {"status": [same_ts, "fedora"]},  # Same as local!
        }
    ]
    _, updated = merge_import_tasks(conn, remote, import_content=False)
    assert updated > 0
    row = _task(conn, tid)
    assert row["status"] == "archived"


def test_tombstone_nonexistent_task_materialized(conn):
    """Tombstones must materialize missing archived/cancelled rows on bootstrap."""
    remote = [
        {
            "id": "task-ghost",
            "title": "Ghost task",
            "_tombstone": True,
            "status": "cancelled",
            "updated_at": "2026-01-01T10:00:00",
            "created_at": "2025-12-31T10:00:00",
            "description": "Recovered tombstone description",
            "notes": "Recovered tombstone notes",
            "_field_ts": {"status": ["2026-01-01T10:00:00", "machine-B"]},
        }
    ]
    new_count, updated = merge_import_tasks(conn, remote, import_content=True)

    assert new_count == 1
    assert updated == 0
    row = _task(conn, "task-ghost")
    assert row is not None
    assert row["title"] == "Ghost task"
    assert row["status"] == "cancelled"
    assert row["description"] == "Recovered tombstone description"
    assert row["notes"] == "Recovered tombstone notes"
    assert _fv(conn, "task-ghost", "status") == ("2026-01-01T10:00:00", "machine-B")


# ── Test 6: Clock skew ────────────────────────────────────────────────────


def test_clock_skew_still_merges(conn):
    """Remote timestamp far in the future still merges correctly (warning is non-fatal)."""
    tid = "task-hhh"
    far_future = "2099-12-31T23:59:59"

    remote = [
        {
            "id": tid,
            "title": "Future task",
            "status": "not_started",
            "type": "task",
            "created_at": far_future,
            "updated_at": far_future,
        }
    ]
    new_count, _ = merge_import_tasks(conn, remote)

    assert new_count == 1
    assert _task(conn, tid) is not None


# ── Test 7: NULL-fill ─────────────────────────────────────────────────────


def test_null_fill_description_from_remote(conn):
    """Local task with NULL description adopts remote content only when import_content=True."""
    tid = "task-iii"
    ts = "2026-01-01T10:00:00"

    _insert_task(conn, tid, description=None, updated_at=ts)
    upsert_field_versions(
        conn,
        tid,
        ["description"],
        timestamp="2026-01-02T10:00:00",
        machine_id="machine-A",
    )

    remote = [
        {
            "id": tid,
            "description": "Remote content adopted",
            "updated_at": ts,
            "_field_ts": {"description": [ts, "machine-B"]},
        }
    ]
    # PF-02 fix: import_content=False must NOT null-fill content
    merge_import_tasks(conn, remote, import_content=False)
    assert _task(conn, tid)["description"] is None

    # But import_content=True SHOULD null-fill
    merge_import_tasks(conn, remote, import_content=True)
    assert _task(conn, tid)["description"] == "Remote content adopted"


def test_null_fill_does_not_overwrite_existing_content(conn):
    """NULL-fill must NOT clobber existing local description."""
    tid = "task-jjj"
    ts = "2026-01-01T10:00:00"

    _insert_task(conn, tid, description="Local content", updated_at=ts)
    upsert_field_versions(
        conn,
        tid,
        ["description"],
        timestamp="2026-01-02T10:00:00",
        machine_id="machine-A",
    )

    remote = [
        {
            "id": tid,
            "description": "Remote content",
            "updated_at": ts,
            "_field_ts": {"description": [ts, "machine-B"]},
        }
    ]
    merge_import_tasks(conn, remote, import_content=False)

    assert _task(conn, tid)["description"] == "Local content"


# ── Test 8: import_content=True vs False ─────────────────────────────────


def test_import_content_false_excludes_content_from_lww(conn):
    """With import_content=False, remote description/notes skipped by LWW even if remote is newer."""
    tid = "task-kkk"
    old_ts = "2026-01-01T09:00:00"
    new_ts = "2026-01-01T11:00:00"

    _insert_task(conn, tid, description="Local desc", updated_at=old_ts)
    # Seed with old timestamp so remote is newer
    upsert_field_versions(
        conn, tid, ["description"], timestamp=old_ts, machine_id="machine-A"
    )

    remote = [
        {
            "id": tid,
            "description": "Remote desc (should be LWW-blocked)",
            "notes": "Remote notes (should be LWW-blocked)",
            "updated_at": new_ts,
            "_field_ts": {
                "description": [new_ts, "machine-B"],
                "notes": [new_ts, "machine-B"],
            },
        }
    ]
    merge_import_tasks(conn, remote, import_content=False)

    # LWW is skipped for content fields when import_content=False
    # NULL-fill won't apply either because local description is not NULL
    assert _task(conn, tid)["description"] == "Local desc"


def test_import_content_true_merges_content_fields(conn):
    """With import_content=True, remote description wins via LWW when remote is newer."""
    tid = "task-lll"
    old_ts = "2026-01-01T09:00:00"
    new_ts = "2026-01-01T11:00:00"

    _insert_task(conn, tid, description="Local desc", updated_at=old_ts)
    upsert_field_versions(
        conn, tid, ["description"], timestamp=old_ts, machine_id="machine-A"
    )

    remote = [
        {
            "id": tid,
            "description": "Remote desc wins",
            "updated_at": new_ts,
            "_field_ts": {"description": [new_ts, "machine-B"]},
        }
    ]
    _, updated = merge_import_tasks(conn, remote, import_content=True)

    assert updated >= 1
    assert _task(conn, tid)["description"] == "Remote desc wins"


def test_metadata_only_merge_preserves_newer_task_updated_at(conn):
    tid = "task-meta-updated-at"
    local_ts = "2026-01-01T10:00:00"
    remote_ts = "2026-01-03T12:00:00"

    _insert_task(conn, tid, title="Stable title", updated_at=local_ts)
    upsert_field_versions(
        conn,
        tid,
        ["title", "status", "priority", "section", "type"],
        timestamp=local_ts,
        machine_id="machine-A",
    )

    remote = [
        {
            "id": tid,
            "title": "Stable title",
            "status": "not_started",
            "priority": "medium",
            "section": "inbox",
            "type": "task",
            "updated_at": remote_ts,
            "_field_ts": {
                "title": [local_ts, "machine-A"],
                "status": [local_ts, "machine-A"],
                "priority": [local_ts, "machine-A"],
                "section": [local_ts, "machine-A"],
                "type": [local_ts, "machine-A"],
            },
        }
    ]

    _, updated = merge_import_tasks(conn, remote, import_content=False)

    assert updated == 1
    assert _task(conn, tid)["updated_at"] == remote_ts
    assert _fv(conn, tid, "title") == (local_ts, "machine-A")


def test_new_task_content_excluded_when_import_content_false(conn):
    """New task inserted with import_content=False must have NULL description/notes."""
    remote = [
        {
            "id": "task-mmm",
            "title": "Task with content",
            "description": "Should be excluded",
            "notes": "Also excluded",
            "status": "not_started",
            "type": "task",
            "created_at": "2026-01-01T10:00:00",
            "updated_at": "2026-01-01T10:00:00",
        }
    ]
    merge_import_tasks(conn, remote, import_content=False)

    row = _task(conn, "task-mmm")
    assert row["description"] is None
    assert row["notes"] is None


def test_new_task_content_included_when_import_content_true(conn):
    """New task inserted with import_content=True must include description/notes."""
    remote = [
        {
            "id": "task-nnn",
            "title": "Task with content",
            "description": "Included",
            "notes": "Also included",
            "status": "not_started",
            "type": "task",
            "created_at": "2026-01-01T10:00:00",
            "updated_at": "2026-01-01T10:00:00",
        }
    ]
    merge_import_tasks(conn, remote, import_content=True)

    row = _task(conn, "task-nnn")
    assert row["description"] == "Included"
    assert row["notes"] == "Also included"


# ── Test 9: New task seeds field_versions from _field_ts ─────────────────


def test_new_task_seeds_field_versions_from_remote_field_ts(conn):
    """New task created from remote must seed task_field_versions from _field_ts."""
    tid = "task-ooo"
    specific_ts = "2026-01-15T08:30:00"
    specific_by = "peer-machine"

    remote = [
        {
            "id": tid,
            "title": "Seeded task",
            "status": "in_progress",
            "priority": "high",
            "type": "task",
            "created_at": "2026-01-15T08:30:00",
            "updated_at": "2026-01-15T08:30:00",
            "_field_ts": {
                "title": [specific_ts, specific_by],
                "status": [specific_ts, specific_by],
                "priority": [specific_ts, specific_by],
            },
        }
    ]
    new_count, _ = merge_import_tasks(conn, remote)

    assert new_count == 1

    # Fields present in _field_ts must be seeded with those exact values
    fv_title = _fv(conn, tid, "title")
    fv_status = _fv(conn, tid, "status")
    fv_priority = _fv(conn, tid, "priority")

    assert fv_title == (specific_ts, specific_by)
    assert fv_status == (specific_ts, specific_by)
    assert fv_priority == (specific_ts, specific_by)


def test_new_task_field_ts_fallback_to_updated_at(conn):
    """New task without _field_ts falls back to task-level updated_at for all field versions."""
    import db_utils

    original_machine_id = db_utils.MACHINE_ID

    tid = "task-ppp"
    task_ts = "2026-02-01T12:00:00"

    remote = [
        {
            "id": tid,
            "title": "No field_ts task",
            "status": "not_started",
            "type": "task",
            "created_at": task_ts,
            "updated_at": task_ts,
            # No _field_ts key
        }
    ]
    merge_import_tasks(conn, remote)

    # Without _field_ts, fallback_ts = updated_at and machine_id = MACHINE_ID
    fv_title = _fv(conn, tid, "title")
    assert fv_title is not None
    assert fv_title[0] == task_ts
    assert fv_title[1] == original_machine_id


# ── Test 10: LWW Content Protection ──────────────────────────────────────


def test_lww_content_protection_no_nullify(conn):
    """LWW must not overwrite non-NULL local content with NULL remote."""
    tid = "test-content-protect"
    old_ts = "2026-01-01T00:00:00"
    new_ts = "2026-01-02T00:00:00"

    _insert_task(conn, tid, description="Important content", updated_at=old_ts)
    upsert_field_versions(
        conn, tid, ["description"], timestamp=old_ts, machine_id="machine-A"
    )

    remote = [
        {
            "id": tid,
            "title": "Test",
            "status": "not_started",
            "description": None,  # Remote has NULL
            "_field_ts": {"description": [new_ts, "machine-B"]},  # But newer timestamp
            "updated_at": new_ts,
        }
    ]
    merge_import_tasks(conn, remote, import_content=True)

    row = conn.execute("SELECT description FROM tasks WHERE id=?", (tid,)).fetchone()
    assert row["description"] == "Important content"  # Local preserved


def test_lww_content_protection_blocks_drastic_shrink(conn):
    """LWW must not overwrite substantial local content with a tiny remote value."""
    tid = "test-content-shrink"
    old_ts = "2026-01-01T00:00:00"
    new_ts = "2026-01-02T00:00:00"
    local_desc = "L" * 2400

    _insert_task(conn, tid, description=local_desc, updated_at=old_ts)
    upsert_field_versions(
        conn, tid, ["description"], timestamp=old_ts, machine_id="machine-A"
    )

    remote = [
        {
            "id": tid,
            "title": "Test",
            "status": "not_started",
            "description": "tiny remote summary",
            "_field_ts": {"description": [new_ts, "machine-B"]},
            "updated_at": new_ts,
        }
    ]
    _, updated = merge_import_tasks(conn, remote, import_content=True)

    row = conn.execute("SELECT description FROM tasks WHERE id=?", (tid,)).fetchone()
    assert row["description"] == local_desc
    assert _fv(conn, tid, "description") == (old_ts, "machine-A")
    assert updated >= 0


def test_lww_content_overwrite_when_remote_has_value(conn):
    """LWW should still overwrite when remote has actual new content."""
    tid = "test-content-overwrite"
    old_ts = "2026-01-01T00:00:00"
    new_ts = "2026-01-02T00:00:00"

    _insert_task(conn, tid, description="Old content", updated_at=old_ts)
    upsert_field_versions(
        conn, tid, ["description"], timestamp=old_ts, machine_id="machine-A"
    )

    remote = [
        {
            "id": tid,
            "title": "Test",
            "status": "not_started",
            "description": "New better content",  # Remote has actual content
            "_field_ts": {"description": [new_ts, "machine-B"]},
            "updated_at": new_ts,
        }
    ]
    merge_import_tasks(conn, remote, import_content=True)

    row = conn.execute("SELECT description FROM tasks WHERE id=?", (tid,)).fetchone()
    assert row["description"] == "New better content"  # Remote wins


# ── Test 11: Same-title tasks keep distinct UUIDs ────────────────────────


def test_same_title_remote_task_survives_cancelled_local(conn):
    """A distinct UUID must not be dropped just because a cancelled task shares its title."""
    _insert_task(
        conn, "old-1", title="Weekly review", status="cancelled", updated_at=now_iso()
    )
    upsert_field_versions(conn, "old-1", ["status"], now_iso())

    remote = [
        {
            "id": "new-uuid-dedup",
            "title": "Weekly review",
            "status": "not_started",
            "updated_at": now_iso(),
            "_field_ts": {},
        }
    ]
    new_count, _ = merge_import_tasks(conn, remote, import_content=False)
    assert new_count == 1
    assert (
        conn.execute("SELECT id FROM tasks WHERE id='new-uuid-dedup'").fetchone()
        is not None
    )


def test_same_title_remote_task_survives_active_local(conn):
    """A distinct UUID must remain a distinct task even when titles collide."""
    _insert_task(
        conn,
        "active-1",
        title="Deploy pipeline",
        status="not_started",
        updated_at=now_iso(),
    )

    remote = [
        {
            "id": "new-uuid-active-dup",
            "title": "Deploy pipeline",
            "status": "not_started",
            "updated_at": now_iso(),
            "_field_ts": {},
        }
    ]
    new_count, _ = merge_import_tasks(conn, remote, import_content=False)
    assert new_count == 1
    assert (
        conn.execute("SELECT id FROM tasks WHERE id='new-uuid-active-dup'").fetchone()
        is not None
    )


def test_same_title_remote_task_survives_done_local(conn):
    """Completed local tasks must not shadow new remote UUIDs."""
    _insert_task(
        conn, "done-1", title="Fix auth bug", status="done", updated_at=now_iso()
    )

    remote = [
        {
            "id": "new-uuid-done-dup",
            "title": "Fix auth bug",
            "status": "not_started",
            "updated_at": now_iso(),
            "_field_ts": {},
        }
    ]
    new_count, _ = merge_import_tasks(conn, remote, import_content=False)
    assert new_count == 1
    assert (
        conn.execute("SELECT id FROM tasks WHERE id='new-uuid-done-dup'").fetchone()
        is not None
    )


def test_dedup_guard_allows_different_title(conn):
    """Different titles continue to insert normally."""
    _insert_task(
        conn, "old-2", title="Weekly review", status="cancelled", updated_at=now_iso()
    )

    remote = [
        {
            "id": "new-uuid-diff",
            "title": "Daily standup",
            "status": "not_started",
            "updated_at": now_iso(),
            "_field_ts": {},
        }
    ]
    new_count, _ = merge_import_tasks(conn, remote, import_content=False)
    assert new_count == 1
    assert (
        conn.execute("SELECT id FROM tasks WHERE id='new-uuid-diff'").fetchone()
        is not None
    )


def test_legacy_updated_order_falls_back_to_timestamp_ordering():
    """Old scalar counters must not beat newer remote timestamps across machines."""
    local_key = _field_version_sort_key(
        "2026-03-31T10:00:00+00:00",
        "machine-A",
        100,
    )
    remote_key = _field_version_sort_key(
        "2026-03-31T11:00:00+00:00",
        "machine-B",
        5,
    )

    assert remote_key > local_key


def test_packed_logical_clock_order_is_globally_comparable():
    older = _pack_logical_clock(1_743_412_800_000, 0)
    newer = _pack_logical_clock(1_743_412_801_000, 0)

    assert _field_version_sort_key("2026-03-31T10:00:01+00:00", "machine-B", newer) > (
        _field_version_sort_key("2026-03-31T10:00:00+00:00", "machine-A", older)
    )


def test_merge_repairs_local_stale_status_from_field_event_authority(conn):
    tid = "task-status-repair"
    task_ts = "2026-04-01T06:09:06.748385+00:00"
    status_ts = "2026-03-31T16:06:01.347617+00:00"
    clock = 116324641102036992

    # Ensure extra columns exist (fixture now includes full schema)
    try:
        conn.execute(
            "ALTER TABLE task_field_versions ADD COLUMN updated_order INTEGER NOT NULL DEFAULT 0"
        )
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute(
            "ALTER TABLE task_field_versions ADD COLUMN source_event_id TEXT DEFAULT NULL"
        )
    except sqlite3.OperationalError:
        pass
    # memory_events already created by fixture; this is a no-op
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_events (
            event_id TEXT PRIMARY KEY,
            event_type TEXT,
            aggregate_kind TEXT,
            aggregate_id TEXT,
            field_name TEXT,
            machine_id TEXT,
            logical_clock INTEGER,
            event_ts TEXT,
            new_value TEXT
        )
        """
    )

    _insert_task(conn, tid, title="Repair me", status="not_started", updated_at=task_ts)
    conn.execute(
        "INSERT INTO task_field_versions "
        "(task_id, field_name, updated_at, updated_by, old_value, new_value, updated_order, source_event_id) "
        "VALUES (?, 'status', ?, ?, ?, ?, ?, ?)",
        (
            tid,
            status_ts,
            "fedora",
            "not_started",
            "done",
            clock,
            "event-status-1",
        ),
    )
    conn.execute(
        "INSERT INTO memory_events "
        "(event_id, event_type, aggregate_kind, aggregate_id, field_name, machine_id, logical_clock, event_ts, new_value) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "event-status-1",
            "task_field_set",
            "task",
            tid,
            "status",
            "fedora",
            clock,
            status_ts,
            "done",
        ),
    )

    remote_events = [
        {
            "event_id": "event-status-1",
            "event_type": "task_field_set",
            "aggregate_kind": "task",
            "aggregate_id": tid,
            "field_name": "status",
            "machine_id": "fedora",
            "logical_clock": clock,
            "event_ts": status_ts,
            "new_value": "done",
        }
    ]
    remote = [
        {
            "id": tid,
            "title": "Repair me",
            "status": "not_started",
            "section": "inbox",
            "priority": "medium",
            "type": "task",
            "updated_at": status_ts,
            "_field_ts": {
                "status": [status_ts, "fedora", clock, "event-status-1"],
            },
        }
    ]

    _, updated = merge_import_tasks(
        conn,
        remote,
        import_content=False,
        remote_events=remote_events,
    )

    assert _task(conn, tid)["status"] == "done"
    assert updated >= 1


def test_merge_records_conflict_objects(conn):
    conn.execute(
        """
        CREATE TABLE memory_conflicts (
            conflict_id TEXT PRIMARY KEY,
            conflict_key TEXT NOT NULL UNIQUE,
            aggregate_kind TEXT NOT NULL,
            aggregate_id TEXT NOT NULL,
            field_name TEXT,
            local_value TEXT,
            remote_value TEXT,
            local_updated_at TEXT,
            remote_updated_at TEXT,
            local_updated_order INTEGER NOT NULL DEFAULT 0,
            remote_updated_order INTEGER NOT NULL DEFAULT 0,
            local_source_event_id TEXT,
            remote_source_event_id TEXT,
            winner TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            rationale TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            resolved_at TEXT
        )
        """
    )
    tid = "task-conflict"
    local_ts = "2026-04-01T08:00:00+00:00"
    remote_ts = "2026-04-01T09:00:00+00:00"
    _insert_task(conn, tid, title="Local title", updated_at=local_ts)
    upsert_field_versions(
        conn, tid, ["title"], timestamp=local_ts, machine_id="machine-A"
    )

    merge_import_tasks(
        conn,
        [
            {
                "id": tid,
                "title": "Remote title",
                "updated_at": remote_ts,
                "_field_ts": {"title": [remote_ts, "machine-B"]},
            }
        ],
    )

    row = conn.execute(
        "SELECT field_name, winner, status FROM memory_conflicts WHERE aggregate_id = ?",
        (tid,),
    ).fetchone()

    assert row["field_name"] == "title"
    assert row["winner"] == "remote"
    assert row["status"] == "open"
