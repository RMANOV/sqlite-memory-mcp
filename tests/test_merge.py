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
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from db_utils import (
    MACHINE_ID,
    _decode_logical_clock,
    _field_version_sort_key,
    _pack_logical_clock,
    _store_task_field_version,
    canonicalize_exported_task_statuses,
    export_memory_events,
    import_memory_events,
    merge_import_tasks,
    now_iso,
    upsert_field_versions,
)

# ── Fixture ───────────────────────────────────────────────────────────────


_SCHEMA = """
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY, title TEXT NOT NULL, description TEXT,
            status TEXT DEFAULT 'not_started', section TEXT DEFAULT 'inbox',
            priority TEXT DEFAULT 'medium', due_date TEXT, project TEXT,
            parent_id TEXT, notes TEXT, recurring TEXT, reminder_at TEXT,
            type TEXT NOT NULL DEFAULT 'task', assignee TEXT, shared_by TEXT,
            visibility TEXT DEFAULT 'private', publish_requested_at TEXT,
            created_at TEXT, updated_at TEXT, tombstone_pushed_at TEXT DEFAULT NULL
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
"""


def _make_conn(db_path):
    c = sqlite3.connect(str(db_path), isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    c.executescript(_SCHEMA)
    return c


@pytest.fixture
def conn(tmp_path):
    c = _make_conn(tmp_path / "test.db")
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


def _ensure_field_event_columns(conn):
    for ddl in (
        "ALTER TABLE task_field_versions ADD COLUMN updated_order INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE task_field_versions ADD COLUMN source_event_id TEXT DEFAULT NULL",
    ):
        try:
            conn.execute(ddl)
        except sqlite3.OperationalError:
            pass


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


def test_bridge_import_probes_field_version_columns_once_per_batch(conn):
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    try:
        merge_import_tasks(
            conn,
            [
                {
                    "id": "task-batch-probe",
                    "title": "Batch probe",
                    "status": "not_started",
                    "type": "task",
                    "created_at": "2026-01-01T00:00:00+00:00",
                    "updated_at": "2026-01-01T00:00:00+00:00",
                }
            ],
        )
    finally:
        conn.set_trace_callback(None)

    probes = [
        sql
        for sql in statements
        if sql.casefold().startswith("pragma table_info('task_field_versions')")
    ]
    assert len(probes) == 4


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
    row = _task(conn, tid)
    assert row["title"] == "Updated title"
    assert row["updated_at"] == new_ts
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

    assert updated >= 1
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


def test_tombstone_with_stale_field_ts_does_not_win_via_metadata_updated_at(conn):
    """A tombstone with explicit stale _field_ts must not win via row metadata.

    Bridge peers can carry fresh task-level updated_at from metadata-only import
    while the status field itself is old. That must not resurrect stale state.
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
    assert updated == 0
    row = _task(conn, tid)
    assert row["status"] == "done"
    assert row["updated_at"] == same_ts


def test_legacy_tombstone_without_field_ts_can_win_via_updated_at(conn):
    tid = "legacy-tombstone"
    local_ts = "2026-03-09T08:00:00"
    remote_ts = "2026-03-19T06:00:00"
    _insert_task(conn, tid, title="Old task", status="done", updated_at=local_ts)

    remote = [
        {
            "id": tid,
            "title": "Old task",
            "status": "archived",
            "updated_at": remote_ts,
            "_tombstone": True,
        }
    ]
    _, updated = merge_import_tasks(conn, remote, import_content=False)

    assert updated > 0
    row = _task(conn, tid)
    assert row["status"] == "archived"
    assert row["updated_at"] == remote_ts


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


@pytest.mark.parametrize("field_version_format", ["list", "dict"])
def test_clock_skew_clamps_field_timestamp_and_packed_order(conn, field_version_format):
    _ensure_field_event_columns(conn)
    future_ts = "2099-12-31T23:59:59+00:00"
    future_order = _pack_logical_clock(
        int(datetime.fromisoformat(future_ts).timestamp() * 1000), 7
    )
    field_version = (
        [future_ts, "future-peer", future_order, None]
        if field_version_format == "list"
        else {
            "updated_at": future_ts,
            "updated_by": "future-peer",
            "updated_order": future_order,
        }
    )
    task_id = f"task-future-{field_version_format}"
    remote = {
        "id": task_id,
        "title": "Future metadata",
        "status": "not_started",
        "type": "task",
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "_field_ts": {"title": field_version},
    }

    merge_import_tasks(conn, [remote])

    stored = conn.execute(
        "SELECT updated_at, updated_order FROM task_field_versions "
        "WHERE task_id = ? AND field_name = 'title'",
        (task_id,),
    ).fetchone()
    cutoff = datetime.now(timezone.utc) + timedelta(seconds=5)
    assert datetime.fromisoformat(stored["updated_at"]) <= cutoff
    physical_ms, counter = _decode_logical_clock(stored["updated_order"])
    assert physical_ms <= int(cutoff.timestamp() * 1000)
    assert counter == 7


def test_clock_skew_reclamps_status_rebuilt_from_event_head(conn):
    _ensure_field_event_columns(conn)
    task_id = "task-future-event-head"
    past_ts = "2026-01-01T00:00:00+00:00"
    future_ts = "2099-12-31T23:59:59+00:00"
    future_order = _pack_logical_clock(
        int(datetime.fromisoformat(future_ts).timestamp() * 1000), 11
    )
    remote = {
        "id": task_id,
        "title": "Future status event",
        "status": "not_started",
        "type": "task",
        "created_at": past_ts,
        "updated_at": past_ts,
        "_field_ts": {
            "status": {
                "updated_at": past_ts,
                "updated_by": "future-peer",
                "new_value": "not_started",
            }
        },
    }
    remote_events = [
        {
            "event_id": "future-status-event",
            "aggregate_kind": "task",
            "aggregate_id": task_id,
            "field_name": "status",
            "machine_id": "future-peer",
            "logical_clock": future_order,
            "event_ts": future_ts,
            "new_value": "done",
        }
    ]

    merge_import_tasks(conn, [remote], remote_events=remote_events)

    task = _task(conn, task_id)
    stored = conn.execute(
        "SELECT updated_at, updated_order FROM task_field_versions "
        "WHERE task_id = ? AND field_name = 'status'",
        (task_id,),
    ).fetchone()
    cutoff = datetime.now(timezone.utc) + timedelta(seconds=5)
    assert task["status"] == "done"
    assert datetime.fromisoformat(task["updated_at"]) <= cutoff
    assert datetime.fromisoformat(stored["updated_at"]) <= cutoff
    physical_ms, counter = _decode_logical_clock(stored["updated_order"])
    assert physical_ms <= int(cutoff.timestamp() * 1000)
    assert counter == 11


def test_clock_skew_preserves_tombstone_dominance_across_hlc_counter_carry(conn):
    _ensure_field_event_columns(conn)
    task_id = "task-future-counter-carry"
    future_ts = "2030-01-01T00:00:00+00:00"
    local_order = _pack_logical_clock(
        int(datetime.fromisoformat(future_ts).timestamp() * 1000), 65_535
    )
    _insert_task(
        conn,
        task_id,
        status="in_progress",
        updated_at=future_ts,
    )
    _store_task_field_version(
        conn,
        task_id,
        "status",
        updated_at=future_ts,
        updated_by="active-peer",
        updated_order=local_order,
    )
    remote = {
        "id": task_id,
        "title": "Dominating tombstone",
        "status": "archived",
        "type": "task",
        "_tombstone": True,
        "updated_at": future_ts,
        "_field_ts": {
            "status": {
                "updated_at": future_ts,
                "updated_by": "tombstone-merge",
                "updated_order": local_order + 1,
                "value": "archived",
            }
        },
    }

    merge_import_tasks(conn, [remote])

    task = _task(conn, task_id)
    stored = conn.execute(
        "SELECT updated_order FROM task_field_versions "
        "WHERE task_id = ? AND field_name = 'status'",
        (task_id,),
    ).fetchone()
    physical_ms, _counter = _decode_logical_clock(stored["updated_order"])
    cutoff_ms = int(
        (datetime.now(timezone.utc) + timedelta(seconds=5)).timestamp() * 1000
    )
    assert task["status"] == "archived"
    assert physical_ms <= cutoff_ms


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


def test_metadata_only_merge_does_not_pollute_task_updated_at(conn):
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

    assert updated == 0
    assert _task(conn, tid)["updated_at"] == local_ts
    assert _fv(conn, tid, "title") == (local_ts, "machine-A")


def test_stale_remote_status_does_not_win_with_newer_task_updated_at(conn):
    tid = "task-stale-status"
    local_status_ts = "2026-05-21T13:36:31.617013+00:00"
    remote_status_ts = "2026-05-18T20:19:22.198077+00:00"
    metadata_ts = "2026-05-22T05:34:57.560101+00:00"

    _insert_task(
        conn, tid, title="Migrated note", status="done", updated_at=local_status_ts
    )
    upsert_field_versions(
        conn,
        tid,
        ["status"],
        timestamp=local_status_ts,
        machine_id="RManov",
    )

    remote = [
        {
            "id": tid,
            "title": "Migrated note",
            "status": "not_started",
            "updated_at": metadata_ts,
            "_field_ts": {
                "status": [remote_status_ts, "fedora"],
                "title": [local_status_ts, "RManov"],
            },
        }
    ]

    _, updated = merge_import_tasks(conn, remote, import_content=False)

    row = _task(conn, tid)
    assert updated == 0
    assert row["status"] == "done"
    assert row["updated_at"] == local_status_ts
    assert _fv(conn, tid, "status") == (local_status_ts, "RManov")


def test_legacy_same_field_ts_source_machine_repairs_status_and_section_once(conn):
    tid = "task-legacy-source-authority"
    field_ts = "2026-05-22T05:34:57.560101+00:00"
    stale_ts = "2026-05-22T05:00:00.000000+00:00"

    _insert_task(
        conn, tid, title="Source authority", status="not_started", updated_at=stale_ts
    )
    upsert_field_versions(
        conn,
        tid,
        ["status", "section"],
        timestamp=field_ts,
        machine_id="fedora",
    )

    authoritative_remote = {
        "id": tid,
        "machine_id": "fedora",
        "source_machine": "fedora",
        "title": "Source authority",
        "status": "done",
        "section": "today",
        "updated_at": field_ts,
        "_field_ts": {
            "status": [field_ts, "fedora"],
            "section": [field_ts, "fedora"],
        },
    }
    peer_remote = {
        "id": tid,
        "machine_id": "windows",
        "source_machine": "windows",
        "title": "Source authority",
        "status": "not_started",
        "section": "inbox",
        "updated_at": field_ts,
        "_field_ts": {
            "status": [field_ts, "fedora"],
            "section": [field_ts, "fedora"],
        },
    }

    merge_import_tasks(conn, [authoritative_remote], import_content=False)
    row = _task(conn, tid)
    assert row["status"] == "done"
    assert row["section"] == "today"

    merge_import_tasks(conn, [peer_remote], import_content=False)
    row = _task(conn, tid)
    assert row["status"] == "done"
    assert row["section"] == "today"
    assert _fv(conn, tid, "status") == (field_ts, "fedora")
    assert _fv(conn, tid, "section") == (field_ts, "fedora")


def test_status_event_authority_repairs_then_peer_payload_cannot_revert(conn):
    tid = "task-status-event-authority-peer"
    field_ts = "2026-05-22T05:34:57.560101+00:00"
    clock = 116616599801692170

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

    _insert_task(
        conn, tid, title="Event authority", status="not_started", updated_at=field_ts
    )
    upsert_field_versions(
        conn,
        tid,
        ["status"],
        timestamp=field_ts,
        machine_id="fedora",
    )

    remote_events = [
        {
            "event_id": "event-status-done",
            "event_type": "task_field_set",
            "aggregate_kind": "task",
            "aggregate_id": tid,
            "field_name": "status",
            "machine_id": "fedora",
            "logical_clock": clock,
            "event_ts": field_ts,
            "new_value": "done",
        }
    ]
    event_authoritative_remote = {
        "id": tid,
        "machine_id": "windows",
        "source_machine": "windows",
        "title": "Event authority",
        "status": "not_started",
        "updated_at": field_ts,
        "_field_ts": {"status": [field_ts, "fedora", clock, "event-status-done"]},
    }
    stale_peer_remote = {
        "id": tid,
        "machine_id": "windows",
        "source_machine": "windows",
        "title": "Event authority",
        "status": "not_started",
        "updated_at": field_ts,
        "_field_ts": {"status": [field_ts, "fedora", clock, "event-status-done"]},
    }

    merge_import_tasks(
        conn,
        [event_authoritative_remote],
        import_content=False,
        remote_events=remote_events,
    )
    assert _task(conn, tid)["status"] == "done"

    merge_import_tasks(conn, [stale_peer_remote], import_content=False)
    assert _task(conn, tid)["status"] == "done"
    assert _fv(conn, tid, "status") == (field_ts, "fedora")


def test_legacy_terminal_row_promotes_then_active_peer_cannot_revert(conn):
    tid = "task-legacy-terminal-row"
    stale_field_ts = "2026-04-25T14:45:45.835158+00:00"
    promoted_row_ts = "2026-05-22T12:39:21.167920+00:00"
    stale_clock = _pack_logical_clock(1770000000000, 1)

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

    _insert_task(
        conn,
        tid,
        title="Commerzbank interview prep",
        status="not_started",
        updated_at=promoted_row_ts,
    )
    conn.execute(
        "INSERT OR REPLACE INTO task_field_versions "
        "(task_id, field_name, updated_at, updated_by, updated_order, source_event_id, new_value) "
        "VALUES (?, 'status', ?, 'fedora', ?, ?, 'not_started')",
        (tid, stale_field_ts, stale_clock, "event-fedora-not-started"),
    )

    rmanov_terminal = {
        "id": tid,
        "_source_machine_id": "RManov",
        "title": "ARCHIVE | Commerzbank interview prep | closed after rejection",
        "status": "archived",
        "updated_at": promoted_row_ts,
        "_field_ts": {
            "status": [
                stale_field_ts,
                "fedora",
                stale_clock,
                "event-fedora-not-started",
            ]
        },
    }

    _, updated = merge_import_tasks(conn, [rmanov_terminal], import_content=False)

    row = _task(conn, tid)
    fv = conn.execute(
        "SELECT updated_at, updated_by, updated_order, new_value "
        "FROM task_field_versions WHERE task_id = ? AND field_name = 'status'",
        (tid,),
    ).fetchone()
    assert updated >= 1
    assert row["status"] == "archived"
    assert row["updated_at"] == promoted_row_ts
    assert fv["updated_at"] == promoted_row_ts
    assert fv["updated_by"] == "RManov"
    assert fv["updated_order"] > stale_clock
    assert fv["new_value"] == "archived"

    stale_fedora_peer = {
        "id": tid,
        "_source_machine_id": "fedora",
        "title": "Commerzbank interview prep",
        "status": "not_started",
        "updated_at": promoted_row_ts,
        "_field_ts": {
            "status": [
                stale_field_ts,
                "fedora",
                stale_clock,
                "event-fedora-not-started",
                "not_started",
            ]
        },
    }

    merge_import_tasks(conn, [stale_fedora_peer], import_content=False)
    assert _task(conn, tid)["status"] == "archived"


def test_legacy_active_row_never_promotes_via_fresh_updated_at(conn):
    tid = "task-legacy-active-row"
    local_ts = "2026-05-22T12:40:00+00:00"
    stale_field_ts = "2026-04-25T14:45:45.835158+00:00"
    fresh_row_ts = "2026-05-23T09:00:00+00:00"
    stale_clock = _pack_logical_clock(1770000000000, 1)

    _ensure_field_event_columns(conn)
    _insert_task(
        conn, tid, title="Active row", status="not_started", updated_at=local_ts
    )
    conn.execute(
        "INSERT OR REPLACE INTO task_field_versions "
        "(task_id, field_name, updated_at, updated_by, updated_order, source_event_id, new_value) "
        "VALUES (?, 'status', ?, 'RManov', ?, ?, 'not_started')",
        (
            tid,
            local_ts,
            _pack_logical_clock(1770000100000, 1),
            "event-local-not-started",
        ),
    )

    active_remote = {
        "id": tid,
        "_source_machine_id": "RManov",
        "title": "Active row",
        "status": "in_progress",
        "updated_at": fresh_row_ts,
        "_field_ts": {
            "status": [stale_field_ts, "fedora", stale_clock, "event-fedora-old"]
        },
    }

    _, updated = merge_import_tasks(conn, [active_remote], import_content=False)

    assert updated == 0
    assert _task(conn, tid)["status"] == "not_started"


def test_legacy_terminal_row_does_not_flip_existing_terminal_status(conn):
    tid = "task-terminal-flip"
    local_ts = "2026-05-22T12:40:00+00:00"
    stale_field_ts = "2026-04-25T14:45:45.835158+00:00"
    fresh_row_ts = "2026-05-23T09:00:00+00:00"
    stale_clock = _pack_logical_clock(1770000000000, 1)

    _ensure_field_event_columns(conn)
    _insert_task(
        conn, tid, title="Terminal row", status="archived", updated_at=local_ts
    )
    conn.execute(
        "INSERT OR REPLACE INTO task_field_versions "
        "(task_id, field_name, updated_at, updated_by, updated_order, source_event_id, new_value) "
        "VALUES (?, 'status', ?, 'RManov', ?, ?, 'archived')",
        (tid, local_ts, _pack_logical_clock(1770000100000, 1), "event-local-archived"),
    )

    competing_terminal = {
        "id": tid,
        "_source_machine_id": "RManov",
        "title": "Terminal row",
        "status": "cancelled",
        "updated_at": fresh_row_ts,
        "_field_ts": {
            "status": [stale_field_ts, "fedora", stale_clock, "event-fedora-old"]
        },
    }

    _, updated = merge_import_tasks(conn, [competing_terminal], import_content=False)

    assert updated == 0
    assert _task(conn, tid)["status"] == "archived"


def test_explicit_status_value_blocks_legacy_terminal_row_promotion(conn):
    tid = "task-explicit-status-blocks-promotion"
    local_ts = "2026-05-22T12:40:00+00:00"
    stale_field_ts = "2026-04-25T14:45:45.835158+00:00"
    fresh_row_ts = "2026-05-23T09:00:00+00:00"
    stale_clock = _pack_logical_clock(1770000000000, 1)

    _ensure_field_event_columns(conn)
    _insert_task(
        conn, tid, title="Explicit status", status="not_started", updated_at=local_ts
    )
    conn.execute(
        "INSERT OR REPLACE INTO task_field_versions "
        "(task_id, field_name, updated_at, updated_by, updated_order, source_event_id, new_value) "
        "VALUES (?, 'status', ?, 'RManov', ?, ?, 'not_started')",
        (
            tid,
            local_ts,
            _pack_logical_clock(1770000100000, 1),
            "event-local-not-started",
        ),
    )

    explicit_stale_remote = {
        "id": tid,
        "_source_machine_id": "RManov",
        "title": "Explicit status",
        "status": "archived",
        "updated_at": fresh_row_ts,
        "_field_ts": {
            "status": [
                stale_field_ts,
                "fedora",
                stale_clock,
                "event-fedora-not-started",
                "archived",
            ]
        },
    }

    _, updated = merge_import_tasks(conn, [explicit_stale_remote], import_content=False)

    assert updated == 0
    assert _task(conn, tid)["status"] == "not_started"


def test_source_legacy_status_payload_can_outrank_stale_event_head(conn):
    tid = "task-source-legacy-outranks-event-head"
    stale_ts = "2026-05-22T10:49:20.981515+00:00"
    source_ts = "2026-05-22T10:51:37.961187+00:00"
    stale_clock = 116617836034850816
    source_clock = 116617845011972097

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

    _insert_task(
        conn,
        tid,
        title="Legacy source status",
        status="in_progress",
        updated_at=source_ts,
    )
    conn.execute(
        "INSERT OR REPLACE INTO task_field_versions "
        "(task_id, field_name, updated_at, updated_by, updated_order, source_event_id) "
        "VALUES (?, 'status', ?, 'fedora', ?, ?)",
        (tid, stale_ts, stale_clock, "event-fedora-in-progress"),
    )

    remote_events = [
        {
            "event_id": "event-fedora-in-progress",
            "event_type": "task_field_set",
            "aggregate_kind": "task",
            "aggregate_id": tid,
            "field_name": "status",
            "machine_id": "fedora",
            "logical_clock": stale_clock,
            "event_ts": stale_ts,
            "new_value": "in_progress",
        }
    ]
    rmanov_remote = {
        "id": tid,
        "machine_id": "RManov",
        "source_machine": "RManov",
        "title": "Legacy source status",
        "status": "done",
        "updated_at": source_ts,
        "_field_ts": {
            "status": [
                source_ts,
                "RManov",
                source_clock,
                "event-rmanov-done",
            ]
        },
    }

    _, updated = merge_import_tasks(
        conn,
        [rmanov_remote],
        import_content=False,
        remote_events=remote_events,
    )

    assert updated == 1
    assert _task(conn, tid)["status"] == "done"
    assert _fv(conn, tid, "status") == (source_ts, "RManov")


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


def test_legacy_field_version_sort_normalizes_mixed_offsets():
    newer = _field_version_sort_key(
        "2026-03-24T10:00:00Z",
        "machine-A",
        0,
    )
    older = _field_version_sort_key(
        "2026-03-24T11:00:00+02:00",
        "machine-B",
        0,
    )

    assert newer > older


def test_legacy_mixed_offset_remote_field_does_not_overwrite_newer_local(conn):
    tid = "task-offset-field"
    local_ts = "2026-03-24T10:00:00Z"
    remote_ts = "2026-03-24T11:00:00+02:00"

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

    assert updated == 0
    assert _task(conn, tid)["title"] == "Local title"
    assert _fv(conn, tid, "title") == (local_ts, "machine-A")


def test_metadata_only_merge_preserves_newer_updated_at_across_mixed_offsets(conn):
    tid = "task-offset-updated-at"
    local_ts = "2026-03-24T10:00:00Z"
    remote_ts = "2026-03-24T11:00:00+02:00"

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

    assert updated == 0
    assert _task(conn, tid)["updated_at"] == local_ts


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
    conn.execute(
        "UPDATE tasks SET tombstone_pushed_at = ? WHERE id = ?",
        ("2026-04-02T08:00:00+00:00", tid),
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

    events_before = conn.execute(
        "SELECT COUNT(*) FROM memory_events WHERE aggregate_id = ?",
        (tid,),
    ).fetchone()[0]

    _, updated = merge_import_tasks(
        conn,
        remote,
        import_content=False,
        remote_events=remote_events,
    )

    assert _task(conn, tid)["status"] == "done"
    assert _task(conn, tid)["tombstone_pushed_at"] == "2026-04-02T08:00:00+00:00"
    assert updated >= 1
    events_after_repair = conn.execute(
        "SELECT COUNT(*) FROM memory_events WHERE aggregate_id = ?",
        (tid,),
    ).fetchone()[0]
    assert events_after_repair == events_before

    _, repeated_updates = merge_import_tasks(
        conn,
        remote,
        import_content=False,
        remote_events=remote_events,
    )
    events_after_repeat = conn.execute(
        "SELECT COUNT(*) FROM memory_events WHERE aggregate_id = ?",
        (tid,),
    ).fetchone()[0]

    assert repeated_updates == 0
    assert events_after_repeat == events_before


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
        "SELECT field_name, winner, status, resolved_at FROM memory_conflicts "
        "WHERE aggregate_id = ?",
        (tid,),
    ).fetchone()

    assert row["field_name"] == "title"
    assert row["winner"] == "remote"
    # A winner means the conflict is auto-decided (terminal), not pending human
    # review, so it is recorded resolved with a resolution timestamp.
    assert row["status"] == "resolved"
    assert row["resolved_at"] is not None


# ── Merge authority: a merge absorbs a write, it does not author one ─────


_REMOTE_TS = "2026-07-20T10:00:00+00:00"
_LOCAL_TS = "2026-07-19T10:00:00+00:00"


def _remote_status_win(task_id, remote_order, *, with_null_fill=False):
    """Remote payload whose status field outranks the local one."""
    # 5th element carries the value: without it the legacy-value guard keeps
    # the local field and no merge happens at all.
    field_ts = {
        "status": [_REMOTE_TS, "fedora", remote_order, "fedora-status-1", "done"]
    }
    remote = {
        "id": task_id,
        "title": "Authority",
        "status": "done",
        "type": "task",
        "created_at": _LOCAL_TS,
        "updated_at": _REMOTE_TS,
        "_field_ts": field_ts,
    }
    if with_null_fill:
        # Deliberately NO _field_ts entry: with one, the LWW loop claims the
        # field and it never reaches the NULL-fill path we want to exercise.
        remote["description"] = "adopted into a local NULL"
    return remote


def _seed_local(conn, task_id, local_order):
    _ensure_field_event_columns(conn)
    _insert_task(conn, task_id, status="in_progress", updated_at=_LOCAL_TS)
    _store_task_field_version(
        conn,
        task_id,
        "status",
        updated_at=_LOCAL_TS,
        updated_by=MACHINE_ID,
        old_value=None,
        new_value="in_progress",
        updated_order=local_order,
        source_event_id="local-status-1",
    )


def _merge_events(conn, task_id, field):
    return conn.execute(
        "SELECT * FROM memory_events WHERE aggregate_id=? AND field_name=? "
        "AND event_type='merge'",
        (task_id, field),
    ).fetchall()


def test_merge_event_carries_remote_field_authority(conn):
    """The audit event must carry the authority it absorbed, not a fresh local one.

    Before the fix it was stamped with a fresh local HLC, which then outranked
    the remote field version on export and reverted the peer's next edit.
    """
    task_id = "task-authority"
    local_order = _pack_logical_clock(1_750_000_000_000, 1)
    remote_order = _pack_logical_clock(1_760_000_000_000, 4)
    _seed_local(conn, task_id, local_order)

    merge_import_tasks(
        conn,
        [_remote_status_win(task_id, remote_order, with_null_fill=True)],
        import_content=True,
    )

    rows = _merge_events(conn, task_id, "status")
    assert len(rows) == 1
    assert rows[0]["machine_id"] == "fedora"
    assert rows[0]["logical_clock"] == remote_order
    assert rows[0]["event_ts"] == _REMOTE_TS
    assert '"synthetic_authority": true' in (rows[0]["payload_json"] or "")

    # NULL-fill is a LOCAL decision (adopting content into a local NULL), so it
    # must keep local authorship and stay out of merged_authority.
    filled = _merge_events(conn, task_id, "description")
    assert len(filled) == 1
    assert filled[0]["machine_id"] == MACHINE_ID
    assert filled[0]["payload_json"] in (None, "")


def test_merge_does_not_export_local_authority_for_absorbed_status(conn):
    """The regression that caused the incident: export claimed a local write."""
    task_id = "task-export-authority"
    local_order = _pack_logical_clock(1_750_000_000_000, 1)
    remote_order = _pack_logical_clock(1_760_000_000_000, 4)
    _seed_local(conn, task_id, local_order)

    merge_import_tasks(conn, [_remote_status_win(task_id, remote_order)])

    exported = [
        {"id": task_id, "status": _task(conn, task_id)["status"], "_field_ts": {}}
    ]
    canonicalize_exported_task_statuses(conn, exported)
    entry = exported[0]["_field_ts"]["status"]

    assert exported[0]["status"] == "done"
    assert entry[1] == "fedora", "export must not claim local authorship"
    assert entry[2] == remote_order


def test_merge_event_keeps_local_clock_for_legacy_peer_without_order(conn):
    """A legacy peer sends no packed clock; stamping it would be incoherent."""
    task_id = "task-legacy-peer"
    _ensure_field_event_columns(conn)
    _insert_task(conn, task_id, status="in_progress", updated_at=_LOCAL_TS)

    remote = {
        "id": task_id,
        "title": "Authority",
        "status": "done",
        "type": "task",
        "created_at": _LOCAL_TS,
        "updated_at": _REMOTE_TS,
        # Carries the value (so the legacy-value guard lets it through) but
        # no packed clock: updated_order parses to 0.
        "_field_ts": {"status": [_REMOTE_TS, "legacy-peer", 0, None, "done"]},
    }
    merge_import_tasks(conn, [remote])

    rows = _merge_events(conn, task_id, "status")
    assert len(rows) == 1
    assert rows[0]["machine_id"] == MACHINE_ID
    assert rows[0]["logical_clock"] > 0
    assert rows[0]["payload_json"] in (None, "")


def test_absorbed_authority_event_round_trips_to_peer_without_inverting(
    conn, tmp_path
):
    """Export/import the absorbed-authority event back to the machine it names.

    export_memory_events has no machine filter and the peer dedupes on
    event_id, so the peer receives a SECOND event claiming its own authorship
    with a clock it already used. Value and clock are identical, so the
    resolved authority must not move.
    """
    task_id = "task-round-trip"
    local_order = _pack_logical_clock(1_750_000_000_000, 1)
    remote_order = _pack_logical_clock(1_760_000_000_000, 4)
    _seed_local(conn, task_id, local_order)
    merge_import_tasks(conn, [_remote_status_win(task_id, remote_order)])

    # The peer already holds its own original write for the same field.
    peer = _make_conn(tmp_path / "peer.db")
    _ensure_field_event_columns(peer)
    _insert_task(peer, task_id, status="done", updated_at=_REMOTE_TS)
    _store_task_field_version(
        peer,
        task_id,
        "status",
        updated_at=_REMOTE_TS,
        updated_by="fedora",
        old_value=None,
        new_value="done",
        updated_order=remote_order,
        source_event_id="fedora-status-1",
    )
    peer.execute(
        "INSERT INTO memory_events (event_id, event_type, aggregate_kind, "
        "aggregate_id, field_name, machine_id, logical_clock, event_ts, new_value) "
        "VALUES ('fedora-status-1','task_field_set','task',?,'status','fedora',?,?,'done')",
        (task_id, remote_order, _REMOTE_TS),
    )

    before = [
        {"id": task_id, "status": _task(peer, task_id)["status"], "_field_ts": {}}
    ]
    canonicalize_exported_task_statuses(peer, before)

    import_memory_events(peer, export_memory_events(conn))

    after = [
        {"id": task_id, "status": _task(peer, task_id)["status"], "_field_ts": {}}
    ]
    canonicalize_exported_task_statuses(peer, after)

    # (в) Deliberately NOT asserting event_id: both events share the sort key
    # (1, logical_clock, machine_id) and the head is picked with a strict `>`
    # over unordered SQL rows, so which id wins is not deterministic. Value,
    # machine_id and clock are identical in both, which is what LWW consumes.
    assert after[0]["status"] == before[0]["status"] == "done"
    assert after[0]["_field_ts"]["status"][1] == "fedora"
    assert after[0]["_field_ts"]["status"][2] == remote_order

    synthetic = peer.execute(
        "SELECT payload_json FROM memory_events "
        "WHERE aggregate_id=? AND field_name='status' AND event_type='merge'",
        (task_id,),
    ).fetchall()
    assert len(synthetic) == 1
    assert '"synthetic_authority": true' in (synthetic[0]["payload_json"] or "")

    original = peer.execute(
        "SELECT payload_json FROM memory_events WHERE event_id='fedora-status-1'"
    ).fetchone()
    assert original["payload_json"] in (None, "")
    peer.close()
