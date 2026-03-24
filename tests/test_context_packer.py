"""Focused tests for task-scoped context pack relevance and preview mode."""

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from context_packer import build_context_pack, warm_recent_task_packs
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


def test_task_preview_pack_keeps_low_trust_for_raw_chunks_and_does_not_persist(conn):
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
    assert result["quality_score"] < 0.5
    assert after == before


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
