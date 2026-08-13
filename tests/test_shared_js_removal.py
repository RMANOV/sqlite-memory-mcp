"""shared.js removal + legacy-fallback fail-closed regression tests.

Covers the four verdict conditions for the bridge payload prune
(debate DAY_OPS_20260811, msgs 97c56247b4e5 / 50e835682760):

(a) the legacy shared.json task fallback fails closed on the transport path
    (missing/corrupt index.json with a non-empty task payload never merges,
    never pushes); per-task file corruption degrades safely under a valid
    index.json;
(b) the shared.js surface is fully gone: contract, staging, pages publish,
    managed .gitattributes, writer;
(c) the generated Kanban HTML has no shared.js / __BRIDGE_DATA__ consumer
    (self-contained: no fetch, no external scripts);
(d) transport invariants live in
    test_bridge_sync_worker.test_bridge_sync_worker_prunes_shared_js_and_keeps_transport
    (shared.json / index.json / tasks/*.json unchanged by the prune).
"""

import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import bridge_merge_driver
import bridge_sync_worker
import db_utils
import task_report
from db_utils import load_remote_tasks_for_merge
from schema import init_db
from surface_contract import (
    BRIDGE_ARTIFACT_SURFACE_CONTRACT,
    BRIDGE_GIT_STAGE_PATHS,
    BRIDGE_PAGES_PUBLISH_PATHS,
)


def _cp(args, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args, returncode, stdout, stderr)


def _legacy_task(task_id="task-legacy-1"):
    return {
        "id": task_id,
        "title": "Legacy task",
        "status": "not_started",
        "created_at": "2026-08-01T00:00:00+00:00",
        "updated_at": "2026-08-01T00:00:00+00:00",
    }


def _sync_fixture(tmp_path, monkeypatch):
    """Bridge dir + fresh DB + fakes for everything that talks to git."""
    db_path = str(tmp_path / "memory.db")
    init_db(db_path)
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    calls = []

    def fake_git_run(repo_dir, *args, timeout=30):
        calls.append(args)
        return _cp(args)

    def fake_git_retry(repo_dir, *args, max_retries=3, timeout=30):
        calls.append(args)
        return _cp(args)

    monkeypatch.setattr(
        bridge_sync_worker, "ensure_bridge_repo_ready", lambda repo: (True, None)
    )
    monkeypatch.setattr(
        bridge_sync_worker,
        "ensure_bridge_git_identity",
        lambda repo: {"changed": False},
    )
    monkeypatch.setattr(
        bridge_sync_worker, "_sync_bridge_repo_fast_forward", lambda repo: (True, None)
    )
    monkeypatch.setattr(bridge_sync_worker, "git_run", fake_git_run)
    monkeypatch.setattr(bridge_sync_worker, "git_retry", fake_git_retry)
    return db_path, bridge_dir, calls


# ── (a) transport fail-closed on the legacy fallback ─────────────────────────


def test_full_sync_fails_closed_when_index_json_missing_but_tasks_exist(
    tmp_path, monkeypatch
):
    db_path, bridge_dir, calls = _sync_fixture(tmp_path, monkeypatch)
    (bridge_dir / "shared.json").write_text(
        json.dumps({"version": 3, "machine_id": "peer", "tasks": [_legacy_task()]}),
        encoding="utf-8",
    )

    result = bridge_sync_worker._main_locked(
        progress_callback=None,
        db_path=db_path,
        bridge_dir=str(bridge_dir),
        force=True,
        pull_only=False,
        ui_profile=None,
        machine_id="testbox",
    )

    assert result["blocked_by_merge_failure"] is True
    assert result["legacy_fallback_blocked"] is True
    assert result["pushed"] is False
    # The cause must reach downstream reporters, not just the log.
    assert "not tombstone-safe" in result["message"]
    assert not any("push" in args for args in calls)
    # The fallback must not be laundered into a fresh export either.
    assert not (bridge_dir / "shared.js").exists()


def test_full_sync_fails_closed_when_index_json_is_corrupt(tmp_path, monkeypatch):
    db_path, bridge_dir, calls = _sync_fixture(tmp_path, monkeypatch)
    (bridge_dir / "shared.json").write_text(
        json.dumps({"version": 3, "machine_id": "peer", "tasks": [_legacy_task()]}),
        encoding="utf-8",
    )
    (bridge_dir / "index.json").write_text("{this is not json", encoding="utf-8")

    result = bridge_sync_worker._main_locked(
        progress_callback=None,
        db_path=db_path,
        bridge_dir=str(bridge_dir),
        force=True,
        pull_only=False,
        ui_profile=None,
        machine_id="testbox",
    )

    assert result["blocked_by_merge_failure"] is True
    assert result["legacy_fallback_blocked"] is True
    assert result["pushed"] is False
    assert not any("push" in args for args in calls)


def test_pull_only_reports_legacy_fallback_block(tmp_path, monkeypatch):
    db_path, bridge_dir, _calls = _sync_fixture(tmp_path, monkeypatch)
    (bridge_dir / "shared.json").write_text(
        json.dumps({"version": 3, "machine_id": "peer", "tasks": [_legacy_task()]}),
        encoding="utf-8",
    )

    result = bridge_sync_worker._main_locked(
        progress_callback=None,
        db_path=db_path,
        bridge_dir=str(bridge_dir),
        force=False,
        pull_only=True,
        ui_profile=None,
        machine_id="testbox",
    )

    assert result["pull_only"] is True
    assert result["pushed"] is False
    assert result["legacy_fallback_blocked"] is True
    assert result["imported_new"] == 0
    assert result["imported_updated"] == 0


def test_parseable_index_without_task_list_is_not_an_empty_manifest(tmp_path):
    # {} parses, but a manifest with no "tasks" list is broken, not empty:
    # every real export writes the key as a list. Trusting it as
    # authoritative-empty would push over rows the merge never absorbed.
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    (bridge_dir / "index.json").write_text("{}", encoding="utf-8")
    payload = {"machine_id": "peer", "tasks": [_legacy_task()]}

    tasks, loaded_from_index = load_remote_tasks_for_merge(str(bridge_dir), payload)

    assert loaded_from_index is False  # routes into the fail-closed gate
    assert len(tasks) == 1

    # A malformed "tasks" value is treated the same way.
    (bridge_dir / "index.json").write_text(
        json.dumps({"machine_id": "peer", "tasks": {"a": 1}}), encoding="utf-8"
    )
    tasks, loaded_from_index = load_remote_tasks_for_merge(str(bridge_dir), payload)
    assert loaded_from_index is False

    # A genuinely empty manifest ("tasks": []) stays authoritative.
    (bridge_dir / "index.json").write_text(
        json.dumps({"machine_id": "peer", "tasks": []}), encoding="utf-8"
    )
    tasks, loaded_from_index = load_remote_tasks_for_merge(str(bridge_dir), payload)
    assert loaded_from_index is True
    assert tasks == []


def test_fallback_with_no_tasks_is_not_a_block():
    # Fresh/empty bridge repo: no index.json, no shared.json tasks — nothing
    # can be resurrected, so initialization must proceed. The engine-level
    # proof (full run over an empty bridge dir still pushes) lives in
    # test_bridge_sync_worker_prunes_shared_js_and_keeps_transport.
    tasks, loaded_from_index = load_remote_tasks_for_merge(
        os.path.join(os.path.dirname(__file__), "does-not-exist"), {}
    )
    assert tasks == []
    assert loaded_from_index is False


def test_corrupt_per_task_file_degrades_without_content_clobber(tmp_path):
    bridge_dir = tmp_path / "bridge"
    (bridge_dir / "tasks").mkdir(parents=True)
    task = _legacy_task("task-corrupt-content")
    (bridge_dir / "index.json").write_text(
        json.dumps({"machine_id": "peer", "tasks": [task]}), encoding="utf-8"
    )
    content_path = db_utils._task_storage_path("task-corrupt-content", str(bridge_dir))
    content_path.write_text("{corrupt json payload", encoding="utf-8")

    tasks, loaded_from_index = load_remote_tasks_for_merge(str(bridge_dir), {})

    # index.json is the manifest of record: the merge stays on the
    # tombstone-safe path and the unreadable content file only means the
    # content fields stay absent (LWW never sees an empty overwrite).
    assert loaded_from_index is True
    assert len(tasks) == 1
    assert tasks[0]["id"] == "task-corrupt-content"
    assert "description" not in tasks[0]
    assert "notes" not in tasks[0]


def test_untrack_failure_fails_closed_before_staging_commit_push(tmp_path, monkeypatch):
    # B-UNTRACK-FAIL (verdict c8fe5a57e0f1): if `git rm --cached` fails
    # (index.lock contention, permissions), the sync must stop BEFORE
    # staging/commit/push — otherwise the stale tracked 24 MB shared.js
    # rides the next transport commit, the exact thing the prune removes.
    db_path, bridge_dir, calls = _sync_fixture(tmp_path, monkeypatch)

    import sqlite3 as _sqlite3

    conn = _sqlite3.connect(db_path, isolation_level=None)
    now = db_utils.now_iso()
    conn.execute(
        "INSERT INTO tasks (id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
        ("task-001", "Task", now, now),
    )
    conn.close()

    def failing_git_run(repo_dir, *args, timeout=30):
        calls.append(args)
        if args[0] == "rm":
            return _cp(
                args,
                returncode=1,
                stderr="fatal: Unable to create '.git/index.lock': File exists.",
            )
        return _cp(args)

    monkeypatch.setattr(bridge_sync_worker, "git_run", failing_git_run)
    monkeypatch.setattr(
        bridge_sync_worker.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, "", ""),
    )
    monkeypatch.setattr(
        bridge_sync_worker, "publish_peer_payloads", lambda path, tasks: {}
    )
    monkeypatch.setattr(
        bridge_sync_worker,
        "create_public_release",
        lambda entities, tasks, machine: None,
    )

    result = bridge_sync_worker._main_locked(
        progress_callback=None,
        db_path=db_path,
        bridge_dir=str(bridge_dir),
        force=True,
        pull_only=False,
        ui_profile=None,
        machine_id="testbox",
    )

    assert result["pushed"] is False
    assert result["git_add_failed"] is True
    assert "untrack" in result["message"]
    assert "index.lock" in result["message"]
    assert any(args[0] == "rm" for args in calls)
    assert not any(args[0] == "add" for args in calls)
    assert not any(args[0] == "commit" for args in calls)
    assert not any("push" in args for args in calls)


# ── (b) the shared.js surface is gone ────────────────────────────────────────


def test_shared_js_is_gone_from_the_surface_contract():
    assert "shared.js" not in BRIDGE_ARTIFACT_SURFACE_CONTRACT
    assert "shared.js" not in BRIDGE_GIT_STAGE_PATHS
    assert "shared.js" not in BRIDGE_PAGES_PUBLISH_PATHS


def test_shared_js_is_gone_from_managed_gitattributes():
    # Exact-token match: "shared.json ..." legitimately starts with the same
    # nine characters, so a substring check would false-positive on it.
    patterns = [
        line.split()[0]
        for line in bridge_merge_driver._GITATTRIBUTES_MANAGED_LINES
        if line and not line.startswith("#")
    ]
    assert "shared.js" not in patterns
    assert "shared.json" in patterns  # the real transport stays managed


def test_shared_js_stays_healable_as_a_legacy_leftover():
    # Deliberate: no longer generated, but a stale copy inherited from an old
    # checkout must heal/discard, never block a sync.
    assert "shared.js" in bridge_merge_driver._GENERATED_UNMERGED_HEALABLE
    assert "shared.js" in db_utils.BRIDGE_GENERATED_FILES
    assert "shared.js.tmp" in db_utils.BRIDGE_GENERATED_TEMP_FILES


def test_engine_has_no_shared_js_writer():
    assert not hasattr(bridge_sync_worker, "_write_shared_js")


# ── (c) no consumer: the Kanban HTML is self-contained ──────────────────────


def test_kanban_html_has_no_shared_js_or_bridge_data_consumer(monkeypatch):
    monkeypatch.setattr(task_report, "_is_overdue", lambda due_date: False)
    html = task_report._build_html(
        [
            {
                "id": "task-1",
                "title": "Canary task",
                "priority": "medium",
                "section": "inbox",
                "type": "task",
            }
        ],
        set(),
    )
    lowered = html.lower()

    assert "__bridge_data__" not in html
    assert "shared.js" not in lowered
    assert "fetch(" not in lowered
    assert "xmlhttprequest" not in lowered
    # Structural self-containment: the board carries NO script at all, so an
    # obfuscated loader (createElement('script'), string-split URLs) cannot
    # hide behind a weaker "<script src" check.
    assert "<script" not in lowered
