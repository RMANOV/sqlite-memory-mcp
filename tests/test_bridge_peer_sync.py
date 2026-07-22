"""Contract tests for optional cross-account bridge publishing."""

from __future__ import annotations

import sqlite3
import subprocess

import bridge_peer_sync
from schema import init_db


def _conn(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def test_merge_tasks_is_lww_and_deterministic():
    current = [
        {"id": "b", "title": "keep", "updated_at": "2026-07-20T10:00:00Z"},
        {"id": "a", "title": "old", "updated_at": "2026-07-20T10:00:00Z"},
    ]
    incoming = [
        {"id": "a", "title": "new", "updated_at": "2026-07-20T11:00:00Z"},
        {"id": "b", "title": "stale", "updated_at": "2026-07-20T09:00:00Z"},
    ]

    merged = bridge_peer_sync._merge_tasks(current, incoming)

    assert [item["id"] for item in merged] == ["a", "b"]
    assert merged[0]["title"] == "new"
    assert merged[1]["title"] == "keep"


def test_publish_peer_payloads_materializes_db_before_network(tmp_path, monkeypatch):
    db_path = str(tmp_path / "memory.db")
    init_db(db_path)
    timestamp = "2026-07-21T10:00:00+00:00"
    with _conn(db_path) as conn:
        conn.execute(
            "INSERT INTO collaborators(github_user, added_at) VALUES(?, ?)",
            ("alice", timestamp),
        )
        conn.execute(
            "INSERT INTO entities(name, entity_type, project, created_at, updated_at) "
            "VALUES(?, ?, ?, ?, ?)",
            ("Shared fact", "fact", "shared:demo", timestamp, timestamp),
        )
        entity_id = conn.execute(
            "SELECT id FROM entities WHERE name='Shared fact'"
        ).fetchone()["id"]
        conn.execute(
            "INSERT INTO observations(entity_id, content, created_at) VALUES(?, ?, ?)",
            (entity_id, "Evidence", timestamp),
        )
        conn.execute(
            "INSERT INTO sharing_rules(entity_name, target_user, share_type, created_at) "
            "VALUES(?, ?, 'entity', ?)",
            ("Shared fact", "alice", timestamp),
        )

    calls = []

    def fake_clone_merge_push(target, key, items, merge, message):
        # An IMMEDIATE writer proves no read transaction leaked into network I/O.
        probe = sqlite3.connect(db_path, isolation_level=None, timeout=0.1)
        try:
            probe.execute("BEGIN IMMEDIATE")
            probe.execute("ROLLBACK")
        finally:
            probe.close()
        calls.append((target, key, items, merge, message))
        return True

    monkeypatch.setattr(bridge_peer_sync, "_clone_merge_push", fake_clone_merge_push)

    result = bridge_peer_sync.publish_peer_payloads(
        db_path,
        [
            {
                "id": "task-1",
                "title": "Assigned",
                "assignee": "bob",
                "updated_at": timestamp,
            }
        ],
    )

    assert result == {"assigned_task_recipients": 1, "knowledge_shared": 1}
    assert [(call[0], call[1]) for call in calls] == [
        ("bob", "shared_tasks"),
        ("alice", "shared_knowledge"),
    ]
    knowledge = calls[1][2]
    assert knowledge[0]["name"] == "Shared fact"
    assert knowledge[0]["observations"][0]["content"] == "Evidence"
    with _conn(db_path) as conn:
        last_sync = conn.execute(
            "SELECT last_sync_at FROM collaborators WHERE github_user='alice'"
        ).fetchone()["last_sync_at"]
    assert last_sync


def test_create_public_release_uses_configured_repo(monkeypatch):
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setenv("BRIDGE_GH_REPO", "owner/repo")
    monkeypatch.setattr(bridge_peer_sync.subprocess, "run", fake_run)

    tag = bridge_peer_sync.create_public_release(
        [{"name": "public"}], [{"id": "task"}], "machine"
    )

    assert tag and tag.startswith("public-v")
    assert calls[0][0][:4] == ["gh", "release", "create", tag]
    assert calls[0][0][calls[0][0].index("--repo") + 1] == "owner/repo"


def test_create_public_release_skips_without_repository(monkeypatch):
    monkeypatch.delenv("BRIDGE_GH_REPO", raising=False)
    monkeypatch.setattr(
        bridge_peer_sync.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("gh must not run without BRIDGE_GH_REPO")
        ),
    )

    assert bridge_peer_sync.create_public_release([{"name": "public"}], [], "m") is None
