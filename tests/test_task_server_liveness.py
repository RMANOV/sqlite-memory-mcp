"""Liveness regression tests for task_server query/lookup surfaces.

Two defects are locked down here:

1. ``query_tasks()`` with default arguments used to build ``WHERE 1=1`` — a
   status exclusion existed only inside the ``overdue_only`` branch. Because
   completed tasks keep their ``due_date``, the default order (priority first,
   then ``due_date ASC NULLS LAST``) buried live work behind finished rows.
2. ``find_by_title`` sorted by ``(score, kind, updated_at, created_at)`` with no
   liveness term, so a finished row could outrank every live match, and the
   unbounded full-scan fallback scored the whole finished-work text body.

Everything runs against a temp DB built by ``schema.init_db`` — never the live
database — following the fixture style of ``tests/test_query_tasks_sort.py``.
"""

import json
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import task_server
from db_utils import TASK_ACTIVE_EXCLUSIONS
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


def _mk(status: str = "", **kwargs) -> str:
    res = json.loads(task_server.create_task_or_note.fn(**kwargs))
    assert "task_id" in res, res
    if status:
        upd = json.loads(task_server.update_task.fn(res["task_id"], status=status))
        assert "error" not in upd, upd
    return res["task_id"]


def _titles(result_json) -> list[str]:
    return [t["title"] for t in json.loads(result_json)["tasks"]]


def _statuses(result_json) -> set[str]:
    return {t["status"] for t in json.loads(result_json)["tasks"]}


# ── Fix 1: query_tasks default liveness ─────────────────────────────────────


def test_query_tasks_default_excludes_finished_rows(task_env):
    """A bare query_tasks() must return live work only, count included."""
    _mk(title="live-not-started")
    _mk(title="live-in-progress", status="in_progress")
    for stale in TASK_ACTIVE_EXCLUSIONS:
        _mk(title=f"stale-{stale}", status=stale)

    out = json.loads(task_server.query_tasks.fn())

    assert sorted(t["title"] for t in out["tasks"]) == [
        "live-in-progress",
        "live-not-started",
    ]
    assert out["total"] == 2  # the count must agree with the filtered rows
    assert _statuses(json.dumps(out)) == {"not_started", "in_progress"}


def test_query_tasks_default_no_longer_buries_live_work_behind_done_rows(task_env):
    """The measured defect: done rows keep due_date and win the default order."""
    # Finished rows that would otherwise sort first: critical priority and the
    # earliest due dates. Completion must NOT clear due_date, so they stay
    # ahead of live work on the historical ordering.
    _mk(title="done-critical-soon", priority="critical", due_date="2020-01-01",
        status="done")
    _mk(title="archived-critical", priority="critical", due_date="2020-01-02",
        status="archived")
    _mk(title="cancelled-high", priority="high", due_date="2020-01-03",
        status="cancelled")
    _mk(title="live-medium", priority="medium", due_date="2030-01-01")

    titles = _titles(task_server.query_tasks.fn())

    assert titles == ["live-medium"]  # first live row is now rank 1, not rank 134


def test_query_tasks_explicit_status_is_left_untouched(task_env):
    """Naming a status means the caller chose the lifecycle — no extra filter."""
    _mk(title="live-row")
    _mk(title="done-row", status="done")
    _mk(title="archived-row", status="archived")

    assert _titles(task_server.query_tasks.fn(status="done")) == ["done-row"]
    assert _titles(task_server.query_tasks.fn(status="archived")) == ["archived-row"]
    assert _titles(task_server.query_tasks.fn(status="not_started")) == ["live-row"]


def test_query_tasks_include_completed_opts_back_in(task_env):
    """include_completed=True restores the historical unfiltered behaviour."""
    _mk(title="live-row")
    _mk(title="done-row", status="done")
    _mk(title="cancelled-row", status="cancelled")

    out = json.loads(task_server.query_tasks.fn(include_completed=True))

    assert sorted(t["title"] for t in out["tasks"]) == [
        "cancelled-row",
        "done-row",
        "live-row",
    ]
    assert out["total"] == 3


def test_query_tasks_completion_preserves_due_date(task_env):
    """History must survive completion — the fix is a filter, not a data wipe."""
    task_id = _mk(title="dated-row", due_date="2026-06-01", status="done")

    with task_server._get_conn() as conn:
        row = conn.execute(
            "SELECT status, due_date FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()

    assert row["status"] == "done"
    assert row["due_date"] == "2026-06-01"


def test_query_tasks_overdue_only_keeps_unconditional_exclusion(task_env):
    """overdue_only never surfaces finished work, even with include_completed."""
    _mk(title="overdue-live", due_date="2020-01-01")
    _mk(title="overdue-done", due_date="2020-01-01", status="done")

    assert _titles(task_server.query_tasks.fn(overdue_only=True)) == ["overdue-live"]
    assert _titles(
        task_server.query_tasks.fn(overdue_only=True, include_completed=True)
    ) == ["overdue-live"]


def test_query_tasks_other_filters_still_get_the_liveness_default(task_env):
    """section/project/priority filters alone must not re-open the stale door."""
    _mk(title="proj-live", project="alpha", section="next", priority="high")
    _mk(title="proj-done", project="alpha", section="next", priority="high",
        status="done")

    assert _titles(task_server.query_tasks.fn(project="alpha")) == ["proj-live"]
    assert _titles(task_server.query_tasks.fn(section="next")) == ["proj-live"]
    assert _titles(task_server.query_tasks.fn(priority="high")) == ["proj-live"]


def test_query_tasks_search_path_applies_the_same_liveness_filter(task_env):
    """The FTS pre-filter shares the WHERE clause, so search is live-only too."""

    class PassthroughSearchEngine:
        def search(self, query, tasks, limit=50, conn=None, use_vector=True):
            return tasks

    task_server._search_engine = PassthroughSearchEngine()
    try:
        _mk(title="beacon live row", description="distinctive beacon phrase")
        _mk(title="beacon done row", description="distinctive beacon phrase",
            status="done")

        default = json.loads(task_server.query_tasks.fn(search="beacon"))
        opted_in = json.loads(
            task_server.query_tasks.fn(search="beacon", include_completed=True)
        )
    finally:
        task_server._search_engine = None

    assert [t["title"] for t in default["tasks"]] == ["beacon live row"]
    assert sorted(t["title"] for t in opted_in["tasks"]) == [
        "beacon done row",
        "beacon live row",
    ]


def test_query_tasks_sort_by_still_validates_with_liveness_default(task_env):
    """The new condition must not bypass the SQL-injection sort guard."""
    _mk(title="anything")

    out = json.loads(task_server.query_tasks.fn(sort_by="id; DROP TABLE tasks"))

    assert "invalid_sort_by" in out["error"]
    with task_server._get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1


# ── Fix 2: find_by_title liveness-first ordering ────────────────────────────


def test_lookup_liveness_demotes_only_finished_task_lifecycle_rows(task_env):
    """Entities carry no lifecycle status and must never be scored as stale."""
    for stale in TASK_ACTIVE_EXCLUSIONS:
        assert task_server._lookup_liveness({"status": stale}) == 0
    assert task_server._lookup_liveness({"status": "not_started"}) == 1
    assert task_server._lookup_liveness({"status": "in_progress"}) == 1
    assert task_server._lookup_liveness({"kind": "entity", "status": None}) == 1
    assert task_server._lookup_liveness({}) == 1


def test_find_by_title_ranks_live_above_stale_on_an_exact_score_tie(task_env):
    _mk(title="alpha beacon done edition", status="done")
    _mk(title="alpha beacon live edition")

    result = json.loads(task_server.find_by_title.fn("alpha beacon"))
    titles = [item["title"] for item in result["matches"]]

    assert titles[0] == "alpha beacon live edition"
    assert "alpha beacon done edition" in titles  # demoted, not deleted
    # Same score, so only the liveness term can be doing the work here.
    scores = {item["title"]: item["score"] for item in result["matches"]}
    assert scores["alpha beacon live edition"] == scores["alpha beacon done edition"]


def test_find_by_title_liveness_outranks_a_higher_scoring_stale_row(task_env):
    """Liveness is the FIRST key: a 320-point stale title loses to a 185 live body."""
    _mk(title="quarterly ledger reconciliation", status="done")
    _mk(title="unrelated planning row", description="quarterly ledger rollup notes")

    result = json.loads(task_server.find_by_title.fn("quarterly ledger"))
    ranked = [(item["title"], item["score"]) for item in result["matches"]]

    assert ranked[0][0] == "unrelated planning row"
    assert ranked[0][1] < ranked[1][1]  # the demoted row scored strictly higher
    assert ranked[1][0] == "quarterly ledger reconciliation"


def test_find_by_title_keeps_stale_rows_reachable_through_the_index(task_env):
    """Demotion, not exclusion: an indexed phrase still resolves to a done row."""
    _mk(title="Byzantine gossip protocol writeup", status="done")

    result = json.loads(task_server.find_by_title.fn("Byzantine gossip"))

    assert result["lookup_strategy"] == "fts_prefilter"
    assert result["matches"][0]["title"] == "Byzantine gossip protocol writeup"
    assert result["matches"][0]["status"] == "done"


def test_find_by_title_full_scan_fallback_keeps_finished_rows_reachable(task_env):
    """Demotion, not exclusion — on the fallback path too.

    An earlier revision filtered done/archived/cancelled out of the unbounded
    scan to avoid reading them. That bought nothing for ranking, because the
    liveness sort key already demotes them, and it turned the last-resort
    recall path into a dead end: a finished row whose only match sat below the
    visibility floor disappeared instead of ranking last. In a system whose job
    is remembering past work, that is the one place the liveness policy costs
    more than it returns.
    """
    _mk(title="ZphqStaleTarget", status="done")

    result = json.loads(task_server.find_by_title.fn("phqstale"))

    assert result["lookup_strategy"] == "full_scan_fallback"
    assert result["count"] == 1
    assert result["matches"][0]["title"] == "ZphqStaleTarget"
    assert result["matches"][0]["status"] == "done"


def test_find_by_title_full_scan_fallback_still_finds_live_rows(task_env):
    """Control for the test above — the fallback itself is not broken."""
    _mk(title="ZphqLiveTarget")

    result = json.loads(task_server.find_by_title.fn("phqlive"))

    assert result["lookup_strategy"] == "full_scan_fallback"
    assert result["matches"][0]["title"] == "ZphqLiveTarget"


def test_find_by_title_ordering_is_deterministic_across_calls(task_env):
    _mk(title="delta beacon one")
    _mk(title="delta beacon two", status="done")
    _mk(title="delta beacon three")

    runs = [
        [item["title"] for item in json.loads(task_server.find_by_title.fn("delta beacon"))["matches"]]
        for _ in range(3)
    ]

    assert runs[0] == runs[1] == runs[2]
    assert runs[0][-1] == "delta beacon two"  # the only stale row sinks to the bottom
