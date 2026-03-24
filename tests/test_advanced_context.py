"""Focused tests for the optional advanced context module."""

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from advanced_context import select_context_items
from context_packer import build_context_pack
from schema import init_db


@pytest.fixture
def conn(tmp_path):
    db_path = str(tmp_path / "advanced-context.db")
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


def _insert_entity(
    conn, entity_id: int, name: str, entity_type: str = "service"
) -> None:
    ts = "2026-03-24T10:00:00+00:00"
    conn.execute(
        "INSERT INTO entities (id, name, entity_type, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        (entity_id, name, entity_type, ts, ts),
    )


def _insert_chunk(
    conn,
    chunk_id: str,
    *,
    entity_id: int | None = None,
    title: str,
    body: str,
    source_ref: str,
    materiality: float = 0.8,
) -> None:
    ts = "2026-03-24T10:00:00+00:00"
    conn.execute(
        "INSERT INTO context_chunks ("
        "chunk_id, session_id, entity_id, source_type, source_ref, source_hash, "
        "title, body, language, state, enrich_policy, materiality_score, created_at, updated_at"
        ") VALUES (?, NULL, ?, 'manual', ?, ?, ?, ?, 'bg', 'enrichable', 'manual', ?, ?, ?)",
        (
            chunk_id,
            entity_id,
            source_ref,
            f"hash-{chunk_id}",
            title,
            body,
            materiality,
            ts,
            ts,
        ),
    )


def test_select_context_items_prefers_diverse_coverage():
    items = [
        {
            "id": "chunk-a",
            "type": "chunk",
            "text": "Merge guard analysis for bridge sync with shrink alarms and diff review.",
            "tokens": 70,
            "group_key": "sync-source",
            "score": 0.92,
            "relevance": 0.84,
            "trust": 0.35,
            "coverage_keys": ["source:sync", "kw:merge", "kw:bridge"],
        },
        {
            "id": "chunk-b",
            "type": "chunk",
            "text": "Bridge sync review details with merge alarms and repeated shrink diagnostics.",
            "tokens": 70,
            "group_key": "sync-source",
            "score": 0.91,
            "relevance": 0.83,
            "trust": 0.35,
            "coverage_keys": ["source:sync", "kw:merge", "kw:bridge"],
        },
        {
            "id": "question-owner",
            "type": "question",
            "text": "Who should review incoming bridge sync conflicts before import?",
            "tokens": 40,
            "score": 0.74,
            "relevance": 0.8,
            "trust": 0.55,
            "coverage_keys": ["question:owner", "kw:review", "kw:incoming"],
        },
    ]

    result = select_context_items(
        items,
        budget=140,
        max_items_by_type={"fact": 6, "claim": 5, "question": 4, "chunk": 6},
        max_chunks_per_group=3,
        strategy={
            "enabled": True,
            "expansion_keywords": ["bridge", "sync", "review"],
        },
    )

    selected_ids = [item["id"] for item in result["selected"]]
    assert "chunk-a" in selected_ids
    assert "question-owner" in selected_ids
    assert "chunk-b" not in selected_ids


def test_advanced_context_graph_expansion_surfaces_related_chunk(conn, monkeypatch):
    monkeypatch.setattr(
        "context_packer.load_config",
        lambda: {
            "enabled": True,
            "context_pack_token_budget_default": 4000,
            "query_expansion_enabled": True,
            "advanced_context_enabled": True,
            "advanced_context_submodular_enabled": True,
            "advanced_context_max_seed_entities": 8,
            "advanced_context_max_related_entities": 12,
            "advanced_context_max_expansion_keywords": 24,
        },
    )

    _insert_task(
        conn,
        "task-graph",
        "Improve MyApp login flow",
        "Stabilize authentication path.",
    )
    _insert_entity(conn, 1, "MyApp", "service")
    _insert_entity(conn, 2, "Redis", "service")
    conn.execute(
        "INSERT INTO task_entity_links (task_id, entity_id, link_type, score, created_at) VALUES (?, ?, 'manual', 1.0, ?)",
        ("task-graph", 1, "2026-03-24T10:00:00+00:00"),
    )
    conn.execute(
        "INSERT INTO relations (from_id, to_id, relation_type, created_at) VALUES (?, ?, ?, ?)",
        (1, 2, "uses_cache", "2026-03-24T10:00:00+00:00"),
    )
    _insert_chunk(
        conn,
        "chunk-redis",
        entity_id=2,
        title="Redis session cache",
        body="Redis stores session tokens and caches OTP state for repeated requests.",
        source_ref="Redis",
    )

    result = build_context_pack(
        conn,
        pack_type="executor",
        target_ref="task-graph",
        token_budget=800,
        persist=False,
    )

    assert "Redis session cache" in result["body"]
    assert result["advanced_context"]["enabled"] is True
    assert result["advanced_context"]["query_expansion_used"] is True
    assert result["advanced_context"]["expanded_entities"] >= 2


def test_advanced_context_can_be_disabled_without_schema_changes(conn, monkeypatch):
    monkeypatch.setattr(
        "context_packer.load_config",
        lambda: {
            "enabled": True,
            "context_pack_token_budget_default": 4000,
            "query_expansion_enabled": False,
            "advanced_context_enabled": False,
            "advanced_context_submodular_enabled": False,
        },
    )

    _insert_task(
        conn,
        "task-off",
        "Improve MyApp login flow",
        "Stabilize authentication path.",
    )
    _insert_entity(conn, 1, "MyApp", "service")
    _insert_entity(conn, 2, "Redis", "service")
    conn.execute(
        "INSERT INTO task_entity_links (task_id, entity_id, link_type, score, created_at) VALUES (?, ?, 'manual', 1.0, ?)",
        ("task-off", 1, "2026-03-24T10:00:00+00:00"),
    )
    conn.execute(
        "INSERT INTO relations (from_id, to_id, relation_type, created_at) VALUES (?, ?, ?, ?)",
        (1, 2, "uses_cache", "2026-03-24T10:00:00+00:00"),
    )
    _insert_chunk(
        conn,
        "chunk-redis-off",
        entity_id=2,
        title="Redis session cache",
        body="Redis stores session tokens and caches OTP state for repeated requests.",
        source_ref="Redis",
    )

    result = build_context_pack(
        conn,
        pack_type="executor",
        target_ref="task-off",
        token_budget=800,
        persist=False,
    )

    assert "Redis session cache" not in result["body"]
    assert result["advanced_context"]["enabled"] is False
    assert result["advanced_context"]["selection_strategy"] == "greedy"
