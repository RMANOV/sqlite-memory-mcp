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
    monkeypatch.setattr(task_server, "_get_conn", _conn_factory(db_path))
    monkeypatch.setattr(task_server, "_vec_sync_task_safe", lambda conn, task_id: None)
    return db_path


def test_find_by_title_matches_done_notes_and_entities_by_partial_title(task_env):
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

    with task_server._get_conn() as conn:
        conn.execute(
            "INSERT INTO entities (name, entity_type, project, created_at, updated_at) "
            "VALUES (?, ?, ?, datetime('now'), datetime('now'))",
            ("Мапинг Студио Архитектура", "project-doc", "mapping-studio"),
        )

    result = json.loads(task_server.find_by_title.fn("мапинг студ"))

    titles = [item["title"] for item in result["matches"]]
    kinds = {item["kind"] for item in result["matches"]}

    assert "Работна среща за мапинг студио" in titles
    assert "Мапинг Студио Архитектура" in titles
    assert {"task", "entity"} <= kinds


def test_find_by_title_normalizes_hyphens_and_underscores(task_env):
    task_server.create_task_or_note.fn(
        title="reports_generator phase-2 followup",
        type="task",
    )

    result = json.loads(task_server.find_by_title.fn("reports generator phase 2"))

    assert result["count"] >= 1
    assert result["matches"][0]["title"] == "reports_generator phase-2 followup"


def test_find_by_title_matches_description_notes_and_project(task_env):
    created = json.loads(
        task_server.create_task_or_note.fn(
            title="Напълно общо заглавие",
            type="task",
            description="Тук вътре стои фразата Byzantine gossip",
            notes="вътрешна бележка за fallback path",
            project="deep-research",
        )
    )
    task_server.update_task.fn(created["task_id"], status="done")

    by_desc = json.loads(task_server.find_by_title.fn("Byzantine gossip"))
    by_notes = json.loads(task_server.find_by_title.fn("fallback path"))
    by_project = json.loads(task_server.find_by_title.fn("deep research"))

    assert any(item["id"] == created["task_id"] for item in by_desc["matches"])
    assert any(
        "description" in (item.get("matched_in") or []) for item in by_desc["matches"]
    )
    assert any(item["id"] == created["task_id"] for item in by_notes["matches"])
    assert any(
        "notes" in (item.get("matched_in") or []) for item in by_notes["matches"]
    )
    assert any(item["id"] == created["task_id"] for item in by_project["matches"])
    assert any(
        "project" in (item.get("matched_in") or []) for item in by_project["matches"]
    )


def test_find_by_title_matches_entity_observations(task_env):
    with task_server._get_conn() as conn:
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

    result = json.loads(task_server.find_by_title.fn("strategic shortfall window"))

    assert any(
        item["kind"] == "entity" and item["id"] == entity_id
        for item in result["matches"]
    )
    assert any(
        "observations" in (item.get("matched_in") or []) for item in result["matches"]
    )
