"""Regression coverage for every table read by the bridge export payload."""

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from db_utils import TaskDAO, bridge_change_summary
from schema import init_db


BEFORE = "2026-08-03T10:00:00+00:00"
AFTER = "2026-08-03T10:01:00+00:00"


@pytest.fixture
def conn(tmp_path):
    db_path = tmp_path / "memory.db"
    init_db(str(db_path))
    connection = sqlite3.connect(str(db_path), isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        yield connection
    finally:
        connection.close()


def _summary(conn):
    return bridge_change_summary(conn, BEFORE)


def _insert_task(conn, task_id="task-1"):
    conn.execute(
        "INSERT INTO tasks "
        "(id, title, description, status, priority, section, type, created_at, updated_at) "
        "VALUES (?, 'Task', '', 'not_started', 'medium', 'inbox', 'task', ?, ?)",
        (task_id, BEFORE, BEFORE),
    )


def _insert_entity(conn, entity_id=1):
    conn.execute(
        "INSERT INTO entities "
        "(id, name, entity_type, project, created_at, updated_at) "
        "VALUES (?, ?, 'person', 'shared:test', ?, ?)",
        (entity_id, f"Entity {entity_id}", BEFORE, BEFORE),
    )


def _insert_claim_parent(conn):
    conn.execute(
        "INSERT INTO context_chunks "
        "(chunk_id, source_type, source_ref, source_hash, body, created_at, updated_at) "
        "VALUES ('chunk-1', 'test', 'source-1', 'hash-1', 'body', ?, ?)",
        (BEFORE, BEFORE),
    )
    conn.execute(
        "INSERT INTO candidate_claims "
        "(claim_id, chunk_id, subject, predicate, object_text, claim_scope, confidence, created_at, updated_at) "
        "VALUES ('claim-1', 'chunk-1', 'subject', 'predicate', 'object', 'memory', 0.5, ?, ?)",
        (BEFORE, BEFORE),
    )


def test_observation_only_change_is_visible_to_incremental_gate(conn):
    _insert_entity(conn)
    conn.execute(
        "INSERT INTO observations (entity_id, content, created_at) VALUES (1, 'new', ?)",
        (AFTER,),
    )

    summary = _summary(conn)

    assert summary["changed_entities"] == 0
    assert summary["changed_observations"] == 1


def test_task_field_version_only_change_is_visible_to_incremental_gate(conn):
    _insert_task(conn)
    conn.execute(
        "INSERT INTO task_field_versions (task_id, field_name, updated_at) "
        "VALUES ('task-1', 'status', ?)",
        (AFTER,),
    )

    summary = _summary(conn)

    assert summary["changed_tasks"] == 0
    assert summary["changed_task_field_versions"] == 1


def test_task_entity_link_only_change_is_visible_to_incremental_gate(conn):
    _insert_task(conn)
    _insert_entity(conn)
    conn.execute(
        "INSERT INTO task_entity_links (task_id, entity_id, created_at) "
        "VALUES ('task-1', 1, ?)",
        (AFTER,),
    )

    summary = _summary(conn)

    assert summary["changed_tasks"] == 0
    assert summary["changed_entities"] == 0
    assert summary["changed_task_entity_links"] == 1


def test_task_entity_unlink_is_visible_and_transportable(conn):
    _insert_task(conn)
    _insert_entity(conn)
    TaskDAO.link_entity(conn, "task-1", 1, created_at=BEFORE)

    assert TaskDAO.unlink_entity(conn, "task-1", 1) == 1

    summary = _summary(conn)
    assert summary["changed_task_entity_links"] == 1
    assert TaskDAO.get_task_links(conn, "task-1") == []
    tombstone = conn.execute(
        "SELECT entity_name, deleted_at FROM task_entity_link_tombstones "
        "WHERE task_id = 'task-1'"
    ).fetchone()
    assert tombstone["entity_name"] == "Entity 1"
    assert tombstone["deleted_at"] > BEFORE


def test_task_attachment_only_change_is_visible_to_incremental_gate(conn):
    _insert_task(conn)
    conn.execute(
        "INSERT INTO task_attachments "
        "(attachment_id, task_id, file_name, stored_relpath, created_at, updated_at) "
        "VALUES ('attachment-1', 'task-1', 'file.txt', 'task-1/file.txt', ?, ?)",
        (AFTER, AFTER),
    )

    summary = _summary(conn)

    assert summary["changed_tasks"] == 0
    assert summary["changed_task_attachments"] == 1


def test_collaborator_manifest_change_is_visible_to_incremental_gate(conn):
    conn.execute(
        "INSERT INTO collaborators (github_user, added_at) VALUES ('teammate', ?)",
        (AFTER,),
    )

    summary = _summary(conn)

    assert summary["changed_collaborators"] == 1


def test_claim_evidence_only_change_is_visible_to_incremental_gate(conn):
    _insert_claim_parent(conn)
    conn.execute(
        "INSERT INTO claim_evidence "
        "(evidence_id, claim_id, evidence_type, evidence_ref, weight, created_at) "
        "VALUES ('evidence-1', 'claim-1', 'test', 'ref-1', 1.0, ?)",
        (AFTER,),
    )

    summary = _summary(conn)

    assert summary["changed_claims"] == 0
    assert summary["changed_claim_evidence"] == 1


def test_memory_artifact_only_change_is_visible_to_incremental_gate(conn):
    conn.execute(
        "INSERT INTO memory_artifacts "
        "(artifact_id, artifact_key, artifact_kind, scope_kind, scope_ref, body, created_at, updated_at) "
        "VALUES ('artifact-1', 'key-1', 'summary', 'test', 'scope-1', 'body', ?, ?)",
        (AFTER, AFTER),
    )

    summary = _summary(conn)

    assert summary["changed_memory_artifacts"] == 1


def test_memory_conflict_only_change_is_visible_to_incremental_gate(conn):
    conn.execute(
        "INSERT INTO memory_conflicts "
        "(conflict_id, conflict_key, aggregate_kind, aggregate_id, winner, created_at, updated_at) "
        "VALUES ('conflict-1', 'key-1', 'task', 'task-1', 'local', ?, ?)",
        (AFTER, AFTER),
    )

    summary = _summary(conn)

    assert summary["changed_memory_conflicts"] == 1


def test_memory_audit_state_only_change_is_visible_to_incremental_gate(conn):
    conn.execute(
        "INSERT INTO memory_audit_state (runner_name, updated_at) VALUES ('audit', ?)",
        (AFTER,),
    )

    summary = _summary(conn)

    assert summary["changed_memory_audit_state"] == 1
