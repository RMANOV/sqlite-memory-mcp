"""Regression tests for MCP bridge server sync paths."""

import json
import os
import sqlite3
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import bridge_server
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


def test_bridge_status_blocks_and_suppresses_remote_counts(bridge_env, monkeypatch):
    _db_path, _bridge_dir = bridge_env
    monkeypatch.setattr(
        bridge_server,
        "_inspect_bridge_repo_blocker",
        lambda repo: "bridge repo has active git operation (rebase-merge)",
    )

    def fail_git(*args):
        raise AssertionError(f"bridge_status must not call git while blocked: {args}")

    monkeypatch.setattr(bridge_server, "_git", fail_git)

    result = json.loads(bridge_server.bridge_status.fn())

    assert result["blocked_by_repo_state"] is True
    assert "rebase-merge" in result["error"]
    assert result["remote_status"] == "suppressed_repo_blocked"
    assert "last_commit" not in result
    assert "remote_count" not in result
    assert "remote_tasks" not in result
    assert result["local_task_status_counts"] == {}


def test_bridge_status_reports_task_count_delta_and_status_counts(
    bridge_env, monkeypatch
):
    db_path, bridge_dir = bridge_env
    ts = "2026-05-22T05:00:00+00:00"
    with _db_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO tasks (id, title, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("local-done", "Done", "done", ts, ts),
        )
        conn.execute(
            "INSERT INTO tasks (id, title, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("local-open", "Open", "not_started", ts, ts),
        )
    (bridge_dir / "shared.json").write_text(
        json.dumps(
            {
                "version": 4,
                "pushed_at": ts,
                "machine_id": "windows-rmanov",
                "tasks": [
                    {"id": "remote-open", "status": "not_started"},
                ],
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        bridge_server, "_inspect_bridge_repo_blocker", lambda repo: None
    )
    monkeypatch.setattr(
        bridge_server,
        "_git",
        lambda *args: _cp(args, stdout="2026-05-22 08:00:00 +0300 bridge: push"),
    )

    result = json.loads(bridge_server.bridge_status.fn())

    assert result["local_tasks"] == 2
    assert result["remote_tasks"] == 1
    assert result["task_count_delta"] == 1
    assert result["task_counts_match"] is False
    assert result["local_task_status_counts"] == {"done": 1, "not_started": 1}
    assert result["remote_task_status_counts"] == {"not_started": 1}


def test_bridge_push_missing_repo_error_includes_real_path(tmp_path, monkeypatch):
    missing_repo = tmp_path / "missing-bridge"
    monkeypatch.setattr(bridge_server, "BRIDGE_REPO", str(missing_repo))

    result = json.loads(bridge_server.bridge_push.fn())

    assert str(missing_repo) in result["error"]
    assert "{BRIDGE_REPO}" not in result["error"]


def test_bridge_push_delegates_to_canonical_worker(bridge_env, monkeypatch):
    db_path, bridge_dir = bridge_env
    captured = {}

    def fake_worker(**kwargs):
        captured.update(kwargs)
        return {"entities": 2, "tasks": 3, "pushed": True}

    monkeypatch.setattr(bridge_server, "_bridge_sync_main", fake_worker)

    result = json.loads(bridge_server.bridge_push.fn(tag="team", force=True))

    assert captured == {
        "db_path": db_path,
        "bridge_repo": str(bridge_dir),
        "force": True,
        "entity_project_prefix": "team",
    }
    assert result["pushed"] is True
    assert result["pushed_to_remote"] is True


def test_bridge_pull_delegates_and_maps_blocked_worker_result(bridge_env, monkeypatch):
    db_path, bridge_dir = bridge_env
    captured = {}

    def fake_worker(**kwargs):
        captured.update(kwargs)
        return {
            "pushed": False,
            "blocked_by_repo_state": True,
            "message": "repo blocked",
        }

    monkeypatch.setattr(bridge_server, "_bridge_sync_main", fake_worker)

    result = json.loads(bridge_server.bridge_pull.fn())

    assert captured == {
        "db_path": db_path,
        "bridge_repo": str(bridge_dir),
        "pull_only": True,
    }
    assert result["error"] == "repo blocked"


def test_bridge_doctor_returns_runtime_parity_and_surface_contract(
    bridge_env, monkeypatch
):
    _, bridge_dir = bridge_env
    monkeypatch.setattr(
        bridge_server,
        "_write_runtime_parity_manifest",
        lambda: {
            "version": "bridge_runtime_v1",
            "all_synced": False,
            "warnings": ["bridge_auto_sync.py: mismatch"],
            "files": [],
        },
    )
    monkeypatch.setattr(
        bridge_server,
        "_runtime_warning_summary",
        lambda report: "BRIDGE WARNING: runtime drift detected",
    )

    result = json.loads(bridge_server.bridge_doctor.fn())

    assert result["repo_exists"] is True
    assert result["bridge_repo"] == str(bridge_dir)
    assert result["runtime_parity"]["all_synced"] is False
    assert result["runtime_warning"] == "BRIDGE WARNING: runtime drift detected"
    assert (
        result["surface_contract"]["bridge_artifacts"]["shared.json"]["git_stage"]
        is True
    )
    assert "updated_at_churn" in result


def test_bridge_doctor_flags_updated_at_churn_clusters(bridge_env):
    db_path, _ = bridge_env
    ts = "2026-05-22T05:34:57.560101+00:00"
    with _db_conn(db_path) as conn:
        for i in range(25):
            conn.execute(
                "INSERT INTO tasks (id, title, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (f"archived-{i}", f"Archived {i}", "archived", ts, ts),
            )

    result = json.loads(bridge_server.bridge_doctor.fn(write_manifest=False))

    churn = result["updated_at_churn"]
    assert churn["suspicious_count"] == 1
    assert churn["clusters"][0]["updated_at"] == ts
    assert churn["clusters"][0]["total"] == 25


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


def test_assign_task_records_field_history_and_events(bridge_env, monkeypatch):
    db_path, _ = bridge_env
    now = "2026-03-28T12:00:00+00:00"
    with _db_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO tasks (id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
            ("task-1", "Task", now, now),
        )

    monkeypatch.setattr(
        bridge_server.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, stdout="alice\n", stderr=""
        ),
    )

    result = json.loads(bridge_server.assign_task.fn("task-1", "alice"))

    with _db_conn(db_path) as conn:
        row = conn.execute(
            "SELECT assignee, shared_by FROM tasks WHERE id = ?",
            ("task-1",),
        ).fetchone()
        fields = conn.execute(
            "SELECT field_name, source_event_id, new_value FROM task_field_versions "
            "WHERE task_id = ? AND field_name IN ('assignee', 'shared_by') "
            "ORDER BY field_name",
            ("task-1",),
        ).fetchall()
        events = conn.execute(
            "SELECT COUNT(*) AS cnt FROM memory_events "
            "WHERE aggregate_kind = 'task' AND aggregate_id = ? AND tool_name = ?",
            ("task-1", "bridge_server.assign_task"),
        ).fetchone()["cnt"]

    assert result["assignee"] == "alice"
    assert row["assignee"] == "alice"
    assert row["shared_by"] == "alice"
    assert len(fields) == 2
    assert all(field["source_event_id"] for field in fields)
    assert {field["new_value"] for field in fields} == {"alice"}
    assert events == 2


def test_review_shared_tasks_approve_creates_ledgered_task(bridge_env):
    db_path, _ = bridge_env
    now = "2026-03-28T12:00:00+00:00"
    with _db_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO pending_shared_tasks "
            "(id, title, description, status, priority, section, due_date, project, "
            "parent_id, notes, recurring, type, assignee, shared_by, created_at, updated_at, received_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "shared-task-1",
                "Shared task",
                "Bridge description",
                "not_started",
                "high",
                "today",
                "2026-03-29",
                "mapping_studio",
                None,
                "Bridge notes",
                None,
                "task",
                "alice",
                "alice",
                now,
                now,
                now,
            ),
        )

    result = json.loads(bridge_server.review_shared_tasks.fn(action="approve"))

    with _db_conn(db_path) as conn:
        task = conn.execute(
            "SELECT title, project, section, assignee, shared_by FROM tasks WHERE id = ?",
            ("shared-task-1",),
        ).fetchone()
        fields = conn.execute(
            "SELECT COUNT(*) AS cnt FROM task_field_versions WHERE task_id = ? AND source_event_id IS NOT NULL",
            ("shared-task-1",),
        ).fetchone()["cnt"]
        events = conn.execute(
            "SELECT COUNT(*) AS cnt FROM memory_events "
            "WHERE aggregate_kind = 'task' AND aggregate_id = ? AND tool_name = ?",
            ("shared-task-1", "bridge_server.review_shared_tasks.approve"),
        ).fetchone()["cnt"]
        pending = conn.execute(
            "SELECT COUNT(*) AS cnt FROM pending_shared_tasks WHERE id = ?",
            ("shared-task-1",),
        ).fetchone()["cnt"]

    assert result["approved"] == 1
    assert task["title"] == "Shared task"
    assert task["project"] == "mapping-studio"
    assert task["section"] == "today"
    assert task["assignee"] == "alice"
    assert task["shared_by"] == "alice"
    assert fields > 0
    assert events > 0
    assert pending == 0


def test_review_shared_tasks_approve_ignores_older_mixed_offset_remote_update(
    bridge_env,
):
    db_path, _ = bridge_env
    task_id = "shared-task-offset"
    local_ts = "2026-03-24T10:00:00Z"
    remote_ts = "2026-03-24T11:00:00+02:00"

    with _db_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO tasks (id, title, description, status, priority, section, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                task_id,
                "Local task",
                "Local description",
                "not_started",
                "medium",
                "inbox",
                local_ts,
                local_ts,
            ),
        )
        conn.execute(
            "INSERT INTO pending_shared_tasks "
            "(id, title, description, status, priority, section, due_date, project, "
            "parent_id, notes, recurring, type, assignee, shared_by, created_at, updated_at, received_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                task_id,
                "Remote stale task",
                "Remote stale description",
                "not_started",
                "medium",
                "inbox",
                None,
                None,
                None,
                None,
                None,
                "task",
                None,
                "alice",
                remote_ts,
                remote_ts,
                remote_ts,
            ),
        )

    result = json.loads(bridge_server.review_shared_tasks.fn(action="approve"))

    with _db_conn(db_path) as conn:
        task = conn.execute(
            "SELECT title, description, updated_at FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        pending = conn.execute(
            "SELECT COUNT(*) AS cnt FROM pending_shared_tasks WHERE id = ?",
            (task_id,),
        ).fetchone()["cnt"]

    assert result["approved"] == 0
    assert task["title"] == "Local task"
    assert task["description"] == "Local description"
    assert task["updated_at"] == local_ts
    assert pending == 0
