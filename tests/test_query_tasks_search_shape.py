"""Regression guard for `query_tasks(search=...)` — the MCP path that has no
resident search index.

`task_server.query_tasks` never calls `TaskSearchEngine.rebuild_index`, so the
engine's `_inverted`/`_task_map` stay empty.  A `task_search` completion guard
that does not test `indexed` for emptiness is vacuously true on that path and
lets the BM25/FTS5 route displace the substring pass for every search, which:

  * halves recall (FTS5 matches whole tokens, `score_task` matches infixes),
  * drops `created_at` from the returned rows (the FTS SELECT has no such
    column), degrading the rendered "Created" column to "—",
  * defeats `summary_only=True`, because FTS rows carry `description`.

The only pre-existing test of this path monkeypatches a fake engine, so none of
the above was covered.  These tests drive the real engine.
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


@pytest.fixture()
def seeded(task_env):
    for title in (
        "exact token export",
        "inflected exported",
        "another export run",
        "exporting pipeline",
        "totally unrelated row",
    ):
        task_server.create_task_or_note.fn(
            title=title,
            type="task",
            description=f"body for {title} with filler prose",
        )
    return None


def _rows(**kwargs):
    return json.loads(task_server.query_tasks.fn(search="export", **kwargs))


def test_search_keeps_infix_and_inflected_matches(seeded):
    payload = _rows()
    titles = {row["title"] for row in payload["tasks"]}

    assert payload["total"] == 4
    assert titles == {
        "exact token export",
        "inflected exported",
        "another export run",
        "exporting pipeline",
    }


def test_search_rows_keep_created_at(seeded):
    payload = _rows()

    assert payload["tasks"], "expected search hits"
    assert all(row.get("created_at") for row in payload["tasks"]), (
        "created_at must survive the search path — the rendered Created column "
        "degrades to '—' without it"
    )


def test_search_honours_summary_only(seeded):
    payload = _rows(summary_only=True)

    assert payload["tasks"], "expected search hits"
    for row in payload["tasks"]:
        assert "description" not in row, (
            "summary_only exists to keep the MCP payload small; FTS rows carry "
            "description and re-inflate it"
        )
