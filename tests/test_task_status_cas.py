from __future__ import annotations

import os
import sqlite3
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from db_utils import (
    apply_task_mutation,
    create_task_with_ledger,
    get_conn_immediate,
)
from schema import init_db
from task_status_cas import (
    SingleUseUndo,
    StatusSingleFlight,
    StatusToken,
    status_token,
    transition_status,
)


@pytest.fixture
def db_path(tmp_path) -> str:
    path = str(tmp_path / "tasks.db")
    init_db(path)
    return path


def _create(db_path: str, task_id: str, status: str, *, type: str = "task") -> None:
    with get_conn_immediate(db_path) as conn:
        create_task_with_ledger(
            conn,
            task_id,
            f"Task {task_id}",
            "2026-07-19T10:00:00+00:00",
            status=status,
            type=type,
            actor_id="test",
        )


def _token(db_path: str, task_id: str) -> StatusToken:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        token = status_token(conn, task_id)
    finally:
        conn.close()
    assert token is not None
    return token


def test_status_token_supports_default_tuple_rows(db_path):
    _create(db_path, "tuple-token", "not_started")
    conn = sqlite3.connect(db_path)
    try:
        token = status_token(conn, "tuple-token")
    finally:
        conn.close()

    assert token is not None
    assert token.task_id == "tuple-token"
    assert token.status == "not_started"
    assert token.updated_order > 0
    assert token.source_event_id


def _status(db_path: str, task_id: str) -> str:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
        assert row is not None
        return str(row[0])
    finally:
        conn.close()


def _status_events(db_path: str, task_id: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return int(
            conn.execute(
                "SELECT COUNT(*) FROM memory_events "
                "WHERE aggregate_kind='task' AND aggregate_id=? "
                "AND field_name='status'",
                (task_id,),
            ).fetchone()[0]
        )
    finally:
        conn.close()


def _foreign_status(db_path: str, task_id: str, target: str) -> None:
    with get_conn_immediate(db_path) as conn:
        result = apply_task_mutation(
            conn,
            task_id,
            {"status": target},
            actor_id="foreign",
            tool_name="test.foreign",
        )
        assert result["updated"] == 1


@pytest.mark.parametrize("source", ["not_started", "in_progress"])
def test_active_task_can_complete_once(db_path, source):
    _create(db_path, "complete", source)
    before = _status_events(db_path, "complete")

    result = transition_status(
        db_path, _token(db_path, "complete"), "done", forbid_path=None
    )

    assert result["outcome"] == "applied"
    assert _status(db_path, "complete") == "done"
    assert _status_events(db_path, "complete") == before + 1
    assert result["status_token"].status == "done"


def test_done_task_archives_and_returns_undo_token(db_path):
    _create(db_path, "archive", "done")

    result = transition_status(
        db_path, _token(db_path, "archive"), "archived", forbid_path=None
    )

    assert result["outcome"] == "applied"
    assert result["undo_token"].previous_status == "done"
    assert _status(db_path, "archive") == "archived"
    conn = sqlite3.connect(db_path)
    try:
        assert (
            conn.execute(
                "SELECT tombstone_pushed_at FROM tasks WHERE id='archive'"
            ).fetchone()[0]
            is None
        )
    finally:
        conn.close()


@pytest.mark.parametrize("source", ["not_started", "in_progress"])
def test_active_archive_requires_explicit_confirmation(db_path, source):
    _create(db_path, "confirm", source)
    token = _token(db_path, "confirm")
    before = _status_events(db_path, "confirm")

    cancelled = transition_status(
        db_path, token, "archived", confirmed=False, forbid_path=None
    )
    applied = transition_status(
        db_path, token, "archived", confirmed=True, forbid_path=None
    )

    assert cancelled["outcome"] == "conflict"
    assert cancelled["updated"] == 0
    assert applied["outcome"] == "applied"
    assert _status_events(db_path, "confirm") == before + 1


@pytest.mark.parametrize(
    ("status", "type"),
    [("archived", "task"), ("cancelled", "task"), ("not_started", "note")],
)
def test_terminal_and_non_task_rows_fail_closed(db_path, status, type):
    _create(db_path, "closed", status, type=type)
    before = _status_events(db_path, "closed")
    token = _token(db_path, "closed") if type == "task" else None
    if token is None:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            version = conn.execute(
                "SELECT updated_order, source_event_id FROM task_field_versions "
                "WHERE task_id='closed' AND field_name='status'"
            ).fetchone()
            assert version is not None
            token = StatusToken(
                "closed",
                status,
                int(version["updated_order"]),
                version["source_event_id"],
            )
        finally:
            conn.close()

    result = transition_status(db_path, token, "done", forbid_path=None)

    assert result["outcome"] == "conflict"
    assert _status_events(db_path, "closed") == before


def test_single_flight_absorbs_replayed_ui_gesture(db_path):
    _create(db_path, "flight", "not_started")
    gate = StatusSingleFlight()
    before = _status_events(db_path, "flight")

    assert gate.begin("flight") is True
    first = transition_status(
        db_path, _token(db_path, "flight"), "done", forbid_path=None
    )
    gate.finish("flight", first)
    assert gate.begin("flight") is False
    replay = gate.replay_result("flight")

    assert replay == {
        "outcome": "noop",
        "updated": 0,
        "reason": "single_flight",
        "first_outcome": "applied",
    }
    assert _status_events(db_path, "flight") == before + 1
    gate.reloaded("flight")
    assert gate.begin("flight") is True


def test_foreign_advance_to_same_target_is_stale_conflict(db_path):
    _create(db_path, "foreign", "not_started")
    stale = _token(db_path, "foreign")
    _foreign_status(db_path, "foreign", "done")
    before = _status_events(db_path, "foreign")

    result = transition_status(db_path, stale, "done", forbid_path=None)

    assert result["outcome"] == "conflict"
    assert _status_events(db_path, "foreign") == before


def test_aba_cycle_invalidates_original_token(db_path):
    _create(db_path, "aba", "done")
    original = _token(db_path, "aba")
    archived = transition_status(db_path, original, "archived", forbid_path=None)
    restored = SingleUseUndo(archived["undo_token"]).apply(db_path, forbid_path=None)
    before = _status_events(db_path, "aba")

    stale = transition_status(db_path, original, "archived", forbid_path=None)

    assert restored["outcome"] == "applied"
    assert _status(db_path, "aba") == "done"
    assert stale["outcome"] == "conflict"
    assert _status_events(db_path, "aba") == before


@pytest.mark.parametrize(("order", "event_id"), [(0, None), (0, "event"), (1, None)])
def test_invalid_or_missing_status_version_fails_closed(db_path, order, event_id):
    _create(db_path, "version", "not_started")
    conn = sqlite3.connect(db_path)
    try:
        if order == 0 and event_id is None:
            conn.execute(
                "DELETE FROM task_field_versions "
                "WHERE task_id='version' AND field_name='status'"
            )
        else:
            conn.execute(
                "UPDATE task_field_versions SET updated_order=?, source_event_id=? "
                "WHERE task_id='version' AND field_name='status'",
                (order, event_id),
            )
        conn.commit()
    finally:
        conn.close()
    before = _status_events(db_path, "version")

    result = transition_status(
        db_path,
        StatusToken("version", "not_started", order, event_id or ""),
        "done",
        forbid_path=None,
    )

    assert result["outcome"] == "conflict"
    assert _status(db_path, "version") == "not_started"
    assert _status_events(db_path, "version") == before


def test_immediate_writer_serializes_then_rejects_stale_token(db_path):
    _create(db_path, "locked", "not_started")
    stale = _token(db_path, "locked")
    writer = sqlite3.connect(db_path, isolation_level=None)
    writer.row_factory = sqlite3.Row
    writer.execute("BEGIN IMMEDIATE")
    applied = apply_task_mutation(
        writer,
        "locked",
        {"status": "done"},
        actor_id="foreign",
        tool_name="test.locked_foreign",
    )
    assert applied["updated"] == 1
    started = threading.Event()

    def attempt():
        started.set()
        return transition_status(db_path, stale, "done", forbid_path=None)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(attempt)
        assert started.wait(timeout=2)
        writer.execute("COMMIT")
        result = future.result(timeout=15)
    writer.close()

    assert result["outcome"] == "conflict"
    assert _status(db_path, "locked") == "done"
    assert _status_events(db_path, "locked") == 2


def test_undo_is_single_use_and_foreign_advance_conflicts(db_path):
    _create(db_path, "undo-once", "done")
    archived = transition_status(
        db_path, _token(db_path, "undo-once"), "archived", forbid_path=None
    )
    undo = SingleUseUndo(archived["undo_token"])

    assert undo.apply(db_path, forbid_path=None)["outcome"] == "applied"
    before_replay = _status_events(db_path, "undo-once")
    assert undo.apply(db_path, forbid_path=None)["outcome"] == "noop"
    assert _status_events(db_path, "undo-once") == before_replay

    _create(db_path, "undo-stale", "done")
    archived = transition_status(
        db_path, _token(db_path, "undo-stale"), "archived", forbid_path=None
    )
    stale_undo = SingleUseUndo(archived["undo_token"])
    _foreign_status(db_path, "undo-stale", "done")
    before_conflict = _status_events(db_path, "undo-stale")

    assert stale_undo.apply(db_path, forbid_path=None)["outcome"] == "conflict"
    assert _status_events(db_path, "undo-stale") == before_conflict


def test_live_path_fence_resolves_symlinks_before_open(db_path, tmp_path):
    _create(db_path, "fenced", "not_started")
    link = tmp_path / "alias.db"
    os.symlink(db_path, link)

    with pytest.raises(PermissionError, match="fenced DB path"):
        transition_status(
            str(link),
            _token(db_path, "fenced"),
            "done",
            forbid_path=db_path,
        )


def test_legacy_mutation_path_remains_unconditional(db_path):
    _create(db_path, "legacy", "not_started")
    with get_conn_immediate(db_path) as conn:
        result = apply_task_mutation(conn, "legacy", {"status": "done"})

    assert result["updated"] == 1
    assert "outcome" not in result
    assert _status(db_path, "legacy") == "done"


@pytest.mark.parametrize("event_id", [None, "", "   "])
def test_status_cas_requires_nonempty_event_id(db_path, event_id):
    _create(db_path, "event-id", "not_started")
    token = _token(db_path, "event-id")
    before = _status_events(db_path, "event-id")

    with pytest.raises(ValueError, match="expected_status_event_id"):
        with get_conn_immediate(db_path) as conn:
            apply_task_mutation(
                conn,
                "event-id",
                {"status": "done"},
                expected_status=token.status,
                expected_status_order=token.updated_order,
                expected_status_event_id=event_id,
            )

    assert _status(db_path, "event-id") == "not_started"
    assert _status_events(db_path, "event-id") == before
