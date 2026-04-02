"""Tests for importing extended memory payloads across machines."""

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from db_utils import (
    add_task_attachment,
    export_index_json,
    export_task_files,
    import_remote_bridge_data,
    load_remote_tasks_for_merge,
    merge_import_tasks,
    now_iso,
    record_memory_event,
    sync_task_attachments_from_remote,
)
from schema import init_db


@pytest.fixture
def conn(tmp_path):
    db_path = str(tmp_path / "memory.db")
    init_db(db_path)
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    yield conn, str(tmp_path)
    conn.close()


def test_import_remote_bridge_data_imports_context_before_claims_and_facts(conn):
    db, bridge_dir = conn
    payload = {
        "context_chunks": [
            {
                "chunk_id": "chunk-1",
                "session_id": None,
                "entity_id": None,
                "source_type": "observation",
                "source_ref": "obs-1",
                "source_hash": "hash-1",
                "title": "Chunk title",
                "body": "Service uses Redis.",
                "language": "en",
                "state": "enrichable",
                "enrich_policy": "manual",
                "materiality_score": 0.8,
                "last_human_update_at": None,
                "last_ai_attempt_at": None,
                "created_at": "2026-03-31T10:00:00+00:00",
                "updated_at": "2026-03-31T10:00:00+00:00",
            }
        ],
        "context_questions": [
            {
                "question_id": "question-1",
                "chunk_id": "chunk-1",
                "question_text": "Which Redis deployment?",
                "question_type": "scope",
                "priority_score": 0.9,
                "state": "open",
                "answered_by": None,
                "answered_at": None,
                "answer_text": None,
                "created_at": "2026-03-31T10:01:00+00:00",
            }
        ],
        "candidate_claims": [
            {
                "claim_id": "claim-1",
                "chunk_id": "chunk-1",
                "subject": "Service",
                "predicate": "uses",
                "object_text": "Redis",
                "object_type": "text",
                "claim_scope": "memory",
                "confidence": 0.91,
                "status": "candidate",
                "requires_human": 0,
                "promoted_to_fact_id": None,
                "created_at": "2026-03-31T10:02:00+00:00",
                "updated_at": "2026-03-31T10:02:00+00:00",
            }
        ],
        "claim_evidence": [
            {
                "evidence_id": "evidence-1",
                "claim_id": "claim-1",
                "evidence_type": "chunk",
                "evidence_ref": "chunk-1",
                "weight": 1.0,
                "excerpt": "Service uses Redis.",
                "source_start": 0,
                "source_end": 18,
                "created_at": "2026-03-31T10:02:30+00:00",
            }
        ],
        "canonical_facts": [
            {
                "fact_id": "fact-1",
                "subject": "Service",
                "predicate": "uses",
                "object_text": "Redis",
                "object_type": "text",
                "fact_scope": "memory",
                "provenance_summary": "Promoted from claim-1",
                "confidence": 0.93,
                "validation_mode": "human_confirmed",
                "source_claim_id": "claim-1",
                "valid_from": "2026-03-31T10:03:00+00:00",
                "valid_to": None,
                "superseded_by_fact_id": None,
                "contradiction_count": 0,
                "created_at": "2026-03-31T10:03:00+00:00",
                "updated_at": "2026-03-31T10:03:00+00:00",
            }
        ],
        "memory_events": [
            {
                "event_id": "event-claim-1",
                "event_type": "claim_extract",
                "aggregate_kind": "claim",
                "aggregate_id": "claim-1",
                "field_name": None,
                "actor_type": "system",
                "actor_id": "test",
                "machine_id": "peer-a",
                "tool_name": "sqlite-intel.extract_candidate_claims",
                "logical_clock": 114255257600000000,
                "event_ts": "2026-03-31T10:02:00+00:00",
                "old_value": None,
                "new_value": None,
                "payload_json": None,
                "parent_event_id": None,
                "source_kind": "chunk",
                "source_ref": "chunk-1",
                "source_excerpt": "Service uses Redis.",
                "source_start": 0,
                "source_end": 18,
            },
            {
                "event_id": "event-fact-1",
                "event_type": "fact_promote",
                "aggregate_kind": "fact",
                "aggregate_id": "fact-1",
                "field_name": None,
                "actor_type": "system",
                "actor_id": "test",
                "machine_id": "peer-a",
                "tool_name": "sqlite-intel.promote_candidate",
                "logical_clock": 114255257665536000,
                "event_ts": "2026-03-31T10:03:00+00:00",
                "old_value": None,
                "new_value": None,
                "payload_json": None,
                "parent_event_id": None,
                "source_kind": "claim",
                "source_ref": "claim-1",
                "source_excerpt": None,
                "source_start": None,
                "source_end": None,
            },
        ],
    }

    result = import_remote_bridge_data(db, bridge_dir, payload)

    assert result["chunks"] == 1
    assert result["questions"] == 1
    assert result["claims"] == 1
    assert result["claim_evidence"] == 1
    assert result["facts"] == 1
    assert (
        db.execute(
            "SELECT chunk_id FROM context_chunks WHERE chunk_id = 'chunk-1'"
        ).fetchone()
        is not None
    )
    assert (
        db.execute(
            "SELECT claim_id FROM candidate_claims WHERE claim_id = 'claim-1'"
        ).fetchone()
        is not None
    )
    assert (
        db.execute(
            "SELECT fact_id FROM canonical_facts WHERE fact_id = 'fact-1'"
        ).fetchone()
        is not None
    )


def test_import_remote_bridge_data_uses_event_heads_for_fact_merge(conn):
    db, bridge_dir = conn
    db.execute(
        "INSERT INTO canonical_facts ("
        "fact_id, subject, predicate, object_text, object_type, fact_scope, "
        "provenance_summary, confidence, validation_mode, source_claim_id, "
        "valid_from, created_at, updated_at"
        ") VALUES ('fact-1', 'Service', 'uses', 'Redis', 'text', 'memory', "
        "'local', 0.95, 'human_confirmed', NULL, '2026-03-31T12:00:00+00:00', "
        "'2026-03-31T12:00:00+00:00', '2026-03-31T12:00:00+00:00')"
    )
    record_memory_event(
        db,
        event_type="fact_promote",
        aggregate_kind="fact",
        aggregate_id="fact-1",
        tool_name="local.test",
        event_ts="2026-03-31T12:00:00+00:00",
    )

    payload = {
        "canonical_facts": [
            {
                "fact_id": "fact-1",
                "subject": "Service",
                "predicate": "uses",
                "object_text": "PostgreSQL",
                "object_type": "text",
                "fact_scope": "memory",
                "provenance_summary": "remote older",
                "confidence": 0.80,
                "validation_mode": "imported",
                "source_claim_id": None,
                "valid_from": "2026-03-31T13:00:00+00:00",
                "valid_to": None,
                "superseded_by_fact_id": None,
                "contradiction_count": 0,
                "created_at": "2026-03-31T13:00:00+00:00",
                "updated_at": "2026-03-31T13:00:00+00:00",
            }
        ],
        "memory_events": [
            {
                "event_id": "remote-old-fact",
                "event_type": "fact_promote",
                "aggregate_kind": "fact",
                "aggregate_id": "fact-1",
                "field_name": None,
                "actor_type": "system",
                "actor_id": "peer",
                "machine_id": "peer-a",
                "tool_name": "remote.test",
                "logical_clock": 42,
                "event_ts": "2026-03-31T13:00:00+00:00",
                "old_value": None,
                "new_value": None,
                "payload_json": None,
                "parent_event_id": None,
                "source_kind": None,
                "source_ref": None,
                "source_excerpt": None,
                "source_start": None,
                "source_end": None,
            }
        ],
    }

    result = import_remote_bridge_data(db, bridge_dir, payload)
    fact = db.execute(
        "SELECT object_text FROM canonical_facts WHERE fact_id = 'fact-1'"
    ).fetchone()

    assert result["facts"] == 0
    assert fact["object_text"] == "Redis"


def test_bridge_roundtrip_keeps_unsafe_ids_and_recent_tombstones(conn):
    source_db, base_dir = conn
    bridge_dir = os.path.join(base_dir, "bridge")
    os.makedirs(bridge_dir, exist_ok=True)

    created = now_iso()
    source_db.execute(
        "INSERT INTO tasks (id, title, description, notes, status, section, priority, "
        "type, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "test_inbox_2",
            "Unsafe active task",
            "Unsafe active description",
            "Unsafe active notes",
            "not_started",
            "inbox",
            "medium",
            "task",
            created,
            created,
        ),
    )
    source_db.execute(
        "INSERT INTO tasks (id, title, description, notes, status, section, priority, "
        "type, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "archived-1",
            "Archived task",
            "Archived description",
            "Archived notes",
            "archived",
            "inbox",
            "medium",
            "task",
            created,
            created,
        ),
    )
    source_db.execute(
        "INSERT INTO tasks (id, title, description, notes, status, section, priority, "
        "type, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "cancelled-1",
            "Cancelled task",
            "Cancelled description",
            "Cancelled notes",
            "cancelled",
            "inbox",
            "medium",
            "task",
            created,
            created,
        ),
    )

    export_task_files(source_db, bridge_dir)
    export_index_json(source_db, bridge_dir)
    remote_tasks, loaded = load_remote_tasks_for_merge(bridge_dir, {})

    recovered_db_path = os.path.join(base_dir, "recovered.db")
    init_db(recovered_db_path)
    recovered = sqlite3.connect(recovered_db_path, isolation_level=None)
    recovered.row_factory = sqlite3.Row
    recovered.execute("PRAGMA foreign_keys=ON")
    try:
        new_count, updated = merge_import_tasks(
            recovered,
            remote_tasks,
            import_content=True,
        )
        assert loaded is True
        assert new_count == 3
        assert updated == 0
        rows = recovered.execute(
            "SELECT id, status, description, notes FROM tasks ORDER BY id"
        ).fetchall()
        by_id = {row["id"]: dict(row) for row in rows}
        assert set(by_id) == {"archived-1", "cancelled-1", "test_inbox_2"}
        assert by_id["test_inbox_2"]["status"] == "not_started"
        assert by_id["archived-1"]["status"] == "archived"
        assert by_id["cancelled-1"]["status"] == "cancelled"
        assert by_id["archived-1"]["description"] == "Archived description"
        assert by_id["cancelled-1"]["notes"] == "Cancelled notes"
    finally:
        recovered.close()


def test_bridge_roundtrip_keeps_task_attachments(conn):
    source_db, base_dir = conn
    bridge_dir = os.path.join(base_dir, "bridge_attachments")
    os.makedirs(bridge_dir, exist_ok=True)
    local_root = os.path.join(base_dir, "local_attachments")
    recovered_root = os.path.join(base_dir, "recovered_attachments")

    created = now_iso()
    source_db.execute(
        "INSERT INTO tasks (id, title, description, notes, status, section, priority, "
        "type, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "task-attach",
            "Attachment task",
            "Task description",
            None,
            "not_started",
            "inbox",
            "medium",
            "task",
            created,
            created,
        ),
    )
    source_file = os.path.join(base_dir, "sample.pdf")
    with open(source_file, "wb") as fh:
        fh.write(b"fake-pdf")

    attachment = add_task_attachment(
        source_db,
        "task-attach",
        source_file,
        local_root=local_root,
    )

    export_task_files(source_db, bridge_dir, attachment_root=local_root)
    export_index_json(source_db, bridge_dir)
    remote_tasks, loaded = load_remote_tasks_for_merge(bridge_dir, {})

    recovered_db_path = os.path.join(base_dir, "recovered_attachments.db")
    init_db(recovered_db_path)
    recovered = sqlite3.connect(recovered_db_path, isolation_level=None)
    recovered.row_factory = sqlite3.Row
    recovered.execute("PRAGMA foreign_keys=ON")
    try:
        merge_import_tasks(recovered, remote_tasks, import_content=True)
        imported, removed = sync_task_attachments_from_remote(
            recovered,
            remote_tasks,
            bridge_dir,
            local_root=recovered_root,
        )
        row = recovered.execute(
            "SELECT file_name, stored_relpath, status FROM task_attachments WHERE task_id = 'task-attach'"
        ).fetchone()
        local_copy = os.path.join(
            recovered_root,
            attachment["stored_relpath"].replace("/", os.sep),
        )
        assert loaded is True
        assert imported == 1
        assert removed == 0
        assert row is not None
        assert row["file_name"] == "sample.pdf"
        assert row["status"] == "active"
        assert os.path.isfile(local_copy)
        with open(local_copy, "rb") as fh:
            assert fh.read() == b"fake-pdf"
    finally:
        recovered.close()
