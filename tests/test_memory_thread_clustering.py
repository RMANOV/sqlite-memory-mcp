"""Hard gates and non-authoritative guarantees for offline Leiden threads."""

from __future__ import annotations

import sqlite3

import pytest

import memory_thread_clustering as clustering
from db_utils import TaskDAO
from schema import init_db

NOW = "2026-07-23T09:00:00+00:00"


@pytest.fixture
def conn(tmp_path):
    path = str(tmp_path / "threads.db")
    init_db(path)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    yield db
    db.close()


def _seed_linked_pair(conn):
    TaskDAO.create(conn, "task-a", "Alpha work", NOW, project="alpha")
    entity_id = int(
        conn.execute(
            "INSERT INTO entities "
            "(name, entity_type, project, created_at, updated_at) "
            "VALUES ('Alpha', 'organization', 'alpha', ?, ?)",
            (NOW, NOW),
        ).lastrowid
    )
    TaskDAO.link_entity(
        conn,
        "task-a",
        entity_id,
        link_type="manual",
        created_at=NOW,
    )
    conn.commit()
    return entity_id


def test_projection_runs_from_zero_labels_as_unvalidated_cold_start(conn, monkeypatch):
    _seed_linked_pair(conn)

    def fake_leiden(graph, *, resolutions, seed):
        nodes = graph.nodes
        return {
            "nodes": nodes,
            "memberships": {
                f"{resolution:.6g}": [0 for _node in nodes]
                for resolution in resolutions
            },
            "seed_stability": {f"{resolution:.6g}": 1.0 for resolution in resolutions},
            "cross_resolution_stability": {
                f"{left:.6g}->{right:.6g}": 1.0
                for left, right in zip(resolutions, resolutions[1:], strict=False)
            },
            "community_counts": {f"{resolution:.6g}": 1 for resolution in resolutions},
            "seeds": (seed, seed + 101, seed + 307),
        }

    monkeypatch.setattr(clustering, "_run_leiden", fake_leiden)
    report = clustering.run_memory_thread_projection(
        conn,
        include_vector=False,
        persist=True,
        restart_transaction_before_persist=True,
    )

    assert report["ok"] is True
    assert report["validation_state"] == "unvalidated_cold_start"
    assert report["label_progress"]["qualified_total"] == 0
    assert report["label_progress"]["gate"]["ready"] is True
    assert report["mutated"] is True
    assert conn.execute("SELECT COUNT(*) FROM link_community_runs").fetchone()[0] == 1


def test_persisted_projection_is_derived_and_never_creates_links(conn, monkeypatch):
    _seed_linked_pair(conn)
    monkeypatch.setattr(
        clustering,
        "decision_progress",
        lambda _conn: {
            "qualified_total": 120,
            "qualified_accepted": 60,
            "qualified_rejected": 60,
            "gate": {"ready": True},
        },
    )

    def fake_leiden(graph, *, resolutions, seed):
        nodes = graph.nodes
        return {
            "nodes": nodes,
            "memberships": {
                f"{resolution:.6g}": [0 for _node in nodes]
                for resolution in resolutions
            },
            "seed_stability": {f"{resolution:.6g}": 1.0 for resolution in resolutions},
            "cross_resolution_stability": {
                f"{left:.6g}->{right:.6g}": 1.0
                for left, right in zip(resolutions, resolutions[1:], strict=False)
            },
            "community_counts": {f"{resolution:.6g}": 1 for resolution in resolutions},
            "seeds": (seed, seed + 101, seed + 307),
        }

    monkeypatch.setattr(clustering, "_run_leiden", fake_leiden)
    before_links = conn.execute("SELECT COUNT(*) FROM task_entity_links").fetchone()[0]

    report = clustering.run_memory_thread_projection(
        conn,
        include_vector=False,
        persist=True,
    )

    assert report["ok"] is True
    assert report["persisted"] is True
    assert conn.execute("SELECT COUNT(*) FROM link_community_runs").fetchone()[0] == 1
    assert (
        conn.execute("SELECT COUNT(*) FROM link_community_memberships").fetchone()[0]
        >= 2
    )
    assert (
        conn.execute("SELECT COUNT(*) FROM task_entity_links").fetchone()[0]
        == before_links
    )


def test_sparse_graph_uses_bounded_structural_edges(conn):
    _seed_linked_pair(conn)

    graph, report = clustering.build_sparse_memory_graph(
        conn,
        lexical_top_k=0,
        include_vector=False,
    )

    assert report["edge_count"] >= 1
    assert report["vector"]["enabled"] is False
    assert any(
        "explicit_task_entity_link" in reasons for reasons in graph.reasons.values()
    )


def test_stability_metric_is_adjusted_for_chance():
    assert clustering._adjusted_rand_agreement([0, 0, 1, 1], [3, 3, 7, 7]) == 1.0
    assert clustering._adjusted_rand_agreement([0, 0, 1, 1], [0, 1, 0, 1]) < 0
