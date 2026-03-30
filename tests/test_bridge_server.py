"""Regression tests for MCP bridge server sync paths."""

import json
import os
import sqlite3
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import bridge_server
import db_utils
from schema import init_db


def _cp(args, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(["git", *args], returncode, stdout, stderr)


def _conn_factory(db_path: str):
    def _open():
        conn = sqlite3.connect(db_path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    return _open


def _db_conn(db_path: str):
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@pytest.fixture
def bridge_env(tmp_path, monkeypatch):
    db_path = str(tmp_path / "memory.db")
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    init_db(db_path)
    monkeypatch.setattr(bridge_server, "BRIDGE_REPO", str(bridge_dir))
    monkeypatch.setattr(bridge_server, "_get_conn", _conn_factory(db_path))
    return db_path, bridge_dir


def test_bridge_pull_imports_task_content_from_per_task_files(bridge_env, monkeypatch):
    db_path, bridge_dir = bridge_env
    task_id = "task-001"
    ts = "2026-03-26T12:00:00+00:00"
    (bridge_dir / "tasks").mkdir()
    (bridge_dir / "index.json").write_text(
        json.dumps(
            {
                "version": 4,
                "tasks": [
                    {
                        "id": task_id,
                        "title": "Remote task",
                        "created_at": ts,
                        "updated_at": ts,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (bridge_dir / "tasks" / f"{task_id}.json").write_text(
        json.dumps(
            {
                "id": task_id,
                "description": "Bridge description",
                "notes": "Bridge notes",
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        bridge_server,
        "_ensure_bridge_repo_ready",
        lambda repo: (True, None),
    )
    monkeypatch.setattr(bridge_server, "_git", lambda *args: _cp(args))

    result = json.loads(bridge_server.bridge_pull.fn())

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT description, notes FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    conn.close()

    assert result["new_tasks"] == 1
    assert row["description"] == "Bridge description"
    assert row["notes"] == "Bridge notes"


def test_bridge_push_blocks_when_bridge_repo_is_not_safe(tmp_path, monkeypatch):
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    monkeypatch.setattr(bridge_server, "BRIDGE_REPO", str(bridge_dir))
    monkeypatch.setattr(
        bridge_server,
        "_ensure_bridge_repo_ready",
        lambda repo: (
            False,
            "commit or stash bridge repo edits before sync: index.html",
        ),
    )

    git_calls = []

    def fake_git(*args):
        git_calls.append(args)
        return _cp(args)

    monkeypatch.setattr(bridge_server, "_git", fake_git)

    result = json.loads(bridge_server.bridge_push.fn())

    assert result["blocked_by_repo_state"] is True
    assert "index.html" in result["error"]
    assert git_calls == []


def test_bridge_push_missing_repo_error_includes_real_path(tmp_path, monkeypatch):
    missing_repo = tmp_path / "missing-bridge"
    monkeypatch.setattr(bridge_server, "BRIDGE_REPO", str(missing_repo))

    result = json.loads(bridge_server.bridge_push.fn())

    assert str(missing_repo) in result["error"]
    assert "{BRIDGE_REPO}" not in result["error"]


def test_bridge_push_writes_and_stages_shared_js(bridge_env, monkeypatch):
    _, bridge_dir = bridge_env
    git_calls = []

    def fake_git(*args):
        git_calls.append(args)
        if args == ("status", "--porcelain"):
            return _cp(args, stdout="M shared.json\nM shared.js\n")
        return _cp(args)

    monkeypatch.setattr(
        bridge_server,
        "_ensure_bridge_repo_ready",
        lambda repo: (True, None),
    )
    monkeypatch.setattr(bridge_server, "_git", fake_git)

    result = json.loads(bridge_server.bridge_push.fn(force=True))
    shared_js = (bridge_dir / "shared.js").read_text(encoding="utf-8")

    assert result["pushed_to_remote"] is True
    assert shared_js.startswith("window.__BRIDGE_DATA__ = ")
    assert any(args[0] == "add" and "shared.js" in args for args in git_calls)


def test_bridge_pull_falls_back_to_shared_json_when_index_is_corrupt(
    bridge_env, monkeypatch
):
    db_path, bridge_dir = bridge_env
    task_id = "task-legacy-1"
    ts = "2026-03-26T12:00:00+00:00"
    (bridge_dir / "index.json").write_text("{bad json", encoding="utf-8")
    (bridge_dir / "shared.json").write_text(
        json.dumps(
            {
                "tasks": [
                    {
                        "id": task_id,
                        "title": "Legacy fallback task",
                        "status": "not_started",
                        "priority": "medium",
                        "section": "inbox",
                        "created_at": ts,
                        "updated_at": ts,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(bridge_server, "_ensure_bridge_repo_ready", lambda repo: (True, None))
    monkeypatch.setattr(bridge_server, "_git", lambda *args: _cp(args))

    result = json.loads(bridge_server.bridge_pull.fn())

    with _db_conn(db_path) as conn:
        row = conn.execute(
            "SELECT title FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()

    assert result["new_tasks"] == 1
    assert row["title"] == "Legacy fallback task"


def test_bridge_pull_falls_back_to_shared_json_when_entities_index_is_corrupt(
    bridge_env, monkeypatch
):
    db_path, bridge_dir = bridge_env
    ts = "2026-03-26T12:00:00+00:00"
    (bridge_dir / "entities_index.json").write_text("{bad json", encoding="utf-8")
    (bridge_dir / "shared.json").write_text(
        json.dumps(
            {
                "entities": [
                    {
                        "name": "Legacy fallback entity",
                        "entityType": "note",
                        "project": "shared:bridge",
                        "observations": [{"content": "obs", "createdAt": ts}],
                        "createdAt": ts,
                        "updatedAt": ts,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(bridge_server, "_ensure_bridge_repo_ready", lambda repo: (True, None))
    monkeypatch.setattr(bridge_server, "_git", lambda *args: _cp(args))

    result = json.loads(bridge_server.bridge_pull.fn())

    with _db_conn(db_path) as conn:
        row = conn.execute(
            "SELECT name FROM entities WHERE name = ?",
            ("Legacy fallback entity",),
        ).fetchone()

    assert result["new_entities"] == 1
    assert row["name"] == "Legacy fallback entity"


def test_bridge_push_merges_remote_entities_task_content_and_ratings(
    bridge_env, monkeypatch
):
    db_path, bridge_dir = bridge_env
    old = "2026-03-28T12:00:00+00:00"
    new = "2026-03-29T12:00:00+00:00"

    with _db_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO tasks (id, title, description, status, priority, section, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("task-1", "Synced task", "old desc", "not_started", "medium", "inbox", old, old),
        )
        for field in db_utils.MERGEABLE_FIELDS:
            conn.execute(
                "INSERT INTO task_field_versions (task_id, field_name, updated_at, updated_by) "
                "VALUES (?, ?, ?, ?)",
                ("task-1", field, old, "local"),
            )

    (bridge_dir / "tasks").mkdir()
    (bridge_dir / "entities").mkdir()
    (bridge_dir / "index.json").write_text(
        json.dumps(
            {
                "version": 4,
                "tasks": [
                    {
                        "id": "task-1",
                        "title": "Synced task",
                        "status": "not_started",
                        "priority": "medium",
                        "section": "inbox",
                        "created_at": old,
                        "updated_at": new,
                        "_field_ts": {
                            "description": [new, "remote"],
                            "title": [old, "remote"],
                            "status": [old, "remote"],
                            "priority": [old, "remote"],
                            "section": [old, "remote"],
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (bridge_dir / "tasks" / "task-1.json").write_text(
        json.dumps({"id": "task-1", "description": "remote desc"}),
        encoding="utf-8",
    )
    (bridge_dir / "entities_index.json").write_text(
        json.dumps(
            {
                "version": 1,
                "entities": [
                    {
                        "id": 101,
                        "name": "Remote entity",
                        "entityType": "note",
                        "project": "shared:bridge",
                        "createdAt": old,
                        "updatedAt": new,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (bridge_dir / "entities" / "101.json").write_text(
        json.dumps(
            {
                "id": 101,
                "name": "Remote entity",
                "entityType": "note",
                "project": "shared:bridge",
                "observations": [{"content": "remote obs", "createdAt": new}],
                "createdAt": old,
                "updatedAt": new,
            }
        ),
        encoding="utf-8",
    )
    (bridge_dir / "shared.json").write_text(
        json.dumps(
            {
                "relations": [],
                "knowledge_ratings": [
                    {
                        "entity_name": "Remote entity",
                        "rater_id": "alice",
                        "content_hash": "hash-1",
                        "specificity": 0.9,
                        "falsifiability": 0.8,
                        "internal_consistency": 0.7,
                        "novelty": 0.6,
                        "rated_at": new,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    def fake_git(*args):
        if args == ("status", "--porcelain"):
            return _cp(args, stdout="M shared.json\nM index.json\nM entities_index.json\n")
        return _cp(args)

    monkeypatch.setattr(bridge_server, "_ensure_bridge_repo_ready", lambda repo: (True, None))
    monkeypatch.setattr(bridge_server, "_git", fake_git)

    result = json.loads(bridge_server.bridge_push.fn(force=True))

    with _db_conn(db_path) as conn:
        task = conn.execute(
            "SELECT description FROM tasks WHERE id = ?",
            ("task-1",),
        ).fetchone()
        entity = conn.execute(
            "SELECT name FROM entities WHERE name = ?",
            ("Remote entity",),
        ).fetchone()
        ratings = conn.execute(
            "SELECT COUNT(*) AS cnt FROM knowledge_ratings"
        ).fetchone()["cnt"]

    exported_task = json.loads(
        (bridge_dir / "tasks" / "task-1.json").read_text(encoding="utf-8")
    )
    exported_entities = json.loads(
        (bridge_dir / "entities_index.json").read_text(encoding="utf-8")
    )

    assert result["pushed_to_remote"] is True
    assert task["description"] == "remote desc"
    assert exported_task["description"] == "remote desc"
    assert entity["name"] == "Remote entity"
    assert any(e["name"] == "Remote entity" for e in exported_entities["entities"])
    assert ratings == 1


def test_bridge_push_does_not_skip_relation_only_changes(bridge_env, monkeypatch):
    _, bridge_dir = bridge_env
    last_push = "2026-03-29T10:00:00+00:00"
    relation_ts = "2026-03-29T11:00:00+00:00"
    old = "2026-03-29T09:00:00+00:00"

    with bridge_server._get_conn() as conn:
        conn.execute(
            "INSERT INTO entities (name, entity_type, project, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            ("Entity A", "note", "shared:bridge", old, old),
        )
        conn.execute(
            "INSERT INTO entities (name, entity_type, project, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            ("Entity B", "note", "shared:bridge", old, old),
        )
        a_id = conn.execute(
            "SELECT id FROM entities WHERE name = ?",
            ("Entity A",),
        ).fetchone()["id"]
        b_id = conn.execute(
            "SELECT id FROM entities WHERE name = ?",
            ("Entity B",),
        ).fetchone()["id"]
        conn.execute(
            "INSERT INTO relations (from_id, to_id, relation_type, created_at) VALUES (?, ?, ?, ?)",
            (a_id, b_id, "related_to", relation_ts),
        )
        conn.execute(
            "INSERT OR REPLACE INTO bridge_meta(key, value) VALUES('last_push_at', ?)",
            (last_push,),
        )

    def fake_git(*args):
        if args == ("status", "--porcelain"):
            return _cp(args, stdout="M shared.json\n")
        return _cp(args)

    monkeypatch.setattr(bridge_server, "_ensure_bridge_repo_ready", lambda repo: (True, None))
    monkeypatch.setattr(bridge_server, "_git", fake_git)

    result = json.loads(bridge_server.bridge_push.fn())
    payload = json.loads((bridge_dir / "shared.json").read_text(encoding="utf-8"))

    assert result["pushed_to_remote"] is True
    assert payload["relations"] == [
        {
            "from": "Entity A",
            "to": "Entity B",
            "relationType": "related_to",
            "createdAt": relation_ts,
        }
    ]


def test_bridge_push_promotes_ready_pending_public_tasks_before_skip(
    bridge_env, monkeypatch
):
    db_path, bridge_dir = bridge_env
    last_push = "2026-03-29T10:00:00+00:00"
    requested = "2026-03-29T09:00:00+00:00"
    created = "2026-03-29T08:00:00+00:00"

    with _db_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO tasks (id, title, status, priority, section, visibility, publish_requested_at, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "task-public-1",
                "Publish me",
                "not_started",
                "medium",
                "inbox",
                "pending_public",
                requested,
                created,
                created,
            ),
        )
        conn.execute(
            "INSERT OR REPLACE INTO bridge_meta(key, value) VALUES('last_push_at', ?)",
            (last_push,),
        )

    def fake_git(*args):
        if args == ("status", "--porcelain"):
            return _cp(args, stdout="M shared.json\nM index.json\n")
        return _cp(args)

    monkeypatch.setattr(bridge_server, "_ensure_bridge_repo_ready", lambda repo: (True, None))
    monkeypatch.setattr(bridge_server, "_git", fake_git)

    result = json.loads(bridge_server.bridge_push.fn())

    with _db_conn(db_path) as conn:
        row = conn.execute(
            "SELECT visibility FROM tasks WHERE id = ?",
            ("task-public-1",),
        ).fetchone()

    assert result["pushed_to_remote"] is True
    assert result["promoted_to_public"]["tasks"] == 1
    assert row["visibility"] == "public"


def test_bridge_pull_skips_spoofed_collaboration_payloads(bridge_env, monkeypatch):
    db_path, bridge_dir = bridge_env
    now = "2026-03-28T12:00:00+00:00"
    (bridge_dir / "shared.json").write_text(
        json.dumps(
            {
                "owner": "mallory",
                "shared_knowledge": [
                    {
                        "name": "Spoofed shared entity",
                        "entityType": "note",
                        "observations": [{"content": "spoofed", "createdAt": now}],
                        "sharedBy": "alice",
                    }
                ],
                "public_knowledge": {
                    "entities": [
                        {
                            "name": "Spoofed public entity",
                            "entityType": "note",
                            "observations": [{"content": "public", "createdAt": now}],
                        }
                    ]
                },
                "knowledge_ratings": [
                    {
                        "entity_name": "Rated entity",
                        "rater_id": "alice",
                        "content_hash": "hash-1",
                        "specificity": 0.8,
                        "falsifiability": 0.7,
                        "internal_consistency": 0.9,
                        "novelty": 0.6,
                        "rated_at": now,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with _db_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO collaborators (github_user, trust_level, added_at) VALUES (?, ?, ?)",
            ("alice", "read_write", now),
        )
        conn.execute(
            "INSERT INTO entities (name, entity_type, visibility, created_at, updated_at) "
            "VALUES (?, ?, 'public', ?, ?)",
            ("Rated entity", "note", now, now),
        )
        entity_id = conn.execute(
            "SELECT id FROM entities WHERE name = ?",
            ("Rated entity",),
        ).fetchone()["id"]
        conn.execute(
            "INSERT INTO observations (entity_id, content, created_at) VALUES (?, ?, ?)",
            (entity_id, "existing observation", now),
        )

    monkeypatch.setattr(bridge_server, "_ensure_bridge_repo_ready", lambda repo: (True, None))
    monkeypatch.setattr(bridge_server, "_git", lambda *args: _cp(args))

    result = json.loads(bridge_server.bridge_pull.fn())

    with _db_conn(db_path) as conn:
        pending = conn.execute("SELECT COUNT(*) AS cnt FROM pending_shared_entities").fetchone()[
            "cnt"
        ]
        ratings = conn.execute("SELECT COUNT(*) AS cnt FROM knowledge_ratings").fetchone()[
            "cnt"
        ]

    assert "staged_shared_knowledge" not in result
    assert "staged_public_knowledge" not in result
    assert "imported_ratings" not in result
    assert pending == 0
    assert ratings == 0


def test_bridge_pull_accepts_bound_collaboration_payloads(bridge_env, monkeypatch):
    db_path, bridge_dir = bridge_env
    now = "2026-03-28T12:00:00+00:00"
    (bridge_dir / "shared.json").write_text(
        json.dumps(
            {
                "owner": "alice",
                "shared_knowledge": [
                    {
                        "name": "Trusted shared entity",
                        "entityType": "note",
                        "observations": [{"content": "shared", "createdAt": now}],
                        "sharedBy": "alice",
                    }
                ],
                "public_knowledge": {
                    "entities": [
                        {
                            "name": "Trusted public entity",
                            "entityType": "note",
                            "observations": [{"content": "public", "createdAt": now}],
                        }
                    ]
                },
                "knowledge_ratings": [
                    {
                        "entity_name": "Rated entity",
                        "rater_id": "alice",
                        "content_hash": "hash-1",
                        "specificity": 0.8,
                        "falsifiability": 0.7,
                        "internal_consistency": 0.9,
                        "novelty": 0.6,
                        "rated_at": now,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with _db_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO collaborators (github_user, trust_level, added_at) VALUES (?, ?, ?)",
            ("alice", "read_write", now),
        )
        conn.execute(
            "INSERT INTO entities (name, entity_type, visibility, created_at, updated_at) "
            "VALUES (?, ?, 'public', ?, ?)",
            ("Rated entity", "note", now, now),
        )
        entity_id = conn.execute(
            "SELECT id FROM entities WHERE name = ?",
            ("Rated entity",),
        ).fetchone()["id"]
        conn.execute(
            "INSERT INTO observations (entity_id, content, created_at) VALUES (?, ?, ?)",
            (entity_id, "existing observation", now),
        )

    monkeypatch.setattr(bridge_server, "_ensure_bridge_repo_ready", lambda repo: (True, None))
    monkeypatch.setattr(bridge_server, "_git", lambda *args: _cp(args))

    result = json.loads(bridge_server.bridge_pull.fn())

    with _db_conn(db_path) as conn:
        pending = conn.execute("SELECT COUNT(*) AS cnt FROM pending_shared_entities").fetchone()[
            "cnt"
        ]
        ratings = conn.execute("SELECT COUNT(*) AS cnt FROM knowledge_ratings").fetchone()[
            "cnt"
        ]

    assert result["staged_shared_knowledge"] == 1
    assert result["staged_public_knowledge"] == 1
    assert result["imported_ratings"] == 1
    assert pending == 2
    assert ratings == 1


def test_assign_task_rejects_invalid_github_user(bridge_env):
    db_path, _ = bridge_env
    now = "2026-03-28T12:00:00+00:00"
    with _db_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO tasks (id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
            ("task-1", "Task", now, now),
        )

    result = json.loads(bridge_server.assign_task.fn("task-1", "../bad-user"))

    assert "Invalid GitHub username" in result["error"]
