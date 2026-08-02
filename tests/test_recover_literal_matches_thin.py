"""B2: a recovery pass that ran must not suppress the one that follows it.

``_recover_literal_matches`` has two recovery passes behind one fuzzy answer:
the on-disk FTS5/BM25 index, and a substring scorer over the caller's own pool.
FTS5 matches whole tokens; the substring scorer matches infixes.  They fail on
different queries, which is exactly why both exist.

The defect these tests pin is in how the second pass was gated.  It ran when
``fts_rows is None`` — when FTS had never been *consulted* — rather than when
the merged answer was still short.  Two reachable shapes broke on that:

1.  ``_fts5_search`` returns ``[]`` (not ``None``) when the index matched rows
    that the caller's view then filtered out.  ``[] is None`` is False, so a
    pass that contributed nothing counted as one that had already recovered.
2.  FTS contributing *one* row satisfies the same gate, so a thin answer stayed
    thin even though the substring pass held the rest.

Both make the search strictly worse for having an index: the same query with
``conn=None`` recovers rows that the same query with a live ``conn`` does not.
That inversion is the assertion — a differential, not a hard-coded baseline, so
it cannot rot as the ranking changes.

The tests drive the real method against a real on-disk FTS5 table.  Stubbing
``_fts5_search`` would prove only that the stub was called; the ``[]``-vs-
``None`` distinction lives inside it.
"""

from __future__ import annotations

import pathlib
import sqlite3
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import task_search  # noqa: E402

# "port" is a whole token in exactly one title and an INFIX in the other two.
# That split is the whole point: FTS5 can see only the first, ``score_task``
# sees all three.
POOL = [
    {"id": "t1", "title": "export pipeline", "description": "", "status": "active",
     "created_at": "2026-01-01T00:00:00"},
    {"id": "t2", "title": "transport layer", "description": "", "status": "active",
     "created_at": "2026-01-02T00:00:00"},
    {"id": "t3", "title": "port forwarding", "description": "", "status": "active",
     "created_at": "2026-01-03T00:00:00"},
]
# Indexed on disk but outside the caller's view — the row that makes
# ``_fts5_search`` return ``[]`` instead of ``None``.
OUT_OF_VIEW = {"id": "t9", "title": "port scanner audit", "description": "",
               "status": "done", "created_at": "2026-01-04T00:00:00"}


def _make_conn(rows: list[dict]) -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute(
        "CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, description TEXT, "
        "notes TEXT, project TEXT, status TEXT, section TEXT, priority INTEGER, "
        "parent_id TEXT, type TEXT, created_at TEXT, updated_at TEXT, due_date TEXT)"
    )
    c.execute(
        "CREATE VIRTUAL TABLE tasks_fts USING fts5(id UNINDEXED, title, description, notes)"
    )
    for t in rows:
        c.execute(
            "INSERT INTO tasks (id, title, description, notes, status, created_at) "
            "VALUES (?, ?, ?, '', ?, ?)",
            (t["id"], t["title"], t["description"], t["status"], t["created_at"]),
        )
        c.execute(
            "INSERT INTO tasks_fts (id, title, description, notes) VALUES (?, ?, ?, '')",
            (t["id"], t["title"], t["description"]),
        )
    c.commit()
    return c


@pytest.fixture()
def conn():
    c = _make_conn(POOL)
    yield c
    c.close()


@pytest.fixture()
def conn_out_of_view():
    """Index holds ONLY a row the caller's view excludes."""
    c = _make_conn([POOL[0], POOL[1], OUT_OF_VIEW])
    yield c
    c.close()


def _engine() -> task_search.TaskSearchEngine:
    """An engine object without the native SmartKey engine or the CVM on disk."""
    return object.__new__(task_search.TaskSearchEngine)


# --- premises ---------------------------------------------------------------
# These guard the two tests that matter. If FTS5's tokenizer ever started
# matching infixes, or the empty-view filter started returning None, the
# behavioural tests below would pass for the wrong reason and stop protecting
# anything.

def test_premise_fts5_sees_only_the_whole_token(conn):
    rows = task_search.TaskSearchEngine._fts5_search(conn, "port", POOL, 10, broaden=False)
    assert [r["id"] for r in rows or []] == ["t3"], f"premise broken: {rows}"


def test_premise_out_of_view_match_returns_empty_list_not_none(conn_out_of_view):
    view = [POOL[0], POOL[1]]
    rows = task_search.TaskSearchEngine._fts5_search(
        conn_out_of_view, "port", view, 10, broaden=False
    )
    assert rows == [], (
        "premise broken: this is the [] the gate could not tell apart from None; "
        f"got {rows!r}"
    )


# --- the defect, as behaviour ----------------------------------------------

def test_a_partial_fts_hit_does_not_suppress_the_substring_pass(conn):
    eng = _engine()
    indexed = {"t3": POOL[2]}  # t1/t2 outside the resident index -> FTS consulted

    with_index = eng._recover_literal_matches([], "port", POOL, 10, conn, indexed)
    without_index = eng._recover_literal_matches([], "port", POOL, 10, None, indexed)

    assert {r["id"] for r in with_index} >= {r["id"] for r in without_index}, (
        "the on-disk index made the answer worse — one FTS hit satisfied the "
        f"gate and the substring pass never ran: {[r['id'] for r in with_index]} "
        f"vs {[r['id'] for r in without_index]}"
    )
    assert {r["id"] for r in with_index} == {"t1", "t2", "t3"}


def test_an_empty_fts_result_does_not_suppress_the_substring_pass(conn_out_of_view):
    eng = _engine()
    view = [POOL[0], POOL[1]]
    indexed = {"t1": POOL[0]}
    # A NON-empty fuzzy answer, which is the only state this method is ever
    # reached in: the FTS pass exists to *complete* a ranked answer. With an
    # empty one the first clause of the gate fires on its own and hides the
    # defect — the empty-list confusion is only observable once there is
    # something to complete.
    fuzzy = [dict(POOL[0], rank=1.0)]

    with_index = eng._recover_literal_matches(
        list(fuzzy), "port", view, 10, conn_out_of_view, indexed
    )
    without_index = eng._recover_literal_matches(list(fuzzy), "port", view, 10, None, indexed)

    assert {r["id"] for r in with_index} >= {r["id"] for r in without_index}, (
        "FTS matched only rows outside the view and returned [], which the gate "
        f"read as 'already recovered': {[r['id'] for r in with_index]} vs "
        f"{[r['id'] for r in without_index]}"
    )
    assert {r["id"] for r in with_index} == {"t1", "t2"}


# --- the invariants the fix must not break ---------------------------------

def test_overlapping_passes_do_not_duplicate_rows(conn):
    """Both passes now run on a thin answer, so their overlap must collapse."""
    eng = _engine()
    out = eng._recover_literal_matches([], "port", POOL, 10, conn, {"t1": POOL[0]})

    ids = [r["id"] for r in out]
    assert len(ids) == len(set(ids)), f"duplicate rows across the two passes: {ids}"


def test_recovered_rows_keep_the_callers_row_shape(conn):
    """Pins the projection: FTS rows carry no ``created_at``, pool rows do."""
    eng = _engine()
    out = eng._recover_literal_matches([], "port", POOL, 10, conn, {"t1": POOL[0]})

    hit = next(r for r in out if r["id"] == "t3")
    assert hit.get("created_at") == "2026-01-03T00:00:00", (
        f"recovered row lost the caller's fields: {sorted(hit)}"
    )


def test_a_full_answer_is_left_alone(conn):
    """The top-up is for THIN answers; a full quota must not be re-ranked."""
    eng = _engine()
    fuzzy = [dict(POOL[1], rank=1.0), dict(POOL[0], rank=0.9)]

    out = eng._recover_literal_matches(fuzzy, "port", POOL, 2, conn, {"t1": POOL[0]})

    assert len(out) == 2
    assert [r["id"] for r in out] == ["t2", "t1"], (
        f"a full fuzzy answer was disturbed: {[r['id'] for r in out]}"
    )
