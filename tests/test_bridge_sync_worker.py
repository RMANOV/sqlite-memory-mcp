"""Focused tests for bridge sync safety checks."""

import json
import os
import sqlite3
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import bridge_sync_worker
from bridge_sync_worker import _auto_heal_sync_safety, _check_sync_safety
import db_utils
from schema import init_db


@pytest.fixture
def setup(tmp_path):
    db_path = str(tmp_path / "test.db")
    bridge_dir = str(tmp_path / "bridge")
    os.makedirs(os.path.join(bridge_dir, "tasks"), exist_ok=True)

    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            description TEXT DEFAULT NULL,
            notes TEXT DEFAULT NULL
        )
        """
    )
    yield conn, bridge_dir
    conn.close()


def test_check_sync_safety_flags_drastic_description_shrink(setup):
    conn, bridge_dir = setup
    task_id = "task-001"
    conn.execute(
        "INSERT INTO tasks (id, title, description, notes) VALUES (?, ?, ?, ?)",
        (task_id, "Big task", "x" * 200, None),
    )
    with open(
        os.path.join(bridge_dir, "tasks", f"{task_id}.json"),
        "w",
        encoding="utf-8",
    ) as fh:
        json.dump(
            {
                "id": task_id,
                "title": "Big task",
                "description": "x" * 2400,
                "notes": None,
            },
            fh,
        )

    safety = _check_sync_safety(conn, bridge_dir)

    assert safety["is_safe"] is False
    assert safety["descriptions_shrunk"] == 1
    assert safety["examples"][0]["task_id"] == task_id


def test_auto_heal_sync_safety_restores_bridge_content(tmp_path):
    db_path = str(tmp_path / "memory.db")
    bridge_dir = str(tmp_path / "bridge")
    os.makedirs(os.path.join(bridge_dir, "tasks"), exist_ok=True)
    init_db(db_path)

    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        now = db_utils.now_iso()
        conn.execute(
            "INSERT INTO tasks (id, title, description, status, section, priority, type, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "task-restore",
                "Needs restore",
                "x" * 500,
                "not_started",
                "inbox",
                "medium",
                "task",
                now,
                now,
            ),
        )
        with open(
            os.path.join(bridge_dir, "tasks", "task-restore.json"),
            "w",
            encoding="utf-8",
        ) as fh:
            json.dump(
                {
                    "id": "task-restore",
                    "title": "Needs restore",
                    "description": "x" * 5000,
                    "notes": "Bridge notes",
                    "updated_at": now,
                },
                fh,
            )

        repairs = _auto_heal_sync_safety(conn, bridge_dir)
        row = conn.execute(
            "SELECT description, notes FROM tasks WHERE id = 'task-restore'"
        ).fetchone()
        safety = _check_sync_safety(conn, bridge_dir)

        assert repairs["tasks_touched"] == 1
        assert repairs["restored_descriptions"] == 1
        assert repairs["restored_notes"] == 1
        assert len(row["description"]) == 5000
        assert row["notes"] == "Bridge notes"
        assert safety["is_safe"] is True
    finally:
        conn.close()


def _cp(args, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(["git", *args], returncode, stdout, stderr)


def test_bridge_repo_ready_blocks_user_managed_dirty_files(monkeypatch):
    calls = []

    def fake_git_run(repo_dir, *args, timeout=30):
        calls.append(args)
        if args == ("rev-parse", "--abbrev-ref", "HEAD"):
            return _cp(args, stdout="main\n")
        if args == ("status", "--porcelain"):
            return _cp(args, stdout=" M index.html\n")
        raise AssertionError(f"Unexpected git call: {args}")

    monkeypatch.setattr(db_utils, "git_run", fake_git_run)

    ok, msg = db_utils.ensure_bridge_repo_ready("bridge")

    assert ok is False
    assert "index.html" in msg
    assert (
        "checkout",
        "--",
        "shared.json",
        "shared.js",
        "index.json",
        "entities_index.json",
        "tasks",
        "entities",
        "public_knowledge",
    ) not in calls


def test_bridge_repo_ready_discards_generated_artifacts(monkeypatch):
    calls = []
    statuses = iter(
        [
            _cp(
                ("status", "--porcelain"), stdout=" M shared.json\n?? tasks/new.json\n"
            ),
            _cp(("status", "--porcelain"), stdout=""),
        ]
    )

    def fake_git_run(repo_dir, *args, timeout=30):
        calls.append(args)
        if args == ("rev-parse", "--abbrev-ref", "HEAD"):
            return _cp(args, stdout="main\n")
        if args == ("status", "--porcelain"):
            return next(statuses)
        if args[:2] in {("checkout", "--"), ("clean", "-fd")}:
            return _cp(args)
        raise AssertionError(f"Unexpected git call: {args}")

    monkeypatch.setattr(db_utils, "git_run", fake_git_run)

    ok, msg = db_utils.ensure_bridge_repo_ready("bridge")

    assert ok is True
    assert msg is None
    assert any(args[:2] == ("checkout", "--") for args in calls)
    assert any(args[:2] == ("clean", "-fd") for args in calls)


def test_bridge_repo_ready_discards_legacy_lock_file(monkeypatch):
    calls = []
    statuses = iter(
        [
            _cp(("status", "--porcelain"), stdout="?? .bridge_sync.lock\n"),
            _cp(("status", "--porcelain"), stdout=""),
        ]
    )

    def fake_git_run(repo_dir, *args, timeout=30):
        calls.append(args)
        if args == ("rev-parse", "--abbrev-ref", "HEAD"):
            return _cp(args, stdout="main\n")
        if args == ("status", "--porcelain"):
            return next(statuses)
        if args[:2] in {("checkout", "--"), ("clean", "-fd")}:
            return _cp(args)
        raise AssertionError(f"Unexpected git call: {args}")

    monkeypatch.setattr(db_utils, "git_run", fake_git_run)

    ok, msg = db_utils.ensure_bridge_repo_ready("bridge")

    assert ok is True
    assert msg is None
    assert any(
        ".bridge_sync.lock" in args for args in calls if args[:2] == ("clean", "-fd")
    )


def test_bridge_repo_ready_discards_stale_generated_tmp_files(monkeypatch):
    calls = []
    statuses = iter(
        [
            _cp(
                ("status", "--porcelain"),
                stdout=" M index.json\n M entities_index.json\n?? shared.tmp\n",
            ),
            _cp(("status", "--porcelain"), stdout=""),
        ]
    )

    def fake_git_run(repo_dir, *args, timeout=30):
        calls.append(args)
        if args == ("rev-parse", "--abbrev-ref", "HEAD"):
            return _cp(args, stdout="main\n")
        if args == ("status", "--porcelain"):
            return next(statuses)
        if args[:2] in {("checkout", "--"), ("clean", "-fd")}:
            return _cp(args)
        raise AssertionError(f"Unexpected git call: {args}")

    monkeypatch.setattr(db_utils, "git_run", fake_git_run)

    ok, msg = db_utils.ensure_bridge_repo_ready("bridge")

    assert ok is True
    assert msg is None
    clean_calls = [args for args in calls if args[:2] == ("clean", "-fd")]
    assert clean_calls
    assert any("shared.tmp" in args for args in clean_calls)


def test_tmp_write_path_uses_distinct_target_names(tmp_path):
    shared_json = tmp_path / "shared.json"
    shared_js = tmp_path / "shared.js"

    assert bridge_sync_worker._tmp_write_path(shared_json).name == "shared.json.tmp"
    assert bridge_sync_worker._tmp_write_path(shared_js).name == "shared.js.tmp"


def test_bridge_repo_ready_recovers_detached_head(monkeypatch):
    calls = []

    def fake_git_run(repo_dir, *args, timeout=30):
        calls.append(args)
        if args == ("rev-parse", "--abbrev-ref", "HEAD"):
            return _cp(args, stdout="HEAD\n")
        if args == ("checkout", "main"):
            return _cp(args)
        if args == ("status", "--porcelain"):
            return _cp(args, stdout="")
        raise AssertionError(f"Unexpected git call: {args}")

    monkeypatch.setattr(db_utils, "git_run", fake_git_run)

    ok, msg = db_utils.ensure_bridge_repo_ready("bridge")

    assert ok is True
    assert msg is None
    assert ("checkout", "main") in calls


def test_ensure_bridge_git_identity_copies_source_repo_identity(monkeypatch):
    calls = []

    def fake_git_run(repo_dir, *args, timeout=30):
        calls.append((repo_dir, args))
        if repo_dir == "bridge" and args == ("config", "--get", "user.name"):
            return _cp(args, returncode=1, stderr="missing")
        if repo_dir == "bridge" and args == ("config", "--get", "user.email"):
            return _cp(args, stdout="r.manov@gmail.com\n")
        if repo_dir == "source" and args == ("config", "--get", "user.name"):
            return _cp(args, stdout="RMANOV\n")
        if repo_dir == "source" and args == ("config", "--get", "user.email"):
            return _cp(args, stdout="96174405+RMANOV@users.noreply.github.com\n")
        if repo_dir == "bridge" and args[:2] == ("config", "user.name"):
            return _cp(args)
        if repo_dir == "bridge" and args[:2] == ("config", "user.email"):
            return _cp(args)
        return _cp(args, returncode=1, stderr="unexpected")

    monkeypatch.setattr(db_utils, "git_run", fake_git_run)

    result = db_utils.ensure_bridge_git_identity("bridge", source_repo_dir="source")

    assert result["changed"] is True
    assert result["user_name"] == "RMANOV"
    assert result["user_email"] == "96174405+RMANOV@users.noreply.github.com"
    assert ("bridge", ("config", "user.name", "RMANOV")) in calls
    assert (
        "bridge",
        ("config", "user.email", "96174405+RMANOV@users.noreply.github.com"),
    ) in calls


def test_git_retry_returns_timeout_result(monkeypatch):
    def fake_git_run(repo_dir, *args, timeout=30):
        raise subprocess.TimeoutExpired(["git", *args], timeout)

    monkeypatch.setattr(db_utils, "git_run", fake_git_run)

    result = db_utils.git_retry("bridge", "push", max_retries=1, timeout=7)

    assert result.returncode == 124
    assert "timed out after 7s" in result.stderr


def test_bridge_sync_worker_pull_only_skips_export_and_push(tmp_path, monkeypatch):
    db_path = str(tmp_path / "memory.db")
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    init_db(db_path)

    git_calls = []

    def fake_git_retry(repo_dir, *args, max_retries=3, timeout=30):
        git_calls.append(args)
        if args[:3] == ("pull", "--rebase", "--autostash"):
            return _cp(args)
        raise AssertionError(f"Unexpected git_retry call: {args}")

    monkeypatch.setattr(
        bridge_sync_worker, "ensure_bridge_repo_ready", lambda repo: (True, None)
    )
    monkeypatch.setattr(bridge_sync_worker, "git_retry", fake_git_retry)
    monkeypatch.setattr(
        bridge_sync_worker, "load_remote_tasks_for_merge", lambda *a, **k: ([], True)
    )
    monkeypatch.setattr(
        bridge_sync_worker,
        "import_remote_bridge_data",
        lambda *a, **k: {"entities": 0, "relations": 0, "ratings": 0},
    )
    monkeypatch.setattr(
        bridge_sync_worker, "sync_task_attachments_from_remote", lambda *a, **k: (0, 0)
    )

    result = bridge_sync_worker.main(
        db_path=db_path,
        bridge_repo=str(bridge_dir),
        pull_only=True,
    )

    assert result["pull_only"] is True
    assert result["pushed"] is False
    assert git_calls == [("pull", "--rebase", "--autostash")]


def test_bridge_sync_worker_writes_and_stages_shared_js(tmp_path, monkeypatch):
    db_path = str(tmp_path / "memory.db")
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    init_db(db_path)

    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    now = db_utils.now_iso()
    conn.execute(
        "INSERT INTO tasks (id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
        ("task-001", "Task", now, now),
    )
    conn.close()

    git_calls = []

    def fake_git_run(repo_dir, *args, timeout=30):
        git_calls.append(args)
        return _cp(args)

    def fake_git_retry(repo_dir, *args, max_retries=3, timeout=30):
        git_calls.append(args)
        return _cp(args)

    monkeypatch.setattr(
        bridge_sync_worker, "ensure_bridge_repo_ready", lambda repo: (True, None)
    )
    monkeypatch.setattr(bridge_sync_worker, "git_run", fake_git_run)
    monkeypatch.setattr(bridge_sync_worker, "git_retry", fake_git_retry)
    monkeypatch.setattr(
        bridge_sync_worker.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, "", ""),
    )

    result = bridge_sync_worker.main(
        force=True, bridge_repo=str(bridge_dir), db_path=db_path
    )

    shared_js = (bridge_dir / "shared.js").read_text(encoding="utf-8")

    assert result["pushed"] is True
    assert shared_js.startswith("window.__BRIDGE_DATA__ = ")
    assert any(args[0] == "add" and "shared.js" in args for args in git_calls)


def test_bridge_sync_worker_auto_heals_shrink_instead_of_blocking(
    tmp_path, monkeypatch
):
    db_path = str(tmp_path / "memory.db")
    bridge_dir = tmp_path / "bridge"
    (bridge_dir / "tasks").mkdir(parents=True)
    init_db(db_path)

    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    now = db_utils.now_iso()
    conn.execute(
        "INSERT INTO tasks (id, title, description, status, section, priority, type, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "task-001",
            "Shrink task",
            "x" * 500,
            "not_started",
            "inbox",
            "medium",
            "task",
            now,
            now,
        ),
    )
    conn.close()

    with open(bridge_dir / "tasks" / "task-001.json", "w", encoding="utf-8") as fh:
        json.dump(
            {
                "id": "task-001",
                "title": "Shrink task",
                "description": "x" * 5000,
                "notes": None,
                "updated_at": now,
            },
            fh,
        )

    git_calls = []

    def fake_git_run(repo_dir, *args, timeout=30):
        git_calls.append(args)
        return _cp(args)

    def fake_git_retry(repo_dir, *args, max_retries=3, timeout=30):
        git_calls.append(args)
        return _cp(args)

    monkeypatch.setattr(
        bridge_sync_worker, "ensure_bridge_repo_ready", lambda repo: (True, None)
    )
    monkeypatch.setattr(bridge_sync_worker, "git_run", fake_git_run)
    monkeypatch.setattr(bridge_sync_worker, "git_retry", fake_git_retry)
    monkeypatch.setattr(
        bridge_sync_worker.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, "", ""),
    )

    result = bridge_sync_worker.main(
        force=False,
        bridge_repo=str(bridge_dir),
        db_path=db_path,
    )

    healed_conn = sqlite3.connect(db_path, isolation_level=None)
    healed_conn.row_factory = sqlite3.Row
    try:
        row = healed_conn.execute(
            "SELECT description FROM tasks WHERE id = 'task-001'"
        ).fetchone()
    finally:
        healed_conn.close()

    assert result["pushed"] is True
    assert result.get("blocked_by_safety") is not True
    assert len(row["description"]) == 5000


def test_export_tasks_prefers_authoritative_status_event(tmp_path):
    db_path = str(tmp_path / "memory.db")
    init_db(db_path)

    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    task_ts = "2026-04-01T06:09:06.748385+00:00"
    status_ts = "2026-03-31T16:06:01.347617+00:00"
    conn.execute(
        "INSERT INTO tasks (id, title, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        ("task-001", "Task", "not_started", task_ts, task_ts),
    )
    db_utils.upsert_field_versions(
        conn,
        "task-001",
        ["status"],
        timestamp=status_ts,
        machine_id="fedora",
        old_values={"status": "not_started"},
        new_values={"status": "done"},
        tool_name="task_tray.mark_done",
    )

    tasks = bridge_sync_worker._export_tasks(conn)

    assert tasks[0]["status"] == "done"
    conn.close()


def test_bridge_sync_worker_exports_memory_ledger_sections(tmp_path, monkeypatch):
    db_path = str(tmp_path / "memory.db")
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    init_db(db_path)

    git_calls = []

    def fake_git_run(repo_dir, *args, timeout=30):
        git_calls.append(args)
        return _cp(args)

    def fake_git_retry(repo_dir, *args, max_retries=3, timeout=30):
        git_calls.append(args)
        return _cp(args)

    monkeypatch.setattr(
        bridge_sync_worker, "ensure_bridge_repo_ready", lambda repo: (True, None)
    )
    monkeypatch.setattr(bridge_sync_worker, "git_run", fake_git_run)
    monkeypatch.setattr(bridge_sync_worker, "git_retry", fake_git_retry)
    monkeypatch.setattr(
        bridge_sync_worker.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, "", ""),
    )

    result = bridge_sync_worker.main(
        force=True, bridge_repo=str(bridge_dir), db_path=db_path
    )
    payload = json.loads((bridge_dir / "shared.json").read_text(encoding="utf-8"))

    assert result["pushed"] is True
    assert "context_chunks" in payload
    assert "context_annotations" in payload
    assert "context_questions" in payload
    assert "candidate_claims" in payload
    assert "claim_evidence" in payload
    assert "canonical_facts" in payload
    assert "provenance_links" in payload
    assert "knowledge_links" in payload
    assert "memory_events" in payload
    assert "memory_audit_issues" in payload
    assert "memory_artifacts" in payload
    assert "memory_conflicts" in payload
    assert "memory_audit_state" in payload
    assert "memory_health" in payload
    assert payload["memory_events"] == []


def test_repo_sync_lock_lives_outside_bridge_repo(tmp_path):
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()

    lock = bridge_sync_worker._RepoSyncLock(str(bridge_dir))

    assert lock._path.parent == bridge_dir.parent
    assert lock._path.parent != bridge_dir
    assert lock._path.name == ".bridge.sync.lock"


def test_bridge_sync_worker_merges_ui_profile_and_pushes_without_db_changes(
    tmp_path, monkeypatch
):
    db_path = str(tmp_path / "memory.db")
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    init_db(db_path)

    now = db_utils.now_iso()
    with sqlite3.connect(db_path, isolation_level=None) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute(
            "INSERT INTO bridge_meta(key, value) VALUES('last_push_at', ?)",
            (now,),
        )
    (bridge_dir / "shared.json").write_text(
        json.dumps(
            {
                "ui_profiles": {
                    "other-host": {"theme": "blue", "updated_at": now},
                }
            }
        ),
        encoding="utf-8",
    )

    git_calls = []

    def fake_git_run(repo_dir, *args, timeout=30):
        git_calls.append(args)
        return _cp(args)

    def fake_git_retry(repo_dir, *args, max_retries=3, timeout=30):
        git_calls.append(args)
        return _cp(args)

    monkeypatch.setattr(
        bridge_sync_worker, "ensure_bridge_repo_ready", lambda repo: (True, None)
    )
    monkeypatch.setattr(bridge_sync_worker, "git_run", fake_git_run)
    monkeypatch.setattr(bridge_sync_worker, "git_retry", fake_git_retry)
    monkeypatch.setattr(
        bridge_sync_worker.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, "", ""),
    )
    monkeypatch.setattr(bridge_sync_worker.socket, "gethostname", lambda: "test-host")

    result = bridge_sync_worker.main(
        force=False,
        bridge_repo=str(bridge_dir),
        db_path=db_path,
        ui_profile={"theme": "amber", "updated_at": now},
    )

    shared = json.loads((bridge_dir / "shared.json").read_text(encoding="utf-8"))

    assert result.get("skipped") is not True
    assert result["pushed"] is True
    assert shared["ui_profiles"]["other-host"]["theme"] == "blue"
    assert shared["ui_profiles"]["test-host"]["theme"] == "amber"
    assert any(args[0] == "commit" for args in git_calls)


def test_bridge_sync_worker_records_last_push_at_from_payload_timestamp(
    tmp_path, monkeypatch
):
    db_path = str(tmp_path / "memory.db")
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    init_db(db_path)

    def fake_git_run(repo_dir, *args, timeout=30):
        return _cp(args)

    def fake_git_retry(repo_dir, *args, max_retries=3, timeout=30):
        return _cp(args)

    monkeypatch.setattr(
        bridge_sync_worker, "ensure_bridge_repo_ready", lambda repo: (True, None)
    )
    monkeypatch.setattr(bridge_sync_worker, "git_run", fake_git_run)
    monkeypatch.setattr(bridge_sync_worker, "git_retry", fake_git_retry)
    monkeypatch.setattr(
        bridge_sync_worker.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, "", ""),
    )

    result = bridge_sync_worker.main(
        force=True, bridge_repo=str(bridge_dir), db_path=db_path
    )
    payload = json.loads((bridge_dir / "shared.json").read_text(encoding="utf-8"))

    with sqlite3.connect(db_path, isolation_level=None) as conn:
        conn.row_factory = sqlite3.Row
        stored = conn.execute(
            "SELECT value FROM bridge_meta WHERE key = 'last_push_at'"
        ).fetchone()["value"]

    assert result["pushed"] is True
    assert stored == payload["pushed_at"]
