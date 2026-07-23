"""Deterministic task↔entity suggestions and silent-accept policy."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

import pytest

from db_utils import TaskDAO, fts_sync_entity
from debate_read_dao import DebateReadDAO
from link_suggestions import (
    AUTO_ACCEPT_LINK_TYPE,
    AUTO_ACCEPT_SOURCE,
    auto_accept_high_confidence_links,
    decision_progress,
    record_link_decision,
    suggest_links,
)
from schema import init_db

NOW = "2026-07-23T09:00:00+00:00"


@pytest.fixture
def database(tmp_path):
    path = str(tmp_path / "links.db")
    init_db(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    yield path, conn
    conn.close()


def _entity(conn, name, *, project=None, observation="") -> int:
    cursor = conn.execute(
        "INSERT INTO entities "
        "(name, entity_type, project, created_at, updated_at) "
        "VALUES (?, 'organization', ?, ?, ?)",
        (name, project, NOW, NOW),
    )
    entity_id = int(cursor.lastrowid)
    if observation:
        conn.execute(
            "INSERT INTO observations (entity_id, content, created_at) "
            "VALUES (?, ?, ?)",
            (entity_id, observation, NOW),
        )
    fts_sync_entity(conn, entity_id)
    return entity_id


def _task(conn, task_id, title, *, project=None, priority="high") -> None:
    TaskDAO.create(
        conn,
        task_id,
        title,
        NOW,
        project=project,
        priority=priority,
        section="today",
    )


def test_auto_accept_requires_exact_mention_corroboration_and_margin(database):
    _path, conn = database
    alpha = _entity(
        conn,
        "Alpha Systems",
        project="client-alpha",
        observation="Alpha Systems implementation",
    )
    _entity(conn, "Beta Labs", project="client-alpha")
    _task(
        conn,
        "task-alpha",
        "Prepare Alpha Systems rollout",
        project="client-alpha",
    )
    conn.commit()

    result = auto_accept_high_confidence_links(
        conn,
        as_of=datetime(2026, 7, 23, 12, tzinfo=timezone.utc),
    )

    assert result["accepted_count"] == 1
    accepted = result["accepted"][0]
    assert accepted["entity_id"] == alpha
    assert "distinctive_exact+fts" in accepted["corroborators"]
    assert accepted["margin"] >= 0.08
    link = conn.execute(
        "SELECT link_type FROM task_entity_links "
        "WHERE task_id='task-alpha' AND entity_id=?",
        (alpha,),
    ).fetchone()
    assert link["link_type"] == AUTO_ACCEPT_LINK_TYPE
    decision = conn.execute(
        "SELECT decision_source, decided_by FROM link_suggestion_decisions "
        "WHERE task_id='task-alpha' AND entity_id=?",
        (alpha,),
    ).fetchone()
    assert decision["decision_source"] == AUTO_ACCEPT_SOURCE
    assert decision["decided_by"].startswith("system/")


def test_auto_accept_is_globally_bounded_to_three_per_day(database):
    _path, conn = database
    for index in range(4):
        name = f"Unique Company {index}"
        _entity(conn, name, project=f"p{index}", observation=name)
        _task(conn, f"task-{index}", f"Review {name}", project=f"p{index}")
    conn.commit()

    first = auto_accept_high_confidence_links(
        conn,
        as_of=datetime(2026, 7, 23, 12, tzinfo=timezone.utc),
    )
    second = auto_accept_high_confidence_links(
        conn,
        as_of=datetime(2026, 7, 23, 13, tzinfo=timezone.utc),
    )

    assert first["accepted_count"] == 3
    assert second["accepted_count"] == 0
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM link_suggestion_decisions WHERE decision_source=?",
            (AUTO_ACCEPT_SOURCE,),
        ).fetchone()[0]
        == 3
    )


def test_weak_candidates_stay_hidden_and_unlinked(database):
    _path, conn = database
    entity_id = _entity(conn, "Alpha Systems", project="shared")
    _task(conn, "task-generic", "Prepare quarterly review", project="shared")
    conn.commit()

    result = auto_accept_high_confidence_links(conn)

    assert result["accepted_count"] == 0
    assert (
        conn.execute(
            "SELECT 1 FROM task_entity_links WHERE task_id='task-generic'"
        ).fetchone()
        is None
    )
    suggestions = suggest_links(conn, "task-generic")
    assert suggestions["suggestions"][0]["entity_id"] == entity_id


def test_auto_labels_do_not_count_as_human_labels_in_fail_fast_mode(database):
    _path, conn = database
    entity_id = _entity(conn, "Alpha Systems", project="alpha", observation="Alpha")
    _task(conn, "task-alpha", "Alpha Systems work", project="alpha")
    conn.commit()
    auto_accept_high_confidence_links(conn)

    progress = decision_progress(conn)
    assert progress["by_source"][AUTO_ACCEPT_SOURCE]["accepted"] == 1
    assert progress["qualified_total"] == 0
    assert progress["gate"] == {
        "ready": True,
        "mode": "zero_label_fail_fast",
        "unvalidated": True,
        "minimum_total": 0,
        "minimum_accepted": 0,
        "minimum_rejected": 0,
        "remaining_total": 0,
        "remaining_accepted": 0,
        "remaining_rejected": 0,
    }

    reviewed = record_link_decision(
        conn,
        task_id="task-alpha",
        entity_id=entity_id,
        decision="rejected",
        decided_by="human",
    )
    assert reviewed["decision"] == "rejected"
    assert decision_progress(conn)["qualified_rejected"] == 1
    assert (
        conn.execute(
            "SELECT 1 FROM task_entity_links WHERE task_id='task-alpha'"
        ).fetchone()
        is None
    )
    assert suggest_links(conn, "task-alpha")["suggestions"] == []


def test_existing_unlink_tool_records_auto_rejection_and_preserves_daily_cap(
    database, monkeypatch
):
    import entity_server

    _path, conn = database
    entity_id = _entity(
        conn,
        "Alpha Systems",
        project="alpha",
        observation="Alpha Systems",
    )
    _task(conn, "task-alpha", "Alpha Systems work", project="alpha")
    conn.commit()
    auto_accept_high_confidence_links(conn)

    @contextmanager
    def use_test_connection():
        yield conn

    monkeypatch.setattr(entity_server, "_get_conn", use_test_connection)
    result = json.loads(entity_server.unlink_task_entity("task-alpha", "Alpha Systems"))

    assert result["removed"] is True
    row = conn.execute(
        "SELECT decision, decision_source FROM link_suggestion_decisions "
        "WHERE task_id='task-alpha' AND entity_id=?",
        (entity_id,),
    ).fetchone()
    assert dict(row) == {
        "decision": "rejected",
        "decision_source": "auto_high_confidence_rejected_by_human",
    }
    assert decision_progress(conn)["qualified_rejected"] == 1
    # A human rejection never reopens the same pair for automatic acceptance.
    assert auto_accept_high_confidence_links(conn)["accepted_count"] == 0


def test_waiting_surface_returns_only_three_recent_reversible_auto_links(database):
    path, conn = database
    for index in range(4):
        name = f"Relevant Entity {index}"
        _entity(conn, name, project=f"p{index}", observation=name)
        _task(conn, f"task-{index}", f"Handle {name}", project=f"p{index}")
    conn.commit()
    auto_accept_high_confidence_links(
        conn,
        as_of=datetime(2026, 7, 23, 12, tzinfo=timezone.utc),
    )
    conn.commit()

    dao = DebateReadDAO(
        path,
        clock=lambda: datetime(2026, 7, 23, 12, tzinfo=timezone.utc),
    )
    try:
        rows = dao.recent_auto_links(hours=24, limit=99)
    finally:
        dao.close()

    assert len(rows) == 3
    assert all(row["reversible"] for row in rows)
    assert all(row["id"].startswith("link:") for row in rows)
    assert all(row["reasons"] for row in rows)


def test_schema_backfills_legacy_manual_links_without_qualifying_them(database):
    path, conn = database
    entity_id = _entity(conn, "Manual Entity")
    _task(conn, "manual-task", "Manual task")
    TaskDAO.link_entity(
        conn, "manual-task", entity_id, link_type="manual", created_at=NOW
    )
    conn.commit()
    conn.close()

    init_db(path)
    check = sqlite3.connect(path)
    check.row_factory = sqlite3.Row
    try:
        row = check.execute(
            "SELECT decision, decision_source FROM link_suggestion_decisions "
            "WHERE task_id='manual-task' AND entity_id=?",
            (entity_id,),
        ).fetchone()
        assert dict(row) == {
            "decision": "accepted",
            "decision_source": "legacy_manual_link",
        }
        assert decision_progress(check)["qualified_total"] == 0
    finally:
        check.close()
