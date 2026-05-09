"""Tests for reflect_apply / reflect_review / reflect_discard (Tools 20-22)."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from db_utils import now_iso
from schema import init_db


@pytest.fixture
def server(tmp_path, monkeypatch):
    db_path = str(tmp_path / "phase1_apply.db")
    init_db(db_path)
    monkeypatch.setenv("SQLITE_MEMORY_DB", db_path)
    for mod in (
        "intel_server",
        "db_utils",
        "reflection",
        "reflection_dao",
        "reflection_apply",
        "schema",
    ):
        sys.modules.pop(mod, None)
    import intel_server  # noqa: F401

    yield intel_server


@pytest.fixture
def conn(tmp_path):
    db_path = str(tmp_path / "phase1_apply_dao.db")
    init_db(db_path)
    c = sqlite3.connect(db_path, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    yield c
    c.close()


def _seed_stale_task(db_path: str, task_id: str = "stale-1") -> None:
    long_ago = (datetime.now(timezone.utc) - timedelta(days=120)).date().isoformat()
    c = sqlite3.connect(db_path, isolation_level=None)
    try:
        c.execute(
            "INSERT INTO tasks (id, title, status, priority, section, due_date, "
            "type, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                task_id,
                "Stale task",
                "not_started",
                "medium",
                "today",
                long_ago,
                "task",
                now_iso(),
                now_iso(),
            ),
        )
    finally:
        c.close()


def _accept_first_candidate(db_path: str, run_id: str) -> str:
    """Mark the first candidate from this run as accepted; return candidate_id."""
    c = sqlite3.connect(db_path, isolation_level=None)
    try:
        row = c.execute(
            "SELECT candidate_id FROM reflection_candidates WHERE run_id = ? LIMIT 1",
            (run_id,),
        ).fetchone()
        cid = row[0] if row else None
        if cid:
            c.execute(
                "UPDATE reflection_candidates SET human_decision = 'accept', "
                "decided_by = 'tester', decided_at = ? WHERE candidate_id = ?",
                (now_iso(), cid),
            )
        return cid
    finally:
        c.close()


# ── reflect_apply ──────────────────────────────────────────────────────


def test_apply_archives_accepted_stale_overdue_task(server):
    db_path = os.environ["SQLITE_MEMORY_DB"]
    _seed_stale_task(db_path, "stale-archive")
    raw = server.reflect_start(stale_days=60)
    rid = json.loads(raw)["run_id"]
    cid = _accept_first_candidate(db_path, rid)
    assert cid is not None

    raw_apply = server.reflect_apply(rid)
    summary = json.loads(raw_apply)
    assert summary["applied"] == 1, summary
    assert summary["skipped"] == []
    assert summary["failed"] == []

    # Verify task is now archived
    c = sqlite3.connect(db_path)
    try:
        status = c.execute(
            "SELECT status FROM tasks WHERE id = 'stale-archive'"
        ).fetchone()[0]
        assert status == "archived"
        # Snapshot was written
        n = c.execute(
            "SELECT COUNT(*) FROM reflection_apply_snapshots WHERE run_id = ?",
            (rid,),
        ).fetchone()[0]
        assert n == 1
    finally:
        c.close()


def test_apply_idempotent_on_repeat(server):
    db_path = os.environ["SQLITE_MEMORY_DB"]
    _seed_stale_task(db_path, "stale-idem")
    rid = json.loads(server.reflect_start(stale_days=60))["run_id"]
    _accept_first_candidate(db_path, rid)

    server.reflect_apply(rid)
    raw_again = server.reflect_apply(rid)
    summary = json.loads(raw_again)
    assert summary["applied"] == 0
    assert any(
        s.get("reason") == "already_applied" for s in summary["skipped"]
    ), summary["skipped"]


def test_apply_rejects_run_not_completed(server, monkeypatch):
    """Manually create a pending run so apply must reject."""
    db_path = os.environ["SQLITE_MEMORY_DB"]
    c = sqlite3.connect(db_path, isolation_level=None)
    try:
        c.execute(
            "INSERT INTO reflection_runs (run_id, version, status, created_by, "
            "created_at) VALUES ('rfl-pend-apply', 'reflect_v1.0', 'pending', "
            "'tester', '2026-01-01T00:00:00')"
        )
    finally:
        c.close()
    raw = server.reflect_apply("rfl-pend-apply")
    out = json.loads(raw)
    assert out.get("error_type") == "invalid_state_transition"


def test_apply_run_not_found(server):
    raw = server.reflect_apply("rfl-does-not-exist")
    out = json.loads(raw)
    assert out.get("error_type") == "not_found"


def test_apply_skips_target_already_deleted(server):
    db_path = os.environ["SQLITE_MEMORY_DB"]
    _seed_stale_task(db_path, "ghost-task")
    rid = json.loads(server.reflect_start(stale_days=60))["run_id"]
    _accept_first_candidate(db_path, rid)

    # Delete the task before apply
    c = sqlite3.connect(db_path, isolation_level=None)
    try:
        c.execute("DELETE FROM tasks WHERE id = 'ghost-task'")
    finally:
        c.close()

    summary = json.loads(server.reflect_apply(rid))
    assert summary["applied"] == 0
    assert any(
        s.get("reason") == "target_not_found" for s in summary["skipped"]
    )


def test_apply_with_candidate_id_csv_filter(server):
    """Seed two stale tasks, accept both, apply only one via filter."""
    db_path = os.environ["SQLITE_MEMORY_DB"]
    _seed_stale_task(db_path, "csv-a")
    _seed_stale_task(db_path, "csv-b")
    rid = json.loads(server.reflect_start(stale_days=60))["run_id"]

    # Accept all candidates from this run
    c = sqlite3.connect(db_path, isolation_level=None)
    try:
        rows = c.execute(
            "SELECT candidate_id, target_ref FROM reflection_candidates "
            "WHERE run_id = ?",
            (rid,),
        ).fetchall()
        for cand_id, _ in rows:
            c.execute(
                "UPDATE reflection_candidates SET human_decision='accept', "
                "decided_by='t', decided_at=? WHERE candidate_id = ?",
                (now_iso(), cand_id),
            )
    finally:
        c.close()

    target_to_keep = next(r[0] for r in rows if r[1] == "csv-a")
    raw = server.reflect_apply(rid, candidate_ids_csv=target_to_keep)
    summary = json.loads(raw)
    assert summary["considered"] == 1
    assert summary["applied"] == 1


# ── reflect_review ─────────────────────────────────────────────────────


def test_review_returns_candidates_with_decoded_evidence(server):
    db_path = os.environ["SQLITE_MEMORY_DB"]
    _seed_stale_task(db_path, "rev-1")
    rid = json.loads(server.reflect_start(stale_days=60))["run_id"]

    raw = server.reflect_review(rid)
    out = json.loads(raw)
    assert "candidates" in out
    assert out["total"] >= 1
    cand = out["candidates"][0]
    assert "evidence" in cand
    assert isinstance(cand["evidence"], dict)


def test_review_already_applied_flag(server):
    db_path = os.environ["SQLITE_MEMORY_DB"]
    _seed_stale_task(db_path, "rev-applied")
    rid = json.loads(server.reflect_start(stale_days=60))["run_id"]
    _accept_first_candidate(db_path, rid)
    server.reflect_apply(rid)

    raw = server.reflect_review(rid)
    out = json.loads(raw)
    applied = [c for c in out["candidates"] if c.get("already_applied")]
    assert len(applied) >= 1


def test_review_filter_by_decision(server):
    db_path = os.environ["SQLITE_MEMORY_DB"]
    _seed_stale_task(db_path, "rev-filt")
    rid = json.loads(server.reflect_start(stale_days=60))["run_id"]
    _accept_first_candidate(db_path, rid)

    raw = server.reflect_review(rid, decision_filter="accept")
    out = json.loads(raw)
    assert all(c["human_decision"] == "accept" for c in out["candidates"])


def test_review_filter_by_candidate_type(server):
    db_path = os.environ["SQLITE_MEMORY_DB"]
    _seed_stale_task(db_path, "rev-type")
    rid = json.loads(server.reflect_start(stale_days=60))["run_id"]

    raw = server.reflect_review(rid, candidate_type_filter="stale_overdue_tasks")
    out = json.loads(raw)
    assert all(
        c["candidate_type"] == "stale_overdue_tasks" for c in out["candidates"]
    )


# ── reflect_discard ────────────────────────────────────────────────────


def test_discard_terminal_run_cascades(server):
    """Discarding a completed run removes inputs + candidates via FK CASCADE."""
    db_path = os.environ["SQLITE_MEMORY_DB"]
    _seed_stale_task(db_path, "discard-1")
    rid = json.loads(server.reflect_start(stale_days=60))["run_id"]

    # Sanity — verify dependents exist
    c = sqlite3.connect(db_path)
    try:
        n_inputs = c.execute(
            "SELECT COUNT(*) FROM reflection_inputs WHERE run_id = ?", (rid,)
        ).fetchone()[0]
        n_cands = c.execute(
            "SELECT COUNT(*) FROM reflection_candidates WHERE run_id = ?", (rid,)
        ).fetchone()[0]
    finally:
        c.close()
    assert n_inputs >= 1 and n_cands >= 1

    raw = server.reflect_discard(rid)
    out = json.loads(raw)
    assert out["rows_deleted"] == 1

    c = sqlite3.connect(db_path)
    try:
        for tbl in (
            "reflection_runs",
            "reflection_inputs",
            "reflection_candidates",
        ):
            n = c.execute(
                f"SELECT COUNT(*) FROM {tbl} WHERE run_id = ?", (rid,)
            ).fetchone()[0]
            assert n == 0, f"{tbl} not cascaded"
    finally:
        c.close()


def test_discard_rejects_pending(server):
    db_path = os.environ["SQLITE_MEMORY_DB"]
    c = sqlite3.connect(db_path, isolation_level=None)
    try:
        c.execute(
            "INSERT INTO reflection_runs (run_id, version, status, created_by, "
            "created_at) VALUES ('rfl-pend-discard', 'reflect_v1.0', 'pending', "
            "'tester', '2026-01-01T00:00:00')"
        )
    finally:
        c.close()
    raw = server.reflect_discard("rfl-pend-discard")
    out = json.loads(raw)
    assert out.get("error_type") == "invalid_state_transition"


def test_discard_run_not_found(server):
    raw = server.reflect_discard("rfl-ghost")
    out = json.loads(raw)
    assert out.get("error_type") == "not_found"


# ── DAO-level tests for new helpers ────────────────────────────────────


def test_dao_add_apply_snapshot_validates_target_kind(conn):
    from reflection_dao import add_apply_snapshot, ReflectionStateError

    with pytest.raises(ReflectionStateError, match="unknown_target_kind"):
        add_apply_snapshot(
            conn,
            "rfl-x",
            "cand-x",
            target_kind="email",
            target_ref="t",
            before_state={},
            after_state={},
        )


def test_dao_has_apply_snapshot_returns_correct_bool(conn):
    from reflection_dao import (
        add_apply_snapshot,
        add_candidate,
        create_run,
        has_apply_snapshot,
    )

    rid = create_run(conn)
    cid = add_candidate(
        conn,
        rid,
        candidate_type="t",
        suggested_action="merge",
        target_kind="task",
        target_ref="t1",
        evidence={},
    )
    assert has_apply_snapshot(conn, rid, cid) is False
    add_apply_snapshot(
        conn,
        rid,
        cid,
        target_kind="task",
        target_ref="t1",
        before_state={"x": 1},
        after_state={"x": 2},
    )
    assert has_apply_snapshot(conn, rid, cid) is True


def test_dao_list_apply_snapshots_filters(conn):
    from reflection_dao import (
        add_apply_snapshot,
        add_candidate,
        create_run,
        list_apply_snapshots,
    )

    rid = create_run(conn)
    cid = add_candidate(
        conn,
        rid,
        candidate_type="t",
        suggested_action="m",
        target_kind="task",
        target_ref="t1",
        evidence={},
    )
    add_apply_snapshot(
        conn,
        rid,
        cid,
        target_kind="task",
        target_ref="t1",
        before_state={},
        after_state={},
    )
    rows, total = list_apply_snapshots(conn, run_id=rid)
    assert total == 1
    assert rows[0]["candidate_id"] == cid
    assert rows[0]["before_state"] == {}


def test_dao_discard_run_requires_terminal(conn):
    from reflection_dao import create_run, discard_run, ReflectionStateError

    rid = create_run(conn)
    with pytest.raises(ReflectionStateError, match="cannot_discard_active_run"):
        discard_run(conn, rid)


def test_dao_discard_run_returns_zero_for_missing(conn):
    from reflection_dao import discard_run, ReflectionStateError

    with pytest.raises(ReflectionStateError, match="run_not_found"):
        discard_run(conn, "rfl-nope")
