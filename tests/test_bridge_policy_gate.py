"""Regressions for complete private bridge authorization at real entrypoints."""

import json
import sqlite3
import subprocess
import time

import pytest

import bridge_server
import bridge_sync_worker as worker
import db_utils
from schema import init_db


@pytest.mark.parametrize("tool", ["bridge_push", "bridge_pull"])
def test_mcp_denial_precedes_opening_even_a_nonexistent_database(
    tmp_path, monkeypatch, tool
):
    bridge = tmp_path / "bridge"
    bridge.mkdir()
    database = tmp_path / "must-not-exist.db"
    monkeypatch.setenv(
        "SQLITE_MEMORY_BRIDGE_POLICY", str(tmp_path / "absent-policy.json")
    )
    monkeypatch.setattr(bridge_server, "BRIDGE_REPO", str(bridge))
    monkeypatch.setattr(bridge_server, "_get_conn", lambda: sqlite3.connect(database))
    monkeypatch.setattr(
        bridge_server, "_bridge_sync_main", lambda **kw: {"pushed": False}
    )
    result = json.loads(getattr(bridge_server, tool).fn())
    assert not database.exists(), "A denied MCP request opened/created the database"
    assert result.get("blocked_by_bridge_policy") is True


def test_worker_denies_missing_policy_before_backoff_or_lock(tmp_path, monkeypatch):
    bridge = tmp_path / "bridge"
    bridge.mkdir()
    monkeypatch.setenv(
        "SQLITE_MEMORY_BRIDGE_POLICY", str(tmp_path / "absent-policy.json")
    )
    monkeypatch.setattr(worker, "_push_backoff_until", time.monotonic() + 600)
    result = worker.main(
        bridge_repo=str(bridge), db_path=str(tmp_path / "absent.db"), force=True
    )
    assert result.get("blocked_by_bridge_policy") is True
    assert list(bridge.iterdir()) == []
    assert not (tmp_path / "absent.db").exists()


def test_unapproved_full_worker_must_not_export_private_task_or_ledger(
    tmp_path, monkeypatch
):
    bridge = tmp_path / "bridge"
    bridge.mkdir()
    database = str(tmp_path / "memory.db")
    init_db(database)
    with sqlite3.connect(database) as conn:
        conn.execute(
            "INSERT INTO tasks (id,title,description,project,created_at,updated_at) "
            "VALUES ('private-task','PRIVATE_TASK_MARKER','private body','private',?,?)",
            ("2026-11-14T00:00:00+00:00",) * 2,
        )
    monkeypatch.setenv(
        "SQLITE_MEMORY_BRIDGE_POLICY", str(tmp_path / "absent-policy.json")
    )
    monkeypatch.setenv("BRIDGE_PRIVATE_ONLY", "1")
    monkeypatch.setattr(worker, "_push_backoff_until", 0)
    monkeypatch.setattr(
        worker, "ensure_bridge_repo_ready", lambda *a, **kw: (True, None)
    )
    monkeypatch.setattr(worker, "ensure_bridge_git_identity", lambda *a, **kw: {})

    def cp(*a, **kw):
        return subprocess.CompletedProcess(a, 0, "", "")

    monkeypatch.setattr(worker, "git_run", cp)
    monkeypatch.setattr(worker, "git_retry", cp)
    result = worker.main(force=True, bridge_repo=str(bridge), db_path=database)
    assert not (bridge / "shared.json").exists(), (
        "Unapproved private payload was serialized"
    )
    assert result.get("blocked_by_bridge_policy") is True


def test_empty_entity_selection_removes_stale_generated_json_only(tmp_path):
    database = str(tmp_path / "memory.db")
    init_db(database)
    bridge = tmp_path / "bridge"
    entities = bridge / "entities"
    entities.mkdir(parents=True)
    (entities / "23.json").write_text('{"private":"old entity marker"}')
    (entities / "notes.txt").write_text("operator note")
    with sqlite3.connect(database) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("DELETE FROM entities")
        ids, rows = db_utils.export_entity_files(conn, str(bridge))
    assert ids == [] and rows == []
    assert not (entities / "23.json").exists()
    assert (entities / "notes.txt").read_text() == "operator note"


def test_entity_prefix_is_literal_not_sql_wildcard(tmp_path):
    database = str(tmp_path / "memory.db")
    init_db(database)
    with sqlite3.connect(database) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("DELETE FROM entities")
        conn.executemany(
            "INSERT INTO entities(name,entity_type,project,created_at,updated_at) VALUES (?,?,?,?,?)",
            [
                ("wanted", "concept", "team_abc", "now", "now"),
                ("unwanted", "concept", "teamXabc", "now", "now"),
            ],
        )
        rows, _ = worker._export_entities(conn, "team_")
    assert [row["name"] for row in rows] == ["wanted"]
