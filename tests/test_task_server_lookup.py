import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import task_server
from retrieval_contract import RETRIEVAL_CONTRACT_VERSION
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
    monkeypatch.setattr(task_server, "_get_conn", _conn_factory(db_path))
    monkeypatch.setattr(task_server, "_vec_sync_task_safe", lambda conn, task_id: None)
    return db_path


def _seed_lookup_corpus():
    task_server.create_task_or_note.fn(
        title="Bug Hunt Финален Доклад",
        type="note",
        description="long body",
        project="reports_generator",
    )
    created = json.loads(
        task_server.create_task_or_note.fn(
            title="Работна среща за мапинг студио",
            type="task",
            project="mapping-studio",
        )
    )
    task_server.update_task.fn(created["task_id"], status="done")
    task_server.create_task_or_note.fn(
        title="reports_generator phase-2 followup",
        type="task",
    )
    general = json.loads(
        task_server.create_task_or_note.fn(
            title="Напълно общо заглавие",
            type="task",
            description="Тук вътре стои фразата Byzantine gossip",
            notes="вътрешна бележка за fallback path",
            project="deep-research",
        )
    )
    task_server.update_task.fn(general["task_id"], status="done")
    task_server.create_task_or_note.fn(
        title="Research Note Rollout",
        type="task",
        description="strong remembered phrase path",
    )

    with task_server._get_conn() as conn:
        conn.execute(
            "INSERT INTO entities (name, entity_type, project, created_at, updated_at) "
            "VALUES (?, ?, ?, datetime('now'), datetime('now'))",
            ("Мапинг Студио Архитектура", "project-doc", "mapping-studio"),
        )
        cur = conn.execute(
            "INSERT INTO entities (name, entity_type, project, created_at, updated_at) "
            "VALUES (?, ?, ?, datetime('now'), datetime('now'))",
            ("Общо име", "research-note", "reports_generator"),
        )
        entity_id = cur.lastrowid
        conn.execute(
            "INSERT INTO observations (entity_id, content, created_at) VALUES (?, ?, datetime('now'))",
            (entity_id, "Специфична фраза: strategic shortfall window"),
        )
        conn.execute(
            "INSERT INTO entities (name, entity_type, project, created_at, updated_at) "
            "VALUES (?, ?, ?, datetime('now'), datetime('now'))",
            ("Шумен елемент", "research-note", "general"),
        )


def test_find_by_title_matches_done_notes_and_entities_by_partial_title(task_env):
    _seed_lookup_corpus()

    result = json.loads(task_server.find_by_title.fn("мапинг студ"))

    titles = [item["title"] for item in result["matches"]]
    kinds = {item["kind"] for item in result["matches"]}

    assert "Работна среща за мапинг студио" in titles
    assert "Мапинг Студио Архитектура" in titles
    assert {"task", "entity"} <= kinds


def test_find_by_title_normalizes_hyphens_and_underscores(task_env):
    _seed_lookup_corpus()

    result = json.loads(task_server.find_by_title.fn("reports generator phase 2"))

    assert result["count"] >= 1
    assert result["matches"][0]["title"] == "reports_generator phase-2 followup"


def test_find_by_title_matches_description_notes_and_project(task_env):
    _seed_lookup_corpus()

    by_desc = json.loads(task_server.find_by_title.fn("Byzantine gossip"))
    by_notes = json.loads(task_server.find_by_title.fn("fallback path"))
    by_project = json.loads(task_server.find_by_title.fn("deep research"))

    expected_title = "Напълно общо заглавие"
    assert any(item["title"] == expected_title for item in by_desc["matches"])
    assert any(
        "description" in (item.get("matched_in") or []) for item in by_desc["matches"]
    )
    assert any(item["title"] == expected_title for item in by_notes["matches"])
    assert any(
        "notes" in (item.get("matched_in") or []) for item in by_notes["matches"]
    )
    assert any(item["title"] == expected_title for item in by_project["matches"])
    assert any(
        "project" in (item.get("matched_in") or []) for item in by_project["matches"]
    )


def test_upsert_note_by_title_project_updates_existing_note_in_place(task_env):
    created = json.loads(
        task_server.upsert_note_by_title_project.fn(
            title="Daily research note",
            project="sqlite-memory-mcp",
            description="first body",
        )
    )
    updated = json.loads(
        task_server.upsert_note_by_title_project.fn(
            title="  daily   research NOTE ",
            project="sqlite-memory-mcp",
            description="updated body",
            notes="source=retry",
        )
    )

    assert created["action"] == "created"
    assert updated["action"] == "updated"
    assert updated["task_id"] == created["task_id"]

    with task_server._get_conn() as conn:
        rows = conn.execute(
            "SELECT id, description, notes FROM tasks "
            "WHERE type = 'note' AND project = ?",
            ("sqlite-memory-mcp",),
        ).fetchall()

    assert len(rows) == 1
    assert rows[0]["description"] == "updated body"
    assert rows[0]["notes"] == "source=retry"


def test_upsert_note_by_title_project_can_return_existing_without_mutation(task_env):
    created = json.loads(
        task_server.upsert_note_by_title_project.fn(
            title="Stable note",
            project="sqlite-memory-mcp",
            description="first body",
        )
    )
    existing = json.loads(
        task_server.upsert_note_by_title_project.fn(
            title="Stable note",
            project="sqlite-memory-mcp",
            description="should not replace",
            update_if_found=False,
        )
    )

    assert existing["action"] == "existing"
    assert existing["task_id"] == created["task_id"]

    with task_server._get_conn() as conn:
        body = conn.execute(
            "SELECT description FROM tasks WHERE id = ?", (created["task_id"],)
        ).fetchone()["description"]

    assert body == "first body"


def test_find_by_title_matches_entity_observations(task_env):
    _seed_lookup_corpus()

    result = json.loads(task_server.find_by_title.fn("strategic shortfall window"))

    assert any(
        item["kind"] == "entity" and item["title"] == "Общо име"
        for item in result["matches"]
    )
    assert any(
        "observations" in (item.get("matched_in") or []) for item in result["matches"]
    )


def test_find_by_title_returns_contract_metadata_and_hides_low_confidence_noise(
    task_env,
):
    _seed_lookup_corpus()

    result = json.loads(task_server.find_by_title.fn("research note"))

    assert result["ranking_contract_version"] == RETRIEVAL_CONTRACT_VERSION
    assert result["matches"][0]["title"] == "Research Note Rollout"
    assert result["matches"][0]["primary_surface"] == "title"
    assert result["matches"][0]["confidence"] == "high"
    assert result["hidden_low_confidence_count"] >= 1
    assert all(item["confidence"] != "low" for item in result["matches"])


def test_find_by_title_eval_corpus_keeps_top1_and_top3_hit_rate(task_env):
    _seed_lookup_corpus()
    corpus_path = Path(__file__).with_name("fixtures") / "retrieval_eval_corpus.json"
    corpus = json.loads(corpus_path.read_text(encoding="utf-8"))

    top1_hits = 0
    top3_hits = 0
    for case in corpus:
        result = json.loads(task_server.find_by_title.fn(case["query"]))
        titles = [item["title"] for item in result["matches"]]
        top1_title = titles[0] if titles else None
        top3 = set(titles[:3])
        if top1_title == case["expect_top1_title"]:
            top1_hits += 1
        if set(case["expect_top3_titles"]).issubset(top3):
            top3_hits += 1

    total = len(corpus)
    assert top1_hits == total
    assert top3_hits == total
