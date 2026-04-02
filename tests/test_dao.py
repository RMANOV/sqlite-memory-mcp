"""Unit tests for TaskDAO (db_utils.py).

Uses tmp_path for full isolation — no production DB touched.
Run: pytest tests/test_dao.py -v
"""

import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from db_utils import MERGEABLE_FIELDS, TaskDAO, now_iso, upsert_field_versions

# ── Minimal schema required by TaskDAO ───────────────────────────────────

_SCHEMA = """
CREATE TABLE tasks (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    description TEXT DEFAULT NULL,
    status      TEXT NOT NULL DEFAULT 'not_started',
    priority    TEXT DEFAULT 'medium',
    section     TEXT DEFAULT 'inbox',
    due_date    TEXT DEFAULT NULL,
    project     TEXT DEFAULT NULL,
    parent_id   TEXT DEFAULT NULL REFERENCES tasks(id) ON DELETE SET NULL,
    notes       TEXT DEFAULT NULL,
    recurring   TEXT DEFAULT NULL,
    reminder_at TEXT DEFAULT NULL,
    type        TEXT NOT NULL DEFAULT 'task',
    assignee    TEXT DEFAULT NULL,
    shared_by   TEXT DEFAULT NULL,
    visibility           TEXT DEFAULT 'private',
    publish_requested_at TEXT DEFAULT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE task_field_versions (
    task_id     TEXT NOT NULL,
    field_name  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    updated_by  TEXT NOT NULL,
    old_value   TEXT DEFAULT NULL,
    new_value   TEXT DEFAULT NULL,
    PRIMARY KEY (task_id, field_name)
);

CREATE TABLE entities (
    id          INTEGER PRIMARY KEY,
    name        TEXT UNIQUE NOT NULL,
    entity_type TEXT NOT NULL,
    project     TEXT DEFAULT NULL,
    shared_by   TEXT DEFAULT NULL,
    origin      TEXT DEFAULT 'local',
    visibility           TEXT DEFAULT 'private',
    publish_requested_at TEXT DEFAULT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE observations (
    id        INTEGER PRIMARY KEY,
    entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    content   TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(entity_id, content)
);

CREATE TABLE task_entity_links (
    task_id   TEXT    NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    link_type TEXT    NOT NULL DEFAULT 'manual',
    score     REAL    DEFAULT NULL,
    created_at TEXT   NOT NULL,
    PRIMARY KEY (task_id, entity_id)
);

CREATE VIRTUAL TABLE tasks_fts USING fts5(
    title, description, notes,
    content='tasks', content_rowid='rowid',
    tokenize = "unicode61 remove_diacritics 2"
);

CREATE TRIGGER tasks_fts_ai AFTER INSERT ON tasks BEGIN
    INSERT INTO tasks_fts(rowid, title, description, notes)
    VALUES (new.rowid, new.title, new.description, new.notes);
END;

CREATE TRIGGER tasks_fts_ad AFTER DELETE ON tasks BEGIN
    INSERT INTO tasks_fts(tasks_fts, rowid, title, description, notes)
    VALUES ('delete', old.rowid, old.title, old.description, old.notes);
END;

CREATE TRIGGER tasks_fts_au AFTER UPDATE ON tasks BEGIN
    INSERT INTO tasks_fts(tasks_fts, rowid, title, description, notes)
    VALUES ('delete', old.rowid, old.title, old.description, old.notes);
    INSERT INTO tasks_fts(rowid, title, description, notes)
    VALUES (new.rowid, new.title, new.description, new.notes);
END;

CREATE VIRTUAL TABLE memory_fts USING fts5(
    name, entity_type, observations_text,
    tokenize = "unicode61 remove_diacritics 2"
);
"""


@pytest.fixture
def conn(tmp_path):
    """Isolated in-memory-equivalent SQLite connection per test."""
    db_path = str(tmp_path / "test.db")
    c = sqlite3.connect(db_path, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    c.executescript(_SCHEMA)
    yield c
    c.close()


# ── Helpers ───────────────────────────────────────────────────────────────


def _make_task(conn, task_id="t1", title="Test task", **kwargs):
    """Insert a task and seed field versions. Returns task_id."""
    ts = kwargs.pop("now", now_iso())
    TaskDAO.create(conn, task_id, title, ts, **kwargs)
    upsert_field_versions(conn, task_id, MERGEABLE_FIELDS, ts)
    return task_id


def _make_entity(conn, name="EntityA", entity_type="concept"):
    """Insert a minimal entity. Returns entity_id."""
    ts = now_iso()
    cur = conn.execute(
        "INSERT INTO entities (name, entity_type, created_at, updated_at) VALUES (?, ?, ?, ?)",
        (name, entity_type, ts, ts),
    )
    return cur.lastrowid


# ── create / get_by_id ────────────────────────────────────────────────────


def test_create_and_get_roundtrip(conn):
    _make_task(conn, "t1", "Buy milk", priority="high", section="today")
    row = TaskDAO.get_by_id(conn, "t1")
    assert row is not None
    assert row["title"] == "Buy milk"
    assert row["priority"] == "high"
    assert row["section"] == "today"
    assert row["status"] == "not_started"


def test_create_null_optional_fields(conn):
    _make_task(conn, "t2", "Minimal")
    row = TaskDAO.get_by_id(conn, "t2")
    assert row["description"] is None
    assert row["due_date"] is None
    assert row["notes"] is None


def test_create_special_characters(conn):
    title = "Fix bug: <async/await> & 'quotes' \"double\""
    _make_task(conn, "t3", title)
    row = TaskDAO.get_by_id(conn, "t3")
    assert row["title"] == title


def test_get_by_id_missing(conn):
    assert TaskDAO.get_by_id(conn, "nonexistent") is None


# ── exists ────────────────────────────────────────────────────────────────


def test_exists_true(conn):
    _make_task(conn, "t1")
    assert TaskDAO.exists(conn, "t1") is True


def test_exists_false(conn):
    assert TaskDAO.exists(conn, "ghost") is False


# ── update ────────────────────────────────────────────────────────────────


def test_update_single_field(conn):
    _make_task(conn, "t1", "Original")
    ts = now_iso()
    rc = TaskDAO.update(conn, "t1", {"title": "Updated", "updated_at": ts})
    assert rc == 1
    assert TaskDAO.get_by_id(conn, "t1")["title"] == "Updated"


def test_update_multiple_fields(conn):
    _make_task(conn, "t1", "Task")
    ts = now_iso()
    rc = TaskDAO.update(
        conn, "t1", {"status": "done", "priority": "critical", "updated_at": ts}
    )
    assert rc == 1
    row = TaskDAO.get_by_id(conn, "t1")
    assert row["status"] == "done"
    assert row["priority"] == "critical"


def test_update_missing_task_returns_zero(conn):
    rc = TaskDAO.update(conn, "ghost", {"title": "X", "updated_at": now_iso()})
    assert rc == 0


def test_update_empty_fields_returns_zero(conn):
    _make_task(conn, "t1")
    assert TaskDAO.update(conn, "t1", {}) == 0


# ── delete ────────────────────────────────────────────────────────────────


def test_delete_existing(conn):
    _make_task(conn, "t1")
    rc = TaskDAO.delete(conn, "t1")
    assert rc == 1
    assert TaskDAO.get_by_id(conn, "t1") is None


def test_delete_missing_returns_zero(conn):
    assert TaskDAO.delete(conn, "ghost") == 0


# ── get_active ────────────────────────────────────────────────────────────


def test_get_active_excludes_terminal_statuses(conn):
    _make_task(conn, "active", "Active task", status="not_started")
    _make_task(conn, "inprog", "In progress", status="in_progress")
    _make_task(conn, "done", "Done task", status="done")
    _make_task(conn, "arch", "Archived", status="archived")
    _make_task(conn, "cancel", "Cancelled", status="cancelled")

    active = TaskDAO.get_active(conn)
    ids = {r["id"] for r in active}
    assert "active" in ids
    assert "inprog" in ids
    assert "done" not in ids
    assert "arch" not in ids
    assert "cancel" not in ids


def test_get_active_empty_db(conn):
    assert TaskDAO.get_active(conn) == []


# ── search (FTS5) ─────────────────────────────────────────────────────────


def test_search_finds_by_title(conn):
    _make_task(conn, "t1", "Refactor authentication module")
    _make_task(conn, "t2", "Write unit tests")
    results = TaskDAO.search(conn, "authentication")
    assert len(results) == 1
    assert results[0]["id"] == "t1"


def test_search_finds_by_notes(conn):
    _make_task(
        conn, "t1", "Generic task", notes="Remember to check the budget forecast"
    )
    results = TaskDAO.search(conn, "budget")
    assert len(results) == 1


def test_search_empty_query_returns_empty(conn):
    _make_task(conn, "t1", "Some task")
    assert TaskDAO.search(conn, "") == []
    assert TaskDAO.search(conn, "   ") == []


def test_search_no_match_returns_empty(conn):
    _make_task(conn, "t1", "Buy groceries")
    assert TaskDAO.search(conn, "xyzzy_nomatch") == []


# ── count_active ──────────────────────────────────────────────────────────


def test_count_active_empty(conn):
    assert TaskDAO.count_active(conn) == 0


def test_count_active_includes_done_excludes_archived(conn):
    # count_active excludes only 'archived' and 'cancelled', NOT 'done'
    _make_task(conn, "t1", "Not started", status="not_started")
    _make_task(conn, "t2", "Done", status="done")
    _make_task(conn, "t3", "Archived", status="archived")
    _make_task(conn, "t4", "Cancelled", status="cancelled")
    assert TaskDAO.count_active(conn) == 2  # t1 + t2


# ── count_by_visibility ───────────────────────────────────────────────────


def test_count_by_visibility(conn):
    _make_task(conn, "t1", "Private", visibility="private")
    _make_task(conn, "t2", "Pending public", visibility="pending_public")
    _make_task(conn, "t3", "Public", visibility="public")
    _make_task(conn, "t4", "Also private", visibility="private")

    assert TaskDAO.count_by_visibility(conn, "private") == 2
    assert TaskDAO.count_by_visibility(conn, "pending_public") == 1
    assert TaskDAO.count_by_visibility(conn, "public") == 1
    assert TaskDAO.count_by_visibility(conn, "nonexistent") == 0


# ── archive_done ──────────────────────────────────────────────────────────


def _old_ts(days=10):
    """ISO timestamp N days in the past."""
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def test_archive_done_archives_old(conn):
    old = _old_ts(15)
    _make_task(conn, "old_done", "Old done task", status="done", now=old)
    # Force updated_at to be old (create sets it to now by default)
    conn.execute("UPDATE tasks SET updated_at = ? WHERE id = ?", (old, "old_done"))

    archived = TaskDAO.archive_done(conn, older_than_days=7)
    assert "old_done" in archived
    assert TaskDAO.get_by_id(conn, "old_done")["status"] == "archived"


def test_archive_done_skips_recent(conn):
    _make_task(conn, "recent_done", "Recent done", status="done")
    archived = TaskDAO.archive_done(conn, older_than_days=7)
    assert "recent_done" not in archived
    assert TaskDAO.get_by_id(conn, "recent_done")["status"] == "done"


def test_archive_done_skips_notes_type(conn):
    old = _old_ts(15)
    _make_task(conn, "old_note", "Old note", status="done", type="note", now=old)
    conn.execute("UPDATE tasks SET updated_at = ? WHERE id = ?", (old, "old_note"))

    archived = TaskDAO.archive_done(conn, older_than_days=7)
    assert "old_note" not in archived  # archive_done only targets type='task'


# ── promote_pending_public ────────────────────────────────────────────────


def test_promote_due_today_skips_archived_and_cancelled(conn):
    today = datetime.now(timezone.utc).date().isoformat()
    _make_task(
        conn,
        "todo",
        "Due today",
        due_date=today,
        section="next",
        status="not_started",
    )
    _make_task(
        conn,
        "arch",
        "Archived due",
        due_date=today,
        section="next",
        status="archived",
    )
    _make_task(
        conn,
        "cancel",
        "Cancelled due",
        due_date=today,
        section="inbox",
        status="cancelled",
    )

    moved = TaskDAO.promote_due_today(conn)

    assert moved == 1
    assert TaskDAO.get_by_id(conn, "todo")["section"] == "today"
    assert TaskDAO.get_by_id(conn, "arch")["section"] == "next"
    assert TaskDAO.get_by_id(conn, "cancel")["section"] == "inbox"


def test_promote_pending_public(conn):
    past = _old_ts(1)
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()

    _make_task(
        conn, "t1", "Ready", visibility="pending_public", publish_requested_at=past
    )
    _make_task(
        conn, "t2", "NotYet", visibility="pending_public", publish_requested_at=future
    )
    _make_task(conn, "t3", "Private", visibility="private")

    cutoff = now_iso()
    promoted = TaskDAO.promote_pending_public(conn, cutoff)
    assert promoted == 1
    assert TaskDAO.get_by_id(conn, "t1")["visibility"] == "public"
    assert TaskDAO.get_by_id(conn, "t2")["visibility"] == "pending_public"
    assert TaskDAO.get_by_id(conn, "t3")["visibility"] == "private"


# ── link_entity / unlink_entity / get_task_links / get_entity_tasks / get_linked_entity_ids


def test_link_and_get_task_links(conn):
    _make_task(conn, "t1", "Task")
    eid = _make_entity(conn, "ProjectAlpha")
    TaskDAO.link_entity(conn, "t1", eid, link_type="manual", score=0.9)

    links = TaskDAO.get_task_links(conn, "t1")
    assert len(links) == 1
    assert links[0]["entity_name"] == "ProjectAlpha"
    assert links[0]["link_type"] == "manual"
    assert links[0]["score"] == pytest.approx(0.9)


def test_link_upsert_updates_existing(conn):
    _make_task(conn, "t1", "Task")
    eid = _make_entity(conn, "EntityX")
    TaskDAO.link_entity(conn, "t1", eid, link_type="manual", score=0.5)
    TaskDAO.link_entity(conn, "t1", eid, link_type="auto", score=0.8)

    links = TaskDAO.get_task_links(conn, "t1")
    assert len(links) == 1  # upsert, not duplicate
    assert links[0]["link_type"] == "auto"
    assert links[0]["score"] == pytest.approx(0.8)


def test_unlink_entity(conn):
    _make_task(conn, "t1", "Task")
    eid = _make_entity(conn, "EntityY")
    TaskDAO.link_entity(conn, "t1", eid)
    rc = TaskDAO.unlink_entity(conn, "t1", eid)
    assert rc == 1
    assert TaskDAO.get_task_links(conn, "t1") == []


def test_unlink_nonexistent_returns_zero(conn):
    _make_task(conn, "t1", "Task")
    assert TaskDAO.unlink_entity(conn, "t1", 999) == 0


def test_get_entity_tasks(conn):
    _make_task(conn, "t1", "Task A")
    _make_task(conn, "t2", "Task B")
    eid = _make_entity(conn, "SharedEntity")
    TaskDAO.link_entity(conn, "t1", eid)
    TaskDAO.link_entity(conn, "t2", eid)

    tasks = TaskDAO.get_entity_tasks(conn, eid)
    assert {r["id"] for r in tasks} == {"t1", "t2"}


def test_get_linked_entity_ids(conn):
    _make_task(conn, "t1", "Task")
    e1 = _make_entity(conn, "E1")
    e2 = _make_entity(conn, "E2")
    TaskDAO.link_entity(conn, "t1", e1)
    TaskDAO.link_entity(conn, "t1", e2)

    ids = TaskDAO.get_linked_entity_ids(conn, "t1")
    assert ids == {e1, e2}


def test_get_linked_entity_ids_empty(conn):
    _make_task(conn, "t1", "Task")
    assert TaskDAO.get_linked_entity_ids(conn, "t1") == set()
