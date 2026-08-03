"""Tests for memory audit, replay, and truth-maintenance helpers."""

import json
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from memory_audit import (
    _repair_context_pack_artifacts,
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


def test_run_memory_audit_batches_provenance_source_timestamps(conn):
    _insert_fact(conn, "fact-source", object_text="Redis")
    _add_fact_provenance(conn, "fact-source")
    conn.execute(
        "INSERT INTO context_packs ("
        "pack_id, session_id, entity_id, pack_type, target_ref, input_signature, "
        "token_budget, body, freshness_score, contract_version, created_at"
        ") VALUES ('pack-old', NULL, NULL, 'executor', NULL, 'sig', 100, "
        "'body', 1.0, 'memory_contract_v2', '2026-03-31T08:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO provenance_links ("
        "provenance_id, subject_kind, subject_ref, source_kind, source_ref, "
        "excerpt, confidence, created_at"
        ") VALUES ('prov-pack-old', 'context_pack', 'pack-old', 'fact', "
        "'fact-source', 'source fact', 1.0, '2026-03-31T08:00:00+00:00')"
    )

    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    try:
        result = run_memory_audit(conn, repair=False)
    finally:
        conn.set_trace_callback(None)

    stale = [
        issue
        for issue in result["issues"]
        if issue["issue_type"] == "context_pack_stale"
    ]
    timestamp_reads = [
        statement
        for statement in statements
        if "FROM canonical_facts WHERE fact_id IN (" in statement
    ]
    assert stale[0]["subject_ref"] == "pack-old"
    assert len(timestamp_reads) == 1


def test_context_pack_repair_preserves_quality_confidence_and_uses_unknown_for_missing(
    conn,
):
    ts = "2026-03-31T08:00:00+00:00"
    conn.execute(
        "INSERT INTO context_packs ("
        "pack_id, session_id, entity_id, pack_type, target_ref, input_signature, "
        "token_budget, body, freshness_score, contract_version, created_at"
        ") VALUES ('pack-confidence', NULL, NULL, 'executor', 'task-a', 'sig', "
        "100, 'pack body', 0.91, 'memory_contract_v2', ?)",
        (ts,),
    )
    conn.execute(
        "INSERT INTO memory_artifacts ("
        "artifact_id, artifact_key, artifact_kind, scope_kind, scope_ref, title, "
        "body, confidence, status, created_at, updated_at"
        ") VALUES ('artifact-confidence', 'summary:context_pack:pack-confidence', "
        "'summary', 'context_pack', 'pack-confidence', 'executor context summary', "
        "'pack body', 0.42, 'active', ?, ?)",
        (ts, ts),
    )

    assert _repair_context_pack_artifacts(conn) == 0
    existing = conn.execute(
        "SELECT confidence FROM memory_artifacts "
        "WHERE artifact_id = 'artifact-confidence'"
    ).fetchone()
    assert existing["confidence"] == pytest.approx(0.42)

    conn.execute("DELETE FROM memory_artifacts WHERE artifact_id='artifact-confidence'")
    assert _repair_context_pack_artifacts(conn) == 1
    recovered = conn.execute(
        "SELECT confidence FROM memory_artifacts "
        "WHERE artifact_key = 'summary:context_pack:pack-confidence'"
    ).fetchone()
    assert recovered["confidence"] == pytest.approx(0.0)


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


def test_rebuild_task_from_events_never_overwrites_newer_materialized_state(conn):
    ledger_ts = "2026-03-31T08:00:00+00:00"
    row_ts = "2026-03-31T09:00:00+00:00"
    conn.execute(
        "INSERT INTO tasks (id, title, status, priority, section, type, created_at, updated_at) "
        "VALUES ('task-newer-row', 'Ledger title', 'not_started', 'medium', "
        "'inbox', 'task', ?, ?)",
        (ledger_ts, ledger_ts),
    )
    from db_utils import upsert_field_versions

    upsert_field_versions(
        conn,
        "task-newer-row",
        ("title",),
        timestamp=ledger_ts,
        machine_id="fedora",
        new_values={"title": "Ledger title"},
        tool_name="test.newer_materialized_guard",
    )
    conn.execute(
        "UPDATE tasks SET title = 'Newer materialized title', updated_at = ? "
        "WHERE id = 'task-newer-row'",
        (row_ts,),
    )

    result = rebuild_task_from_events(conn, "task-newer-row", repair=True)
    row = conn.execute(
        "SELECT title, updated_at FROM tasks WHERE id = 'task-newer-row'"
    ).fetchone()

    assert result["repaired_fields"] == []
    assert result["repair_skipped_reason"] == "materialized_newer_than_ledger"
    assert row["title"] == "Newer materialized title"
    assert row["updated_at"] == row_ts

    audit = run_memory_audit(conn, repair=True)
    assert any(
        issue["issue_type"] == "task_write_bypass"
        and issue["subject_ref"] == "task-newer-row"
        for issue in audit["issues"]
    )


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


def test_rebuild_task_from_events_repairs_structured_recurring_value(conn):
    ts = "2026-03-31T11:00:00+00:00"
    conn.execute(
        "INSERT INTO tasks (id, title, status, priority, section, recurring, type, created_at, updated_at) "
        "VALUES ('task-recurring', 'Recurring task', 'not_started', 'medium', 'inbox', ?, 'task', ?, ?)",
        ('{"every":"week","day":1}', ts, ts),
    )
    from db_utils import upsert_field_versions

    upsert_field_versions(
        conn,
        "task-recurring",
        ("recurring",),
        timestamp="2026-03-31T11:05:00+00:00",
        machine_id="fedora",
        old_values={"recurring": '{"every":"week","day":1}'},
        new_values={"recurring": {"every": "month", "day": 15}},
        tool_name="test.rebuild_task_recurring",
    )

    result = rebuild_task_from_events(conn, "task-recurring", repair=True)
    row = conn.execute(
        "SELECT recurring FROM tasks WHERE id = 'task-recurring'"
    ).fetchone()

    assert "recurring" in result["repaired_fields"]
    assert json.loads(row["recurring"]) == {"every": "month", "day": 15}


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


def test_repair_event_is_not_export_authority(conn):
    """A repair records that a replay happened; it must not become authority.

    Its fresh local clock used to outrank the peer-authored head it replayed,
    so the export claimed a local write and the peer's closure was reverted.
    """
    from db_utils import (
        MACHINE_ID,
        _pack_logical_clock,
        canonicalize_exported_task_statuses,
    )

    ts = "2026-03-31T08:00:00+00:00"
    head_ts = "2026-04-01T09:00:00+00:00"
    head_clock = _pack_logical_clock(1_760_000_000_000, 4)
    conn.execute(
        "INSERT INTO tasks (id, title, status, priority, section, type, "
        "created_at, updated_at) VALUES ('task-repair-authority', 'Repair me', "
        "'not_started', 'medium', 'inbox', 'task', ?, ?)",
        (ts, ts),
    )
    conn.execute(
        "INSERT INTO memory_events (event_id, event_type, aggregate_kind, "
        "aggregate_id, field_name, machine_id, tool_name, logical_clock, "
        "event_ts, new_value) "
        "VALUES ('peer-status-1','task_field_set','task','task-repair-authority',"
        "'status','fedora','peer.test',?,?,'done')",
        (head_clock, head_ts),
    )

    result = rebuild_task_from_events(conn, "task-repair-authority", repair=True)
    assert "status" in result["repaired_fields"]

    repair_event = conn.execute(
        "SELECT machine_id, logical_clock FROM memory_events "
        "WHERE aggregate_id='task-repair-authority' AND field_name='status' "
        "AND event_type='repair'"
    ).fetchone()
    assert repair_event is not None, "bookkeeping is still recorded"
    assert repair_event["machine_id"] == MACHINE_ID
    assert repair_event["logical_clock"] > head_clock

    exported = [{"id": "task-repair-authority", "status": "done", "_field_ts": {}}]
    canonicalize_exported_task_statuses(conn, exported)
    entry = exported[0]["_field_ts"]["status"]
    assert entry[1] == "fedora", "repair must not become the exported authority"
    assert entry[2] == head_clock
