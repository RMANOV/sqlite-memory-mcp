"""Tests for memory audit, replay, and truth-maintenance helpers."""

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from memory_audit import (
    govern_fact,
    list_memory_audit_issues,
    maybe_run_memory_audit,
    replay_memory_events,
    rebuild_task_from_events,
    run_memory_audit,
)
from schema import init_db


@pytest.fixture
def conn(tmp_path):
    db_path = str(tmp_path / "memory.db")
    init_db(db_path)
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    yield conn
    conn.close()


def _insert_chunk(conn, chunk_id: str, body: str = "Chunk body") -> str:
    ts = "2026-03-31T08:00:00+00:00"
    conn.execute(
        "INSERT INTO context_chunks ("
        "chunk_id, session_id, entity_id, source_type, source_ref, source_hash, "
        "title, body, language, state, enrich_policy, materiality_score, created_at, updated_at"
        ") VALUES (?, NULL, NULL, 'observation', ?, ?, ?, ?, 'bg', 'enrichable', 'manual', 0.8, ?, ?)",
        (chunk_id, f"source-{chunk_id}", f"hash-{chunk_id}", chunk_id, body, ts, ts),
    )
    return ts


def _insert_claim(
    conn,
    claim_id: str,
    *,
    chunk_id: str,
    with_evidence: bool,
) -> str:
    ts = "2026-03-31T08:05:00+00:00"
    conn.execute(
        "INSERT INTO candidate_claims ("
        "claim_id, chunk_id, subject, predicate, object_text, object_type, "
        "claim_scope, confidence, status, requires_human, created_at, updated_at"
        ") VALUES (?, ?, 'Service', 'uses', 'Redis', 'text', 'memory', 0.9, 'candidate', 0, ?, ?)",
        (claim_id, chunk_id, ts, ts),
    )
    if with_evidence:
        conn.execute(
            "INSERT INTO claim_evidence ("
            "evidence_id, claim_id, evidence_type, evidence_ref, weight, excerpt, "
            "source_start, source_end, created_at"
            ") VALUES (?, ?, 'chunk', ?, 1.0, 'Service uses Redis', 0, 18, ?)",
            (f"ev-{claim_id}", claim_id, chunk_id, ts),
        )
    return ts


def _insert_fact(
    conn,
    fact_id: str,
    *,
    object_text: str,
    source_claim_id: str | None = None,
) -> str:
    ts = "2026-03-31T08:10:00+00:00"
    conn.execute(
        "INSERT INTO canonical_facts ("
        "fact_id, subject, predicate, object_text, object_type, fact_scope, "
        "provenance_summary, confidence, validation_mode, source_claim_id, "
        "valid_from, created_at, updated_at"
        ") VALUES (?, 'Service', 'uses', ?, 'text', 'memory', 'test provenance', "
        "0.95, 'human_confirmed', ?, ?, ?, ?)",
        (fact_id, object_text, source_claim_id, ts, ts, ts),
    )
    return ts


def _add_fact_provenance(conn, fact_id: str) -> None:
    ts = "2026-03-31T08:11:00+00:00"
    conn.execute(
        "INSERT INTO provenance_links ("
        "provenance_id, subject_kind, subject_ref, source_kind, source_ref, "
        "span_start, span_end, excerpt, confidence, created_at"
        ") VALUES (?, 'fact', ?, 'chunk', ?, 0, 10, 'evidence', 1.0, ?)",
        (f"prov-{fact_id}", fact_id, f"chunk-{fact_id}", ts),
    )


def test_run_memory_audit_backfills_fact_provenance_from_source_claim(conn):
    _insert_chunk(conn, "chunk-a")
    _insert_claim(conn, "claim-a", chunk_id="chunk-a", with_evidence=True)
    _insert_fact(conn, "fact-a", object_text="Redis", source_claim_id="claim-a")

    result = run_memory_audit(conn, repair=True)

    prov_rows = conn.execute(
        "SELECT source_kind, source_ref FROM provenance_links "
        "WHERE subject_kind = 'fact' AND subject_ref = 'fact-a' "
        "ORDER BY source_kind, source_ref"
    ).fetchall()

    assert result["repairs"]["fact_provenance_backfilled"] >= 1
    assert ("claim", "claim-a") in {
        (r["source_kind"], r["source_ref"]) for r in prov_rows
    }
    assert not any(
        i["issue_type"] == "fact_missing_provenance" for i in result["issues"]
    )


def test_run_memory_audit_detects_claim_missing_evidence(conn):
    _insert_chunk(conn, "chunk-b")
    _insert_claim(conn, "claim-b", chunk_id="chunk-b", with_evidence=False)

    result = run_memory_audit(conn, repair=False)
    issues = list_memory_audit_issues(conn)

    assert any(i["issue_type"] == "claim_missing_evidence" for i in result["issues"])
    assert any(i["issue_type"] == "claim_missing_evidence" for i in issues["issues"])


def test_govern_fact_contradiction_updates_counts_and_replay(conn):
    _insert_fact(conn, "fact-left", object_text="Redis")
    _insert_fact(conn, "fact-right", object_text="PostgreSQL")
    _add_fact_provenance(conn, "fact-left")
    _add_fact_provenance(conn, "fact-right")

    result = govern_fact(
        conn,
        "fact-left",
        "contradict",
        target_fact_id="fact-right",
        rationale="Two incompatible implementations",
    )
    replay = replay_memory_events(
        conn,
        aggregate_kind="fact",
        aggregate_id="fact-left",
        limit=20,
    )

    left = conn.execute(
        "SELECT contradiction_count FROM canonical_facts WHERE fact_id = 'fact-left'"
    ).fetchone()["contradiction_count"]
    right = conn.execute(
        "SELECT contradiction_count FROM canonical_facts WHERE fact_id = 'fact-right'"
    ).fetchone()["contradiction_count"]

    assert result["changed"] is True
    assert left == 1
    assert right == 1
    assert any(ev["event_type"] == "fact_contradict" for ev in replay["events"])


def test_govern_fact_supersede_sets_validity_and_links(conn):
    _insert_fact(conn, "fact-old", object_text="Redis 6")
    _insert_fact(conn, "fact-new", object_text="Redis 7")
    _add_fact_provenance(conn, "fact-old")
    _add_fact_provenance(conn, "fact-new")

    result = govern_fact(
        conn,
        "fact-old",
        "supersede",
        target_fact_id="fact-new",
        rationale="Newer deployment state",
        effective_at="2026-03-31T09:00:00+00:00",
    )

    old_row = conn.execute(
        "SELECT valid_to, superseded_by_fact_id FROM canonical_facts WHERE fact_id = 'fact-old'"
    ).fetchone()
    link = conn.execute(
        "SELECT relation_type FROM knowledge_links WHERE subject_ref = 'fact-new' "
        "AND object_ref = 'fact-old' AND active = 1"
    ).fetchone()

    assert result["changed"] is True
    assert old_row["valid_to"] == "2026-03-31T09:00:00+00:00"
    assert old_row["superseded_by_fact_id"] == "fact-new"
    assert link["relation_type"] == "supersedes"


def test_rebuild_task_from_events_repairs_stale_materialization(conn):
    ts = "2026-03-31T08:00:00+00:00"
    conn.execute(
        "INSERT INTO tasks (id, title, status, priority, section, type, created_at, updated_at) "
        "VALUES ('task-audit', 'Audit me', 'not_started', 'medium', 'inbox', 'task', ?, ?)",
        (ts, ts),
    )
    conn.execute(
        "UPDATE tasks SET status = 'not_started', updated_at = ? WHERE id = 'task-audit'",
        ("2026-03-31T08:01:00+00:00",),
    )
    from db_utils import upsert_field_versions

    upsert_field_versions(
        conn,
        "task-audit",
        ("status",),
        timestamp="2026-03-31T08:05:00+00:00",
        machine_id="fedora",
        old_values={"status": "not_started"},
        new_values={"status": "done"},
        tool_name="test.rebuild_task",
    )

    result = rebuild_task_from_events(conn, "task-audit", repair=True)
    row = conn.execute("SELECT status FROM tasks WHERE id = 'task-audit'").fetchone()

    assert "status" in result["repaired_fields"]
    assert row["status"] == "done"


def test_run_memory_audit_detects_task_write_bypass(conn):
    ts = "2026-03-31T09:00:00+00:00"
    conn.execute(
        "INSERT INTO tasks (id, title, status, priority, section, type, created_at, updated_at) "
        "VALUES ('task-bypass', 'Original', 'not_started', 'medium', 'inbox', 'task', ?, ?)",
        (ts, ts),
    )
    from db_utils import upsert_field_versions

    upsert_field_versions(
        conn,
        "task-bypass",
        ("title",),
        timestamp=ts,
        machine_id="win",
        new_values={"title": "Original"},
        tool_name="test.write_guard",
    )
    conn.execute(
        "UPDATE tasks SET title = 'Bypassed', updated_at = '2026-03-31T10:00:00+00:00' "
        "WHERE id = 'task-bypass'"
    )

    result = run_memory_audit(conn, repair=False)

    assert any(issue["issue_type"] == "task_write_bypass" for issue in result["issues"])


def test_maybe_run_memory_audit_respects_schedule(conn):
    first = maybe_run_memory_audit(
        conn,
        runner_name="test-runner",
        cadence_minutes=60,
        repair=False,
        emit_event=False,
    )
    second = maybe_run_memory_audit(
        conn,
        runner_name="test-runner",
        cadence_minutes=60,
        repair=False,
        emit_event=False,
    )

    assert first["audit_version"] == "memory_audit_v2"
    assert second["status"] == "skipped_due"
