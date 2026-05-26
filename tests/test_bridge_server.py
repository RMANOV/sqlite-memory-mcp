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


def test_bridge_pull_does_not_resurrect_archived_task_from_remote_done(
    bridge_env, monkeypatch
):
    db_path, bridge_dir = bridge_env
    task_id = "task-archived"
    old = "2026-05-23T22:54:59+00:00"
    new = "2026-05-26T07:22:12+00:00"

    with _db_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO tasks "
            "(id, title, status, priority, section, due_date, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                task_id,
                "ARCHIVE | stale remote must not reopen",
                "archived",
                "low",
                "done",
                None,
                old,
                old,
            ),
        )
        for field, value in (
            ("status", "archived"),
            ("section", "done"),
            ("priority", "low"),
            ("due_date", None),
        ):
            conn.execute(
                "INSERT INTO task_field_versions "
                "(task_id, field_name, updated_at, updated_by, new_value, updated_order) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (task_id, field, old, "fedora", value, 116626351682748440),
            )

    (bridge_dir / "tasks").mkdir()
    (bridge_dir / "index.json").write_text(
        json.dumps(
            {
                "version": 4,
                "tasks": [
                    {
                        "id": task_id,
                        "title": "ARCHIVE | stale remote must not reopen",
                        "status": "done",
                        "priority": "medium",
                        "section": "waiting",
                        "due_date": "2026-05-20",
                        "created_at": old,
                        "updated_at": new,
                        "_field_ts": {
                            "status": [new, "RManov", 116639670793142275, "evt-status"],
                            "section": [new, "RManov", 116639670793142276, "evt-section"],
                            "priority": [new, "RManov", 116639670793142277, "evt-priority"],
                            "due_date": [new, "RManov", 116639670793142278, "evt-due"],
                        },
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
                "status": "done",
                "priority": "medium",
                "section": "waiting",
                "due_date": "2026-05-20",
                "_field_ts": {
                    "status": [new, "RManov", 116639670793142275, "evt-status"],
                    "section": [new, "RManov", 116639670793142276, "evt-section"],
                    "priority": [new, "RManov", 116639670793142277, "evt-priority"],
                    "due_date": [new, "RManov", 116639670793142278, "evt-due"],
                },
            }
        ),
        encoding="utf-8",
    )
    (bridge_dir / "shared.json").write_text(
        json.dumps({"version": 4, "memory_events": []}),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        bridge_server,
        "_ensure_bridge_repo_ready",
        lambda repo: (True, None),
    )
    monkeypatch.setattr(bridge_server, "_git", lambda *args: _cp(args))

    result = json.loads(bridge_server.bridge_pull.fn())

    with _db_conn(db_path) as conn:
        row = conn.execute(
            "SELECT status, priority, section, due_date FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        conflicts = conn.execute(
            "SELECT field_name, winner, rationale FROM memory_conflicts "
            "WHERE aggregate_kind='task' AND aggregate_id=? "
            "ORDER BY field_name",
            (task_id,),
        ).fetchall()

    assert result["updated_tasks"] == 0
    assert dict(row) == {
        "status": "archived",
        "priority": "low",
        "section": "done",
        "due_date": None,
    }
    assert {c["field_name"] for c in conflicts} == {
        "due_date",
        "priority",
        "section",
        "status",
    }
    assert {c["winner"] for c in conflicts} == {"guard_local"}
    assert all("hidden terminal" in c["rationale"] for c in conflicts)


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


def test_bridge_push_git_sync_failure_fails_closed_without_reset(bridge_env, monkeypatch):
    _db_path, _bridge_dir = bridge_env
    git_calls = []

    monkeypatch.setattr(
        bridge_server,
        "_ensure_bridge_repo_ready",
        lambda repo: (True, None),
    )
    monkeypatch.setattr(
        bridge_server,
        "_ensure_bridge_git_identity",
        lambda repo: {"changed": False},
    )

    def fake_git(*args):
        git_calls.append(args)
        if args[:3] == ("fetch", "origin", "main"):
            return _cp(args, returncode=1, stderr="CONFLICT (content): shared.json")
        raise AssertionError(f"Unexpected git call: {args}")

    monkeypatch.setattr(bridge_server, "_git", fake_git)

    result = json.loads(bridge_server.bridge_push.fn())

    assert result["blocked_by_repo_state"] is True
    assert "CONFLICT" in result["error"]
    assert not any(args and args[0] == "pull" for args in git_calls)
    assert not any(args[:2] == ("reset", "--hard") for args in git_calls)
    assert not any(args[:2] == ("rebase", "--abort") for args in git_calls)


def test_bridge_push_diverged_history_blocks_before_rebase(bridge_env, monkeypatch):
    _db_path, _bridge_dir = bridge_env
    git_calls = []

    monkeypatch.setattr(
        bridge_server,
        "_ensure_bridge_repo_ready",
        lambda repo: (True, None),
    )
    monkeypatch.setattr(
        bridge_server,
        "_ensure_bridge_git_identity",
        lambda repo: {"changed": False},
    )

    def fake_git(*args):
        git_calls.append(args)
        if args[:3] == ("fetch", "origin", "main"):
            return _cp(args)
        if args == ("rev-parse", "HEAD"):
            return _cp(args, stdout="local-sha\n")
        if args == ("rev-parse", "origin/main"):
            return _cp(args, stdout="remote-sha\n")
        if args == ("merge-base", "HEAD", "origin/main"):
            return _cp(args, stdout="base-sha\n")
        raise AssertionError(f"Unexpected git call: {args}")

    monkeypatch.setattr(bridge_server, "_git", fake_git)

    result = json.loads(bridge_server.bridge_push.fn())

    assert result["blocked_by_repo_state"] is True
    assert "diverged" in result["error"]
    assert not any(args and args[0] == "pull" for args in git_calls)
    assert not any(args[:2] == ("rebase", "--abort") for args in git_calls)


def test_bridge_pull_git_failure_blocks_import(bridge_env, monkeypatch):
    _db_path, _bridge_dir = bridge_env
    monkeypatch.setattr(
        bridge_server,
        "_ensure_bridge_repo_ready",
        lambda repo: (True, None),
    )
    monkeypatch.setattr(
        bridge_server,
        "_git",
        lambda *args: _cp(args, returncode=1, stderr="fatal: unresolved conflict"),
    )

    result = json.loads(bridge_server.bridge_pull.fn())

    assert result["blocked_by_repo_state"] is True
    assert result["git_pull_failed"] is True
    assert "unresolved conflict" in result["error"]


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
    assert any(args[:2] == ("add", "-f") for args in git_calls)
    assert not any(args[:2] == ("add", "-f") and "extended_memory/" in args for args in git_calls)


def test_bridge_push_git_add_failure_fails_closed_without_commit_or_push(
    bridge_env, monkeypatch
):
    _db_path, _bridge_dir = bridge_env
    git_calls = []

    def fake_git(*args):
        git_calls.append(args)
        if args[0] == "add":
            return _cp(args, returncode=128, stderr="fatal: pathspec failed")
        if args[0] in {"commit", "push", "status"}:
            raise AssertionError(f"git {args[0]} must not run after add failure")
        return _cp(args)

    monkeypatch.setattr(
        bridge_server,
        "_ensure_bridge_repo_ready",
        lambda repo: (True, None),
    )
    monkeypatch.setattr(bridge_server, "_git", fake_git)

    result = json.loads(bridge_server.bridge_push.fn(force=True))

    assert result["pushed_to_remote"] is False
    assert result["git_add_failed"] is True
    assert "pathspec failed" in result["error"]
    assert any(args[0] == "add" for args in git_calls)
    assert not any(args[0] in {"commit", "push", "status"} for args in git_calls)


def test_bridge_push_shared_js_generation_failure_fails_closed(
    bridge_env, monkeypatch
):
    _db_path, _bridge_dir = bridge_env
    git_calls = []

    def fake_git(*args):
        git_calls.append(args)
        if args[0] in {"add", "commit", "push", "status"}:
            raise AssertionError(f"git {args[0]} must not run after shared.js failure")
        return _cp(args)

    monkeypatch.setattr(
        bridge_server,
        "_ensure_bridge_repo_ready",
        lambda repo: (True, None),
    )
    monkeypatch.setattr(bridge_server, "_git", fake_git)
    monkeypatch.setattr(
        bridge_server,
        "_write_shared_js",
        lambda *args, **kwargs: "bridge shared.js generation failed: denied",
    )

    result = json.loads(bridge_server.bridge_push.fn(force=True))

    assert result["pushed_to_remote"] is False
    assert result["generated_file_failed"] is True
    assert "shared.js generation failed" in result["error"]
    assert not any(args[0] in {"add", "commit", "push", "status"} for args in git_calls)


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

    monkeypatch.setattr(
        bridge_server, "_ensure_bridge_repo_ready", lambda repo: (True, None)
    )
    monkeypatch.setattr(bridge_server, "_git", lambda *args: _cp(args))

    result = json.loads(bridge_server.bridge_pull.fn())

    with _db_conn(db_path) as conn:
        row = conn.execute(
            "SELECT title FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()

    assert result["new_tasks"] == 1
    assert row["title"] == "Legacy fallback task"


def test_bridge_pull_shared_json_ignores_older_mixed_offset_remote_task(
    bridge_env, monkeypatch
):
    db_path, bridge_dir = bridge_env
    task_id = "task-legacy-offset"
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

    (bridge_dir / "index.json").write_text("{bad json", encoding="utf-8")
    (bridge_dir / "shared.json").write_text(
        json.dumps(
            {
                "tasks": [
                    {
                        "id": task_id,
                        "title": "Remote stale task",
                        "description": "Remote stale description",
                        "status": "not_started",
                        "priority": "medium",
                        "section": "inbox",
                        "created_at": remote_ts,
                        "updated_at": remote_ts,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        bridge_server, "_ensure_bridge_repo_ready", lambda repo: (True, None)
    )
    monkeypatch.setattr(bridge_server, "_git", lambda *args: _cp(args))

    result = json.loads(bridge_server.bridge_pull.fn())

    with _db_conn(db_path) as conn:
        row = conn.execute(
            "SELECT title, description, updated_at FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()

    assert result["updated_tasks"] == 0
    assert row["title"] == "Local task"
    assert row["description"] == "Local description"
    assert row["updated_at"] == local_ts


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

    monkeypatch.setattr(
        bridge_server, "_ensure_bridge_repo_ready", lambda repo: (True, None)
    )
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
            (
                "task-1",
                "Synced task",
                "old desc",
                "not_started",
                "medium",
                "inbox",
                old,
                old,
            ),
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
            return _cp(
                args, stdout="M shared.json\nM index.json\nM entities_index.json\n"
            )
        return _cp(args)

    monkeypatch.setattr(
        bridge_server, "_ensure_bridge_repo_ready", lambda repo: (True, None)
    )
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

    monkeypatch.setattr(
        bridge_server, "_ensure_bridge_repo_ready", lambda repo: (True, None)
    )
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

    monkeypatch.setattr(
        bridge_server, "_ensure_bridge_repo_ready", lambda repo: (True, None)
    )
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


def test_bridge_push_shared_payload_uses_canonical_task_export_columns(
    bridge_env, monkeypatch
):
    db_path, bridge_dir = bridge_env
    created = "2026-03-29T08:00:00+00:00"
    reminder = "2026-03-30T09:00:00+00:00"
    publish_requested = "2026-03-29T09:00:00+00:00"
    with _db_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO tasks (id, title, status, priority, section, reminder_at, "
            "visibility, publish_requested_at, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "task-canonical-cols",
                "Canonical columns",
                "not_started",
                "medium",
                "inbox",
                reminder,
                "private",
                publish_requested,
                created,
                created,
            ),
        )

    def fake_git(*args):
        if args == ("status", "--porcelain"):
            return _cp(args, stdout="M shared.json\nM index.json\n")
        return _cp(args)

    monkeypatch.setattr(
        bridge_server, "_ensure_bridge_repo_ready", lambda repo: (True, None)
    )
    monkeypatch.setattr(bridge_server, "_git", fake_git)

    result = json.loads(bridge_server.bridge_push.fn(force=True))
    payload = json.loads((bridge_dir / "shared.json").read_text(encoding="utf-8"))
    task = next(item for item in payload["tasks"] if item["id"] == "task-canonical-cols")

    assert result["pushed_to_remote"] is True
    assert task["reminder_at"] == reminder
    assert task["visibility"] == "private"
    assert task["publish_requested_at"] == publish_requested


def test_bridge_push_forces_full_task_export_when_index_would_reference_missing_file(
    bridge_env, monkeypatch
):
    db_path, bridge_dir = bridge_env
    old = "2026-03-29T08:00:00+00:00"
    with _db_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO tasks (id, title, description, status, priority, section, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "task-missing-file",
                "Missing file task",
                "description must survive",
                "not_started",
                "medium",
                "inbox",
                old,
                old,
            ),
        )
        conn.execute(
            "INSERT OR REPLACE INTO bridge_meta(key, value) VALUES('last_push_at', ?)",
            ("2099-01-01T00:00:00+00:00",),
        )

    def fake_git(*args):
        if args == ("status", "--porcelain"):
            return _cp(args, stdout="M shared.json\nM index.json\nM tasks/task-missing-file.json\n")
        return _cp(args)

    monkeypatch.setattr(
        bridge_server, "_ensure_bridge_repo_ready", lambda repo: (True, None)
    )
    monkeypatch.setattr(bridge_server, "_git", fake_git)

    result = json.loads(bridge_server.bridge_push.fn(force=True))
    task_file = bridge_dir / "tasks" / "task-missing-file.json"

    assert result["pushed_to_remote"] is True
    assert task_file.is_file()
    assert json.loads(task_file.read_text(encoding="utf-8"))["description"] == (
        "description must survive"
    )


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

    monkeypatch.setattr(
        bridge_server, "_ensure_bridge_repo_ready", lambda repo: (True, None)
    )
    monkeypatch.setattr(bridge_server, "_git", lambda *args: _cp(args))

    result = json.loads(bridge_server.bridge_pull.fn())

    with _db_conn(db_path) as conn:
        pending = conn.execute(
            "SELECT COUNT(*) AS cnt FROM pending_shared_entities"
        ).fetchone()["cnt"]
        ratings = conn.execute(
            "SELECT COUNT(*) AS cnt FROM knowledge_ratings"
        ).fetchone()["cnt"]

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

    monkeypatch.setattr(
        bridge_server, "_ensure_bridge_repo_ready", lambda repo: (True, None)
    )
    monkeypatch.setattr(bridge_server, "_git", lambda *args: _cp(args))

    result = json.loads(bridge_server.bridge_pull.fn())

    with _db_conn(db_path) as conn:
        pending = conn.execute(
            "SELECT COUNT(*) AS cnt FROM pending_shared_entities"
        ).fetchone()["cnt"]
        ratings = conn.execute(
            "SELECT COUNT(*) AS cnt FROM knowledge_ratings"
        ).fetchone()["cnt"]

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
