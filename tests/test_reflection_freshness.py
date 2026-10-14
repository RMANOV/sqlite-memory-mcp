"""Reflection approvals must not overwrite work from another SQLite session."""

from __future__ import annotations

import sqlite3

import pytest

import reflection_dao as dao
from db_utils import apply_task_mutation, create_task_with_ledger, get_conn_immediate
from reflection import audit_reflection_candidates
from reflection_apply import apply_run
from schema import init_db


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "reflection-freshness.db")
    init_db(path)
    with get_conn_immediate(path) as conn:
        create_task_with_ledger(
            conn,
            "task-1",
            "Synthetic overdue task",
            "2020-01-01T00:00:00+00:00",
            status="not_started",
            section="today",
            due_date="2020-01-01",
        )
    return path


def _candidate(path):
    with get_conn_immediate(path) as conn:
        rid = dao.create_run(conn)
        dao.start_run(conn, rid)
        item = audit_reflection_candidates(conn)["candidates"]["stale_overdue_tasks"][0]
        cid = dao.add_candidate(
            conn,
            rid,
            candidate_type="stale_overdue_tasks",
            suggested_action=item["suggested_action"],
            target_kind="task",
            target_ref=item["id"],
            evidence=item,
        )
        dao.finish_run(conn, rid, "completed")
    with get_conn_immediate(path) as conn:
        assert dao.decide_candidate(conn, cid, "accept", "reviewer")
    return rid, cid


def _task(path):
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        return dict(conn.execute("SELECT * FROM tasks WHERE id='task-1'").fetchone())


def test_reopened_task_survives_old_approval(db_path):
    rid, cid = _candidate(db_path)
    with get_conn_immediate(db_path) as other_session:
        apply_task_mutation(
            other_session,
            "task-1",
            {"status": "in_progress", "notes": "Work resumed"},
            tool_name="test.other_session",
            actor_id="session-B",
        )
    before = _task(db_path)
    with get_conn_immediate(db_path) as conn:
        result = apply_run(conn, rid)
    after = _task(db_path)
    assert after == before, {"before": before, "after": after, "result": result}
    assert result["applied"] == 0
    assert {"candidate_id": cid, "reason": "stale_review"} in result["skipped"]


def test_unchanged_approval_applies_once(db_path):
    rid, cid = _candidate(db_path)
    with get_conn_immediate(db_path) as conn:
        result = apply_run(conn, rid)
    assert result["applied"] == 1, result
    assert _task(db_path)["status"] == "archived"
    with get_conn_immediate(db_path) as conn:
        result = apply_run(conn, rid)
    assert result["applied"] == 0
    assert {"candidate_id": cid, "reason": "already_applied"} in result["skipped"]


def _effect_counts(conn):
    return tuple(
        conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in (
            "memory_events",
            "task_field_versions",
            "reflection_apply_snapshots",
        )
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"description": "New implementation plan"},
        {"due_date": "2099-01-01"},
        {"priority": "high", "assignee": "another-person"},
        {"recurring": "daily", "reminder_at": "2099-01-01T10:00:00+00:00"},
    ],
)
def test_non_status_edits_refuse_and_preserve_audit(db_path, changes):
    rid, cid = _candidate(db_path)
    with get_conn_immediate(db_path) as other:
        apply_task_mutation(other, "task-1", changes)
    before = _task(db_path)
    with get_conn_immediate(db_path) as conn:
        counts = _effect_counts(conn)
        assert apply_run(conn, rid)["applied"] == 0
        assert _effect_counts(conn) == counts
    with get_conn_immediate(db_path) as conn:
        candidate = dao.get_candidate(conn, cid)
        error = candidate["apply_error"]
        assert error["reason"] == "stale_review"
        assert error["attempted_by"] == "user"
        assert error["attempted_at"]
        assert error["requires_new_review"] is True
        assert candidate["human_decision"] == "accept"
        assert candidate["decided_by"] == "reviewer"
        with pytest.raises(
            dao.ReflectionStateError, match="candidate_requires_new_review"
        ):
            dao.decide_candidate(conn, cid, "accept")
        result = apply_run(conn, rid)
        assert result["skipped"] == [
            {"candidate_id": cid, "reason": "candidate_requires_new_review"}
        ]
        assert dao.get_candidate(conn, cid)["apply_error"] == error
    assert _task(db_path) == before


def test_raw_edit_with_unchanged_timestamp_is_refused(db_path):
    rid, cid = _candidate(db_path)
    with sqlite3.connect(db_path) as other:
        other.execute(
            "UPDATE tasks SET title='Changed without touching the clock' WHERE id='task-1'"
        )
    before = _task(db_path)
    with get_conn_immediate(db_path) as conn:
        assert apply_run(conn, rid)["skipped"] == [
            {"candidate_id": cid, "reason": "stale_review"}
        ]
    assert _task(db_path) == before


def test_aba_with_same_timestamp_is_refused(db_path):
    rid, cid = _candidate(db_path)
    before = _task(db_path)
    with get_conn_immediate(db_path) as other:
        for status in ("in_progress", "not_started"):
            apply_task_mutation(
                other, "task-1", {"status": status}, timestamp=before["updated_at"]
            )
    assert _task(db_path) == before  # only the logical field versions expose ABA
    with get_conn_immediate(db_path) as conn:
        assert apply_run(conn, rid)["skipped"] == [
            {"candidate_id": cid, "reason": "stale_review"}
        ]
    assert _task(db_path) == before


def test_accept_does_not_refresh_previously_reviewed_state(db_path):
    rid, cid = _candidate(db_path)
    with get_conn_immediate(db_path) as conn:
        reviewed = dao.get_candidate(conn, cid)["reviewed_state"]
        dao.decide_candidate(conn, cid, "defer")
    with get_conn_immediate(db_path) as other:
        apply_task_mutation(other, "task-1", {"notes": "Changed before acceptance"})
    with get_conn_immediate(db_path) as conn:
        dao.decide_candidate(conn, cid, "accept")
        assert dao.get_candidate(conn, cid)["reviewed_state"] == reviewed
        assert apply_run(conn, rid)["applied"] == 0


@pytest.mark.parametrize("stored", [None, "{bad json", "{}", "[]", '"text"', "null"])
def test_missing_legacy_or_malformed_review_fails_closed(db_path, stored):
    rid, cid = _candidate(db_path)
    before = _task(db_path)
    with get_conn_immediate(db_path) as conn:
        conn.execute(
            "UPDATE reflection_candidates SET reviewed_state_json=? WHERE candidate_id=?",
            (stored, cid),
        )
        counts = _effect_counts(conn)
        result = apply_run(conn, rid)
        assert result["skipped"] == [
            {"candidate_id": cid, "reason": "missing_review_state"}
        ]
        assert _effect_counts(conn) == counts
    assert _task(db_path) == before


@pytest.mark.parametrize("failure_point", ["snapshot", "mutation"])
def test_failure_rolls_back_task_and_ledger_but_preserves_outer_transaction(
    db_path, monkeypatch, failure_point
):
    import reflection_apply

    rid, cid = _candidate(db_path)
    before = _task(db_path)
    original_mutation = reflection_apply.apply_task_mutation

    def fail_snapshot(*args, **kwargs):
        raise RuntimeError("injected snapshot failure")

    def fail_mutation(*args, **kwargs):
        original_mutation(*args, **kwargs)
        raise RuntimeError("injected mutation failure after write")

    if failure_point == "snapshot":
        monkeypatch.setattr(dao, "add_apply_snapshot", fail_snapshot)
    else:
        monkeypatch.setattr(reflection_apply, "apply_task_mutation", fail_mutation)
    with get_conn_immediate(db_path) as conn:
        outer_id = dao.create_run(conn)
        counts = _effect_counts(conn)
        result = apply_run(conn, rid)
        assert result["applied"] == 0
        assert result["failed"][0]["candidate_id"] == cid
        assert "injected" in result["failed"][0]["error"]
        assert _effect_counts(conn) == counts
    with get_conn_immediate(db_path) as conn:
        assert dao.get_run(conn, outer_id) is not None
        assert dao.get_candidate(conn, cid)["apply_error"] is None
    assert _task(db_path) == before


def test_concurrent_writer_cannot_enter_compare_mutate_gap(db_path, monkeypatch):
    import reflection_apply

    rid, _ = _candidate(db_path)
    real_mutation = reflection_apply.apply_task_mutation
    observed = []

    def contend(*args, **kwargs):
        # Called after comparison, at the mutation boundary. A real second
        # SQLite connection tries to win precisely the formerly unsafe gap.
        with sqlite3.connect(db_path, isolation_level=None, timeout=0) as other:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                other.execute("UPDATE tasks SET status='in_progress' WHERE id='task-1'")
            observed.append("writer blocked")
        return real_mutation(*args, **kwargs)

    monkeypatch.setattr(reflection_apply, "apply_task_mutation", contend)
    # Exercise the direct DAO autocommit case, without any wrapper write lock.
    with sqlite3.connect(db_path, isolation_level=None) as conn:
        conn.row_factory = sqlite3.Row
        assert apply_run(conn, rid)["applied"] == 1
    assert observed == ["writer blocked"]
    assert _task(db_path)["status"] == "archived"


def test_concurrent_apply_uses_fresh_idempotency_check(db_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    rid, cid = _candidate(db_path)
    both_listed = Barrier(2)
    real_list = dao.list_candidates

    def list_then_wait(*args, **kwargs):
        result = real_list(*args, **kwargs)
        both_listed.wait(timeout=10)
        return result

    monkeypatch.setattr(dao, "list_candidates", list_then_wait)

    def apply_session():
        with sqlite3.connect(db_path, isolation_level=None, timeout=5) as conn:
            conn.row_factory = sqlite3.Row
            return apply_run(conn, rid)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: apply_session(), range(2)))
    assert sorted(r["applied"] for r in results) == [0, 1]
    assert all(r["failed"] == [] for r in results)
    assert {"candidate_id": cid, "reason": "already_applied"} in [
        s for r in results for s in r["skipped"]
    ]
    with get_conn_immediate(db_path) as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM reflection_apply_snapshots").fetchone()[
                0
            ]
            == 1
        )


def test_revocation_after_listing_is_honored(db_path, monkeypatch):
    rid, cid = _candidate(db_path)
    real_list = dao.list_candidates

    def list_then_revoke(*args, **kwargs):
        result = real_list(*args, **kwargs)
        with get_conn_immediate(db_path) as other:
            dao.decide_candidate(other, cid, "reject", "second-reviewer")
        return result

    monkeypatch.setattr(dao, "list_candidates", list_then_revoke)
    before = _task(db_path)
    with sqlite3.connect(db_path, isolation_level=None) as conn:
        conn.row_factory = sqlite3.Row
        result = apply_run(conn, rid)
    assert result["skipped"] == [
        {"candidate_id": cid, "reason": "decision_not_accepted"}
    ]
    assert _task(db_path) == before


def test_stale_outer_read_transaction_cannot_upgrade(db_path):
    rid, _ = _candidate(db_path)
    with sqlite3.connect(db_path, isolation_level=None, timeout=0) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN")
        conn.execute("SELECT * FROM tasks").fetchall()
        with get_conn_immediate(db_path) as other:
            apply_task_mutation(other, "task-1", {"status": "in_progress"})
        result = apply_run(conn, rid)
        assert result["applied"] == 0
        assert "locked" in result["failed"][0]["error"]
        conn.rollback()
    assert _task(db_path)["status"] == "in_progress"


def test_migration_keeps_legacy_approvals_unbound(db_path):
    rid, cid = _candidate(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "ALTER TABLE reflection_candidates DROP COLUMN reviewed_state_json"
        )
        conn.execute("ALTER TABLE reflection_candidates DROP COLUMN apply_error_json")
    init_db(db_path)
    init_db(db_path)
    with get_conn_immediate(db_path) as conn:
        assert dao.get_candidate(conn, cid)["reviewed_state"] is None
        result = apply_run(conn, rid)
        assert result["skipped"] == [
            {"candidate_id": cid, "reason": "missing_review_state"}
        ]
    assert _task(db_path)["status"] == "not_started"


def test_public_tools_expose_refusal_and_fresh_review_can_succeed(db_path, monkeypatch):
    import json
    import intel_server
    from db_utils import get_conn

    monkeypatch.setattr(intel_server, "_get_conn", lambda: get_conn(db_path))
    monkeypatch.setattr(
        intel_server, "_get_conn_immediate", lambda: get_conn_immediate(db_path)
    )
    rid = json.loads(intel_server.reflect_start())["run_id"]
    candidate = json.loads(intel_server.reflect_review(rid))["candidates"][0]
    cid = candidate["candidate_id"]
    assert candidate["reviewed_state"]["task"]["status"] == "not_started"
    assert (
        json.loads(intel_server.reflect_decide(cid, "accept"))["decision"] == "accept"
    )
    with get_conn_immediate(db_path) as other:
        apply_task_mutation(other, "task-1", {"status": "in_progress"})
    before = _task(db_path)
    result = json.loads(intel_server.reflect_apply(rid))
    assert result["applied"] == 0
    assert result["skipped"] == [{"candidate_id": cid, "reason": "stale_review"}]
    assert _task(db_path) == before
    candidate = json.loads(intel_server.reflect_review(rid))["candidates"][0]
    assert candidate["apply_error"]["reason"] == "stale_review"
    assert candidate["already_applied"] is False
    # A newly eligible task needs a new candidate and a new explicit decision.
    with get_conn_immediate(db_path) as other:
        apply_task_mutation(other, "task-1", {"status": "not_started"})
    new_rid = json.loads(intel_server.reflect_start())["run_id"]
    fresh = json.loads(intel_server.reflect_review(new_rid))["candidates"][0]
    assert fresh["candidate_id"] != cid
    intel_server.reflect_decide(fresh["candidate_id"], "accept")
    assert json.loads(intel_server.reflect_apply(new_rid))["applied"] == 1
