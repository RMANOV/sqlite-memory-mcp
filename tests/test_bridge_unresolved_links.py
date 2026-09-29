"""Preserve transport edges a partial entity snapshot cannot materialize."""

import json
import sqlite3

import pytest

from db_utils import export_task_files, record_task_entity_link_tombstone
from schema import init_db


@pytest.fixture
def bridge(tmp_path):
    db_path = tmp_path / "memory.db"
    init_db(str(db_path))
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    path = tmp_path / "bridge"
    path.mkdir()
    conn.execute(
        "INSERT INTO tasks(id,title,description,notes,created_at,updated_at) VALUES(?,?,?,?,?,?)",
        ("task-1", "Task title", "Full task body", "Task note", "2026-08-01T00:00:00Z", "2026-08-01T00:00:00Z"),
    )
    export_task_files(conn, str(path))
    task_path = path / "tasks" / "task-1.json"
    original = json.loads(task_path.read_text(encoding="utf-8"))
    yield conn, path, task_path, original
    conn.close()


def seed_link(task_path, task, name):
    link = {"name": name, "link_type": "manual", "score": 0.7, "created_at": "2026-08-01T00:00:00Z"}
    task_path.write_text(json.dumps({**task, "_links": [link]}), encoding="utf-8")
    return link


def test_unknown_entity_link_survives_export_without_task_content_changes(bridge):
    conn, path, task_path, original = bridge
    link = seed_link(task_path, original, "Entity absent from this peer")
    for _ in range(2):
        export_task_files(conn, str(path))
        exported = json.loads(task_path.read_text(encoding="utf-8"))
        assert exported == {**original, "_links": [link]}
    assert conn.execute("SELECT count(*) FROM entities").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM task_entity_links").fetchone()[0] == 0


def test_deleted_link_to_known_local_entity_is_not_resurrected(bridge):
    conn, path, task_path, original = bridge
    conn.execute("INSERT INTO entities(name,entity_type,created_at,updated_at) VALUES(?,?,?,?)",
                 ("Known entity", "concept", "2026-08-01T00:00:00Z", "2026-08-01T00:00:00Z"))
    seed_link(task_path, original, "Known entity")
    export_task_files(conn, str(path))
    assert json.loads(task_path.read_text(encoding="utf-8")) == original


def test_explicit_unknown_link_tombstone_wins_over_old_transport_link(bridge):
    conn, path, task_path, original = bridge
    seed_link(task_path, original, "Unknown deleted entity")
    record_task_entity_link_tombstone(
        conn, task_id="task-1", entity_name="Unknown deleted entity", link_type="manual",
        score=0.7, created_at="2026-08-01T00:00:00Z", deleted_at="2026-08-02T00:00:00Z",
    )
    export_task_files(conn, str(path))
    exported = json.loads(task_path.read_text(encoding="utf-8"))
    assert exported["_links"] == []
    assert exported["_link_tombstones"][0]["name"] == "Unknown deleted entity"
    assert exported["description"] == original["description"]


def test_unpushed_task_tombstone_keeps_its_status_with_unresolved_edge(bridge):
    conn, path, task_path, original = bridge
    seed_link(task_path, original, "Unknown entity")
    conn.execute("UPDATE tasks SET status='archived' WHERE id='task-1'")
    export_task_files(conn, str(path))
    exported = json.loads(task_path.read_text(encoding="utf-8"))
    assert exported["status"] == "archived"
    assert exported["_links"][0]["name"] == "Unknown entity"


def test_unrelated_task_file_cannot_supply_unresolved_links(bridge):
    conn, path, task_path, original = bridge
    seed_link(task_path, {**original, "id": "different-task"}, "Unknown entity")
    export_task_files(conn, str(path))
    assert json.loads(task_path.read_text(encoding="utf-8"))["_links"] == []
