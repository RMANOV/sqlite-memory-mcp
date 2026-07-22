"""TruthScore search batching and bounded top-N contract regressions."""

import json
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import collab_server
from schema import init_db


@pytest.fixture
def collab_env(tmp_path, monkeypatch):
    db_path = str(tmp_path / "memory.db")
    init_db(db_path)
    traces: list[str] = []

    def open_conn():
        conn = sqlite3.connect(db_path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.set_trace_callback(traces.append)
        return conn

    monkeypatch.setattr(collab_server, "_get_conn", open_conn)
    return db_path, traces


def _seed_truth_entities(db_path: str) -> None:
    with sqlite3.connect(db_path, isolation_level=None) as conn:
        conn.row_factory = sqlite3.Row
        now = "2026-07-22T09:00:00+00:00"
        entities = (
            ("Alpha exact", "low signal"),
            ("Alpha secondary", "well supported"),
        )
        for name, observation in entities:
            cur = conn.execute(
                "INSERT INTO entities "
                "(name,entity_type,project,visibility,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?)",
                (name, "research", "shared:test", "public", now, now),
            )
            conn.execute(
                "INSERT INTO observations (entity_id,content,created_at) VALUES (?,?,?)",
                (cur.lastrowid, observation, now),
            )

        content_hash = collab_server._content_hash(
            "Alpha secondary", ["well supported"]
        )
        for index in range(4):
            conn.execute(
                "INSERT INTO knowledge_ratings "
                "(entity_name,rater_id,content_hash,specificity,falsifiability,"
                "internal_consistency,novelty,verification_outcome,usefulness,rated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    "Alpha secondary",
                    f"rater-{index}",
                    content_hash,
                    1.0,
                    1.0,
                    1.0,
                    1.0,
                    "confirmed",
                    1.0,
                    now,
                ),
            )


def test_truth_sort_and_filter_happen_before_requested_limit(collab_env):
    db_path, _traces = collab_env
    _seed_truth_entities(db_path)

    ranked = json.loads(
        collab_server.search_public_knowledge.fn(
            "alpha", sort_by="truth_score", limit=1
        )
    )
    filtered = json.loads(
        collab_server.search_public_knowledge.fn("alpha", min_truth_score=0.5, limit=1)
    )

    assert ranked["entities"][0]["name"] == "Alpha secondary"
    assert filtered["entities"][0]["name"] == "Alpha secondary"
    assert ranked["ranking_scope"] == "bm25_candidate_pool"
    assert ranked["candidate_pool_size"] == 2
    assert ranked["candidate_pool_limit"] == 100


def test_truth_search_batches_observations_and_ratings(collab_env):
    db_path, traces = collab_env
    _seed_truth_entities(db_path)
    traces.clear()

    result = json.loads(
        collab_server.search_public_knowledge.fn(
            "alpha", sort_by="truth_score", limit=2
        )
    )

    selects = [
        statement.casefold()
        for statement in traces
        if statement.lstrip().lower().startswith("select")
    ]
    observation_reads = [
        statement for statement in selects if " from observations " in statement
    ]
    rating_reads = [
        statement for statement in selects if " from knowledge_ratings " in statement
    ]
    assert result["count"] == 2
    assert len(observation_reads) == 1
    assert len(rating_reads) == 1
