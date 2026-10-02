"""Hidden-state incident regressions; synthetic SQLite and bridge bytes only."""

import copy
import json
import sqlite3
import subprocess

import pytest

import bridge_sync_worker as worker
import db_utils as db
import memory_audit
import task_server
from schema import init_db


OLD = "2026-09-24T10:00:00+00:00"
CLOSED = "2026-10-02T05:00:00+00:00"
NEW = "2026-10-02T06:00:00+00:00"
LATER_EDIT = "2026-10-02T07:00:00+00:00"
TID = "hidden-fixture"


@pytest.fixture
def case(tmp_path):
    path = tmp_path / "memory.db"
    init_db(str(path))
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    bridge = tmp_path / "bridge"
    for directory in (bridge / "tasks", bridge / "attachments", bridge / "entities"):
        directory.mkdir(parents=True)
    yield conn, bridge, path
    assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    conn.close()


def author(
    conn, value, ts=OLD, *, event_id="status-author", field="status", **overrides
):
    event = {
        "event_id": event_id,
        "event_type": "task_field_set",
        "aggregate_kind": "task",
        "aggregate_id": TID,
        "field_name": field,
        "new_value": value,
        "machine_id": "fixture-peer",
        "event_ts": ts,
        "logical_clock": db._pack_logical_clock(db._iso_to_epoch_ms(ts), 1),
        "tool_name": "fixture.update_task",
        **overrides,
    }
    db.import_memory_events(conn, [event])
    return event


def seed(conn, hidden="archived", *, versioned=False):
    conn.execute(
        "INSERT INTO tasks(id,title,status,created_at,updated_at) VALUES(?,?,?,?,?)",
        (TID, "Hidden incident fixture", hidden, OLD, CLOSED),
    )
    event = author(conn, hidden if versioned else "done", CLOSED if versioned else OLD)
    db._store_task_field_version(
        conn,
        TID,
        "status",
        updated_at=event["event_ts"],
        updated_by=event["machine_id"],
        updated_order=event["logical_clock"],
        source_event_id=event["event_id"],
        new_value=event["new_value"],
    )
    return event


def payload(value="done", ts=OLD, *, event=None, tombstone=False):
    order = db._pack_logical_clock(db._iso_to_epoch_ms(ts), 1)
    return {
        "id": TID,
        "title": "Hidden incident fixture",
        "status": value,
        "created_at": OLD,
        "updated_at": ts,
        "_field_ts": {
            "status": [
                ts,
                "fixture-peer",
                order,
                event["event_id"] if event else None,
                value,
            ]
        },
        **({"_tombstone": True} if tombstone else {}),
    }


def domain_snapshot(conn):
    return (
        dict(conn.execute("SELECT * FROM tasks WHERE id=?", (TID,)).fetchone()),
        dict(
            conn.execute(
                "SELECT * FROM task_field_versions WHERE task_id=? AND field_name='status'",
                (TID,),
            ).fetchone()
        ),
        [
            tuple(r)
            for r in conn.execute(
                "SELECT * FROM memory_events WHERE aggregate_id=? AND field_name='status' ORDER BY event_id",
                (TID,),
            )
        ],
    )


def files(bridge):
    return {
        p.relative_to(bridge).as_posix(): p.read_bytes()
        for p in bridge.rglob("*")
        if p.is_file()
    }


@pytest.mark.parametrize("hidden", ["archived", "cancelled"])
@pytest.mark.parametrize("import_content", [False, True])
@pytest.mark.parametrize("remote_ts", ["2026-09-23T10:00:00+00:00", OLD, NEW])
def test_old_visible_authority_cannot_repair_hidden_row(
    case, hidden, import_content, remote_ts
):
    conn, _, _ = case
    seed(conn, hidden)
    before = domain_snapshot(conn)
    for _ in range(2):
        db.merge_import_tasks(
            conn, [payload(ts=remote_ts)], import_content=import_content
        )
        assert domain_snapshot(conn) == before
    conflict = conn.execute(
        "SELECT winner FROM memory_conflicts WHERE aggregate_id=? AND field_name='status'",
        (TID,),
    ).fetchone()
    assert conflict and conflict[0] == "guard_local"


@pytest.mark.parametrize("hidden", ["archived", "cancelled"])
def test_blocked_repair_keeps_scheduling_guard(case, hidden):
    conn, _, _ = case
    seed(conn, hidden)
    remote = payload(ts=NEW)
    remote.update(
        section="today",
        priority="critical",
        due_date="2026-10-03",
        reminder_at=NEW,
        recurring="daily",
    )
    before = domain_snapshot(conn)
    db.merge_import_tasks(conn, [remote], import_content=True)
    assert domain_snapshot(conn) == before


@pytest.mark.parametrize("hidden", ["archived", "cancelled"])
def test_genuine_causal_reopening_ignores_unrelated_row_timestamp(case, hidden):
    conn, bridge, _ = case
    seed(conn, hidden, versioned=True)
    conn.execute(
        "UPDATE tasks SET description='Later unrelated edit', updated_at=? WHERE id=?",
        (LATER_EDIT, TID),
    )
    reopened = author(
        conn, "not_started", NEW, event_id="real-reopen", old_value=hidden
    )
    remote = payload("not_started", NEW, event=reopened)
    db.merge_import_tasks(conn, [copy.deepcopy(remote)], remote_events=[reopened])
    assert (
        conn.execute("SELECT status FROM tasks WHERE id=?", (TID,)).fetchone()[0]
        == "not_started"
    )
    db.export_task_files(conn, str(bridge))
    exported = json.loads(
        (bridge / "tasks" / f"{TID}.json").read_text(encoding="utf-8")
    )
    assert exported["_field_ts"]["status"] == remote["_field_ts"]["status"]


@pytest.mark.parametrize(
    "bad",
    [
        {"aggregate_id": "other-task"},
        {"aggregate_kind": "entity"},
        {"field_name": "title"},
        {"event_type": "repair"},
        {"event_type": "merge"},
    ],
)
def test_foreign_or_bookkeeping_event_cannot_author_hidden_reopening(case, bad):
    conn, _, _ = case
    seed(conn, versioned=True)
    event = author(
        conn, "done", NEW, event_id="not-a-reopen", old_value="archived", **bad
    )
    before = domain_snapshot(conn)
    db.merge_import_tasks(conn, [payload(ts=NEW, event=event)], remote_events=[event])
    assert domain_snapshot(conn) == before


@pytest.mark.parametrize("missing", [False, True])
@pytest.mark.parametrize("wire", ["visible-tombstone", "hidden-with-visible-field"])
def test_contradictory_tombstones_reject_before_batch_writes(case, missing, wire):
    conn, _, _ = case
    if not missing:
        seed(conn)
    remote = payload(ts=NEW, tombstone=True)
    if wire == "hidden-with-visible-field":
        remote["status"] = "archived"
    before = conn.total_changes
    with pytest.raises(ValueError, match="status|tombstone"):
        db.merge_import_tasks(
            conn, [{"id": "would-be-inserted", "title": "Earlier batch row"}, remote]
        )
    assert conn.total_changes == before
    assert (
        conn.execute(
            "SELECT count(*) FROM tasks WHERE id='would-be-inserted'"
        ).fetchone()[0]
        == 0
    )


def test_conflicting_duplicate_uuid_rejects_before_writes(case):
    conn, _, _ = case
    seed(conn, "done", versioned=True)
    before = domain_snapshot(conn)
    with pytest.raises(ValueError, match="duplicate"):
        db.merge_import_tasks(
            conn,
            [payload("archived", NEW, tombstone=True), payload("done", LATER_EDIT)],
        )
    assert domain_snapshot(conn) == before


def test_ordered_hidden_to_hidden_tombstone_remains_valid(case):
    conn, _, _ = case
    seed(conn, "archived", versioned=True)
    db.merge_import_tasks(conn, [payload("cancelled", NEW, tombstone=True)])
    assert (
        conn.execute("SELECT status FROM tasks WHERE id=?", (TID,)).fetchone()[0]
        == "cancelled"
    )


@pytest.mark.parametrize("hidden", ["archived", "cancelled"])
def test_export_conflict_does_not_mutate_mixed_payload(case, hidden):
    conn, _, _ = case
    seed(conn, hidden)
    conn.execute(
        "INSERT INTO tasks(id,title,status,created_at,updated_at) VALUES('safe','Safe','not_started',?,?)",
        (OLD, OLD),
    )
    author(conn, "done", CLOSED, event_id="safe-author", aggregate_id="safe")
    tasks = [
        {"id": "safe", "status": "not_started", "_field_ts": {}},
        {"id": TID, "status": hidden, "_field_ts": {}},
    ]
    before = copy.deepcopy(tasks)
    with pytest.raises(db.TaskExportConflict, match="status"):
        db.canonicalize_exported_task_statuses(conn, tasks)
    assert tasks == before


@pytest.mark.parametrize("incremental", [False, True])
@pytest.mark.parametrize("supplied_overrides", [False, True])
def test_export_blocks_before_attachment_copy_or_cleanup(
    case, tmp_path, incremental, supplied_overrides
):
    conn, bridge, _ = case
    seed(conn)
    root = tmp_path / "source-attachments"
    relative = f"{TID}/blob.bin"
    (root / TID).mkdir(parents=True)
    (root / relative).write_bytes(b"new source bytes")
    (bridge / "attachments" / TID).mkdir()
    (bridge / "attachments" / relative).write_bytes(b"old bridge bytes")
    (bridge / "attachments" / "orphan.bin").write_bytes(b"keep on blocked export")
    (bridge / "tasks" / "stale.json").write_text('{"id":"stale"}', encoding="utf-8")
    conn.execute(
        "INSERT INTO task_attachments(attachment_id,task_id,file_name,stored_relpath,status,created_at,updated_at) "
        "VALUES('fixture-attachment',?,'blob.bin',?,'active',?,?)",
        (TID, relative, OLD, OLD),
    )
    before = files(bridge)
    with pytest.raises(db.TaskExportConflict, match="status"):
        db.export_task_files(
            conn,
            str(bridge),
            changed_since=OLD if incremental else None,
            attachment_root=str(root),
            export_overrides={} if supplied_overrides else None,
        )
    assert files(bridge) == before


def test_public_aged_out_hidden_conflict_blocks_worker_export_and_git(
    case, monkeypatch
):
    conn, bridge, path = case
    seed(conn)
    conn.execute(
        "UPDATE tasks SET visibility='public', tombstone_pushed_at=? WHERE id=?",
        (OLD, TID),
    )
    # Retention must be unambiguously expired, independently of the test date.
    conn.execute(
        "UPDATE tasks SET tombstone_pushed_at='2020-01-01T00:00:00+00:00' WHERE id=?",
        (TID,),
    )
    for name in ("index.json", "shared.json", "kanban_payload.json"):
        (bridge / name).write_text('{"tasks":[]}', encoding="utf-8")
    before = files(bridge)
    calls = []
    monkeypatch.setattr(worker, "ensure_bridge_repo_ready", lambda _: (True, None))
    monkeypatch.setattr(
        worker, "ensure_bridge_git_identity", lambda _: {"changed": False}
    )
    monkeypatch.setattr(
        worker, "_sync_bridge_repo_fast_forward", lambda _: (True, None)
    )
    monkeypatch.setattr(
        worker, "load_remote_tasks_for_merge", lambda *a, **kw: ([], True)
    )
    monkeypatch.setattr(memory_audit, "maybe_run_memory_audit", lambda *a, **kw: {})

    def git(repo, *args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(worker, "git_run", git)
    monkeypatch.setattr(worker, "git_retry", git)
    result = worker.main(force=True, bridge_repo=str(bridge), db_path=str(path))
    assert result.get("blocked_by_task_export_conflict"), result
    assert not result["pushed"]
    assert not any(args[0] in ("add", "commit", "push") for args in calls)
    assert files(bridge) == before


@pytest.mark.parametrize("hidden", ["archived", "cancelled"])
def test_audit_preserves_hidden_status_while_repairing_newer_title(case, hidden):
    conn, _, _ = case
    seed(conn, hidden)
    author(conn, "New title", LATER_EDIT, event_id="title-author", field="title")
    before_status = domain_snapshot(conn)[1:]
    result = memory_audit.rebuild_task_from_events(conn, TID, repair=True)
    assert conn.execute("SELECT status,title FROM tasks WHERE id=?", (TID,)).fetchone()[
        :
    ] == (hidden, "New title")
    assert result["repaired_fields"] == ["title"]
    assert "status" in result["blocked_fields"]
    assert domain_snapshot(conn)[1:] == before_status
    assert (
        memory_audit.rebuild_task_from_events(conn, TID, repair=True)["repaired_fields"]
        == []
    )


@pytest.mark.parametrize("hidden", ["archived", "cancelled"])
def test_explicit_mcp_confirmation_authors_once_and_allows_later_reopening(
    case, monkeypatch, hidden
):
    conn, bridge, path = case
    seed(conn, hidden)
    monkeypatch.setattr(
        task_server, "_get_write_conn", lambda: db.get_conn_immediate(str(path))
    )
    monkeypatch.setattr(task_server, "_get_conn", lambda: db.get_conn(str(path)))
    monkeypatch.setattr(
        task_server, "_vec_sync_task_safe", lambda _: pytest.fail("not a content edit")
    )
    result = json.loads(task_server.update_task.fn(TID, status=hidden))
    assert "error" not in result
    _, version, events = domain_snapshot(conn)
    assert version["new_value"] == hidden
    event = conn.execute(
        "SELECT * FROM memory_events WHERE event_id=?", (version["source_event_id"],)
    ).fetchone()
    assert event["event_type"] == "task_field_set"
    assert event["tool_name"] == "sqlite-tasks.update_task"
    assert event["old_value"] == event["new_value"] == hidden
    assert (
        conn.execute(
            "SELECT tombstone_pushed_at FROM tasks WHERE id=?", (TID,)
        ).fetchone()[0]
        is None
    )
    db.export_task_files(conn, str(bridge))
    db.export_index_json(conn, str(bridge))
    snapshot = domain_snapshot(conn)
    task_server.update_task.fn(TID, status=hidden)
    assert domain_snapshot(conn) == snapshot
    assert len(events) == 2
    task_server.update_task.fn(TID, status="not_started")
    db.export_task_files(conn, str(bridge))
    exported = json.loads(
        (bridge / "tasks" / f"{TID}.json").read_text(encoding="utf-8")
    )
    assert exported["status"] == exported["_field_ts"]["status"][4] == "not_started"


def test_unrelated_edit_and_consistent_confirmation_do_not_author_status(case):
    conn, _, _ = case
    seed(conn, versioned=True)
    before = domain_snapshot(conn)[1:]
    assert db.apply_task_mutation(conn, TID, {"status": "archived"})["updated"] == 0
    db.apply_task_mutation(conn, TID, {"description": "Unrelated edit"})
    assert domain_snapshot(conn)[1:] == before


def test_new_genuine_event_cannot_supply_missing_archive_token(case):
    conn, _, _ = case
    seed(conn)
    reopened = author(
        conn, "not_started", NEW, event_id="ambiguous-reopen", old_value="archived"
    )
    before = domain_snapshot(conn)
    db.merge_import_tasks(
        conn, [payload("not_started", NEW, event=reopened)], remote_events=[reopened]
    )
    assert domain_snapshot(conn) == before


def test_export_can_project_valid_causal_reopening_before_import_repair(case):
    conn, _, _ = case
    seed(conn, versioned=True)
    reopened = author(
        conn, "not_started", NEW, event_id="real-reopen", old_value="archived"
    )
    tasks = [{"id": TID, "status": "archived", "_field_ts": {}}]
    db.canonicalize_exported_task_statuses(conn, tasks)
    assert tasks[0]["status"] == "not_started"
    assert (
        tasks[0]["_field_ts"]["status"]
        == payload("not_started", NEW, event=reopened)["_field_ts"]["status"]
    )


def test_audit_genuine_reopening_does_not_claim_repair_authority(case):
    conn, _, _ = case
    seed(conn, versioned=True)
    reopened = author(
        conn, "not_started", NEW, event_id="real-reopen", old_value="archived"
    )
    result = memory_audit.rebuild_task_from_events(conn, TID, repair=True)
    assert result["repaired_fields"] == ["status"]
    exported = [{"id": TID, "status": "not_started", "_field_ts": {}}]
    db.canonicalize_exported_task_statuses(conn, exported)
    assert exported[0]["_field_ts"]["status"][3] == reopened["event_id"]


@pytest.mark.parametrize("surface", ["index", "public"])
def test_direct_projection_export_conflict_preserves_bytes(case, surface):
    conn, bridge, _ = case
    seed(conn)
    conn.execute("UPDATE tasks SET visibility='public' WHERE id=?", (TID,))
    (bridge / "index.json").write_text('{"tasks":[]}', encoding="utf-8")
    before = files(bridge)
    with pytest.raises(db.TaskExportConflict, match="status"):
        if surface == "index":
            db.export_index_json(conn, str(bridge), export_overrides={})
        else:
            worker._export_public_knowledge(conn)
    assert files(bridge) == before


@pytest.mark.parametrize("hidden", ["archived", "cancelled"])
def test_explicit_confirmation_overrides_pending_real_reopening(case, hidden):
    conn, bridge, _ = case
    seed(conn, hidden, versioned=True)
    reopened = author(
        conn, "not_started", NEW, event_id="real-reopen", old_value=hidden
    )
    assert (
        db.apply_task_mutation(
            conn, TID, {"status": hidden}, tool_name="fixture.confirm"
        )["updated"]
        == 1
    )
    version = domain_snapshot(conn)[1]
    assert version["updated_order"] > reopened["logical_clock"]
    assert version["new_value"] == hidden
    before = domain_snapshot(conn)
    assert db.apply_task_mutation(conn, TID, {"status": hidden})["updated"] == 0
    db.export_index_json(conn, str(bridge))
    assert domain_snapshot(conn) == before
    item = json.loads((bridge / "index.json").read_text(encoding="utf-8"))["tasks"][0]
    assert item["status"] == hidden and item["_tombstone"]


@pytest.mark.parametrize("aged", [False, True])
def test_pending_reopening_all_projections_roundtrip_before_row_repair(
    case, tmp_path, aged
):
    conn, bridge, _ = case
    seed(conn, versioned=True)
    reopened = author(
        conn, "not_started", NEW, event_id="real-reopen", old_value="archived"
    )
    if aged:
        conn.execute(
            "UPDATE tasks SET tombstone_pushed_at='2020-01-01T00:00:00+00:00' WHERE id=?",
            (TID,),
        )
    shared = worker._export_tasks(conn)
    db.export_task_files(conn, str(bridge), changed_since=LATER_EDIT)
    db.export_index_json(conn, str(bridge))
    worker.write_kanban_payload(str(bridge), {"tasks": shared})
    index = json.loads((bridge / "index.json").read_text(encoding="utf-8"))["tasks"]
    task = json.loads((bridge / "tasks" / f"{TID}.json").read_text(encoding="utf-8"))
    kanban = json.loads((bridge / "kanban_payload.json").read_text(encoding="utf-8"))[
        "tasks"
    ]
    for surface in (shared, index, [task], kanban):
        item = next(t for t in surface if t["id"] == TID)
        assert item["status"] == "not_started" and not item.get("_tombstone")
    fresh_path = tmp_path / "fresh-peer.db"
    init_db(str(fresh_path))
    with db.get_conn(str(fresh_path)) as fresh:
        db.import_memory_events(fresh, [reopened])
        db.merge_import_tasks(fresh, index, remote_events=[reopened])
        assert (
            fresh.execute("SELECT status FROM tasks WHERE id=?", (TID,)).fetchone()[0]
            == "not_started"
        )


def test_peer_reopening_followed_by_visible_edit_is_not_blocked(case):
    conn, _, _ = case
    seed(conn, versioned=True)
    author(conn, "not_started", NEW, event_id="real-reopen", old_value="archived")
    edited = author(
        conn,
        "in_progress",
        LATER_EDIT,
        event_id="later-visible-edit",
        old_value="not_started",
    )
    db.merge_import_tasks(
        conn, [payload("in_progress", LATER_EDIT, event=edited)], remote_events=[edited]
    )
    assert (
        conn.execute("SELECT status FROM tasks WHERE id=?", (TID,)).fetchone()[0]
        == "in_progress"
    )


def test_unrelated_hidden_value_cannot_witness_reopening(case):
    conn, _, _ = case
    seed(conn, "cancelled", versioned=True)
    event = author(
        conn, "not_started", NEW, event_id="wrong-hidden-reopen", old_value="archived"
    )
    before = domain_snapshot(conn)
    db.merge_import_tasks(
        conn, [payload("not_started", NEW, event=event)], remote_events=[event]
    )
    assert domain_snapshot(conn) == before


def test_visible_projection_is_not_stamped_as_pushed_tombstone(case):
    conn, bridge, _ = case
    seed(conn, versioned=True)
    author(conn, "not_started", NEW, event_id="real-reopen", old_value="archived")
    exported = db.export_task_files(conn, str(bridge))
    before = domain_snapshot(conn)
    assert db.mark_tombstones_pushed(conn, exported, LATER_EDIT) == 0
    assert domain_snapshot(conn) == before


def test_later_hidden_author_requires_another_reopening(case):
    conn, _, _ = case
    seed(conn, versioned=True)
    author(conn, "not_started", NEW, event_id="real-reopen", old_value="archived")
    author(
        conn, "cancelled", LATER_EDIT, event_id="later-closure", old_value="not_started"
    )
    last = author(
        conn,
        "done",
        "2026-10-02T08:00:00+00:00",
        event_id="unrelated-done",
        old_value="in_progress",
    )
    before = domain_snapshot(conn)
    db.merge_import_tasks(
        conn, [payload("done", last["event_ts"], event=last)], remote_events=[last]
    )
    assert domain_snapshot(conn) == before
    reopened = author(
        conn,
        "not_started",
        "2026-10-02T09:00:00+00:00",
        event_id="second-reopen",
        old_value="cancelled",
    )
    db.merge_import_tasks(
        conn,
        [payload("not_started", reopened["event_ts"], event=reopened)],
        remote_events=[reopened],
    )
    assert (
        conn.execute("SELECT status FROM tasks WHERE id=?", (TID,)).fetchone()[0]
        == "not_started"
    )
