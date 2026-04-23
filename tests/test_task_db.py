# tests/test_task_db.py
import os
import sqlite3
import pytest

import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import task_tray


@pytest.fixture
def db(tmp_path):
    """Fresh in-memory-like DB for each test."""
    db_path = str(tmp_path / "test.db")
    from task_tray import TaskDB

    tdb = TaskDB(db_path)
    return tdb


class TestTaskDB:
    def test_taskdb_init_applies_shared_schema_migrations(self, db):
        task_cols = {
            row[1] for row in db._conn.execute("PRAGMA table_info('tasks')").fetchall()
        }
        version_cols = {
            row[1]
            for row in db._conn.execute(
                "PRAGMA table_info('task_field_versions')"
            ).fetchall()
        }

        assert {"reminder_at", "visibility", "publish_requested_at"} <= task_cols
        assert {"old_value", "new_value"} <= version_cols
        assert (
            db._conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'task_entity_links'"
            ).fetchone()
            is not None
        )

    def test_get_tasks_empty(self, db):
        assert db.get_tasks() == []

    def test_add_task_minimal(self, db):
        task_id = db.add_task("Test task")
        assert task_id is not None
        tasks = db.get_tasks()
        assert len(tasks) == 1
        assert tasks[0]["title"] == "Test task"
        assert tasks[0]["section"] == "inbox"
        assert tasks[0]["priority"] == "medium"
        assert tasks[0]["status"] == "not_started"

    def test_add_task_full(self, db):
        db.add_task(
            "Full task",
            section="today",
            priority="high",
            due_date="2026-03-04",
            project="test-proj",
        )
        tasks = db.get_tasks()
        assert tasks[0]["section"] == "today"
        assert tasks[0]["priority"] == "high"
        assert tasks[0]["due_date"] == "2026-03-04"

    def test_add_task_persists_notes(self, db):
        task_id = db.add_task(
            "Task with notes",
            description="Main body",
            notes="Internal details",
        )
        row = next(t for t in db.get_tasks() if t["id"] == task_id)
        assert row["description"] == "Main body"
        assert row["notes"] == "Internal details"

    def test_mark_done(self, db):
        tid = db.add_task("To complete")
        db.mark_done(tid)
        tasks = db.get_tasks()
        assert tasks[0]["status"] == "done"

    def test_update_task(self, db):
        tid = db.add_task("Original")
        db.update_task(tid, title="Updated", section="next", priority="low")
        t = db.get_tasks()[0]
        assert t["title"] == "Updated"
        assert t["section"] == "next"

    def test_delete_task(self, db):
        tid = db.add_task("To delete")
        db.delete_task(tid)
        assert db.get_tasks() == []

    def test_get_by_section(self, db):
        db.add_task("A", section="today")
        db.add_task("B", section="inbox")
        db.add_task("C", section="today")
        today = db.get_tasks(section="today")
        assert len(today) == 2

    def test_get_overdue(self, db):
        db.add_task("Past", due_date="2020-01-01", section="today")
        db.add_task("Future", due_date="2099-01-01", section="today")
        db.add_task("No date", section="today")
        overdue = db.get_overdue()
        assert len(overdue) == 1
        assert overdue[0]["title"] == "Past"

    def test_get_summary(self, db):
        db.add_task("A", section="today")
        db.add_task("B", due_date="2020-01-01")
        db.add_task("C", status="done")
        s = db.get_summary()
        assert s["total"] >= 2
        assert s["overdue"] >= 1

    def test_mark_done_then_undo(self, db):
        tid = db.add_task("Toggle")
        db.mark_done(tid)
        db.update_task(tid, status="not_started")
        assert db.get_tasks()[0]["status"] == "not_started"

    def test_add_task_to_each_section(self, db):
        for section in ("today", "inbox", "next", "waiting", "someday"):
            db.add_task(f"Task in {section}", section=section)
        assert len(db.get_tasks()) == 5

    def test_hidden_statuses_filtered(self, db):
        db.add_task("Visible")
        tid = db.add_task("Hidden")
        db.update_task(tid, status="archived")
        assert len(db.get_tasks()) == 1

    def test_on_change_callback(self, db):
        calls = []
        db.on_change = lambda: calls.append(1)
        db.add_task("Trigger")
        db.mark_done(db.get_tasks()[0]["id"])
        db.update_task(db.get_tasks()[0]["id"], title="Changed")
        db.delete_task(db.get_tasks()[0]["id"])
        assert len(calls) == 4

    def test_link_and_unlink_entity(self, db):
        tid = db.add_task("Task with link")
        db._conn.execute(
            "INSERT INTO entities (name, entity_type, created_at, updated_at) VALUES (?, ?, datetime('now'), datetime('now'))",
            ("EntityA", "concept"),
        )
        entity_id = db._conn.execute(
            "SELECT id FROM entities WHERE name = ?", ("EntityA",)
        ).fetchone()[0]

        assert db.link_task_entity(tid, entity_id) is True
        links = db.get_task_links(tid)
        assert len(links) == 1
        assert links[0]["entity_name"] == "EntityA"

        assert db.unlink_task_entity(tid, entity_id) is True
        assert db.get_task_links(tid) == []

    def test_missing_task_mutations_do_not_create_audit_rows(self, db):
        assert db.mark_done("ghost") is False
        assert db.update_task("ghost", title="Updated") is False
        assert db.delete_task("ghost") is False
        count = db._conn.execute(
            "SELECT COUNT(*) FROM task_field_versions WHERE task_id = 'ghost'"
        ).fetchone()[0]
        assert count == 0

    def test_delete_task_only_cancels_matching_recurring_series(self, db):
        recurring_weekly = '{"every":"week","day":"monday"}'
        recurring_monthly = '{"every":"month","day":15}'

        target_id = db.add_task("Recurring Review", project="ops", status="done")
        db.update_task(target_id, recurring=recurring_weekly)

        same_series_id = db.add_task("Recurring Review", project="ops", status="done")
        db.update_task(same_series_id, recurring=recurring_weekly)

        other_series_id = db.add_task("Recurring Review", project="ops", status="done")
        db.update_task(other_series_id, recurring=recurring_monthly)

        assert db.delete_task(target_id) is True

        rows = {
            row["id"]: row["status"]
            for row in db._conn.execute(
                "SELECT id, status FROM tasks WHERE id IN (?, ?, ?)",
                (target_id, same_series_id, other_series_id),
            ).fetchall()
        }
        assert rows[target_id] == "cancelled"
        assert rows[same_series_id] == "cancelled"
        assert rows[other_series_id] == "done"

    def test_search_entities_hybrid_tolerates_missing_task_link_table(
        self, db, monkeypatch
    ):
        db._conn.execute("DROP TABLE task_entity_links")
        monkeypatch.setattr(
            db,
            "search_entities",
            lambda query, limit: [
                {
                    "rowid": 1,
                    "name": "EntityA",
                    "entity_type": "concept",
                    "obs_count": 0,
                }
            ],
        )

        results = db.search_entities_hybrid("EntityA", use_vector=False)

        assert len(results) == 1
        assert results[0]["task_count"] == 0
        assert results[0]["name"] == "EntityA"

    def test_search_entities_fast_returns_empty_on_fts_sqlite_error(
        self, db, monkeypatch
    ):
        class BrokenConn:
            def execute(self, sql, params=()):
                raise sqlite3.OperationalError("fts unavailable")

        class BrokenConnCtx:
            def __enter__(self):
                return BrokenConn()

            def __exit__(self, exc_type, exc_val, exc_tb):
                return False

        monkeypatch.setattr(task_tray, "get_conn", lambda db_path=None: BrokenConnCtx())

        assert db.search_entities_fast("query") == []
