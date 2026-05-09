"""Phase 1 schema migration tests for Memory Reflection (reflect_v1.0).

Covers the 4 new tables — reflection_runs, reflection_inputs,
reflection_candidates, reflection_apply_snapshots — created in schema.py
per approved corrections C1, C2, C5, C6, C9, C10, C11, C13 from entity
MemoryReflection_DreamsAlignmentCorrections.

NOTE: SQLite enforces FOREIGN KEY constraints only when
`PRAGMA foreign_keys = ON` is set per-connection. CASCADE tests
explicitly enable the pragma; other tests don't need it.
"""

from __future__ import annotations

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from schema import init_db
from db_utils import now_iso


@pytest.fixture
def db(tmp_path):
    db_path = str(tmp_path / "reflect_phase1.db")
    init_db(db_path)
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


@pytest.fixture
def db_with_fk(tmp_path):
    """Connection with foreign-key enforcement explicitly enabled."""
    db_path = str(tmp_path / "reflect_phase1_fk.db")
    init_db(db_path)
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    yield conn
    conn.close()


def _create_run(conn: sqlite3.Connection, run_id: str = "run-001", **overrides) -> str:
    fields = {
        "run_id": run_id,
        "created_at": now_iso(),
    }
    fields.update(overrides)
    cols = ", ".join(fields.keys())
    ph = ", ".join("?" * len(fields))
    conn.execute(f"INSERT INTO reflection_runs ({cols}) VALUES ({ph})", tuple(fields.values()))
    return run_id


def _create_input(conn: sqlite3.Connection, run_id: str, input_type: str = "tasks") -> str:
    iid = f"input-{run_id}-{input_type}"
    conn.execute(
        "INSERT INTO reflection_inputs (input_id, run_id, input_type, input_ref_json, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (iid, run_id, input_type, '{"project":"x"}', now_iso()),
    )
    return iid


def _create_candidate(
    conn: sqlite3.Connection,
    run_id: str,
    candidate_id: str = "cand-001",
    target_kind: str = "task",
    human_decision: str | None = None,
) -> str:
    conn.execute(
        "INSERT INTO reflection_candidates "
        "(candidate_id, run_id, candidate_type, suggested_action, target_kind, "
        "target_ref, evidence_json, human_decision, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            candidate_id,
            run_id,
            "exact_duplicate_titles",
            "merge_or_archive",
            target_kind,
            "task-xyz",
            '{"why":"identical title"}',
            human_decision,
            now_iso(),
        ),
    )
    return candidate_id


# ── Schema presence ─────────────────────────────────────────────────────


def test_phase1_tables_exist(db):
    expected = {
        "reflection_runs",
        "reflection_inputs",
        "reflection_candidates",
        "reflection_apply_snapshots",
    }
    rows = db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'reflection_%'"
    ).fetchall()
    found = {r["name"] for r in rows}
    assert expected <= found, f"Missing: {expected - found}"


def test_phase1_indexes_exist(db):
    """Verify all C1/C10 indexes for status/archived/run/decision queries."""
    rows = db.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_reflection_%'"
    ).fetchall()
    found = {r["name"] for r in rows}
    expected = {
        "idx_reflection_runs_status_created",
        "idx_reflection_runs_archived",
        "idx_reflection_inputs_run",
        "idx_reflection_candidates_run",
        "idx_reflection_candidates_decision",
        "idx_reflection_candidates_target",
        "idx_reflection_apply_run",
        "idx_reflection_apply_target",
    }
    assert expected <= found, f"Missing indexes: {expected - found}"


def test_init_db_is_idempotent(tmp_path):
    """C1: schema.py is the migration mechanism; init_db must be safe to re-run."""
    db_path = str(tmp_path / "idem.db")
    init_db(db_path)
    init_db(db_path)
    init_db(db_path)
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'reflection_%'"
        ).fetchall()
        assert len(rows) == 4
    finally:
        conn.close()


# ── Default values ──────────────────────────────────────────────────────


def test_reflection_runs_default_version_and_status(db):
    """C11 default version = reflect_v1.0; C1 default status = pending."""
    _create_run(db, "run-defaults")
    row = db.execute(
        "SELECT version, status, created_by FROM reflection_runs WHERE run_id = ?",
        ("run-defaults",),
    ).fetchone()
    assert row["version"] == "reflect_v1.0"
    assert row["status"] == "pending"
    assert row["created_by"] == "system"


# ── CHECK constraints (C1, C5, C3, target_kind, decision) ───────────────


@pytest.mark.parametrize(
    "valid_status",
    ["pending", "running", "completed", "failed", "canceled"],
)
def test_reflection_runs_status_check_accepts_all_valid(db, valid_status):
    _create_run(db, f"run-{valid_status}", status=valid_status)


def test_reflection_runs_status_check_rejects_invalid(db):
    with pytest.raises(sqlite3.IntegrityError):
        _create_run(db, "run-bad", status="abandoned")


def test_reflection_runs_error_type_accepts_null(db):
    _create_run(db, "run-no-error", error_type=None)


@pytest.mark.parametrize(
    "valid_error",
    [
        "timeout",
        "internal_error",
        "input_session_unavailable",
        "input_too_large",
        "instructions_too_long",
        "candidate_limit_exceeded",
    ],
)
def test_reflection_runs_error_type_accepts_taxonomy(db, valid_error):
    _create_run(db, f"run-{valid_error}", error_type=valid_error)


def test_reflection_runs_error_type_rejects_invalid(db):
    with pytest.raises(sqlite3.IntegrityError):
        _create_run(db, "run-bad-error", error_type="oopsie")


@pytest.mark.parametrize("valid_input", ["tasks", "sessions", "entities", "notes"])
def test_reflection_inputs_input_type_accepts_resource_types(db, valid_input):
    _create_run(db, "run-i")
    iid = f"in-{valid_input}"
    db.execute(
        "INSERT INTO reflection_inputs (input_id, run_id, input_type, input_ref_json, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (iid, "run-i", valid_input, "{}", now_iso()),
    )


def test_reflection_inputs_input_type_rejects_unknown(db):
    _create_run(db, "run-i2")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO reflection_inputs (input_id, run_id, input_type, input_ref_json, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("bad", "run-i2", "emails", "{}", now_iso()),
        )


@pytest.mark.parametrize("valid_kind", ["task", "entity", "note", "observation"])
def test_reflection_candidates_target_kind_accepts_valid(db, valid_kind):
    _create_run(db, "run-c")
    _create_candidate(db, "run-c", candidate_id=f"c-{valid_kind}", target_kind=valid_kind)


def test_reflection_candidates_target_kind_rejects_invalid(db):
    _create_run(db, "run-c2")
    with pytest.raises(sqlite3.IntegrityError):
        _create_candidate(db, "run-c2", candidate_id="bad-kind", target_kind="email")


@pytest.mark.parametrize("decision", [None, "accept", "reject", "defer"])
def test_reflection_candidates_decision_accepts_null_and_enum(db, decision):
    _create_run(db, "run-d")
    _create_candidate(
        db,
        "run-d",
        candidate_id=f"c-{decision or 'null'}",
        human_decision=decision,
    )


def test_reflection_candidates_decision_rejects_invalid(db):
    _create_run(db, "run-d2")
    with pytest.raises(sqlite3.IntegrityError):
        _create_candidate(db, "run-d2", candidate_id="c-bad", human_decision="maybe")


# ── ON DELETE CASCADE (C9 + general hygiene) ────────────────────────────


def test_cascade_delete_run_removes_inputs_and_candidates_and_snapshots(db_with_fk):
    """C9: deleting a run cascades to inputs, candidates, and apply snapshots."""
    conn = db_with_fk
    _create_run(conn, "run-cascade")
    _create_input(conn, "run-cascade", "tasks")
    _create_candidate(conn, "run-cascade", "cand-cascade")
    conn.execute(
        "INSERT INTO reflection_apply_snapshots "
        "(snapshot_id, run_id, candidate_id, target_kind, target_ref, "
        "before_state_json, after_state_json, applied_by, applied_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "snap-cascade",
            "run-cascade",
            "cand-cascade",
            "task",
            "task-xyz",
            "{}",
            "{}",
            "tester",
            now_iso(),
        ),
    )

    # Sanity: rows exist
    assert (
        conn.execute(
            "SELECT COUNT(*) AS c FROM reflection_inputs WHERE run_id = ?",
            ("run-cascade",),
        ).fetchone()["c"]
        == 1
    )
    assert (
        conn.execute(
            "SELECT COUNT(*) AS c FROM reflection_candidates WHERE run_id = ?",
            ("run-cascade",),
        ).fetchone()["c"]
        == 1
    )
    assert (
        conn.execute(
            "SELECT COUNT(*) AS c FROM reflection_apply_snapshots WHERE run_id = ?",
            ("run-cascade",),
        ).fetchone()["c"]
        == 1
    )

    conn.execute("DELETE FROM reflection_runs WHERE run_id = ?", ("run-cascade",))

    for table in (
        "reflection_inputs",
        "reflection_candidates",
        "reflection_apply_snapshots",
    ):
        rows = conn.execute(
            f"SELECT COUNT(*) AS c FROM {table} WHERE run_id = ?", ("run-cascade",)
        ).fetchone()
        assert rows["c"] == 0, f"{table} still has rows after run delete"


def test_cascade_delete_candidate_removes_its_snapshots(db_with_fk):
    """Apply snapshots cascade from candidate as well as run."""
    conn = db_with_fk
    _create_run(conn, "run-cand-cascade")
    _create_candidate(conn, "run-cand-cascade", "cand-x")
    conn.execute(
        "INSERT INTO reflection_apply_snapshots "
        "(snapshot_id, run_id, candidate_id, target_kind, target_ref, "
        "before_state_json, after_state_json, applied_by, applied_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("snap-x", "run-cand-cascade", "cand-x", "task", "t1", "{}", "{}", "tester", now_iso()),
    )

    conn.execute("DELETE FROM reflection_candidates WHERE candidate_id = ?", ("cand-x",))
    rows = conn.execute(
        "SELECT COUNT(*) AS c FROM reflection_apply_snapshots WHERE candidate_id = ?",
        ("cand-x",),
    ).fetchone()
    assert rows["c"] == 0


# ── Phase 0.5 regression (corrections must not break existing reflect_audit) ─


def test_phase05_reflect_audit_still_works_after_phase1_schema(db):
    """Smoke: Phase 0.5 reflect_audit on the same DB after Phase 1 schema."""
    from reflection import audit_reflection_candidates

    # Run on empty Phase-1-augmented DB; should still return empty report
    report = audit_reflection_candidates(db)
    assert report["version"].startswith("reflect_audit_v0.5")
    assert report["summary"]["total_candidates"] == 0
