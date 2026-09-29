"""Transport preservation and explicit parent-clear authority, isolated SQLite only."""

import copy
import hashlib
import json
import sqlite3
import subprocess

import pytest

import bridge_sync_worker as worker
import db_utils as db
import task_server
from schema import init_db


LEGACY = ["2026-01-02T00:00:00+00:00", "peer-a", 0, None]
HLC = ["2026-01-01T00:00:00+00:00", "peer-b",
       db._pack_logical_clock(db._iso_to_epoch_ms("2026-01-01T00:00:00Z"), 1),
       "00000000000000000000000000000001"]


@pytest.fixture(params=[LEGACY, HLC], ids=["legacy", "packed-hlc"])
def case(tmp_path, request, monkeypatch):
    monkeypatch.setenv("BRIDGE_PRESERVE_ORPHAN_ATTACHMENTS", "1")
    path = tmp_path / "fixture.db"
    init_db(str(path))
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    bridge = tmp_path / "bridge"
    (bridge / "tasks").mkdir(parents=True)
    (bridge / "attachments").mkdir()
    (bridge / "attachments" / "orphan.bin").write_bytes(b"opaque attachment bytes")
    clock = copy.deepcopy(request.param)
    remote = {
        "id": "child", "title": "Missing parent fixture", "description": "Original description",
        "notes": "", "status": "not_started", "priority": "medium", "section": "inbox",
        "type": "task", "parent_id": "missing-parent", "created_at": clock[0], "updated_at": clock[0],
        "_field_ts": {"parent_id": clock, "created_at": copy.deepcopy(LEGACY),
                      "updated_at": copy.deepcopy(HLC)},
        "_links": [{"name": "absent entity", "link_type": "manual", "score": .7,
                    "created_at": clock[0]}],
    }
    task_path = bridge / "tasks" / "child.json"
    task_path.write_text(json.dumps(remote), encoding="utf-8")
    db.merge_import_tasks(conn, [copy.deepcopy(remote)], import_content=True)
    assert conn.execute("SELECT parent_id FROM tasks WHERE id='child'").fetchone()[0] is None
    yield conn, bridge, remote, path
    conn.close()


def emit(conn, bridge):
    overrides = db.prepare_task_export_overrides(conn, str(bridge))
    shared = worker._export_tasks(conn, export_overrides=overrides)
    db.export_task_files(conn, str(bridge), export_overrides=overrides)
    db.export_index_json(conn, str(bridge), export_overrides=overrides)
    worker.write_kanban_payload(str(bridge), {"tasks": shared})
    task = json.loads((bridge / "tasks" / "child.json").read_text(encoding="utf-8"))
    index = json.loads((bridge / "index.json").read_text(encoding="utf-8"))
    kanban = json.loads((bridge / "kanban_payload.json").read_text(encoding="utf-8"))
    return task, next(t for t in index["tasks"] if t["id"] == "child"), shared, kanban


def assert_views(conn, bridge, parent, clock=None):
    task, index, shared, kanban = emit(conn, bridge)
    active = [t for t in shared if t["id"] == "child"]
    for record in [task, index, *active]:
        assert record["parent_id"] == parent
        if clock is not None:
            assert record["_field_ts"]["parent_id"] == clock
    for record in kanban.get("tasks", []):
        if record["id"] == "child":
            assert record["parent_id"] == parent
    return task, index


def test_partial_import_all_views_idempotent_preserve_history_and_bytes(case):
    conn, bridge, remote, _ = case
    events_before = conn.execute("SELECT count(*) FROM memory_events").fetchone()[0]
    digest = hashlib.sha256((bridge / "attachments" / "orphan.bin").read_bytes()).hexdigest()
    first, _ = assert_views(conn, bridge, remote["parent_id"], remote["_field_ts"]["parent_id"])
    second, _ = assert_views(conn, bridge, remote["parent_id"], remote["_field_ts"]["parent_id"])
    assert first == second
    for field in ("created_at", "updated_at"):
        assert first["_field_ts"][field] == remote["_field_ts"][field]
    assert first["_links"] == remote["_links"]
    assert conn.execute("SELECT count(*) FROM memory_events").fetchone()[0] == events_before
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert hashlib.sha256((bridge / "attachments" / "orphan.bin").read_bytes()).hexdigest() == digest


def test_unrelated_edit_and_later_parent_arrival_do_not_erase_transport(case):
    conn, bridge, remote, _ = case
    db.apply_task_mutation(conn, "child", {"title": "Edited title"}, tool_name="test")
    db.create_task_with_ledger(conn, "missing-parent", "Arrived parent", db.now_iso())
    # Transport fix deliberately does not fabricate a semantic child write.
    assert conn.execute("SELECT parent_id FROM tasks WHERE id='child'").fetchone()[0] is None
    assert_views(conn, bridge, remote["parent_id"], remote["_field_ts"]["parent_id"])


def test_reimport_with_parent_event_keeps_unresolved_reference_fk_safe(case):
    conn, bridge, remote, _ = case
    clock = remote["_field_ts"]["parent_id"]
    event_id = clock[3] or "00000000000000000000000000000002"
    remote["_field_ts"]["parent_id"][3] = event_id
    conn.execute(
        "UPDATE task_field_versions SET source_event_id=? "
        "WHERE task_id='child' AND field_name='parent_id'", (event_id,),
    )
    event = {
        "event_id": event_id, "event_type": "task_field_set", "aggregate_kind": "task",
        "aggregate_id": "child", "field_name": "parent_id", "event_ts": clock[0],
        "machine_id": clock[1], "logical_clock": clock[2],
        "old_value": None, "new_value": "missing-parent",
    }
    (bridge / "tasks" / "child.json").write_text(json.dumps(remote), encoding="utf-8")
    for _ in range(2):
        db.merge_import_tasks(conn, [copy.deepcopy(remote)], import_content=True, remote_events=[event])
        assert conn.execute("SELECT parent_id FROM tasks WHERE id='child'").fetchone()[0] is None
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert_views(conn, bridge, "missing-parent", remote["_field_ts"]["parent_id"])
    db.create_task_with_ledger(conn, "missing-parent", "Parent arrives", db.now_iso())
    db.merge_import_tasks(conn, [copy.deepcopy(remote)], import_content=True, remote_events=[event])
    assert conn.execute("SELECT parent_id FROM tasks WHERE id='child'").fetchone()[0] == "missing-parent"
    assert_views(conn, bridge, "missing-parent", remote["_field_ts"]["parent_id"])


def test_actual_mcp_clear_normalized_null_creates_authority_and_survives_reimport(case, monkeypatch):
    conn, bridge, remote, path = case
    old_clock = copy.deepcopy(remote["_field_ts"]["parent_id"])
    monkeypatch.setattr(task_server, "_get_write_conn", lambda: db.get_conn_immediate(str(path)))
    monkeypatch.setattr(task_server, "_get_conn", lambda: db.get_conn(str(path)))
    monkeypatch.setattr(task_server, "_vec_sync_task_safe", lambda _: pytest.fail("not a content edit"))
    result = json.loads(task_server.update_task.fn("child", parent_id="CLEAR"))
    assert "error" not in result
    version = conn.execute("SELECT * FROM task_field_versions WHERE task_id='child' AND field_name='parent_id'").fetchone()
    assert version["updated_order"] >= db._HLC_PACKED_MIN
    assert version["source_event_id"] and version["source_event_id"] != old_clock[3]
    event = conn.execute("SELECT * FROM memory_events WHERE event_id=?", (version["source_event_id"],)).fetchone()
    assert event["new_value"] is None and event["old_value"] is None
    assert event["tool_name"] == "sqlite-tasks.update_task"
    assert json.loads(event["payload_json"])["mutation_intent"] == "explicit_clear"
    assert conn.execute("SELECT updated_at FROM tasks WHERE id='child'").fetchone()[0] == version["updated_at"]
    task, _ = assert_views(conn, bridge, None)
    new_clock = task["_field_ts"]["parent_id"]
    # Old remote edge cannot return through a later import or repeated export.
    db.merge_import_tasks(conn, [copy.deepcopy(remote)], import_content=True)
    db.create_task_with_ledger(conn, "missing-parent", "Parent arrives after clear", db.now_iso())
    db.merge_import_tasks(conn, [copy.deepcopy(remote)], import_content=True)
    assert_views(conn, bridge, None, new_clock)


def test_null_noop_without_explicit_clear_remains_noop(case):
    conn, _, _, _ = case
    before = conn.execute("SELECT * FROM task_field_versions WHERE task_id='child'").fetchall()
    result = db.apply_task_mutation(conn, "child", {"parent_id": None}, tool_name="test")
    assert result["updated"] == 0
    assert before == conn.execute("SELECT * FROM task_field_versions WHERE task_id='child'").fetchall()


def test_new_parent_and_newer_clear_win(case):
    conn, bridge, _, _ = case
    db.create_task_with_ledger(conn, "new-parent", "New parent", db.now_iso())
    db.apply_task_mutation(conn, "child", {"parent_id": "new-parent"}, tool_name="test")
    assert_views(conn, bridge, "new-parent")
    db.apply_task_mutation(conn, "child", {"parent_id": None}, explicit_clear_fields=("parent_id",), tool_name="test")
    assert_views(conn, bridge, None)


def test_newer_remote_unresolved_parent_preserves_wire_value_and_clear_intent(case):
    conn, bridge, remote, _ = case
    db.create_task_with_ledger(conn, "old-parent", "Existing parent", db.now_iso())
    conn.execute("UPDATE tasks SET parent_id='old-parent' WHERE id='child'")
    clock = ["2026-02-01T00:00:00Z", "peer-c",
             db._pack_logical_clock(db._iso_to_epoch_ms("2026-02-01T00:00:00Z"), 1),
             "00000000000000000000000000000003"]
    remote["parent_id"] = "other-missing-parent"
    remote["_field_ts"]["parent_id"] = clock
    remote["updated_at"] = clock[0]
    event = {
        "event_id": clock[3], "event_type": "task_field_set", "aggregate_kind": "task",
        "aggregate_id": "child", "field_name": "parent_id", "event_ts": clock[0],
        "machine_id": clock[1], "logical_clock": clock[2],
        "old_value": "old-parent", "new_value": remote["parent_id"],
    }
    db.import_memory_events(conn, [event])
    (bridge / "tasks" / "child.json").write_text(json.dumps(remote), encoding="utf-8")
    db.merge_import_tasks(conn, [copy.deepcopy(remote)], import_content=True, remote_events=[event])
    assert conn.execute("SELECT parent_id FROM tasks WHERE id='child'").fetchone()[0] is None
    version = conn.execute("SELECT new_value, source_event_id FROM task_field_versions "
                           "WHERE task_id='child' AND field_name='parent_id'").fetchone()
    assert tuple(version) == ("other-missing-parent", clock[3])
    assert_views(conn, bridge, "other-missing-parent", clock)
    db.apply_task_mutation(conn, "child", {"parent_id": None},
                           explicit_clear_fields=("parent_id",), tool_name="test")
    db.merge_import_tasks(conn, [copy.deepcopy(remote)], import_content=True, remote_events=[event])
    assert_views(conn, bridge, None)
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize("bad", [None, [], ["bad", "peer", 0, None], [LEGACY[0], "", 0, None], [LEGACY[0], "peer", "oops", None]])
def test_malformed_clock_blocks_before_task_or_attachment_writes(case, bad):
    conn, bridge, remote, _ = case
    corrupt = copy.deepcopy(remote)
    corrupt["_field_ts"]["parent_id"] = bad
    path = bridge / "tasks" / "child.json"
    path.write_text(json.dumps(corrupt), encoding="utf-8")
    before = {p.relative_to(bridge).as_posix(): p.read_bytes() for p in bridge.rglob("*") if p.is_file()}
    with pytest.raises(db.TaskExportConflict):
        db.export_task_files(conn, str(bridge))
    assert before == {p.relative_to(bridge).as_posix(): p.read_bytes() for p in bridge.rglob("*") if p.is_file()}


def test_wrong_identity_and_equal_key_different_event_block(case):
    conn, bridge, remote, _ = case
    bad = copy.deepcopy(remote)
    bad["id"] = "some-other-task"
    path = bridge / "tasks" / "child.json"
    path.write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(db.TaskExportConflict):
        db.export_index_json(conn, str(bridge))
    bad = copy.deepcopy(remote)
    bad["_field_ts"]["parent_id"][3] = "different-event"
    path.write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(db.TaskExportConflict):
        db.prepare_task_export_overrides(conn, str(bridge))


def test_newer_remote_clock_requires_import_not_silent_clear(case):
    conn, bridge, remote, _ = case
    bad = copy.deepcopy(remote)
    bad["_field_ts"]["parent_id"] = ["2026-09-01T00:00:00Z", "peer", 117195787468800000, None]
    (bridge / "tasks" / "child.json").write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(db.TaskExportConflict):
        db.prepare_task_export_overrides(conn, str(bridge))


def test_archived_tombstone_stays_archived_with_preserved_edge(case):
    conn, bridge, remote, _ = case
    db.apply_task_mutation(conn, "child", {"status": "archived"}, tool_name="test")
    task, index = assert_views(conn, bridge, remote["parent_id"])
    assert task["status"] == index["status"] == "archived"
    assert index["_tombstone"] is True


def test_matching_clear_event_wins_over_stale_value_at_same_clock(case):
    conn, bridge, remote, _ = case
    db.apply_task_mutation(conn, "child", {"parent_id": None}, explicit_clear_fields=("parent_id",), tool_name="test")
    version = conn.execute("SELECT * FROM task_field_versions WHERE task_id='child' AND field_name='parent_id'").fetchone()
    remote["_field_ts"]["parent_id"] = [version["updated_at"], version["updated_by"],
                                          version["updated_order"], version["source_event_id"]]
    (bridge / "tasks" / "child.json").write_text(json.dumps(remote), encoding="utf-8")
    assert_views(conn, bridge, None)


def test_synthetic_null_event_is_not_explicit_clear(case):
    conn, bridge, remote, _ = case
    # This is the actual legacy event writer, not a fabricated event dictionary.
    db.upsert_field_versions(
        conn, "child", ("parent_id",), db.now_iso(), old_values={"parent_id": None},
        new_values={"parent_id": None}, tool_name="legacy.seed_parent_version",
    )
    version = conn.execute("SELECT * FROM task_field_versions WHERE task_id='child' AND field_name='parent_id'").fetchone()
    remote["_field_ts"]["parent_id"] = [version["updated_at"], version["updated_by"],
                                          version["updated_order"], version["source_event_id"]]
    path = bridge / "tasks" / "child.json"
    path.write_text(json.dumps(remote), encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(db.TaskExportConflict, match="lacks explicit-clear intent"):
        db.export_task_files(conn, str(bridge))
    assert path.read_bytes() == before


def test_explicit_clear_flag_requires_recorded_parent_null(case):
    conn, _, _, _ = case
    with pytest.raises(ValueError):
        db.upsert_field_versions(conn, "child", ("parent_id",), new_values={"parent_id": "not-null"},
                                 explicit_clear_fields=("parent_id",))


def test_dict_and_two_item_legacy_versions_preserve_identical_authority(case):
    conn, bridge, remote, _ = case
    ts, writer, order, event = remote["_field_ts"]["parent_id"]
    remote["_field_ts"]["parent_id"] = {
        "updated_at": ts, "updated_by": writer, "updated_order": order, "source_event_id": event,
    }
    (bridge / "tasks" / "child.json").write_text(json.dumps(remote), encoding="utf-8")
    assert_views(conn, bridge, remote["parent_id"])
    if order == 0 and event is None:
        remote["_field_ts"]["parent_id"] = [ts, writer]
        (bridge / "tasks" / "child.json").write_text(json.dumps(remote), encoding="utf-8")
        assert_views(conn, bridge, remote["parent_id"])


@pytest.mark.parametrize("kwargs", [
    {"explicit_clear_fields": ("description",)},
    {"explicit_clear_fields": ("parent_id",), "record_events": False},
    {"explicit_clear_fields": ("parent_id",), "touch_updated_at": False},
])
def test_clear_flag_cannot_be_used_without_authority(case, kwargs):
    conn, _, _, _ = case
    with pytest.raises(ValueError):
        db.apply_task_mutation(conn, "child", {"parent_id": None}, **kwargs)


def mock_sync(monkeypatch):
    calls = []
    monkeypatch.setattr(worker, "ensure_bridge_repo_ready", lambda _: (True, None))
    monkeypatch.setattr(worker, "ensure_bridge_git_identity", lambda _: {"changed": False})
    monkeypatch.setattr(worker, "_sync_bridge_repo_fast_forward", lambda _: (True, None))
    def git(repo, *args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")
    monkeypatch.setattr(worker, "git_run", git)
    monkeypatch.setattr(worker, "git_retry", git)
    monkeypatch.setattr(worker, "publish_peer_payloads", lambda *a: {})
    monkeypatch.setattr(worker, "create_public_release", lambda *a: None)
    monkeypatch.setattr(worker, "_deploy_pages_privacy_shell", lambda *a: pytest.fail("unexpected deploy"))
    monkeypatch.delenv("CLOUDFLARE_API_TOKEN", raising=False)
    import memory_audit
    monkeypatch.setattr(memory_audit, "maybe_run_memory_audit", lambda *a, **kw: {})
    return calls


@pytest.mark.parametrize("with_event_ledger", [False, True])
def test_canonical_worker_uses_same_parent_for_shared_index_task_and_kanban(case, monkeypatch, with_event_ledger):
    conn, bridge, remote, path = case
    if with_event_ledger:
        clock = remote["_field_ts"]["parent_id"]
        clock[3] = clock[3] or "00000000000000000000000000000004"
        conn.execute("UPDATE task_field_versions SET source_event_id=? "
                     "WHERE task_id='child' AND field_name='parent_id'", (clock[3],))
        event = {
            "event_id": clock[3], "event_type": "task_field_set", "aggregate_kind": "task",
            "aggregate_id": "child", "field_name": "parent_id", "event_ts": clock[0],
            "machine_id": clock[1], "logical_clock": clock[2],
            "old_value": None, "new_value": "missing-parent",
        }
        (bridge / "tasks" / "child.json").write_text(json.dumps(remote), encoding="utf-8")
        (bridge / "shared.json").write_text(json.dumps({"memory_events": [event]}), encoding="utf-8")
    (bridge / "index.json").write_text(json.dumps({"tasks": [remote]}), encoding="utf-8")
    calls = mock_sync(monkeypatch)
    for _ in range(2):
        result = worker.main(force=True, bridge_repo=str(bridge), db_path=str(path))
        assert result["pushed"] is True, result
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        for name in ("index.json", "shared.json", "kanban_payload.json"):
            payload = json.loads((bridge / name).read_text(encoding="utf-8"))
            child = next(t for t in payload["tasks"] if t["id"] == "child")
            assert child["parent_id"] == remote["parent_id"], name
    assert any(args[0] == "push" for args in calls)


def test_canonical_worker_conflict_never_stages_or_overwrites(case, monkeypatch):
    conn, bridge, remote, path = case
    corrupt = copy.deepcopy(remote)
    corrupt["_field_ts"]["parent_id"] = None
    (bridge / "tasks" / "child.json").write_text(json.dumps(corrupt), encoding="utf-8")
    calls = mock_sync(monkeypatch)
    # Keep malformed transport out of import: exercise export conflict gate, not importer errors.
    monkeypatch.setattr(worker, "load_remote_tasks_for_merge", lambda *a, **kw: ([], True))
    before = (bridge / "tasks" / "child.json").read_bytes()
    result = worker.main(force=True, bridge_repo=str(bridge), db_path=str(path))
    assert result.get("blocked_by_task_export_conflict"), result
    assert not result["pushed"]
    assert not any(args[0] in ("add", "commit", "push") for args in calls)
    assert (bridge / "tasks" / "child.json").read_bytes() == before
    assert not (bridge / "shared.json").exists()


def test_private_only_worker_skips_external_actions_despite_configured_targets(case, monkeypatch):
    conn, bridge, remote, path = case
    (bridge / "index.json").write_text(json.dumps({"tasks": [remote]}), encoding="utf-8")
    mock_sync(monkeypatch)
    monkeypatch.setenv("BRIDGE_PRIVATE_ONLY", "1")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "synthetic-token-not-a-secret")
    monkeypatch.setenv("BRIDGE_GH_REPO", "fixture/public-repo")
    conn.execute("INSERT INTO collaborators(github_user,trust_level,added_at) VALUES('fixture-peer','read_write',?)", (db.now_iso(),))
    db.apply_task_mutation(conn, "child", {"assignee": "fixture-peer", "visibility": "public"}, tool_name="test")
    db.create_task_with_ledger(conn, "pending-task", "Pending private fixture", db.now_iso(),
                              visibility="pending_public", publish_requested_at="2020-01-01T00:00:00Z")
    def forbidden(*args, **kwargs):
        pytest.fail("private-only sync attempted external publishing or promotion")
    for name in ("publish_peer_payloads", "create_public_release", "_deploy_pages_privacy_shell",
                 "promote_pending_public_entities"):
        monkeypatch.setattr(worker, name, forbidden)
    result = worker.main(force=True, bridge_repo=str(bridge), db_path=str(path))
    assert result["pushed"] is True and result["private_only"] is True
    assert conn.execute("SELECT visibility FROM tasks WHERE id='pending-task'").fetchone()[0] == "pending_public"
    assert result["promoted_to_public"] == {"entities": 0, "tasks": 0}
