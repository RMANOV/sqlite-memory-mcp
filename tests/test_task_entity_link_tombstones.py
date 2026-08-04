"""Task↔entity unlink must be transportable, not just locally applied.

A hard ``DELETE`` from ``task_entity_links`` leaves no timestamp behind, so the
incremental bridge gate cannot see it and a peer never learns the link is gone.
These regressions cover the tombstone contract that replaces it: the marker is
keyed by the exported entity *name* (stable across peers, unlike ``entity_id``)
and resolves ties in favour of the deletion.

The fixture uses the real ``init_db`` schema on purpose — an inline DDL copy
would silently drop the FK and PK constraints these tests depend on.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from db_utils import (  # noqa: E402
    TaskDAO,
    bridge_change_summary,
    export_task_files,
    merge_import_tasks,
)
from schema import init_db  # noqa: E402

T0 = "2026-08-01T10:00:00+00:00"
T1 = "2026-08-02T10:00:00+00:00"
T2 = "2026-08-03T10:00:00+00:00"
# Beats a deletion stamped with the real wall clock.
LATER = "2099-01-01T00:00:00+00:00"


@pytest.fixture
def conn(tmp_path):
    db_path = tmp_path / "memory.db"
    init_db(str(db_path))
    connection = sqlite3.connect(str(db_path), isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        yield connection
    finally:
        connection.close()


def _task(conn, task_id="task-1"):
    conn.execute(
        "INSERT INTO tasks (id, title, status, created_at, updated_at) "
        "VALUES (?, 'Task', 'not_started', ?, ?)",
        (task_id, T0, T0),
    )


def _entity(conn, entity_id=1, name="Alpha"):
    conn.execute(
        "INSERT INTO entities (id, name, entity_type, project, created_at, updated_at) "
        "VALUES (?, ?, 'person', 'shared:test', ?, ?)",
        (entity_id, name, T0, T0),
    )
    return entity_id


def _tombstone(conn, task_id="task-1", entity_name="Alpha"):
    return conn.execute(
        "SELECT * FROM task_entity_link_tombstones "
        "WHERE task_id = ? AND entity_name = ?",
        (task_id, entity_name),
    ).fetchone()


def _live_names(conn, task_id="task-1"):
    return [row["entity_name"] for row in TaskDAO.get_task_links(conn, task_id)]


# ── schema upgrade ───────────────────────────────────────────────────────────


def test_database_without_tombstone_table_regains_it_idempotently(tmp_path):
    """A DB predating this release must acquire the table, keeping its links."""
    db_path = tmp_path / "legacy.db"
    init_db(str(db_path))

    legacy = sqlite3.connect(str(db_path), isolation_level=None)
    legacy.execute("PRAGMA foreign_keys=OFF")
    legacy.execute("DROP INDEX idx_tel_tombstone_task")
    legacy.execute("DROP TABLE task_entity_link_tombstones")
    legacy.execute(
        "INSERT INTO tasks (id, title, status, created_at, updated_at) "
        "VALUES ('task-1', 'Task', 'not_started', ?, ?)",
        (T0, T0),
    )
    legacy.execute(
        "INSERT INTO entities (id, name, entity_type, project, created_at, updated_at) "
        "VALUES (1, 'Alpha', 'person', 'shared:test', ?, ?)",
        (T0, T0),
    )
    legacy.execute(
        "INSERT INTO task_entity_links "
        "(task_id, entity_id, link_type, score, created_at) "
        "VALUES ('task-1', 1, 'manual', NULL, ?)",
        (T0,),
    )
    legacy.close()

    init_db(str(db_path))
    init_db(str(db_path))  # idempotent

    upgraded = sqlite3.connect(str(db_path))
    names = {
        row[0]
        for row in upgraded.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'index')"
        )
    }
    surviving = upgraded.execute("SELECT COUNT(*) FROM task_entity_links").fetchone()[0]
    upgraded.close()

    assert "task_entity_link_tombstones" in names
    assert "idx_tel_tombstone_task" in names
    assert surviving == 1


# ── unlink / re-link ─────────────────────────────────────────────────────────


def test_unlink_records_tombstone_and_hides_the_link(conn):
    _task(conn)
    _entity(conn)
    TaskDAO.link_entity(conn, "task-1", 1, created_at=T0)

    assert TaskDAO.unlink_entity(conn, "task-1", 1) == 1

    assert _live_names(conn) == []
    row = _tombstone(conn)
    assert row["created_at"] == T0
    assert row["deleted_at"] > T0
    # A second unlink has nothing left to remove.
    assert TaskDAO.unlink_entity(conn, "task-1", 1) == 0


def test_relink_with_newer_timestamp_clears_the_tombstone(conn):
    _task(conn)
    _entity(conn)
    TaskDAO.link_entity(conn, "task-1", 1, created_at=T0)
    # ``unlink_entity`` stamps the deletion with the real wall clock, so the
    # re-link must be dated after it to win.
    TaskDAO.unlink_entity(conn, "task-1", 1)

    TaskDAO.link_entity(conn, "task-1", 1, created_at=LATER)

    assert _live_names(conn) == ["Alpha"]
    assert _tombstone(conn) is None


def test_relink_with_stale_timestamp_leaves_the_tombstone_authoritative(conn):
    """An older re-link must not silently win the export."""
    _task(conn)
    _entity(conn)
    conn.execute(
        "INSERT INTO task_entity_link_tombstones "
        "(task_id, entity_name, link_type, score, created_at, deleted_at) "
        "VALUES ('task-1', 'Alpha', 'manual', NULL, ?, ?)",
        (T0, T2),
    )

    TaskDAO.link_entity(conn, "task-1", 1, created_at=T1)

    assert _tombstone(conn)["deleted_at"] == T2


def test_unlink_is_visible_to_the_incremental_gate(conn):
    _task(conn)
    _entity(conn)
    TaskDAO.link_entity(conn, "task-1", 1, created_at=T0)
    baseline = bridge_change_summary(conn, T1)
    assert baseline["changed_task_entity_links"] == 0

    TaskDAO.unlink_entity(conn, "task-1", 1)

    assert bridge_change_summary(conn, T1)["changed_task_entity_links"] == 1


# ── export ───────────────────────────────────────────────────────────────────


def _exported(conn, bridge_dir, task_id="task-1"):
    os.makedirs(str(bridge_dir), exist_ok=True)
    export_task_files(conn, str(bridge_dir))
    path = os.path.join(str(bridge_dir), "tasks", f"{task_id}.json")
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def test_export_puts_the_tombstone_in_its_own_wire_key(conn, tmp_path):
    """Deletions must not ride inside ``_links`` — an old peer would revive them."""
    _task(conn)
    _entity(conn)
    TaskDAO.link_entity(conn, "task-1", 1, created_at=T0)
    TaskDAO.unlink_entity(conn, "task-1", 1)

    exported = _exported(conn, tmp_path / "bridge")

    assert exported["_links"] == []
    assert len(exported["_link_tombstones"]) == 1
    assert exported["_link_tombstones"][0]["name"] == "Alpha"
    assert exported["_link_tombstones"][0]["deleted_at"]


def test_export_omits_the_tombstone_key_when_nothing_was_deleted(conn, tmp_path):
    _task(conn)
    _entity(conn)
    TaskDAO.link_entity(conn, "task-1", 1, created_at=T0)

    exported = _exported(conn, tmp_path / "bridge")

    assert [r["name"] for r in exported["_links"]] == ["Alpha"]
    assert "_link_tombstones" not in exported


def test_export_collapses_an_inconsistent_active_plus_tombstone_pair(conn, tmp_path):
    """Never two wire records for one task/entity name — the deletion wins ties."""
    _task(conn)
    _entity(conn)
    TaskDAO.link_entity(conn, "task-1", 1, created_at=T1)
    conn.execute(
        "INSERT INTO task_entity_link_tombstones "
        "(task_id, entity_name, link_type, score, created_at, deleted_at) "
        "VALUES ('task-1', 'Alpha', 'manual', NULL, ?, ?)",
        (T0, T1),
    )

    exported = _exported(conn, tmp_path / "bridge")

    assert exported["_links"] == []
    assert [r["deleted_at"] for r in exported["_link_tombstones"]] == [T1]


# ── import (merge_import_tasks) ──────────────────────────────────────────────


def _remote(records):
    """A remote task with each record routed into its own wire bucket."""
    task = {
        "id": "task-1",
        "_links": [r for r in records if not r.get("deleted_at")],
    }
    tombstones = [r for r in records if r.get("deleted_at")]
    if tombstones:
        task["_link_tombstones"] = tombstones
    return [task]


def _link(name="Alpha", created_at=T0, deleted_at=None):
    record = {
        "name": name,
        "link_type": "manual",
        "score": None,
        "created_at": created_at,
    }
    if deleted_at is not None:
        record["deleted_at"] = deleted_at
    return record


def test_import_remote_tombstone_removes_the_local_link(conn):
    _task(conn)
    _entity(conn)
    TaskDAO.link_entity(conn, "task-1", 1, created_at=T0)

    merge_import_tasks(conn, _remote([_link(created_at=T0, deleted_at=T1)]))

    assert _live_names(conn) == []
    assert _tombstone(conn)["deleted_at"] == T1


def test_import_stale_remote_active_loses_to_local_tombstone(conn):
    _task(conn)
    _entity(conn)
    conn.execute(
        "INSERT INTO task_entity_link_tombstones "
        "(task_id, entity_name, link_type, score, created_at, deleted_at) "
        "VALUES ('task-1', 'Alpha', 'manual', NULL, ?, ?)",
        (T0, T2),
    )

    merge_import_tasks(conn, _remote([_link(created_at=T1)]))

    assert _live_names(conn) == []
    assert _tombstone(conn)["deleted_at"] == T2


def test_import_newer_remote_active_reactivates_the_link(conn):
    _task(conn)
    _entity(conn)
    conn.execute(
        "INSERT INTO task_entity_link_tombstones "
        "(task_id, entity_name, link_type, score, created_at, deleted_at) "
        "VALUES ('task-1', 'Alpha', 'manual', NULL, ?, ?)",
        (T0, T1),
    )

    merge_import_tasks(conn, _remote([_link(created_at=T2)]))

    assert _live_names(conn) == ["Alpha"]
    assert _tombstone(conn) is None


def test_import_equal_timestamp_resolves_to_the_deletion(conn):
    _task(conn)
    _entity(conn)
    TaskDAO.link_entity(conn, "task-1", 1, created_at=T1)

    merge_import_tasks(conn, _remote([_link(created_at=T0, deleted_at=T1)]))

    assert _live_names(conn) == []
    assert _tombstone(conn)["deleted_at"] == T1


def test_import_retains_a_tombstone_for_an_unknown_entity(conn):
    """The entity may not exist locally; the deletion must still be recorded."""
    _task(conn)

    merge_import_tasks(conn, _remote([_link(name="Ghost", deleted_at=T1)]))

    assert _tombstone(conn, entity_name="Ghost")["deleted_at"] == T1


def test_import_of_the_same_tombstone_twice_is_idempotent(conn):
    _task(conn)
    _entity(conn)
    TaskDAO.link_entity(conn, "task-1", 1, created_at=T0)
    payload = _remote([_link(created_at=T0, deleted_at=T1)])

    merge_import_tasks(conn, payload)
    merge_import_tasks(conn, payload)

    rows = conn.execute("SELECT COUNT(*) FROM task_entity_link_tombstones").fetchone()[
        0
    ]
    assert rows == 1
    assert _live_names(conn) == []


def test_import_without_the_tombstone_key_is_not_an_error(conn):
    """A payload written before v3.13.5 simply carries no deletions."""
    _task(conn)
    _entity(conn)

    merge_import_tasks(conn, [{"id": "task-1", "_links": [_link(created_at=T1)]}])

    assert _live_names(conn) == ["Alpha"]


# ── entity merge ─────────────────────────────────────────────────────────────


def test_entity_merge_leaves_a_tombstone_for_the_absorbed_name(conn, monkeypatch):
    """The source name disappears from the payload — peers must be told."""
    import entity_server

    _task(conn)
    _entity(conn, 1, "Alpha")
    _entity(conn, 2, "Beta")
    TaskDAO.link_entity(conn, "task-1", 1, created_at=T0)
    monkeypatch.setattr(entity_server, "_get_conn", lambda: _NoCloseConn(conn))

    result = json.loads(
        entity_server.merge_entities.fn(
            source_name="Alpha", target_name="Beta", dry_run=False
        )
    )

    assert "error" not in result
    assert _tombstone(conn, entity_name="Alpha")["deleted_at"]
    assert _live_names(conn) == ["Beta"]


class _NoCloseConn:
    """Hand the test's own connection to a server module without closing it."""

    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self._conn

    def __exit__(self, *exc):
        return False
