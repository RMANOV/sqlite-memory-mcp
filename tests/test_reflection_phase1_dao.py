"""DAO-layer tests for Phase 1 Memory Reflection (reflect_v1.0).

Covers state-machine guards (cancel/archive transitions), default values,
limits enforcement (C14 instructions cap + sessions cap), pagination
correctness, and JSON encoding/decoding for inputs/candidates.
"""

from __future__ import annotations

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from reflection_dao import (
    MAX_INSTRUCTIONS_CHARS,
    MAX_SESSIONS_PER_RUN,
    ReflectionStateError,
    add_candidate,
    add_input,
    archive_run,
    cancel_run,
    candidate_decision_counts,
    create_run,
    decide_candidate,
    finish_run,
    get_candidate,
    get_run,
    list_candidates,
    list_inputs,
    list_runs,
    start_run,
)
from schema import init_db


@pytest.fixture
def conn(tmp_path):
    db_path = str(tmp_path / "phase1_dao.db")
    init_db(db_path)
    c = sqlite3.connect(db_path, isolation_level=None)
    c.row_factory = sqlite3.Row
    yield c
    c.close()


# ── create_run / get_run ─────────────────────────────────────────────────


def test_create_run_uses_defaults(conn):
    rid = create_run(conn)
    row = get_run(conn, rid)
    assert row is not None
    assert row["status"] == "pending"
    assert row["version"] == "reflect_v1.0"
    assert row["created_by"] == "system"
    assert row["started_at"] is None
    assert row["ended_at"] is None
    assert row["archived_at"] is None


def test_create_run_accepts_overrides(conn):
    rid = create_run(
        conn,
        version="reflect_v0.5",
        model="claude-opus-4-7",
        instructions="focus on stale entries",
        created_by="alice",
    )
    row = get_run(conn, rid)
    assert row["version"] == "reflect_v0.5"
    assert row["model"] == "claude-opus-4-7"
    assert row["instructions"] == "focus on stale entries"
    assert row["created_by"] == "alice"


def test_create_run_rejects_too_long_instructions(conn):
    too_long = "x" * (MAX_INSTRUCTIONS_CHARS + 1)
    with pytest.raises(ReflectionStateError, match="instructions_too_long"):
        create_run(conn, instructions=too_long)


def test_get_run_returns_none_for_missing(conn):
    assert get_run(conn, "nope") is None


# ── start_run ────────────────────────────────────────────────────────────


def test_start_run_pending_to_running(conn):
    rid = create_run(conn)
    changed = start_run(conn, rid)
    assert changed is True
    row = get_run(conn, rid)
    assert row["status"] == "running"
    assert row["started_at"] is not None


def test_start_run_idempotent_on_running(conn):
    rid = create_run(conn)
    start_run(conn, rid)
    changed = start_run(conn, rid)
    assert changed is False


def test_start_run_rejects_terminal(conn):
    rid = create_run(conn)
    start_run(conn, rid)
    finish_run(conn, rid, "completed")
    with pytest.raises(ReflectionStateError, match="cannot_start_terminal_run"):
        start_run(conn, rid)


def test_start_run_rejects_missing(conn):
    with pytest.raises(ReflectionStateError, match="run_not_found"):
        start_run(conn, "rfl-missing")


# ── finish_run ───────────────────────────────────────────────────────────


def test_finish_run_sets_ended_at(conn):
    rid = create_run(conn)
    start_run(conn, rid)
    changed = finish_run(conn, rid, "completed", usage={"candidate_count": 3})
    assert changed is True
    row = get_run(conn, rid)
    assert row["status"] == "completed"
    assert row["ended_at"] is not None
    assert row["usage_json"] is not None
    assert "candidate_count" in row["usage_json"]


def test_finish_run_rejects_non_terminal_status(conn):
    rid = create_run(conn)
    with pytest.raises(ReflectionStateError, match="finish_requires_terminal_status"):
        finish_run(conn, rid, "running")


def test_finish_run_idempotent_after_terminal(conn):
    rid = create_run(conn)
    finish_run(conn, rid, "failed", error_type="timeout", error_message="too slow")
    changed = finish_run(conn, rid, "completed")
    assert changed is False
    row = get_run(conn, rid)
    assert row["status"] == "failed"  # unchanged


def test_finish_run_validates_error_type(conn):
    rid = create_run(conn)
    with pytest.raises(ReflectionStateError, match="unknown_error_type"):
        finish_run(conn, rid, "failed", error_type="oopsie")


# ── cancel_run ───────────────────────────────────────────────────────────


def test_cancel_run_pending_succeeds(conn):
    rid = create_run(conn)
    cancel_run(conn, rid)
    row = get_run(conn, rid)
    assert row["status"] == "canceled"


def test_cancel_run_running_succeeds(conn):
    rid = create_run(conn)
    start_run(conn, rid)
    cancel_run(conn, rid)
    row = get_run(conn, rid)
    assert row["status"] == "canceled"


def test_cancel_run_rejects_terminal(conn):
    rid = create_run(conn)
    finish_run(conn, rid, "completed")
    with pytest.raises(ReflectionStateError, match="cannot_cancel_terminal_run"):
        cancel_run(conn, rid)


# ── archive_run ──────────────────────────────────────────────────────────


def test_archive_run_requires_terminal(conn):
    rid = create_run(conn)
    with pytest.raises(ReflectionStateError, match="cannot_archive_active_run"):
        archive_run(conn, rid)


def test_archive_run_succeeds_after_completed(conn):
    rid = create_run(conn)
    finish_run(conn, rid, "completed")
    changed = archive_run(conn, rid)
    assert changed is True
    row = get_run(conn, rid)
    assert row["archived_at"] is not None


def test_archive_run_idempotent(conn):
    rid = create_run(conn)
    finish_run(conn, rid, "canceled")
    archive_run(conn, rid)
    changed = archive_run(conn, rid)
    assert changed is False


# ── list_runs (pagination + filters) ─────────────────────────────────────


def test_list_runs_excludes_archived_by_default(conn):
    a = create_run(conn)
    b = create_run(conn)
    finish_run(conn, b, "completed")
    archive_run(conn, b)
    rows, total = list_runs(conn)
    ids = {r["run_id"] for r in rows}
    assert a in ids and b not in ids
    assert total == 1


def test_list_runs_include_archived(conn):
    a = create_run(conn)
    b = create_run(conn)
    finish_run(conn, b, "completed")
    archive_run(conn, b)
    rows, total = list_runs(conn, include_archived=True)
    ids = {r["run_id"] for r in rows}
    assert a in ids and b in ids
    assert total == 2


def test_list_runs_status_filter(conn):
    create_run(conn)  # pending
    rid_done = create_run(conn)
    finish_run(conn, rid_done, "completed")
    rows, total = list_runs(conn, status_filter="completed")
    assert total == 1
    assert rows[0]["run_id"] == rid_done


def test_list_runs_rejects_unknown_status(conn):
    with pytest.raises(ReflectionStateError, match="unknown_status"):
        list_runs(conn, status_filter="zombie")


def test_list_runs_pagination(conn):
    for _ in range(5):
        create_run(conn)
    page1, total = list_runs(conn, limit=2, offset=0)
    page2, _ = list_runs(conn, limit=2, offset=2)
    assert total == 5
    assert len(page1) == 2
    assert len(page2) == 2
    assert {r["run_id"] for r in page1}.isdisjoint({r["run_id"] for r in page2})


def test_list_runs_clamps_limit(conn):
    create_run(conn)
    rows, _ = list_runs(conn, limit=99999)
    assert len(rows) <= 100  # clamped


# ── reflection_inputs ────────────────────────────────────────────────────


def test_add_input_validates_type(conn):
    rid = create_run(conn)
    with pytest.raises(ReflectionStateError, match="unknown_input_type"):
        add_input(conn, rid, "emails", {})


def test_add_input_enforces_session_limit(conn):
    rid = create_run(conn)
    too_many = {"session_ids": [f"sesn_{i}" for i in range(MAX_SESSIONS_PER_RUN + 1)]}
    with pytest.raises(ReflectionStateError, match="input_too_large"):
        add_input(conn, rid, "sessions", too_many)


def test_list_inputs_decodes_json(conn):
    rid = create_run(conn)
    add_input(conn, rid, "tasks", {"project": "alpha"})
    add_input(conn, rid, "sessions", {"session_ids": ["s1", "s2"]})
    rows = list_inputs(conn, rid)
    assert len(rows) == 2
    types = {r["input_type"] for r in rows}
    assert types == {"tasks", "sessions"}
    by_type = {r["input_type"]: r["input_ref"] for r in rows}
    assert by_type["tasks"]["project"] == "alpha"
    assert by_type["sessions"]["session_ids"] == ["s1", "s2"]


# ── reflection_candidates ────────────────────────────────────────────────


def test_add_candidate_validates_target_kind(conn):
    rid = create_run(conn)
    with pytest.raises(ReflectionStateError, match="unknown_target_kind"):
        add_candidate(
            conn,
            rid,
            candidate_type="test",
            suggested_action="merge",
            target_kind="email",
            target_ref="x",
            evidence={},
        )


def test_get_candidate_decodes_evidence_and_proposed_state(conn):
    rid = create_run(conn)
    cid = add_candidate(
        conn,
        rid,
        candidate_type="exact_duplicate_titles",
        suggested_action="merge_or_archive",
        target_kind="task",
        target_ref="t1",
        evidence={"why": "identical"},
        proposed_state={"status": "archived"},
        confidence=0.95,
    )
    row = get_candidate(conn, cid)
    assert row["evidence"] == {"why": "identical"}
    assert row["proposed_state"] == {"status": "archived"}
    assert row["confidence"] == 0.95


def test_list_candidates_filters_pending_decision(conn):
    rid = create_run(conn)
    c1 = add_candidate(
        conn,
        rid,
        candidate_type="x",
        suggested_action="merge",
        target_kind="task",
        target_ref="t1",
        evidence={},
    )
    c2 = add_candidate(
        conn,
        rid,
        candidate_type="x",
        suggested_action="merge",
        target_kind="task",
        target_ref="t2",
        evidence={},
    )
    decide_candidate(conn, c1, "accept")
    rows, total = list_candidates(conn, rid, decision_filter="pending")
    assert total == 1
    assert rows[0]["candidate_id"] == c2


def test_list_candidates_filters_by_decision(conn):
    rid = create_run(conn)
    c1 = add_candidate(
        conn,
        rid,
        candidate_type="x",
        suggested_action="merge",
        target_kind="task",
        target_ref="t1",
        evidence={},
    )
    add_candidate(
        conn,
        rid,
        candidate_type="x",
        suggested_action="merge",
        target_kind="task",
        target_ref="t2",
        evidence={},
    )
    decide_candidate(conn, c1, "reject")
    rows, _ = list_candidates(conn, rid, decision_filter="reject")
    assert {r["candidate_id"] for r in rows} == {c1}


def test_list_candidates_filters_by_type(conn):
    rid = create_run(conn)
    add_candidate(
        conn,
        rid,
        candidate_type="exact_duplicate_titles",
        suggested_action="merge",
        target_kind="task",
        target_ref="t1",
        evidence={},
    )
    add_candidate(
        conn,
        rid,
        candidate_type="stale_overdue_tasks",
        suggested_action="archive",
        target_kind="task",
        target_ref="t2",
        evidence={},
    )
    rows, total = list_candidates(
        conn, rid, candidate_type_filter="stale_overdue_tasks"
    )
    assert total == 1
    assert rows[0]["target_ref"] == "t2"


def test_decide_candidate_validates_enum(conn):
    rid = create_run(conn)
    cid = add_candidate(
        conn,
        rid,
        candidate_type="x",
        suggested_action="merge",
        target_kind="task",
        target_ref="t",
        evidence={},
    )
    with pytest.raises(ReflectionStateError, match="unknown_decision"):
        decide_candidate(conn, cid, "maybe")


def test_decide_candidate_returns_false_for_missing(conn):
    assert decide_candidate(conn, "nope", "accept") is False


def test_candidate_decision_counts_aggregates(conn):
    rid = create_run(conn)
    cids = []
    for i in range(4):
        cids.append(
            add_candidate(
                conn,
                rid,
                candidate_type="x",
                suggested_action="merge",
                target_kind="task",
                target_ref=f"t{i}",
                evidence={},
            )
        )
    decide_candidate(conn, cids[0], "accept")
    decide_candidate(conn, cids[1], "accept")
    decide_candidate(conn, cids[2], "reject")
    counts = candidate_decision_counts(conn, rid)
    assert counts == {"pending": 1, "accept": 2, "reject": 1, "defer": 0, "total": 4}
