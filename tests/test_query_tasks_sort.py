"""Focused tests for query_tasks flexible sorting (sort_by / sort_order).

Covers: default ordering unchanged, per-column sorts, NULLS LAST for dates,
validation errors, case-insensitive direction, and tie-breaker determinism.
"""

import json
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import task_server
from schema import init_db


def _conn_factory(db_path: str):
    def _open():
        conn = sqlite3.connect(db_path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    return _open


@pytest.fixture
def task_env(tmp_path, monkeypatch):
    db_path = str(tmp_path / "memory.db")
    init_db(db_path)
    conn_factory = _conn_factory(db_path)
    monkeypatch.setattr(task_server, "_get_conn", conn_factory)
    monkeypatch.setattr(task_server, "_get_write_conn", conn_factory)
    monkeypatch.setattr(task_server, "_vec_sync_task_safe", lambda task_id: None)
    return db_path


def _mk(**kwargs):
    res = json.loads(task_server.create_task_or_note.fn(**kwargs))
    assert "task_id" in res, res
    return res["task_id"]


def _titles(result_json):
    return [t["title"] for t in json.loads(result_json)["tasks"]]


def _seed():
    # Mix of priorities and due dates (some NULL) to exercise ordering.
    _mk(title="alpha", priority="low", due_date="2026-06-10")
    _mk(title="bravo", priority="critical", due_date="2026-06-20")
    _mk(title="charlie", priority="high")  # no due date (NULL)
    _mk(title="delta", priority="medium", due_date="2026-06-01")


# ── Default ordering unchanged ──────────────────────────────────────────────


def test_default_order_is_priority_first(task_env):
    """sort_by omitted -> historical default: critical first, then due_date."""
    _seed()
    titles = _titles(task_server.query_tasks.fn())
    # Priority order: critical(bravo), high(charlie), medium(delta), low(alpha)
    assert titles == ["bravo", "charlie", "delta", "alpha"]


def test_default_order_matches_explicit_empty_params(task_env):
    """Passing sort_by='' / sort_order='' must equal the no-arg default."""
    _seed()
    a = _titles(task_server.query_tasks.fn())
    b = _titles(task_server.query_tasks.fn(sort_by="", sort_order=""))
    assert a == b


# ── created_at asc / desc ───────────────────────────────────────────────────


def test_sort_created_at_asc_then_desc(task_env):
    _mk(title="first")
    _mk(title="second")
    _mk(title="third")
    asc = _titles(task_server.query_tasks.fn(sort_by="created_at", sort_order="asc"))
    desc = _titles(task_server.query_tasks.fn(sort_by="created_at", sort_order="desc"))
    assert asc == ["first", "second", "third"]
    assert desc == list(reversed(asc))


# ── due_date with NULLS LAST ────────────────────────────────────────────────


def test_sort_due_date_asc_nulls_last(task_env):
    _seed()
    titles = _titles(task_server.query_tasks.fn(sort_by="due_date"))
    # Dated soonest-first: delta(06-01), alpha(06-10), bravo(06-20); charlie NULL last.
    assert titles == ["delta", "alpha", "bravo", "charlie"]
    assert titles[-1] == "charlie"  # NULL due_date pushed to the bottom


def test_sort_due_date_desc_keeps_nulls_last(task_env):
    _seed()
    titles = _titles(task_server.query_tasks.fn(sort_by="due_date", sort_order="desc"))
    assert titles[-1] == "charlie"  # NULL stays last regardless of direction
    assert titles[:3] == ["bravo", "alpha", "delta"]


# ── priority default direction (highest first) ──────────────────────────────


def test_sort_priority_default_is_highest_first(task_env):
    _seed()
    titles = _titles(task_server.query_tasks.fn(sort_by="priority"))
    assert titles[0] == "bravo"  # critical
    assert titles[-1] == "alpha"  # low


def test_sort_priority_asc_is_lowest_first(task_env):
    _seed()
    titles = _titles(task_server.query_tasks.fn(sort_by="priority", sort_order="asc"))
    assert titles[0] == "alpha"  # low
    assert titles[-1] == "bravo"  # critical


# ── case-insensitive direction ──────────────────────────────────────────────


def test_sort_order_case_insensitive(task_env):
    _seed()
    upper = _titles(task_server.query_tasks.fn(sort_by="due_date", sort_order="DESC"))
    lower = _titles(task_server.query_tasks.fn(sort_by="due_date", sort_order="desc"))
    assert upper == lower


# ── validation errors ───────────────────────────────────────────────────────


def test_invalid_sort_by_raises_error(task_env):
    _seed()
    out = json.loads(task_server.query_tasks.fn(sort_by="id; DROP TABLE tasks"))
    assert "error" in out
    assert "invalid_sort_by" in out["error"]
    # No silent fallback, no execution.
    assert "allowed:" in out["error"]


def test_invalid_sort_order_raises_error(task_env):
    _seed()
    out = json.loads(
        task_server.query_tasks.fn(sort_by="created_at", sort_order="sideways")
    )
    assert "error" in out
    assert "invalid_sort_order" in out["error"]


# ── tie-breaker determinism ─────────────────────────────────────────────────


def test_tie_breaker_is_deterministic(task_env):
    # All same priority + same due_date -> ties resolved by created_at DESC, id.
    _mk(title="t1", priority="medium", due_date="2026-07-01")
    _mk(title="t2", priority="medium", due_date="2026-07-01")
    _mk(title="t3", priority="medium", due_date="2026-07-01")
    runs = [_titles(task_server.query_tasks.fn(sort_by="due_date")) for _ in range(3)]
    assert runs[0] == runs[1] == runs[2]  # stable across repeated calls


def test_default_path_tie_breaker_deterministic(task_env):
    # Same priority, no due dates -> default path must still be stable.
    _mk(title="d1", priority="high")
    _mk(title="d2", priority="high")
    _mk(title="d3", priority="high")
    runs = [_titles(task_server.query_tasks.fn()) for _ in range(3)]
    assert runs[0] == runs[1] == runs[2]
