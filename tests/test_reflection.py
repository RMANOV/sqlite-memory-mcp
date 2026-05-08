"""Tests for reflect_audit Phase 0.5 — read-only consolidation candidate audit."""

from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from reflection import audit_reflection_candidates, format_audit_markdown
from schema import init_db
from db_utils import now_iso


@pytest.fixture
def db(tmp_path):
    db_path = str(tmp_path / "reflect.db")
    init_db(db_path)
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


def _insert_task(conn: sqlite3.Connection, **kwargs) -> str:
    """Insert a task row with sensible defaults; only required NOT NULL fields used."""
    now = kwargs.pop("now", now_iso())
    tid = kwargs.get("id") or f"task-{abs(hash((kwargs.get('title', '?'), now))) % 100000}"
    conn.execute(
        """
        INSERT INTO tasks (
            id, title, description, status, priority, section, due_date,
            project, parent_id, notes, type, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            tid,
            kwargs.get("title", "X"),
            kwargs.get("description"),
            kwargs.get("status", "not_started"),
            kwargs.get("priority", "medium"),
            kwargs.get("section", "inbox"),
            kwargs.get("due_date"),
            kwargs.get("project"),
            kwargs.get("parent_id"),
            kwargs.get("notes"),
            kwargs.get("type", "task"),
            kwargs.get("created_at", now),
            kwargs.get("updated_at", now),
        ),
    )
    return tid


def _insert_entity(conn: sqlite3.Connection, **kwargs) -> int:
    """Insert entity; entities.id is INTEGER PRIMARY KEY (autoincrement)."""
    now = kwargs.pop("now", now_iso())
    cur = conn.execute(
        """
        INSERT INTO entities (name, entity_type, project, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            kwargs.get("name", "EntityX"),
            kwargs.get("entity_type", "person"),
            kwargs.get("project"),
            kwargs.get("created_at", now),
            kwargs.get("updated_at", now),
        ),
    )
    return cur.lastrowid


def _insert_observation(conn: sqlite3.Connection, entity_id: int, content: str = "obs") -> None:
    conn.execute(
        "INSERT INTO observations (entity_id, content, created_at) VALUES (?, ?, ?)",
        (entity_id, content, now_iso()),
    )


def test_audit_returns_zero_candidates_on_empty_db(db):
    report = audit_reflection_candidates(db)
    assert report["version"] == "reflect_audit_v0.5_dry_run"
    assert report["summary"]["total_candidates"] == 0
    for category_count in report["summary"]["by_category"].values():
        assert category_count == 0


def test_audit_detects_exact_duplicate_titles(db):
    _insert_task(db, id="t1", title="Build prototype", project="alpha")
    _insert_task(db, id="t2", title="Build prototype", project="alpha")
    _insert_task(db, id="t3", title="Different task", project="alpha")
    _insert_task(db, id="t4", title="Build prototype", project="beta")  # different project, not dup

    dup = audit_reflection_candidates(db)["candidates"]["exact_duplicate_titles"]
    assert len(dup) == 1
    assert dup[0]["duplicate_count"] == 2
    assert dup[0]["project"] == "alpha"
    assert sorted(dup[0]["task_ids"]) == ["t1", "t2"]
    assert dup[0]["suggested_action"] == "merge_or_archive"


def test_audit_excludes_archived_from_duplicates(db):
    _insert_task(db, id="active", title="Same title", status="not_started")
    _insert_task(db, id="archived", title="Same title", status="archived")

    dup = audit_reflection_candidates(db)["candidates"]["exact_duplicate_titles"]
    # Only one active row left → no duplicate
    assert dup == []


def test_audit_detects_stale_overdue_tasks(db):
    long_ago = (datetime.now(timezone.utc) - timedelta(days=120)).date().isoformat()
    recent = (datetime.now(timezone.utc) - timedelta(days=10)).date().isoformat()

    _insert_task(db, id="stale", title="Old", due_date=long_ago, status="not_started")
    _insert_task(db, id="recent", title="New", due_date=recent, status="not_started")
    _insert_task(db, id="done_old", title="Done", due_date=long_ago, status="done")

    stale = audit_reflection_candidates(db, stale_days=60)["candidates"]["stale_overdue_tasks"]
    ids = [s["id"] for s in stale]
    assert ids == ["stale"]


def test_audit_detects_empty_description_notes(db):
    _insert_task(db, id="full_note", title="Has body", description="content", type="note")
    _insert_task(db, id="empty_note", title="Empty body", description="", type="note")
    _insert_task(db, id="null_note", title="Null body", description=None, type="note")
    _insert_task(db, id="task_empty", title="A task", description=None, type="task")  # not a note

    empty = audit_reflection_candidates(db)["candidates"]["empty_description_notes"]
    ids = {x["id"] for x in empty}
    assert ids == {"empty_note", "null_note"}


def test_audit_detects_orphan_parent_tasks(db):
    _insert_task(db, id="parent", title="Parent")
    _insert_task(db, id="valid_child", title="Valid", parent_id="parent")
    _insert_task(db, id="orphan", title="Orphan", parent_id="nonexistent-id")

    orphans = audit_reflection_candidates(db)["candidates"]["orphan_parent_tasks"]
    assert len(orphans) == 1
    assert orphans[0]["id"] == "orphan"
    assert orphans[0]["missing_parent_id"] == "nonexistent-id"


def test_audit_detects_abandoned_inbox_items(db):
    long_ago_iso = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
    recent_iso = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()

    _insert_task(
        db,
        id="abandoned",
        title="Old inbox",
        section="inbox",
        status="not_started",
        created_at=long_ago_iso,
        updated_at=long_ago_iso,
    )
    _insert_task(
        db,
        id="fresh",
        title="Fresh inbox",
        section="inbox",
        status="not_started",
        updated_at=recent_iso,
    )
    _insert_task(
        db,
        id="moved_to_today",
        title="Promoted",
        section="today",
        status="not_started",
        updated_at=long_ago_iso,
    )

    abandoned = audit_reflection_candidates(db, abandoned_inbox_days=30)["candidates"][
        "abandoned_inbox_items"
    ]
    ids = [a["id"] for a in abandoned]
    assert ids == ["abandoned"]


def test_audit_detects_entities_without_observations(db):
    full_id = _insert_entity(db, name="Full")
    _insert_observation(db, full_id, "real observation")
    empty_id = _insert_entity(db, name="Empty")  # no observations

    bare = audit_reflection_candidates(db)["candidates"]["entities_no_observations"]
    names = [b["name"] for b in bare]
    assert names == ["Empty"]
    # Verify only the empty one is reported
    assert {b["id"] for b in bare} == {empty_id}


def test_audit_filters_by_project(db):
    _insert_task(db, id="a1", title="Same", project="alpha")
    _insert_task(db, id="a2", title="Same", project="alpha")
    _insert_task(db, id="b1", title="Other", project="beta")
    _insert_task(db, id="b2", title="Other", project="beta")

    alpha = audit_reflection_candidates(db, project="alpha")["candidates"][
        "exact_duplicate_titles"
    ]
    assert len(alpha) == 1
    assert alpha[0]["project"] == "alpha"


def test_audit_respects_limit_per_category(db):
    long_ago = (datetime.now(timezone.utc) - timedelta(days=120)).date().isoformat()
    for i in range(15):
        _insert_task(db, id=f"stale-{i}", title=f"Stale {i}", due_date=long_ago)

    stale = audit_reflection_candidates(db, stale_days=60, limit_per_category=5)[
        "candidates"
    ]["stale_overdue_tasks"]
    assert len(stale) == 5


def test_format_markdown_renders_summary_and_candidates(db):
    _insert_task(db, id="d1", title="Dup", project="p")
    _insert_task(db, id="d2", title="Dup", project="p")

    report = audit_reflection_candidates(db)
    md = format_audit_markdown(report)
    assert md.startswith("# Reflection Audit")
    assert "Total candidates" in md
    assert "Exact Duplicate Titles" in md
    assert "merge_or_archive" in md


def test_format_markdown_handles_empty_report(db):
    md = format_audit_markdown(audit_reflection_candidates(db))
    assert "Total candidates:** 0" in md
    # No "Top candidates" section content
    assert "merge_or_archive" not in md
