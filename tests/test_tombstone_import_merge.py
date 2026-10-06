"""Tombstone imports keep the remote status value and merge the other fields.

The bridge never transports the event ledger, so on a peer the status value a
remote tombstone carries survives only in ``task_field_versions.new_value``.
Synthetic SQLite and bridge bytes only.
"""

import json
import sqlite3

import pytest

import db_utils as db
from schema import init_db

OLD = "2026-09-24T10:00:00+00:00"
CLOSED = "2026-10-02T05:00:00+00:00"
NEW = "2026-10-02T06:00:00+00:00"
LATER_EDIT = "2026-10-02T07:00:00+00:00"
TID = "tombstone-fixture"
LOCAL = "fixture-local"
PEER = "fixture-peer"


def clock(ts, counter=1):
    return db._pack_logical_clock(db._iso_to_epoch_ms(ts), counter)


@pytest.fixture
def case(tmp_path):
    path = tmp_path / "memory.db"
    init_db(str(path))
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    bridge = tmp_path / "bridge"
    for directory in ("tasks", "attachments", "entities"):
        (bridge / directory).mkdir(parents=True)
    yield conn, bridge
    assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    conn.close()


def seed_row(conn, status, tid=TID, **fields):
    conn.execute(
        "INSERT INTO tasks(id,title,status,created_at,updated_at) VALUES(?,?,?,?,?)",
        (tid, "Fixture", status, OLD, OLD),
    )
    for field, value in fields.items():
        conn.execute(f"UPDATE tasks SET {field}=? WHERE id=?", (value, tid))


def author_local(conn, field, value, ts, tid=TID):
    """A normal local write: authoring event plus its valued field version."""
    event = {
        "event_id": f"local-{tid}-{field}-{ts}",
        "event_type": "task_field_set",
        "aggregate_kind": "task",
        "aggregate_id": tid,
        "field_name": field,
        "new_value": value,
        "machine_id": LOCAL,
        "event_ts": ts,
        "logical_clock": clock(ts),
        "tool_name": "fixture.update_task",
    }
    db.import_memory_events(conn, [event])
    db._store_task_field_version(
        conn,
        tid,
        field,
        updated_at=ts,
        updated_by=LOCAL,
        new_value=value,
        updated_order=event["logical_clock"],
        source_event_id=event["event_id"],
    )


def entry(ts, writer=PEER, value=None, counter=1, *, valued=False):
    """A wire _field_ts entry as export writes it (value only for status/section)."""
    item = [ts, writer, clock(ts, counter), f"{writer}-{ts}-{counter}"]
    return [*item, value] if valued else item


def remote_task(status, ts=NEW, tid=TID, *, tombstone=True, fields=None):
    payload = {
        "id": tid,
        "title": "Fixture",
        "status": status,
        "created_at": OLD,
        "updated_at": ts,
        "_source_machine_id": PEER,
        "_field_ts": {"status": entry(ts, value=status, valued=True)},
        **({"_tombstone": True} if tombstone else {}),
    }
    for counter, (field, (value, field_ts)) in enumerate((fields or {}).items(), 2):
        payload[field] = value
        payload["_field_ts"][field] = entry(
            field_ts,
            value=value,
            counter=counter,
            valued=field in db.FIELD_TS_VALUE_FIELDS,
        )
    return payload


def row(conn, tid=TID):
    return dict(conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())


def version(conn, field, tid=TID):
    return dict(
        conn.execute(
            "SELECT * FROM task_field_versions WHERE task_id=? AND field_name=?",
            (tid, field),
        ).fetchone()
    )


def merge_events(conn, tid=TID):
    return sorted(
        (r["field_name"], r["new_value"])
        for r in conn.execute(
            "SELECT field_name, new_value FROM memory_events "
            "WHERE aggregate_id=? AND event_type='merge'",
            (tid,),
        )
    )


def authority(conn, tid=TID):
    state = db._compute_authoritative_task_statuses(
        conn, [{"id": tid, "status": row(conn, tid)["status"]}]
    )[tid]
    return state["value"], state["updated_order"]


def exported_status(conn, bridge, tid=TID):
    db.export_index_json(conn, str(bridge))
    index = json.loads((bridge / "index.json").read_text(encoding="utf-8"))
    item = next(t for t in index["tasks"] if t["id"] == tid)
    return item["status"], item["_field_ts"]["status"][2], bool(item.get("_tombstone"))


def snapshot(conn):
    return [
        [tuple(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY 1, 2")]
        for table in ("tasks", "task_field_versions", "memory_events")
    ]


def test_tombstone_onto_open_row_keeps_value_and_event(case):
    conn, bridge = case
    seed_row(conn, "not_started")
    author_local(conn, "status", "not_started", OLD)
    remote = remote_task("cancelled")

    assert db.merge_import_tasks(conn, [remote], import_content=True) == (0, 1)

    stored = version(conn, "status")
    assert row(conn)["status"] == "cancelled"
    assert (stored["updated_order"], stored["updated_by"]) == (clock(NEW), PEER)
    assert (stored["old_value"], stored["new_value"]) == ("not_started", "cancelled")
    assert merge_events(conn) == [("status", "cancelled")]
    # Without the stored value the older local event outranked the tombstone
    # and the export of this hidden row was refused.
    assert authority(conn) == ("cancelled", clock(NEW))
    assert exported_status(conn, bridge) == ("cancelled", clock(NEW), True)

    before = snapshot(conn)
    assert db.merge_import_tasks(conn, [remote], import_content=True) == (0, 0)
    assert snapshot(conn) == before


def test_closed_both_sides_merges_newer_remote_fields(case):
    conn, _ = case
    seed_row(conn, "archived", section="inbox", priority="medium", notes="Local")
    author_local(conn, "status", "archived", CLOSED)
    for field, value in (("section", "inbox"), ("priority", "medium")):
        author_local(conn, field, value, OLD)
    author_local(conn, "notes", "Local", OLD)
    status_before = version(conn, "status")
    remote = remote_task(
        "archived",
        CLOSED,
        fields={
            "section": ("today", LATER_EDIT),
            "priority": ("high", LATER_EDIT),
            "notes": ("Remote notes", LATER_EDIT),
        },
    )
    # The peer holds the very same closure write.
    remote["_field_ts"]["status"] = [
        CLOSED,
        LOCAL,
        clock(CLOSED),
        f"local-{TID}-status-{CLOSED}",
        "archived",
    ]

    assert db.merge_import_tasks(conn, [remote], import_content=True) == (0, 3)

    merged = row(conn)
    assert (merged["status"], merged["section"], merged["priority"]) == (
        "archived",
        "today",
        "high",
    )
    assert merged["notes"] == "Remote notes"
    assert version(conn, "status") == status_before
    for field, value in (
        ("section", "today"),
        ("priority", "high"),
        ("notes", "Remote notes"),
    ):
        stored = version(conn, field)
        assert (stored["updated_at"], stored["updated_by"], stored["new_value"]) == (
            LATER_EDIT,
            PEER,
            value,
        )
    assert merge_events(conn) == [
        ("notes", "Remote notes"),
        ("priority", "high"),
        ("section", "today"),
    ]


@pytest.mark.parametrize("row_status", ["cancelled", "not_started"])
def test_equal_key_null_value_is_repaired(case, row_status):
    conn, bridge = case
    seed_row(conn, row_status)
    author_local(conn, "status", "not_started", OLD)
    remote = remote_task("cancelled")
    # What the old tombstone branch left behind: the remote clock, no value.
    db._store_task_field_version(
        conn,
        TID,
        "status",
        updated_at=NEW,
        updated_by=PEER,
        updated_order=clock(NEW),
        source_event_id=remote["_field_ts"]["status"][3],
    )
    assert authority(conn) == ("not_started", clock(OLD))
    if row_status == "cancelled":
        with pytest.raises(db.TaskExportConflict, match="status"):
            exported_status(conn, bridge)
    else:
        assert exported_status(conn, bridge) == ("not_started", clock(OLD), False)

    db.merge_import_tasks(conn, [remote], import_content=True)

    stored = version(conn, "status")
    assert (stored["updated_order"], stored["updated_by"]) == (clock(NEW), PEER)
    assert stored["new_value"] == "cancelled"
    assert row(conn)["status"] == "cancelled"
    assert authority(conn) == ("cancelled", clock(NEW))
    assert exported_status(conn, bridge) == ("cancelled", clock(NEW), True)
    expected_events = [] if row_status == "cancelled" else [("status", "cancelled")]
    assert merge_events(conn) == expected_events

    before = snapshot(conn)
    db.merge_import_tasks(conn, [remote], import_content=True)
    assert snapshot(conn) == before


def test_live_record_merge_is_unchanged(case):
    conn, _ = case
    # 1. A newer live status still merges through the per-field path.
    seed_row(conn, "not_started", tid="live-newer")
    author_local(conn, "status", "not_started", OLD, tid="live-newer")
    # 2. An equal-key unvalued version is a tombstone-only repair.
    seed_row(conn, "in_progress", tid="live-equal")
    db._store_task_field_version(
        conn,
        "live-equal",
        "status",
        updated_at=NEW,
        updated_by=PEER,
        updated_order=clock(NEW),
        source_event_id=f"{PEER}-{NEW}-1",
    )
    # 3. A row that drifted from its valued authority is still repaired.
    seed_row(conn, "not_started", tid="live-drift")
    author_local(conn, "status", "done", CLOSED, tid="live-drift")
    batch = [
        remote_task("in_progress", NEW, "live-newer", tombstone=False),
        remote_task("in_progress", NEW, "live-equal", tombstone=False),
        remote_task("done", CLOSED, "live-drift", tombstone=False),
    ]
    batch[2]["_field_ts"]["status"] = [
        CLOSED,
        LOCAL,
        clock(CLOSED),
        f"local-live-drift-status-{CLOSED}",
        "done",
    ]

    db.merge_import_tasks(conn, batch, import_content=True)

    newer = version(conn, "status", "live-newer")
    assert row(conn, "live-newer")["status"] == "in_progress"
    assert (newer["updated_order"], newer["new_value"]) == (clock(NEW), "in_progress")
    assert merge_events(conn, "live-newer") == [("status", "in_progress")]
    assert row(conn, "live-equal")["status"] == "in_progress"
    assert version(conn, "status", "live-equal")["new_value"] is None
    assert merge_events(conn, "live-equal") == []
    assert row(conn, "live-drift")["status"] == "done"
    assert merge_events(conn, "live-drift") == []


def test_tombstone_equal_key_keeps_local_authored_value(case):
    """A tombstone must not run the live path's equal-key value repair.

    Same clock and writer as the local authoring write but a drifted value:
    only a strictly newer remote key may change a field of a tombstoned row.
    """
    conn, _ = case
    seed_row(conn, "archived", section="inbox")
    author_local(conn, "status", "archived", CLOSED)
    author_local(conn, "section", "inbox", OLD)
    section_before = version(conn, "section")
    remote = remote_task("archived", CLOSED, fields={"section": ("today", OLD)})
    remote["_field_ts"]["status"] = [
        CLOSED,
        LOCAL,
        clock(CLOSED),
        f"local-{TID}-status-{CLOSED}",
        "archived",
    ]
    remote["_field_ts"]["section"] = [
        OLD,
        LOCAL,
        clock(OLD),
        f"local-{TID}-section-{OLD}",
        "today",
    ]

    assert db.merge_import_tasks(conn, [remote], import_content=True) == (0, 0)
    assert row(conn)["section"] == "inbox"
    assert version(conn, "section") == section_before
    assert merge_events(conn) == []
