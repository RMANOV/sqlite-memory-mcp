# tests/test_task_db.py
import os
import pytest

import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


@pytest.fixture
def db(tmp_path):
    """Fresh in-memory-like DB for each test."""
    db_path = str(tmp_path / "test.db")
    from task_tray import TaskDB

    tdb = TaskDB(db_path)
    return tdb


class TestTaskDB:
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
        task_id = db.add_task(
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
