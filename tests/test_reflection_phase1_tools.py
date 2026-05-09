"""Integration tests for Phase 1 Memory Reflection MCP tools.

Tests hit the in-process tool implementations on intel_server (reflect_start,
_status, _history, _cancel, _archive, _decide). Since FastMCP wraps tools
into typed callables, we invoke the underlying Python functions directly
through their registered identifiers, with a temp DB pointed via env var.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from db_utils import now_iso
from schema import init_db


@pytest.fixture
def server(tmp_path, monkeypatch):
    """Spin up a fresh DB and an intel_server module bound to it."""
    db_path = str(tmp_path / "phase1_tools.db")
    init_db(db_path)
    monkeypatch.setenv("SQLITE_MEMORY_DB", db_path)
    # Force a fresh import so _get_conn binds to the temp DB.
    for mod in [
        "intel_server",
        "db_utils",
        "reflection",
        "reflection_dao",
    ]:
        sys.modules.pop(mod, None)
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    import intel_server  # noqa: F401  (re-imported for fresh state)

    yield intel_server


def _seed_overdue_task(db_path: str, task_id: str = "stale-1"):
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        from datetime import datetime, timedelta, timezone

        long_ago = (datetime.now(timezone.utc) - timedelta(days=120)).date().isoformat()
        conn.execute(
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
        conn.close()


# ── reflect_start ────────────────────────────────────────────────────────


def test_reflect_start_creates_run_and_persists_candidates(server, monkeypatch):
    db_path = os.environ["SQLITE_MEMORY_DB"]
    _seed_overdue_task(db_path)

    raw = server.reflect_start(stale_days=60, limit_per_category=10)
    out = json.loads(raw)
    assert out["status"] == "completed"
    assert out["candidates_persisted"] >= 1
    assert "run_id" in out

    # Verify persisted to DB
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    try:
        row = c.execute(
            "SELECT * FROM reflection_runs WHERE run_id = ?", (out["run_id"],)
        ).fetchone()
        assert row["status"] == "completed"
        assert row["started_at"] is not None
        assert row["ended_at"] is not None
        cands = c.execute(
            "SELECT COUNT(*) AS c FROM reflection_candidates WHERE run_id = ?",
            (out["run_id"],),
        ).fetchone()
        assert cands["c"] == out["candidates_persisted"]
        inputs = c.execute(
            "SELECT COUNT(*) AS c FROM reflection_inputs WHERE run_id = ?",
            (out["run_id"],),
        ).fetchone()
        assert inputs["c"] == 1
    finally:
        c.close()


def test_reflect_start_rejects_too_long_instructions(server):
    raw = server.reflect_start(instructions="x" * 5000)
    out = json.loads(raw)
    assert out.get("error_type") == "instructions_too_long"


def test_reflect_start_completes_with_zero_candidates_on_empty_db(server):
    raw = server.reflect_start(stale_days=60)
    out = json.loads(raw)
    assert out["status"] == "completed"
    assert out["candidates_persisted"] == 0


# ── reflect_status ───────────────────────────────────────────────────────


def test_reflect_status_returns_run_inputs_and_counts(server):
    raw = server.reflect_start(stale_days=60)
    out = json.loads(raw)
    rid = out["run_id"]

    status_raw = server.reflect_status(rid)
    s = json.loads(status_raw)
    assert s["run"]["run_id"] == rid
    assert s["run"]["status"] == "completed"
    assert isinstance(s["inputs"], list)
    assert len(s["inputs"]) == 1
    assert s["inputs"][0]["input_type"] == "tasks"
    assert "pending" in s["candidate_counts"]
    assert s["candidate_counts"]["total"] == out["candidates_persisted"]


def test_reflect_status_not_found(server):
    raw = server.reflect_status("rfl-missing")
    s = json.loads(raw)
    assert s.get("error_type") == "not_found"


# ── reflect_history ──────────────────────────────────────────────────────


def test_reflect_history_paginates_and_filters_archived(server):
    ids = []
    for _ in range(3):
        raw = server.reflect_start()
        ids.append(json.loads(raw)["run_id"])

    # Archive one
    server.reflect_archive(ids[0])

    raw = server.reflect_history(limit=10)
    h = json.loads(raw)
    visible = {r["run_id"] for r in h["runs"]}
    assert ids[0] not in visible
    assert ids[1] in visible and ids[2] in visible
    assert h["total"] == 2

    raw = server.reflect_history(include_archived=True)
    h_all = json.loads(raw)
    visible_all = {r["run_id"] for r in h_all["runs"]}
    assert ids[0] in visible_all
    assert h_all["total"] == 3


def test_reflect_history_status_filter(server):
    rid_complete = json.loads(server.reflect_start())["run_id"]
    raw = server.reflect_history(status_filter="completed")
    h = json.loads(raw)
    assert any(r["run_id"] == rid_complete for r in h["runs"])
    assert all(r["status"] == "completed" for r in h["runs"])


def test_reflect_history_rejects_unknown_status(server):
    raw = server.reflect_history(status_filter="zombie")
    h = json.loads(raw)
    assert "error" in h


# ── reflect_cancel ───────────────────────────────────────────────────────


def test_reflect_cancel_rejects_completed(server):
    rid = json.loads(server.reflect_start())["run_id"]
    raw = server.reflect_cancel(rid)
    out = json.loads(raw)
    assert out.get("error_type") == "invalid_state_transition"


def test_reflect_cancel_succeeds_on_pending_run(server):
    """Manually create a pending run (without start) to test cancel."""
    db_path = os.environ["SQLITE_MEMORY_DB"]
    c = sqlite3.connect(db_path, isolation_level=None)
    try:
        from db_utils import now_iso as _now

        c.execute(
            "INSERT INTO reflection_runs (run_id, version, status, created_by, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("rfl-pending", "reflect_v1.0", "pending", "test", _now()),
        )
    finally:
        c.close()
    raw = server.reflect_cancel("rfl-pending")
    out = json.loads(raw)
    assert out["status"] == "canceled"


# ── reflect_archive ──────────────────────────────────────────────────────


def test_reflect_archive_terminal_succeeds(server):
    rid = json.loads(server.reflect_start())["run_id"]
    raw = server.reflect_archive(rid)
    out = json.loads(raw)
    assert out["archived_at"] is not None
    assert out["newly_archived"] is True


def test_reflect_archive_idempotent(server):
    rid = json.loads(server.reflect_start())["run_id"]
    server.reflect_archive(rid)
    raw = server.reflect_archive(rid)
    out = json.loads(raw)
    assert out["newly_archived"] is False


def test_reflect_archive_rejects_pending(server):
    db_path = os.environ["SQLITE_MEMORY_DB"]
    c = sqlite3.connect(db_path, isolation_level=None)
    try:
        from db_utils import now_iso as _now

        c.execute(
            "INSERT INTO reflection_runs (run_id, version, status, created_by, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("rfl-pending-arch", "reflect_v1.0", "pending", "test", _now()),
        )
    finally:
        c.close()
    raw = server.reflect_archive("rfl-pending-arch")
    out = json.loads(raw)
    assert out.get("error_type") == "invalid_state_transition"


# ── reflect_decide ───────────────────────────────────────────────────────


def test_reflect_decide_records_decision(server):
    db_path = os.environ["SQLITE_MEMORY_DB"]
    _seed_overdue_task(db_path)
    rid = json.loads(server.reflect_start())["run_id"]

    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    try:
        cand = c.execute(
            "SELECT candidate_id FROM reflection_candidates WHERE run_id = ? LIMIT 1",
            (rid,),
        ).fetchone()
    finally:
        c.close()
    assert cand is not None, "expected at least one candidate from seed"

    raw = server.reflect_decide(cand["candidate_id"], "accept", "alice")
    out = json.loads(raw)
    assert out["decision"] == "accept"
    assert out["decided_by"] == "alice"


def test_reflect_decide_validates_enum(server):
    raw = server.reflect_decide("c-x", "maybe")
    out = json.loads(raw)
    assert out.get("error_type") == "invalid_argument"


def test_reflect_decide_not_found(server):
    raw = server.reflect_decide("c-nonexistent", "accept")
    out = json.loads(raw)
    assert out.get("error_type") == "not_found"
