"""The contract says finished rows stay reachable on every path. Prove it.

The FastMCP `instructions` string and the `find_by_title` docstring both promise
that done/archived/cancelled rows are never excluded, only ranked below live
work. That promise was false while the full-scan fallback carried a
`WHERE status NOT IN (...)` clause: a finished row whose only match scored below
VISIBLE_SCORE_FLOOR was surfaced by the FTS prefilter, then discarded by the
low-visibility rescan, which re-read from a population that no longer contained
it. The row vanished entirely instead of ranking last.
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


@pytest.fixture()
def task_env(tmp_path, monkeypatch):
    db_path = str(tmp_path / "memory.db")
    init_db(db_path)
    conn_factory = _conn_factory(db_path)
    monkeypatch.setattr(task_server, "_get_conn", conn_factory)
    monkeypatch.setattr(task_server, "_get_write_conn", conn_factory)
    monkeypatch.setattr(task_server, "_vec_sync_task_safe", lambda task_id: None)
    return db_path


def test_low_visibility_finished_row_stays_reachable(task_env):
    """A finished row matching only below the visibility floor must still return."""
    created = json.loads(
        task_server.create_task_or_note.fn(
            title="Ежемесечен отчет",
            type="task",
            notes="beta something in between gamma",
        )
    )
    task_server.update_task.fn(created["task_id"], status="done")

    result = json.loads(task_server.find_by_title.fn("beta gamma"))

    assert result["count"] >= 1, (
        "the contract promises finished rows stay reachable on every path, "
        "including the full-scan fallback"
    )
    assert "Ежемесечен отчет" in [m["title"] for m in result["matches"]]


def test_finished_row_ranks_below_live_row(task_env):
    """Reachable, but never ahead of live work — the other half of the contract."""
    done = json.loads(
        task_server.create_task_or_note.fn(title="Прогресен отчет", type="task")
    )
    task_server.update_task.fn(done["task_id"], status="done")
    task_server.create_task_or_note.fn(title="Прогресен отчет", type="task")

    result = json.loads(task_server.find_by_title.fn("Прогресен отчет"))
    statuses = [m.get("status") for m in result["matches"]]

    assert len(statuses) >= 2, "both rows should be reachable"
    assert statuses[0] != "done", "a live row must outrank the finished one"
