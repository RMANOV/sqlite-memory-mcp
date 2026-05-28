from datetime import datetime, timedelta, timezone
import os
import sqlite3
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import task_tray
from schema import init_db


def test_reminder_delivery_backoff_caps_at_three_total_deliveries():
    state = {}
    reminder_at = "2026-05-01T10:00:00+00:00"

    assert task_tray._should_deliver_reminder(state, "task-1", reminder_at, 0.0)
    assert not task_tray._should_deliver_reminder(state, "task-1", reminder_at, 299.0)

    assert task_tray._should_deliver_reminder(state, "task-1", reminder_at, 300.0)
    assert not task_tray._should_deliver_reminder(state, "task-1", reminder_at, 1199.0)

    assert task_tray._should_deliver_reminder(state, "task-1", reminder_at, 1200.0)
    assert not task_tray._should_deliver_reminder(
        state, "task-1", reminder_at, 99999.0
    )

    key = task_tray._reminder_delivery_key("task-1", reminder_at)
    assert state[key]["count"] == 3


def test_reminder_delivery_state_resets_when_reminder_time_changes():
    state = {}

    assert task_tray._should_deliver_reminder(state, "task-1", "old", 0.0)
    assert not task_tray._should_deliver_reminder(state, "task-1", "old", 10.0)
    assert task_tray._should_deliver_reminder(state, "task-1", "new", 10.0)


def test_clear_reminder_delivery_state_removes_all_task_reminder_keys():
    state = {}
    task_tray._should_deliver_reminder(state, "task-1", "old", 0.0)
    task_tray._should_deliver_reminder(state, "task-1", "new", 0.0)
    task_tray._should_deliver_reminder(state, "task-2", "same", 0.0)

    task_tray._clear_reminder_delivery_state(state, "task-1")

    assert all(key[0] != "task-1" for key in state)
    assert task_tray._reminder_delivery_key("task-2", "same") in state


def test_check_reminders_uses_backoff_and_stops_after_third_notification(
    tmp_path, monkeypatch
):
    db_path = str(tmp_path / "reminders.db")
    init_db(db_path)
    reminder_at = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO tasks "
            "(id, title, description, status, priority, section, reminder_at, "
            "created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "task-reminder",
                "Stretch",
                "Stand up",
                "not_started",
                "medium",
                "today",
                reminder_at,
                now,
                now,
            ),
        )

    class FakeTray:
        def __init__(self):
            self.messages = []

        def showMessage(self, title, body, icon, timeout):
            self.messages.append((title, body, timeout))

    tray = FakeTray()
    app = SimpleNamespace(
        db=SimpleNamespace(db_path=db_path),
        tray=tray,
        _reminder_delivery_state={},
        _active_reminder_keys=set(),
        _active_reminder_dlgs=[],
    )

    ticks = iter([0.0, 299.0, 300.0, 1199.0, 1200.0, 99999.0])
    monkeypatch.setattr(task_tray.time, "monotonic", lambda: next(ticks))

    for _ in range(6):
        task_tray.TaskTrayApp._check_reminders(app)

    assert [message[1] for message in tray.messages] == [
        "Stretch",
        "Stretch",
        "Stretch",
    ]


def test_taskdb_add_task_accepts_reminder_at_and_recurring(tmp_path):
    db_path = str(tmp_path / "add_task.db")
    init_db(db_path)
    db = task_tray.TaskDB(db_path)
    task_id = db.add_task(
        "Reminder bug repro",
        reminder_at="2026-05-28T10:00:00+00:00",
        recurring='{"freq":"daily"}',
    )
    row = db._conn.execute(
        "SELECT reminder_at, recurring FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    assert row[0] == "2026-05-28T10:00:00+00:00"
    assert row[1] == '{"freq":"daily"}'
