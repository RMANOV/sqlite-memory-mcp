"""Rank direction and consumer-scale regressions using real SQLite FTS5.

These catch an inverted BM25 base, premature score rounding, unsupported rank
promotion and accidental replacement with raw strengths that break pack gates.
"""

import json
import math
import sqlite3

import pytest

import context_packer
import smart_retrieval
import vec_search
from schema import init_db


OLD = "2000-01-01T00:00:00+00:00"
FUTURE = "2100-01-01T00:00:00+00:00"


@pytest.fixture
def conn():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(
        """
        CREATE TABLE entities (
            id INTEGER PRIMARY KEY, name TEXT, entity_type TEXT, project TEXT,
            updated_at TEXT
        );
        CREATE TABLE observations (entity_id INTEGER, content TEXT);
        CREATE TABLE relations (from_id INTEGER, to_id INTEGER);
        CREATE TABLE canonical_facts (subject TEXT);
        CREATE TABLE sessions (session_id TEXT, active_files TEXT);
        CREATE VIRTUAL TABLE memory_fts USING fts5(
            name, entity_type, observations_text
        );
        """
    )
    yield db
    db.close()


def corpus(conn, fillers=0, timestamp=OLD):
    documents = [" ".join(["quasar"] * 10), "quasar " + " ".join(["other"] * 9)]
    documents += [" ".join(["unrelated"] * 10)] * fillers
    for eid, body in enumerate(documents, 1):
        conn.execute(
            "INSERT INTO entities VALUES (?, ?, 'note', NULL, ?)",
            (eid, f"Entity{eid}", timestamp),
        )
        conn.execute("INSERT INTO observations VALUES (?, ?)", (eid, body))
        conn.execute(
            "INSERT INTO memory_fts(rowid,name,entity_type,observations_text) "
            "VALUES (?, ?, 'note', ?)",
            (eid, f"Entity{eid}", body),
        )
    return conn.execute(
        "SELECT memory_fts.rowid AS eid, memory_fts.name, memory_fts.entity_type, "
        "e.project, memory_fts.rank FROM memory_fts "
        "JOIN entities e ON e.id=memory_fts.rowid "
        "WHERE memory_fts MATCH 'quasar' ORDER BY memory_fts.rank"
    ).fetchall()


def score(rank, **changes):
    args = dict(
        bm25_rank=rank, updated_at=None, project=None, current_project=None,
        obs_count=0, relation_hops=0, has_canonical_facts=False,
        in_active_session=False,
    )
    args.update(changes)
    return smart_retrieval.compute_composite_score(**args)


@pytest.mark.parametrize("fillers", [0, 10])
def test_real_fts_order_survives_reranking_and_limit(conn, fillers):
    rows = corpus(conn, fillers)
    assert [r["eid"] for r in rows] == [1, 2]
    assert rows[0]["rank"] < rows[1]["rank"] < 0
    if not fillers:
        assert -0.00001 < rows[0]["rank"]
    else:
        assert rows[0]["rank"] < -1

    result = smart_retrieval.rerank_entities(conn, rows[::-1], None, None, None)
    assert [r["eid"] for r in result] == [1, 2]
    assert result[0]["_score"] > result[1]["_score"]
    one = smart_retrieval.rerank_entities(conn, rows[::-1], None, None, None, limit=1)
    assert one[0]["eid"] == 1


@pytest.mark.parametrize("better,worse", [(-10, -1), (-1e-6, -1e-12), (-1e-12, 0)])
def test_better_rank_has_strictly_better_score_for_separable_values(better, worse):
    assert score(better) > score(worse)


def test_float_extremes_are_finite_nondecreasing_and_zero_remains_usable():
    values = [score(r) for r in (0.0, -5e-324, -1e-12, -1, -1e100, -1e308)]
    assert all(math.isfinite(value) and value > 0 for value in values)
    assert values == sorted(values)
    assert values[-1] <= 2 * values[0]


@pytest.mark.parametrize(
    "weaker,stronger",
    [
        (-4503599627370495.5, -4503599627370496.0),
        (-9007199254740992.0, -9007199254740994.0),
    ],
)
def test_adjacent_finite_ranks_do_not_reverse_at_float_saturation(weaker, stronger):
    assert stronger < weaker
    # Saturation may tie, but rounded arithmetic must not reverse two matches.
    assert score(stronger) >= score(weaker)


@pytest.mark.parametrize("vectors_only", [False, True])
def test_actual_rrf_producer_keeps_best_overlap_or_vector_hit(conn, vectors_only):
    rows = corpus(conn)
    vectors = [
        {"eid": 1, "name": "Entity1", "entity_type": "note", "project": None, "distance": 0.1},
        {"eid": 2, "name": "Entity2", "entity_type": "note", "project": None, "distance": 0.4},
    ]
    fused = vec_search.rrf_merge([] if vectors_only else rows, vectors)
    assert fused[0]["eid"] == 1 and fused[0]["rank"] < fused[1]["rank"]
    result = smart_retrieval.rerank_entities(conn, fused[::-1], None, None, None)
    assert [r["eid"] for r in result] == [1, 2]
    assert result[0]["_score"] > result[1]["_score"]


def test_context_consumer_keeps_tiny_rank_precision_at_recency_floor(conn, monkeypatch):
    corpus(conn)
    monkeypatch.setattr(vec_search, "VEC_AVAILABLE", False)
    result = context_packer._find_relevant_entities(conn, "quasar", [], None)
    assert result["by_id"][1] > result["by_id"][2]
    assert result["by_name"]["Entity1"] == result["by_id"][1]
    # Both round to the same displayed value: internal consumers must not.
    assert round(result["by_id"][1], 6) == round(result["by_id"][2], 6)


def test_fresh_tiny_fts_keeps_context_scale_and_explicit_link_floor(conn, monkeypatch):
    corpus(conn, timestamp=FUTURE)
    conn.execute("INSERT INTO entities VALUES (3, 'Linked', 'note', NULL, ?)", (OLD,))
    monkeypatch.setattr(vec_search, "VEC_AVAILABLE", False)
    result = context_packer._find_relevant_entities(conn, "quasar", [3], None)
    assert result["by_id"][1] >= 0.75
    assert result["by_id"][2] >= 0.75
    assert result["by_id"][3] == 0.75


@pytest.mark.parametrize("invalid", [None, "-1", "bad", 1.0, math.nan, math.inf, -math.inf])
def test_invalid_rank_is_finite_noncompetitive_in_helper_and_reranker(conn, invalid):
    assert score(invalid) == 0.0
    rows = [dict(r) for r in corpus(conn)]
    rows[0]["rank"] = invalid
    result = smart_retrieval.rerank_entities(conn, rows, None, None, None)
    assert [r["eid"] for r in result] == [2, 1]
    assert result[1]["_score"] == 0.0
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("row_kind", ["dict", "sqlite_row"])
def test_missing_rank_does_not_crash_or_outrank_valid_hit(conn, row_kind):
    rows = corpus(conn)
    if row_kind == "dict":
        missing = dict(rows[0])
        del missing["rank"]
    else:
        missing = conn.execute(
            "SELECT id AS eid,name,entity_type,project FROM entities WHERE id=1"
        ).fetchone()
    result = smart_retrieval.rerank_entities(conn, [missing, rows[1]], None, None, None)
    assert [r["eid"] for r in result] == [2, 1]
    assert result[1]["_score"] == 0.0


def test_equal_scores_keep_input_order_and_zero_rank_keeps_context_boosts(conn):
    rows = [dict(r, rank=0.0) for r in corpus(conn)]
    rows.reverse()
    result = smart_retrieval.rerank_entities(conn, rows, None, None, None)
    assert [r["eid"] for r in result] == [2, 1]
    assert result[0]["_score"] == result[1]["_score"] > 0
    conn.execute("UPDATE entities SET project='focus' WHERE id=1")
    rows[1]["project"] = "focus"
    result = smart_retrieval.rerank_entities(conn, rows, "focus", None, None)
    assert [r["eid"] for r in result] == [1, 2]


def test_combined_multipliers_and_graph_only_context_remain_effective():
    base = score(-1)
    boosted = score(
        -1, project="focus", current_project="focus", relation_hops=1,
        has_canonical_facts=True, in_active_session=True,
    )
    assert boosted / base == pytest.approx(7.56)
    assert score(0, relation_hops=1) > score(-1e-6)
    assert score(-1, updated_at=OLD) / score(-1, updated_at=FUTURE) == pytest.approx(0.1)


def test_real_task_context_retains_fact_reached_only_via_tiny_fts(tmp_path, monkeypatch):
    db_path = str(tmp_path / "pack.db")
    init_db(db_path)
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    try:
        db.execute(
            "INSERT INTO entities(id,name,entity_type,created_at,updated_at) "
            "VALUES (10001,'SyntheticSubject','note',?,?)", (FUTURE, FUTURE),
        )
        db.execute(
            "INSERT INTO observations(entity_id,content,created_at) VALUES (10001,'quasar',?)",
            (FUTURE,),
        )
        db.execute(
            "INSERT OR REPLACE INTO memory_fts(rowid,name,entity_type,observations_text) "
            "VALUES (10001,'SyntheticSubject','note','quasar')"
        )
        db.execute(
            "INSERT INTO tasks(id,title,status,priority,section,type,created_at,updated_at) "
            "VALUES ('rank-task','quasar','not_started','medium','inbox','task',?,?)",
            (FUTURE, FUTURE),
        )
        db.execute(
            "INSERT INTO canonical_facts(fact_id,subject,predicate,object_text,object_type,"
            "fact_scope,provenance_summary,confidence,validation_mode,created_at,updated_at) "
            "VALUES ('rank-fact','SyntheticSubject','uses','fixture','text','memory',"
            "'synthetic fixture',0.95,'manual',?,?)", (FUTURE, FUTURE),
        )
        db.execute(
            "INSERT INTO provenance_links(provenance_id,subject_kind,subject_ref,source_kind,"
            "source_ref,confidence,created_at) VALUES ('rank-prov','fact','rank-fact','chunk',"
            "'fixture-source',1.0,?)", (FUTURE,),
        )
        monkeypatch.setattr(vec_search, "VEC_AVAILABLE", False)
        result = context_packer.build_context_pack(db, target_ref="rank-task", persist=False)
        assert result["task_scoped"] is True
        assert "SyntheticSubject uses fixture" in result["body"]
    finally:
        db.close()
