"""Focused tests for bridge sync safety checks."""

import json
import os
import sqlite3
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import bridge_sync_worker
from bridge_sync_worker import (
    _auto_heal_sync_safety,
    _check_sync_safety,
    _deploy_pages_privacy_shell,
    _load_bridge_task_snapshots,
    _pages_publish_dir,
)
import db_utils
from schema import init_db


def _write_pages_privacy_shell(bridge_dir):
    publish_dir = bridge_dir / "pages_public"
    publish_dir.mkdir()
    (publish_dir / "index.html").write_text(
        "<html>privacy-shell-v1</html>", encoding="utf-8"
    )
    (publish_dir / "_headers").write_text("/*\n  Cache-Control: no-store\n")
    return publish_dir


def test_pages_publish_dir_is_an_exact_data_free_allowlist(tmp_path):
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    publish_dir = _write_pages_privacy_shell(bridge_dir)

    resolved, error = _pages_publish_dir(str(bridge_dir))

    assert resolved == publish_dir
    assert error is None
    (publish_dir / "shared.json").write_text("{}", encoding="utf-8")
    resolved, error = _pages_publish_dir(str(bridge_dir))
    assert resolved is None
    assert "unexpected=['shared.json']" in error


def test_pages_deploy_never_uses_private_bridge_root(tmp_path, monkeypatch):
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    publish_dir = _write_pages_privacy_shell(bridge_dir)
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(bridge_sync_worker.subprocess, "run", fake_run)

    result = _deploy_pages_privacy_shell(str(bridge_dir))

    assert result == {"deployed": True, "message": None}
    assert calls[0][0][3] == str(publish_dir)
    assert calls[0][0][3] != str(bridge_dir)


def test_pages_deploy_fails_closed_without_privacy_shell(tmp_path):
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()

    result = _deploy_pages_privacy_shell(str(bridge_dir))

    assert result["deployed"] is False
    assert result["blocked_private_source"] is True


def test_memory_events_stream_export_is_byte_equivalent_and_atomic(tmp_path):
    db_path = str(tmp_path / "memory.db")
    bridge_dir = str(tmp_path / "bridge")
    init_db(db_path)
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        rows = [
            (
                f"event-{i}",
                "field.set",
                "task",
                f"task-{i}",
                "description",
                "system",
                None,
                "fedora",
                "test",
                i,
                f"2026-07-19T10:00:0{i}+00:00",
                None,
                f"стойност {i}",
                json.dumps({"i": i}),
                None,
                "test",
                f"ref-{i}",
                "откъс",
                0,
                5,
            )
            for i in range(1, 4)
        ]
        conn.executemany(
            "INSERT INTO memory_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        expected = db_utils.json_dumps(db_utils.export_memory_events(conn))
        rel_path, count = db_utils.write_memory_events_file_streaming(conn, bridge_dir)
    finally:
        conn.close()

    output = tmp_path / "bridge" / rel_path
    assert count == 3
    assert output.read_text(encoding="utf-8") == expected
    assert not output.with_suffix(".json.tmp").exists()


def test_extended_writer_preserves_prestreamed_memory_events(tmp_path):
    bridge_dir = tmp_path / "bridge"
    event_path = bridge_dir / "extended_memory" / "memory_events.json"
    event_path.parent.mkdir(parents=True)
    event_path.write_text('[{"event_id":"sentinel"}]', encoding="utf-8")

    written = db_utils.write_extended_memory_files(
        str(bridge_dir),
        {"memory_events": [], "context_chunks": []},
        skip_keys={"memory_events"},
    )

    assert event_path.read_text(encoding="utf-8") == '[{"event_id":"sentinel"}]'
    assert "extended_memory/memory_events.json" not in written


def test_extended_export_can_omit_materialized_memory_events(tmp_path, monkeypatch):
    db_path = str(tmp_path / "memory.db")
    init_db(db_path)
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    monkeypatch.setattr(
        bridge_sync_worker,
        "export_memory_events",
        lambda _conn: (_ for _ in ()).throw(
            AssertionError("must stream, not materialize")
        ),
    )
    try:
        result = bridge_sync_worker._export_extended_memory(
            conn, include_memory_events=False
        )
    finally:
        conn.close()
    assert "memory_events" not in result


def test_streaming_json_array_parser_handles_tiny_chunks(tmp_path):
    path = tmp_path / "array.json"
    payload = [
        {"id": 1, "text": "кирилица"},
        {"id": 2, "nested": {"ok": True}},
    ]
    path.write_text(db_utils.json_dumps(payload), encoding="utf-8")
    assert list(db_utils._iter_json_array_file(path, chunk_size=5)) == payload


def test_streaming_event_import_keeps_only_required_and_causal_heads(tmp_path):
    db_path = str(tmp_path / "memory.db")
    init_db(db_path)
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row

    def event(event_id, kind, aggregate_id, field, clock):
        return {
            "event_id": event_id,
            "event_type": "field.set",
            "aggregate_kind": kind,
            "aggregate_id": aggregate_id,
            "field_name": field,
            "actor_type": "system",
            "actor_id": None,
            "machine_id": "peer-a",
            "tool_name": "test",
            "logical_clock": clock,
            "event_ts": f"2026-07-19T10:00:{clock:02d}+00:00",
            "old_value": None,
            "new_value": str(clock),
            "payload_json": None,
            "parent_event_id": None,
            "source_kind": None,
            "source_ref": None,
            "source_excerpt": None,
            "source_start": None,
            "source_end": None,
        }

    events = [
        event("status-old", "task", "task-1", "status", 1),
        event("explicit-description", "task", "task-1", "description", 2),
        event("unneeded-description", "task", "task-2", "description", 3),
        event("status-new", "task", "task-1", "status", 4),
        event("fact-old", "fact", "fact-1", None, 5),
        event("fact-new", "fact", "fact-1", None, 6),
    ]
    path = tmp_path / "memory_events.json"
    path.write_text(db_utils.json_dumps(events), encoding="utf-8")
    try:
        processed, subset = db_utils.import_memory_events_file_streaming(
            conn,
            path,
            required_event_ids={"explicit-description"},
            batch_size=2,
        )
        stored = conn.execute("SELECT COUNT(*) FROM memory_events").fetchone()[0]
    finally:
        conn.close()

    assert processed == stored == len(events)
    subset_ids = {item["event_id"] for item in subset}
    assert subset_ids == {"explicit-description", "status-new", "fact-new"}


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
            status TEXT NOT NULL DEFAULT 'not_started',
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


def test_auto_heal_sync_safety_preserves_archived_duplicate_redirect(tmp_path):
    db_path = str(tmp_path / "memory.db")
    bridge_dir = str(tmp_path / "bridge")
    os.makedirs(os.path.join(bridge_dir, "tasks"), exist_ok=True)
    init_db(db_path)

    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        now = db_utils.now_iso()
        local_stub = (
            "ARCHIVED DUPLICATE - DO NOT USE.\n\n"
            "Canonical profile: 05f2c42e. Old body removed. "
            "Do not cite this archived note except as redirect."
        )
        bridge_body = "# Old predictive profile\n" + ("UNKNOWN stale body. " * 250)
        conn.execute(
            "INSERT INTO tasks (id, title, description, status, section, priority, type, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "task-archived-duplicate",
                "ARCHIVED DUPLICATE - DO NOT USE - superseded by 05f2c42e",
                local_stub,
                "archived",
                "someday",
                "high",
                "note",
                now,
                now,
            ),
        )
        with open(
            os.path.join(bridge_dir, "tasks", "task-archived-duplicate.json"),
            "w",
            encoding="utf-8",
        ) as fh:
            json.dump(
                {
                    "id": "task-archived-duplicate",
                    "title": "Old predictive profile",
                    "description": bridge_body,
                    "notes": None,
                    "updated_at": now,
                },
                fh,
            )

        repairs = _auto_heal_sync_safety(conn, bridge_dir)
        row = conn.execute(
            "SELECT description FROM tasks WHERE id = 'task-archived-duplicate'"
        ).fetchone()
        safety = _check_sync_safety(conn, bridge_dir)

        assert repairs["tasks_touched"] == 0
        assert row["description"] == local_stub
        assert safety["is_safe"] is True
        assert safety["descriptions_shrunk"] == 0

        exported = db_utils.export_task_files(conn, bridge_dir)
        with open(
            os.path.join(bridge_dir, "tasks", "task-archived-duplicate.json"),
            encoding="utf-8",
        ) as fh:
            exported_task = json.load(fh)

        assert "task-archived-duplicate" in exported
        assert exported_task["description"] == local_stub
    finally:
        conn.close()


def test_archived_deduplication_prose_is_not_duplicate_redirect():
    task = {
        "title": "Lesson: deduplication strategy",
        "status": "archived",
        "description": "Use canonical entity merge to deduplicate old notes.",
        "notes": None,
    }

    assert db_utils.is_archived_duplicate_redirect_task(task) is False


def test_archived_non_redirect_notes_keep_bridge_shrink_protection(tmp_path):
    db_path = str(tmp_path / "memory.db")
    bridge_dir = str(tmp_path / "bridge")
    os.makedirs(os.path.join(bridge_dir, "tasks"), exist_ok=True)
    init_db(db_path)

    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        now = db_utils.now_iso()
        bridge_body = "# Archived lesson\n" + ("Preserved bridge evidence. " * 250)
        local_body = (
            "Lesson: deduplication strategy. Use canonical entity merge to deduplicate."
        )
        conn.execute(
            "INSERT INTO tasks (id, title, description, status, section, priority, type, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "task-archived-lesson",
                "Lesson: deduplication strategy",
                local_body,
                "archived",
                "someday",
                "medium",
                "note",
                now,
                now,
            ),
        )
        with open(
            os.path.join(bridge_dir, "tasks", "task-archived-lesson.json"),
            "w",
            encoding="utf-8",
        ) as fh:
            json.dump(
                {
                    "id": "task-archived-lesson",
                    "title": "Lesson: deduplication strategy",
                    "description": bridge_body,
                    "notes": None,
                    "updated_at": now,
                },
                fh,
            )

        repairs = _auto_heal_sync_safety(conn, bridge_dir)
        row = conn.execute(
            "SELECT description FROM tasks WHERE id = 'task-archived-lesson'"
        ).fetchone()
        assert repairs["tasks_touched"] == 1
        assert repairs["restored_descriptions"] == 1
        assert row["description"] == bridge_body

        local_export_body = "Archived note without redirect markers."
        conn.execute(
            "INSERT INTO tasks (id, title, description, status, section, priority, type, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "task-archived-export",
                "Archived ordinary note",
                local_export_body,
                "archived",
                "someday",
                "medium",
                "note",
                now,
                now,
            ),
        )
        with open(
            os.path.join(bridge_dir, "tasks", "task-archived-export.json"),
            "w",
            encoding="utf-8",
        ) as fh:
            json.dump(
                {
                    "id": "task-archived-export",
                    "title": "Archived ordinary note",
                    "description": bridge_body,
                    "notes": None,
                    "updated_at": now,
                },
                fh,
            )

        db_utils.export_task_files(conn, bridge_dir)
        with open(
            os.path.join(bridge_dir, "tasks", "task-archived-export.json"),
            encoding="utf-8",
        ) as fh:
            exported_task = json.load(fh)
        assert exported_task["description"] == bridge_body
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


def test_bridge_repo_ready_blocks_rebase_with_user_managed_changes(
    tmp_path, monkeypatch
):
    # E1: a left-behind rebase-merge is auto-abortable, BUT only when the working
    # tree carries no user-managed work. With a non-generated dirty file present,
    # auto-abort is SKIPPED (never discard user work) and the repo stays blocked.
    bridge_dir = tmp_path / "bridge"
    (bridge_dir / ".git" / "rebase-merge").mkdir(parents=True)
    calls = []

    def fake_git_run(repo_dir, *args, timeout=30):
        calls.append(args)
        if args == ("status", "--porcelain"):
            return _cp(args, stdout=" M user_module.py\n")
        raise AssertionError(f"Unexpected git call: {args}")

    monkeypatch.setattr(db_utils, "git_run", fake_git_run)

    ok, msg = db_utils.ensure_bridge_repo_ready(str(bridge_dir))

    assert ok is False
    assert "rebase-merge" in msg
    assert "blocked_by_repo_state preserved" in msg
    # Never aborted over user-managed work:
    assert ("rebase", "--abort") not in calls


def test_bridge_repo_ready_blocks_generated_conflict_without_reset(monkeypatch):
    calls = []

    def fake_git_run(repo_dir, *args, timeout=30):
        calls.append(args)
        if args == ("rev-parse", "--abbrev-ref", "HEAD"):
            return _cp(args, stdout="main\n")
        if args == ("status", "--porcelain"):
            return _cp(args, stdout="UU shared.json\n")
        raise AssertionError(f"Unexpected git call: {args}")

    monkeypatch.setattr(db_utils, "git_run", fake_git_run)

    ok, msg = db_utils.ensure_bridge_repo_ready("bridge")

    assert ok is False
    assert "generated" in msg
    assert "shared.json" in msg
    assert not any(args[:2] == ("rebase", "--abort") for args in calls)
    assert not any(args[:2] == ("reset", "--hard") for args in calls)


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


def test_bridge_repo_ready_discards_extended_memory_artifacts(monkeypatch):
    calls = []
    statuses = iter(
        [
            _cp(
                ("status", "--porcelain"),
                stdout=(
                    " M extended_memory/context_chunks.json\n"
                    "?? extended_memory/memory_events.json.tmp\n"
                ),
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
    assert any(
        "extended_memory" in args for args in calls if args[:2] == ("checkout", "--")
    )
    assert any(
        "extended_memory" in args for args in calls if args[:2] == ("clean", "-fd")
    )


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


def test_non_fast_forward_push_recovery_resets_generated_divergence(tmp_path):
    """A peer winning the push race must not permanently brick the checkout."""

    def git(cwd, *args, check=True):
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            text=True,
            capture_output=True,
            check=check,
        )

    remote = tmp_path / "remote.git"
    writer = tmp_path / "writer"
    local = tmp_path / "local"
    peer = tmp_path / "peer"
    git(tmp_path, "init", "--bare", str(remote))
    git(tmp_path, "clone", str(remote), str(writer))
    git(writer, "config", "user.name", "Bridge Test")
    git(writer, "config", "user.email", "bridge@example.test")
    git(writer, "config", "commit.gpgsign", "false")
    git(writer, "checkout", "-b", "main")
    (writer / "shared.json").write_text('{"version":1}\n', encoding="utf-8")
    git(writer, "add", "shared.json")
    git(writer, "commit", "-m", "base")
    git(writer, "push", "-u", "origin", "main")
    git(tmp_path, "clone", "--branch", "main", str(remote), str(local))
    git(tmp_path, "clone", "--branch", "main", str(remote), str(peer))
    for repo in (local, peer):
        git(repo, "config", "user.name", "Bridge Test")
        git(repo, "config", "user.email", "bridge@example.test")
        git(repo, "config", "commit.gpgsign", "false")

    (local / "local-generated.json").write_text("{}\n", encoding="utf-8")
    git(local, "add", "local-generated.json")
    git(local, "commit", "-m", "local generated export")
    local_generated_sha = git(local, "rev-parse", "HEAD").stdout.strip()

    (peer / "peer.json").write_text("{}\n", encoding="utf-8")
    git(peer, "add", "peer.json")
    git(peer, "commit", "-m", "peer wins")
    git(peer, "push", "origin", "main")

    rejected = git(local, "push", "origin", "main", check=False)
    assert rejected.returncode != 0
    assert bridge_sync_worker._is_non_fast_forward_push_failure(rejected)

    recovered, detail = bridge_sync_worker._recover_non_fast_forward_push(str(local))

    assert recovered is True, detail
    local_head = git(local, "rev-parse", "HEAD").stdout.strip()
    remote_head = git(local, "rev-parse", "origin/main").stdout.strip()
    assert local_head == remote_head
    assert local_head != local_generated_sha
    assert (local / "peer.json").exists()
    assert not (local / "local-generated.json").exists()


def test_generic_push_failure_never_authorizes_hard_reset():
    result = _cp(
        ("push",),
        returncode=1,
        stderr="remote: permission denied by branch protection",
    )

    assert bridge_sync_worker._is_non_fast_forward_push_failure(result) is False


def test_safety_heal_and_check_reuse_one_task_parse(tmp_path, monkeypatch):
    db_path = str(tmp_path / "memory.db")
    bridge_dir = tmp_path / "bridge"
    tasks_dir = bridge_dir / "tasks"
    tasks_dir.mkdir(parents=True)
    init_db(db_path)
    now = db_utils.now_iso()
    with sqlite3.connect(db_path, isolation_level=None) as raw:
        raw.execute(
            "INSERT INTO tasks (id,title,description,notes,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?)",
            ("task-once", "Task", "body", "notes", now, now),
        )
    (tasks_dir / "task-once.json").write_text(
        json.dumps(
            {
                "id": "task-once",
                "title": "Task",
                "description": "body",
                "notes": "notes",
                "updated_at": now,
            }
        ),
        encoding="utf-8",
    )
    real_loads = bridge_sync_worker._json_loads
    parse_count = 0

    def counting_loads(payload):
        nonlocal parse_count
        parse_count += 1
        return real_loads(payload)

    monkeypatch.setattr(bridge_sync_worker, "_json_loads", counting_loads)
    snapshots = _load_bridge_task_snapshots(str(bridge_dir))
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        _auto_heal_sync_safety(conn, str(bridge_dir), bridge_tasks=snapshots)
        safety = _check_sync_safety(conn, str(bridge_dir), bridge_tasks=snapshots)
    finally:
        conn.close()

    assert safety["is_safe"] is True
    assert parse_count == 1


def test_bridge_sync_worker_pull_only_skips_export_and_push(tmp_path, monkeypatch):
    db_path = str(tmp_path / "memory.db")
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    init_db(db_path)

    git_calls = []

    def fake_git_retry(repo_dir, *args, max_retries=3, timeout=30):
        git_calls.append(args)
        if args[:3] == ("fetch", "origin", "main"):
            return _cp(args)
        if args == ("rev-parse", "HEAD"):
            return _cp(args, stdout="same-sha\n")
        if args == ("rev-parse", "origin/main"):
            return _cp(args, stdout="same-sha\n")
        if args == ("merge-base", "HEAD", "origin/main"):
            return _cp(args, stdout="same-sha\n")
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
    assert git_calls == [
        ("fetch", "origin", "main"),
        ("rev-parse", "HEAD"),
        ("rev-parse", "origin/main"),
        ("merge-base", "HEAD", "origin/main"),
    ]


def test_pull_only_repairs_entities_index_before_remote_import(tmp_path, monkeypatch):
    db_path = str(tmp_path / "memory.db")
    bridge_dir = tmp_path / "bridge"
    entities_dir = bridge_dir / "entities"
    entities_dir.mkdir(parents=True)
    init_db(db_path)
    ts = "2026-07-21T10:00:00+00:00"
    (bridge_dir / "shared.json").write_text(
        json.dumps({"version": 4, "entities": [], "relations": [], "tasks": []}),
        encoding="utf-8",
    )
    (bridge_dir / "entities_index.json").write_text(
        '{"entities":[]}\n{"entities":[]}', encoding="utf-8"
    )
    (entities_dir / "41.json").write_text(
        json.dumps(
            {
                "id": 41,
                "name": "Remote entity",
                "entityType": "company",
                "project": "shared:bridge",
                "observations": [{"content": "remote", "createdAt": ts}],
                "createdAt": ts,
                "updatedAt": ts,
            }
        ),
        encoding="utf-8",
    )

    def fake_git_retry(repo_dir, *args, max_retries=3, timeout=30):
        if args == ("fetch", "origin", "main"):
            return _cp(args)
        if args in {
            ("rev-parse", "HEAD"),
            ("rev-parse", "origin/main"),
            ("merge-base", "HEAD", "origin/main"),
        }:
            return _cp(args, stdout="same-sha\n")
        raise AssertionError(f"Unexpected git_retry call: {args}")

    monkeypatch.setattr(
        bridge_sync_worker, "ensure_bridge_repo_ready", lambda repo: (True, None)
    )
    monkeypatch.setattr(bridge_sync_worker, "git_retry", fake_git_retry)

    result = bridge_sync_worker.main(
        db_path=db_path,
        bridge_repo=str(bridge_dir),
        pull_only=True,
    )

    assert result["pull_only"] is True
    with sqlite3.connect(db_path) as conn:
        imported = conn.execute(
            "SELECT name FROM entities WHERE name='Remote entity'"
        ).fetchone()
    assert imported == ("Remote entity",)
    repaired = json.loads(
        (bridge_dir / "entities_index.json").read_text(encoding="utf-8")
    )
    assert repaired["entities"][0]["id"] == 41


def test_bridge_sync_worker_pull_conflict_fails_closed_without_reset(
    tmp_path, monkeypatch
):
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    git_calls = []

    monkeypatch.setattr(
        bridge_sync_worker, "ensure_bridge_repo_ready", lambda repo: (True, None)
    )

    def fake_git_retry(repo_dir, *args, max_retries=3, timeout=30):
        return _cp(args, returncode=1, stderr="CONFLICT (content): shared.json\n")

    def fake_git_run(repo_dir, *args, timeout=30):
        git_calls.append(args)
        return _cp(args)

    monkeypatch.setattr(bridge_sync_worker, "git_retry", fake_git_retry)
    monkeypatch.setattr(bridge_sync_worker, "git_run", fake_git_run)

    result = bridge_sync_worker._main_locked(
        progress_callback=None,
        db_path=str(tmp_path / "memory.db"),
        bridge_dir=str(bridge_dir),
        force=False,
        pull_only=True,
        ui_profile=None,
        machine_id="fedora",
    )

    assert result["blocked_by_repo_state"] is True
    assert result["git_pull_failed"] is True
    assert "CONFLICT" in result["message"]
    assert git_calls == []


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
    peer_calls = []

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
    monkeypatch.setattr(
        bridge_sync_worker,
        "publish_peer_payloads",
        lambda path, tasks: (
            peer_calls.append((path, tasks))
            or {"assigned_task_recipients": 1, "knowledge_shared": 2}
        ),
    )
    monkeypatch.setattr(
        bridge_sync_worker,
        "create_public_release",
        lambda entities, tasks, machine: "public-v-test",
    )

    result = bridge_sync_worker.main(
        force=True, bridge_repo=str(bridge_dir), db_path=db_path
    )

    shared_js = (bridge_dir / "shared.js").read_text(encoding="utf-8")

    assert result["pushed"] is True
    assert shared_js.startswith("window.__BRIDGE_DATA__ = ")
    assert any(args[0] == "add" and "shared.js" in args for args in git_calls)
    assert any(args[:2] == ("add", "-f") for args in git_calls)
    assert not any(
        args[:2] == ("add", "-f") and "extended_memory/" in args for args in git_calls
    )
    assert peer_calls and peer_calls[0][0] == db_path
    assert result["assigned_task_recipients"] == 1
    assert result["knowledge_shared"] == 2
    assert result["github_release"] == "public-v-test"


def test_bridge_sync_worker_git_add_failure_fails_closed_without_commit_or_push(
    tmp_path, monkeypatch
):
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
        if args[0] == "add":
            return _cp(args, returncode=128, stderr="fatal: pathspec failed")
        if args[0] in {"commit", "push"}:
            raise AssertionError(f"git {args[0]} must not run after add failure")
        return _cp(args)

    def fake_git_retry(repo_dir, *args, max_retries=3, timeout=30):
        git_calls.append(args)
        if args[0] == "push":
            raise AssertionError("git push must not run after add failure")
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

    assert result["pushed"] is False
    assert result["git_add_failed"] is True
    assert "pathspec failed" in result["message"]
    assert any(args[0] == "add" for args in git_calls)
    assert not any(args[0] in {"commit", "push"} for args in git_calls)


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


def test_bridge_sync_worker_aborts_push_when_task_merge_raises_db_lock(
    tmp_path, monkeypatch
):
    """Regression: 2026-05-08 19:36 incident — fedora silently caught
    sqlite3.OperationalError("database is locked") from merge_import_tasks
    and continued to export, pushing stale local state that overwrote
    RManov's tombstones (12 archived tasks resurrected). The fix must
    abort export+push when the task merge fails, since the local DB
    has not absorbed the remote tombstones."""
    db_path = str(tmp_path / "memory.db")
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    init_db(db_path)

    git_calls: list[tuple] = []

    def fake_git_run(repo_dir, *args, timeout=30):
        git_calls.append(args)
        return _cp(args)

    def fake_git_retry(repo_dir, *args, max_retries=3, timeout=30):
        git_calls.append(args)
        return _cp(args)

    def raise_db_locked(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(
        bridge_sync_worker, "ensure_bridge_repo_ready", lambda repo: (True, None)
    )
    monkeypatch.setattr(bridge_sync_worker, "git_run", fake_git_run)
    monkeypatch.setattr(bridge_sync_worker, "git_retry", fake_git_retry)
    monkeypatch.setattr(
        bridge_sync_worker,
        "load_remote_tasks_for_merge",
        lambda *a, **k: ([{"id": "task-tomb", "_tombstone": True}], True),
    )
    monkeypatch.setattr(
        bridge_sync_worker,
        "import_remote_bridge_data",
        lambda *a, **k: {"entities": 0, "relations": 0, "ratings": 0},
    )
    monkeypatch.setattr(bridge_sync_worker, "merge_import_tasks", raise_db_locked)
    monkeypatch.setattr(
        bridge_sync_worker, "sync_task_attachments_from_remote", lambda *a, **k: (0, 0)
    )

    result = bridge_sync_worker.main(
        force=True, bridge_repo=str(bridge_dir), db_path=db_path
    )

    assert result["pushed"] is False, "must NOT push when merge fails"
    assert result.get("blocked_by_merge_failure") is True
    assert not any(args[:1] == ("push",) for args in git_calls), (
        "no git push must occur when merge failed"
    )
    assert not (bridge_dir / "shared.json").exists(), (
        "shared.json must not be exported with stale data"
    )
