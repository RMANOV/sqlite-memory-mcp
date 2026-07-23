"""Focused tests for task-scoped context pack relevance and preview mode."""

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from context_packer import (
    _estimate_tokens,
    _find_relevant_entities,
    _prune_context_pack_history,
    build_context_pack,
    warm_recent_task_packs,
)
from schema import init_db


@pytest.fixture
def conn(tmp_path):
    db_path = str(tmp_path / "context.db")
    init_db(db_path)
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    yield conn
    conn.close()


def _insert_task(
    conn, task_id: str, title: str, description: str | None = None
) -> None:
    ts = "2026-03-24T10:00:00+00:00"
    conn.execute(
        "INSERT INTO tasks (id, title, description, status, priority, section, type, created_at, updated_at) "
        "VALUES (?, ?, ?, 'not_started', 'medium', 'inbox', 'task', ?, ?)",
        (task_id, title, description, ts, ts),
    )


def _insert_chunk(
    conn,
    chunk_id: str,
    *,
    title: str,
    body: str,
    materiality: float = 0.8,
    source_ref: str = "observation-ref",
) -> None:
    ts = "2026-03-24T10:00:00+00:00"
    conn.execute(
        "INSERT INTO context_chunks ("
        "chunk_id, session_id, entity_id, source_type, source_ref, source_hash, "
        "title, body, language, state, enrich_policy, materiality_score, created_at, updated_at"
        ") VALUES (?, NULL, NULL, 'observation', ?, ?, ?, ?, 'bg', 'enrichable', 'manual', ?, ?, ?)",
        (chunk_id, source_ref, f"hash-{chunk_id}", title, body, materiality, ts, ts),
    )


def _insert_fact(
    conn,
    fact_id: str,
    *,
    subject: str,
    predicate: str,
    object_text: str,
    confidence: float = 0.95,
    fact_scope: str = "task",
) -> None:
    ts = "2026-03-24T10:00:00+00:00"
    conn.execute(
        "INSERT INTO canonical_facts ("
        "fact_id, subject, predicate, object_text, object_type, fact_scope, "
        "provenance_summary, confidence, validation_mode, source_claim_id, created_at, updated_at"
        ") VALUES (?, ?, ?, ?, 'text', ?, 'test provenance', ?, 'manual', NULL, ?, ?)",
        (fact_id, subject, predicate, object_text, fact_scope, confidence, ts, ts),
    )
    conn.execute(
        "INSERT INTO provenance_links ("
        "provenance_id, subject_kind, subject_ref, source_kind, source_ref, "
        "span_start, span_end, excerpt, confidence, created_at"
        ") VALUES (?, 'fact', ?, 'chunk', ?, NULL, NULL, ?, 1.0, ?)",
        (f"prov-{fact_id}", fact_id, f"source-{fact_id}", f"{subject} {predicate}", ts),
    )


def test_vector_only_entity_hits_are_normalized_for_common_reranker(conn, monkeypatch):
    import smart_retrieval
    import vec_search

    monkeypatch.setattr(vec_search, "VEC_AVAILABLE", True)
    monkeypatch.setattr(
        vec_search,
        "vector_search",
        lambda *_args, **_kwargs: [
            {
                "eid": 42,
                "name": "Vector-only entity",
                "entity_type": "concept",
                "project": None,
                "distance": 0.1,
            }
        ],
    )

    def fake_rerank(_conn, rows, *_args):
        assert rows[0]["rank"] < 0
        return [{**rows[0], "_score": 0.9}]

    monkeypatch.setattr(smart_retrieval, "rerank_entities", fake_rerank)

    result = _find_relevant_entities(
        conn,
        query="vector only",
        linked_ids=[],
        session_id=None,
    )

    assert result["by_id"] == {42: 0.9}


def test_task_scoped_pack_matches_snake_case_tool_keyword(conn):
    task_id = "task-tool"
    _insert_task(
        conn,
        task_id,
        "Fix intelligence context",
        "Investigate mcp_sqlite_memory_create_task_or_note showing up with absurd scoring.",
    )
    _insert_chunk(
        conn,
        "chunk-relevant",
        title="sqlite memory tool usage",
        body="The tool mcp_sqlite_memory_create_task_or_note creates tasks and notes.",
        source_ref="sqlite memory",
    )
    _insert_chunk(
        conn,
        "chunk-noise",
        title="pytest freeze",
        body="Running the full pytest suite can freeze Python 3.14 on this machine.",
        source_ref="pytest",
    )

    result = build_context_pack(
        conn,
        pack_type="executor",
        target_ref=task_id,
        token_budget=1200,
        persist=False,
    )

    assert result["task_scoped"] is True
    assert result["items_included"] >= 1
    assert "mcp_sqlite_memory_create_task_or_note" in result["body"]
    assert "freeze Python 3.14" not in result["body"]
    assert result["relevance_score"] > 0.3


def test_task_preview_pack_hides_weak_chunk_only_context_and_does_not_persist(conn):
    task_id = "task-preview"
    _insert_task(
        conn,
        task_id,
        "Investigate Generator Interface",
        "Need generator interface details.",
    )
    _insert_chunk(
        conn,
        "chunk-a",
        title="Generator Interface",
        body="Generator Interface defines generate(mapped_data, client_name, period_start, period_end, output_dir).",
        source_ref="Generator Interface",
    )

    before = conn.execute("SELECT COUNT(*) AS cnt FROM context_packs").fetchone()["cnt"]
    result = build_context_pack(
        conn,
        pack_type="executor",
        target_ref=task_id,
        token_budget=1200,
        persist=False,
    )
    after = conn.execute("SELECT COUNT(*) AS cnt FROM context_packs").fetchone()["cnt"]

    assert result["items_included"] == 1
    assert result["preview_items_included"] == 1
    assert result["quality_score"] < 0.5
    assert result["previewable"] is False
    assert after == before


def test_executor_body_suppresses_context_fragments_when_fact_is_present(conn):
    task_id = "task-fact-and-chunk"
    _insert_task(
        conn,
        task_id,
        "Investigate Generator Interface",
        "Need generator interface details.",
    )
    _insert_fact(
        conn,
        "fact-generator",
        subject="Generator Interface",
        predicate="defines",
        object_text="generate(mapped_data, client_name, period_start, period_end, output_dir)",
    )
    _insert_chunk(
        conn,
        "chunk-generator",
        title="Generator Interface notes",
        body="Generator Interface defines generate(mapped_data, client_name, period_start, period_end, output_dir).",
        source_ref="Generator Interface",
    )

    result = build_context_pack(
        conn,
        pack_type="executor",
        target_ref=task_id,
        token_budget=1200,
        persist=False,
    )

    assert result["items_included"] >= 2
    assert result["previewable"] is True
    assert "## Canonical Facts" in result["body"]
    assert "## Context Fragments" not in result["body"]
    assert result["sections"]["chunks"] == 0
    fact_line = next(line for line in result["body"].splitlines() if "[FACT]" in line)
    assert result["token_usage"] == _estimate_tokens(fact_line)


def test_executor_pack_hides_contradicted_facts(conn):
    task_id = "task-contradicted-fact"
    _insert_task(
        conn,
        task_id,
        "Redis usage decision",
        "Need only settled facts, not unresolved contradictions.",
    )
    _insert_fact(
        conn,
        "fact-redis",
        subject="Service",
        predicate="uses",
        object_text="Redis",
    )
    conn.execute(
        "UPDATE canonical_facts SET contradiction_count = 1 WHERE fact_id = 'fact-redis'"
    )

    result = build_context_pack(
        conn,
        pack_type="executor",
        target_ref=task_id,
        token_budget=1200,
        persist=False,
    )

    assert "Redis" not in result["body"]
    assert result["sections"]["facts"] == 0


def test_fact_provenance_is_loaded_in_one_batch(conn):
    for index in range(3):
        _insert_fact(
            conn,
            f"fact-batch-{index}",
            subject=f"Batch subject {index}",
            predicate="uses",
            object_text="batched provenance lookup",
        )

    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    try:
        build_context_pack(conn, token_budget=1200, persist=False)
    finally:
        conn.set_trace_callback(None)

    provenance_reads = [
        statement
        for statement in statements
        if "FROM provenance_links" in statement and "subject_kind = 'fact'" in statement
    ]
    assert len(provenance_reads) == 1
    assert " IN (" in provenance_reads[0]


def test_warm_recent_task_packs_only_builds_information_rich_tasks(conn):
    _insert_task(
        conn,
        "task-rich",
        "Rich task",
        "Has enough description for task-scoped packing.",
    )
    _insert_task(conn, "task-empty", "Empty task", None)
    _insert_chunk(
        conn,
        "chunk-rich",
        title="Rich task context",
        body="Has enough description for task-scoped packing and executor context.",
        source_ref="Rich task context",
    )

    stats = warm_recent_task_packs(conn, limit=5)

    assert stats["task_packs_built"] == 1
    assert stats["task_packs_with_context"] == 1


def test_persisted_context_pack_materializes_summary_artifact_v2(conn):
    task_id = "task-artifact"
    _insert_task(
        conn,
        task_id,
        "Generator Interface",
        "Need durable summary artifact with provenance.",
    )
    _insert_fact(
        conn,
        "fact-artifact",
        subject="Generator Interface",
        predicate="defines",
        object_text="generate(mapped_data, client_name, period_start, period_end, output_dir)",
    )

    result = build_context_pack(
        conn,
        pack_type="executor",
        target_ref=task_id,
        token_budget=1200,
        persist=True,
    )

    artifact = conn.execute(
        "SELECT artifact_kind, scope_kind, scope_ref, title "
        "FROM memory_artifacts WHERE artifact_key = ?",
        (f"summary:context_pack:{result['pack_id']}",),
    ).fetchone()
    prov = conn.execute(
        "SELECT COUNT(*) AS cnt FROM provenance_links "
        "WHERE subject_kind = 'artifact' AND subject_ref = ("
        "SELECT artifact_id FROM memory_artifacts WHERE artifact_key = ?"
        ")",
        (f"summary:context_pack:{result['pack_id']}",),
    ).fetchone()["cnt"]

    assert result["contract_version"] == "memory_contract_v2"
    assert result["selection_policy"]["pack_type"] == "executor"
    assert artifact["artifact_kind"] == "summary"
    assert artifact["scope_kind"] == "context_pack"
    assert artifact["scope_ref"] == result["pack_id"]
    assert prov >= 1


def test_prune_context_pack_history_removes_artifacts_and_provenance(conn):
    for index in range(5):
        pack_id = f"pack-prune-{index}"
        artifact_id = f"artifact-prune-{index}"
        ts = f"2026-03-24T10:00:0{index}+00:00"
        conn.execute(
            "INSERT INTO context_packs ("
            "pack_id, session_id, entity_id, pack_type, target_ref, input_signature, "
            "token_budget, body, freshness_score, contract_version, created_at"
            ") VALUES (?, NULL, NULL, 'executor', 'task-prune', ?, 100, ?, 1.0, "
            "'memory_contract_v2', ?)",
            (pack_id, f"sig-{index}", f"body-{index}", ts),
        )
        conn.execute(
            "INSERT INTO memory_artifacts ("
            "artifact_id, artifact_key, artifact_kind, scope_kind, scope_ref, "
            "title, body, confidence, status, created_at, updated_at"
            ") VALUES (?, ?, 'summary', 'context_pack', ?, 'summary', ?, 0.5, "
            "'active', ?, ?)",
            (
                artifact_id,
                f"summary:context_pack:{pack_id}",
                pack_id,
                f"body-{index}",
                ts,
                ts,
            ),
        )
        conn.execute(
            "INSERT INTO provenance_links ("
            "provenance_id, subject_kind, subject_ref, source_kind, source_ref, "
            "confidence, created_at"
            ") VALUES (?, 'context_pack', ?, 'task', 'task-prune', 1.0, ?)",
            (f"pack-prov-{index}", pack_id, ts),
        )
        conn.execute(
            "INSERT INTO provenance_links ("
            "provenance_id, subject_kind, subject_ref, source_kind, source_ref, "
            "confidence, created_at"
            ") VALUES (?, 'artifact', ?, 'task', 'task-prune', 1.0, ?)",
            (f"artifact-prov-{index}", artifact_id, ts),
        )

    _prune_context_pack_history(conn, "executor", "task-prune", keep=2)

    assert conn.execute(
        "SELECT COUNT(*) FROM context_packs WHERE target_ref='task-prune'"
    ).fetchone()[0] == 2
    assert conn.execute(
        "SELECT COUNT(*) FROM memory_artifacts "
        "WHERE scope_kind='context_pack' AND scope_ref LIKE 'pack-prune-%'"
    ).fetchone()[0] == 2
    assert conn.execute(
        "SELECT COUNT(*) FROM provenance_links "
        "WHERE (subject_kind='context_pack' AND subject_ref LIKE 'pack-prune-%') "
        "OR (subject_kind='artifact' AND subject_ref LIKE 'artifact-prune-%')"
    ).fetchone()[0] == 4
