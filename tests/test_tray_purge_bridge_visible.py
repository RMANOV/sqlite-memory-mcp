"""Regression tests: automated tray purge must be bridge-visible.

Root cause (2026-05-08 family of incidents): ``TaskDB.purge_old_done`` hard-DELETEd
old done rows directly. ``bridge_change_summary`` only counts rows whose
``updated_at`` is newer than ``last_push_at``, so a hard delete was invisible to
the incremental skip check — automated sync skipped export+push entirely and the
stale per-task bridge file was never cleaned (manual full export did clean it).

The fix turns the cleanup into a two-tier, bridge-visible operation:
  * Tier 1: old done tasks are transitioned to ``archived`` via
    ``apply_task_mutation`` (bumps ``updated_at`` + writes a status field
    version) so the change is visible to incremental sync and exported as a
    tombstone, which also removes the stale per-task file on full export.
  * Tier 2: tombstones aged past the export window are hard-deleted, after every
    peer has already absorbed the tombstone (no resurrection).
"""

import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from db_utils import (
    TaskDAO,
    _TOMBSTONE_DAYS,
    _task_storage_stem,
    apply_task_mutation,
    bridge_change_summary,
    export_task_files,
    mark_tombstones_pushed,
)
from schema import init_db


def _iso_days_ago(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _insert_task(
    conn,
    task_id,
    *,
    status,
    updated_at,
    title="t",
    type_="task",
    tombstone_pushed_at=None,
):
    conn.execute(
        "INSERT INTO tasks "
        "(id, title, status, type, created_at, updated_at, tombstone_pushed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (task_id, title, status, type_, updated_at, updated_at, tombstone_pushed_at),
    )


@pytest.fixture
def conn(tmp_path):
    db_path = str(tmp_path / "memory.db")
    init_db(db_path)
    connection = sqlite3.connect(db_path, isolation_level=None)
    connection.row_factory = sqlite3.Row
    yield connection
    connection.close()


def test_purge_old_done_is_visible_to_incremental_sync(conn):
    """After purge, bridge_change_summary must report a task change so the
    incremental skip-check (any(change_summary.values())) does NOT skip
    export+push. A bare DELETE would leave changed_tasks == 0."""
    last_push_at = _iso_days_ago(1)
    # Old done task that was already pushed (updated_at predates last_push_at).
    _insert_task(conn, "old-done", status="done", updated_at=_iso_days_ago(40))

    # Precondition: the stale done row is invisible to incremental sync.
    before = bridge_change_summary(conn, last_push_at)
    assert before["changed_tasks"] == 0

    cutoff = _iso_days_ago(30)
    retired = TaskDAO.purge_done(conn, cutoff)
    assert retired == 1

    # Row is tombstoned, not gone (so deletion propagates to peers).
    row = conn.execute(
        "SELECT status FROM tasks WHERE id = 'old-done'"
    ).fetchone()
    assert row is not None
    assert row["status"] == "archived"

    # The deletion is now bridge-visible — incremental sync cannot hide it.
    after = bridge_change_summary(conn, last_push_at)
    assert after["changed_tasks"] >= 1
    assert any(after.values())


def test_purge_then_full_export_removes_stale_per_task_file(conn, tmp_path):
    """End-to-end: after purge + full export, an aged-out task's bridge file is
    cleaned (not left stale). A pre-existing stale file for a row that no longer
    exists is also removed by the full export's cleanup pass."""
    bridge_dir = str(tmp_path / "bridge")
    tasks_dir = os.path.join(bridge_dir, "tasks")
    os.makedirs(tasks_dir, exist_ok=True)

    # A done task aged well past tombstone window: tier-1 archives it, then it is
    # immediately old enough that a later cycle would hard-purge it. Here we just
    # confirm tier-1 keeps it exportable as a tombstone (status archived).
    _insert_task(conn, "recent-done", status="done", updated_at=_iso_days_ago(40))
    # A stale leftover file for a task that no longer exists in the DB at all.
    ghost_stem = _task_storage_stem("ghost-task")
    ghost_path = os.path.join(tasks_dir, f"{ghost_stem}.json")
    with open(ghost_path, "w", encoding="utf-8") as fh:
        fh.write('{"id": "ghost-task"}')

    TaskDAO.purge_done(conn, _iso_days_ago(30))

    # Full export (changed_since=None) runs _cleanup_stale_generated_files.
    export_task_files(conn, bridge_dir, changed_since=None)

    # The ghost file (no backing row) must be cleaned by the full export.
    assert not os.path.exists(ghost_path)

    # The archived task is still exported as a tombstone (within tombstone window),
    # so peers learn of the deletion rather than resurrecting it.
    archived_stem = _task_storage_stem("recent-done")
    assert os.path.exists(os.path.join(tasks_dir, f"{archived_stem}.json"))


def test_purge_tier2_is_push_aware(conn):
    """Tier 2 is push-aware. An aged 'archived' row is hard-deleted ONLY if its
    tombstone was successfully pushed (tombstone_pushed_at set) AND that push has
    itself aged past retention. Retention is measured from the PUSH, not from
    updated_at. 'cancelled' (user soft-delete) is never swept.

    The critical safety property: an aged-but-UN-pushed archived tombstone is
    RETAINED, never hard-deleted — that is the fix for the 2026-05-08 resurrection
    incident class (delete-before-push → peer re-imports stale 'done')."""
    aged = _iso_days_ago(_TOMBSTONE_DAYS + 5)
    fresh = _iso_days_ago(1)
    aged_push = _iso_days_ago(_TOMBSTONE_DAYS + 1)  # push itself aged past retention
    fresh_push = _iso_days_ago(1)  # pushed recently, still inside retention

    # Pushed long ago AND aged → Tier-2 eligible.
    _insert_task(
        conn,
        "aged-pushed-archived",
        status="archived",
        updated_at=aged,
        tombstone_pushed_at=aged_push,
    )
    # Aged row but NEVER pushed → MUST be retained (no resurrection).
    _insert_task(
        conn, "aged-unpushed-archived", status="archived", updated_at=aged
    )
    # Aged updated_at but push is recent → retention runs from push, so retained.
    _insert_task(
        conn,
        "aged-recently-pushed",
        status="archived",
        updated_at=aged,
        tombstone_pushed_at=fresh_push,
    )
    # Cancelled (user soft-delete) aged + even if pushed → never swept.
    _insert_task(
        conn,
        "aged-cancelled",
        status="cancelled",
        updated_at=aged,
        tombstone_pushed_at=aged_push,
    )
    # Fresh archived tombstone, unpushed → retained.
    _insert_task(conn, "fresh-archived", status="archived", updated_at=fresh)

    TaskDAO.purge_done(conn, _iso_days_ago(30))

    remaining = {r["id"] for r in conn.execute("SELECT id FROM tasks").fetchall()}
    # Only the pushed-and-aged archived tombstone is hard-purged.
    assert "aged-pushed-archived" not in remaining
    # Un-pushed aged tombstone is RETAINED (core invariant: no delete before push).
    assert "aged-unpushed-archived" in remaining
    # Retention is from the push: a recently-pushed tombstone stays.
    assert "aged-recently-pushed" in remaining
    # User soft-delete (cancelled) is preserved even when aged and pushed.
    assert "aged-cancelled" in remaining
    # Fresh archived tombstone stays.
    assert "fresh-archived" in remaining


def test_purge_keeps_recent_done_and_notes(conn):
    """Purge must not touch recent done tasks or notes (type != 'task')."""
    _insert_task(conn, "recent-done", status="done", updated_at=_iso_days_ago(2))
    _insert_task(
        conn, "old-note", status="done", updated_at=_iso_days_ago(40), type_="note"
    )

    retired = TaskDAO.purge_done(conn, _iso_days_ago(30))

    assert retired == 0
    statuses = {
        r["id"]: r["status"]
        for r in conn.execute("SELECT id, status FROM tasks").fetchall()
    }
    assert statuses["recent-done"] == "done"
    assert statuses["old-note"] == "done"  # notes excluded from tier-1 archive


def test_offline_aged_tombstone_survives_until_pushed_then_tier2_eligible(
    conn, tmp_path
):
    """Full invariant, end-to-end on the INCREMENTAL export path:

    1. A done task is archived (tombstoned) while the machine is offline and never
       pushed. Its updated_at then ages > _TOMBSTONE_DAYS.
    2. Tier-2 purge must NOT hard-delete it (un-pushed → retained).
    3. An INCREMENTAL export (changed_since AFTER the tombstone's updated_at — i.e.
       the real bug condition where the aged tombstone predates last_push_at) must
       STILL emit the tombstone file. A naive age-gated incremental export would
       drop it, so the deletion would never propagate.
    4. Only after a successful push stamps tombstone_pushed_at (aged past retention)
       does the row become Tier-2 hard-delete eligible.

    This is the regression for the archive-while-offline >30d resurrection class.
    """
    bridge_dir = str(tmp_path / "bridge")
    tasks_dir = os.path.join(bridge_dir, "tasks")
    os.makedirs(tasks_dir, exist_ok=True)

    # Archived-while-offline tombstone, never pushed, updated_at aged past window.
    aged = _iso_days_ago(_TOMBSTONE_DAYS + 10)
    _insert_task(conn, "offline-tomb", status="archived", updated_at=aged)

    # Step 2: Tier-2 must retain the un-pushed aged tombstone.
    TaskDAO.purge_done(conn, _iso_days_ago(30))
    remaining = {r["id"] for r in conn.execute("SELECT id FROM tasks").fetchall()}
    assert "offline-tomb" in remaining, "un-pushed aged tombstone must be retained"

    # Step 3: INCREMENTAL export with changed_since NEWER than the tombstone's
    # updated_at (the exact condition where the old age-gated query dropped it).
    changed_since = _iso_days_ago(1)
    exported = export_task_files(conn, bridge_dir, changed_since=changed_since)
    assert "offline-tomb" in exported, (
        "un-pushed aged tombstone must ride along on incremental export so the "
        "deletion propagates to peers"
    )
    tomb_stem = _task_storage_stem("offline-tomb")
    assert os.path.exists(os.path.join(tasks_dir, f"{tomb_stem}.json"))

    # It is NOT yet Tier-2 eligible (still un-pushed).
    TaskDAO.purge_done(conn, _iso_days_ago(30))
    still = {r["id"] for r in conn.execute("SELECT id FROM tasks").fetchall()}
    assert "offline-tomb" in still

    # Step 4: simulate a successful push of THIS export, stamping the tombstone.
    # Stamp it aged past retention so it becomes Tier-2 eligible immediately.
    aged_push = _iso_days_ago(_TOMBSTONE_DAYS + 1)
    stamped = mark_tombstones_pushed(conn, exported, aged_push)
    assert stamped == 1

    TaskDAO.purge_done(conn, _iso_days_ago(30))
    final = {r["id"] for r in conn.execute("SELECT id FROM tasks").fetchall()}
    assert "offline-tomb" not in final, (
        "after a successful push aged past retention, the tombstone becomes "
        "Tier-2 hard-delete eligible"
    )


def test_mark_tombstones_pushed_ignores_active_and_already_stamped(conn):
    """mark_tombstones_pushed stamps ONLY un-stamped tombstones in the given id
    list. Active tasks are skipped (not tombstones); already-stamped tombstones
    keep their original push timestamp (idempotent, never re-stamped)."""
    _insert_task(conn, "active", status="done", updated_at=_iso_days_ago(1))
    _insert_task(conn, "tomb-new", status="archived", updated_at=_iso_days_ago(2))
    first_push = _iso_days_ago(5)
    _insert_task(
        conn,
        "tomb-already",
        status="cancelled",
        updated_at=_iso_days_ago(2),
        tombstone_pushed_at=first_push,
    )

    now_push = _iso_days_ago(0)
    stamped = mark_tombstones_pushed(
        conn, ["active", "tomb-new", "tomb-already"], now_push
    )
    # Only the previously-unstamped tombstone is newly stamped.
    assert stamped == 1

    rows = {
        r["id"]: r["tombstone_pushed_at"]
        for r in conn.execute(
            "SELECT id, tombstone_pushed_at FROM tasks"
        ).fetchall()
    }
    assert rows["active"] is None  # not a tombstone — never stamped
    assert rows["tomb-new"] == now_push
    assert rows["tomb-already"] == first_push  # preserved, not overwritten


def test_status_change_clears_stale_tombstone_push_stamp(conn):
    """A status change invalidates a prior tombstone push stamp.

    Without this, a reactivated-then-re-archived task would carry the OLD
    push stamp into a NEW tombstone that was never pushed -> it could age out of
    export and become Tier-2 deletable without propagating -> resurrection on a
    peer. apply_task_mutation must clear tombstone_pushed_at on any status write.
    """
    # Archived tombstone, pushed long ago (would be Tier-2 eligible as-is).
    aged_push = _iso_days_ago(_TOMBSTONE_DAYS + 1)
    _insert_task(
        conn,
        "react",
        status="archived",
        updated_at=_iso_days_ago(_TOMBSTONE_DAYS + 5),
        tombstone_pushed_at=aged_push,
    )

    # Reactivate it (un-archive). Status change must clear the stale stamp.
    apply_task_mutation(conn, "react", {"status": "in_progress"})
    stamp = conn.execute(
        "SELECT tombstone_pushed_at FROM tasks WHERE id = 'react'"
    ).fetchone()["tombstone_pushed_at"]
    assert stamp is None, "status change must clear stale tombstone push stamp"

    # Re-archive: a NEW tombstone, never pushed -> NULL stamp.
    apply_task_mutation(conn, "react", {"status": "archived"})
    stamp2 = conn.execute(
        "SELECT tombstone_pushed_at FROM tasks WHERE id = 'react'"
    ).fetchone()["tombstone_pushed_at"]
    assert stamp2 is None

    # Tier-2 must RETAIN the re-archived, un-pushed tombstone.
    TaskDAO.purge_done(conn, _iso_days_ago(30))
    assert (
        conn.execute("SELECT 1 FROM tasks WHERE id = 'react'").fetchone() is not None
    ), "re-archived un-pushed tombstone must be retained (no resurrection)"
