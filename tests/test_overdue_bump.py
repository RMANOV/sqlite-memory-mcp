"""Caller-level tests for the shared overdue-priority DAO path."""

import json
from datetime import datetime, timedelta, timezone

import task_server
from db_utils import TaskDAO, get_conn, now_iso
from overdue_bump import run
from schema import init_db


def _create_overdue(conn, task_id: str, *, task_type: str) -> None:
    TaskDAO.create(
        conn,
        task_id,
        task_id,
        now_iso(),
        priority="low",
        due_date=(datetime.now(timezone.utc) - timedelta(days=2)).date().isoformat(),
        type=task_type,
    )


def test_script_dry_run_and_live_share_task_only_dao_path(tmp_path, capsys):
    db_path = str(tmp_path / "overdue-script.db")
    init_db(db_path)
    with get_conn(db_path) as conn:
        _create_overdue(conn, "overdue-task", task_type="task")
        _create_overdue(conn, "overdue-note", task_type="note")

    assert run(db_path, "high", dry_run=True) == 0
    preview = capsys.readouterr().out
    assert "overdue-task" in preview
    assert "overdue-note" not in preview

    assert run(db_path, "high", dry_run=False) == 0
    with get_conn(db_path) as conn:
        assert TaskDAO.get_by_id(conn, "overdue-task")["priority"] == "high"
        assert TaskDAO.get_by_id(conn, "overdue-note")["priority"] == "low"
        version = conn.execute(
            "SELECT new_value, source_event_id FROM task_field_versions "
            "WHERE task_id = 'overdue-task' AND field_name = 'priority'"
        ).fetchone()
        assert version["new_value"] == "high"
        event = conn.execute(
            "SELECT event_type, tool_name, new_value FROM memory_events "
            "WHERE event_id = ?",
            (version["source_event_id"],),
        ).fetchone()
        assert event["event_type"] == "task_field_set"
        assert event["tool_name"] == "overdue_bump.run"
        assert event["new_value"] == "high"

    assert "Bumped 1 task" in capsys.readouterr().out


def test_task_server_delegates_bump_to_task_dao(tmp_path, monkeypatch):
    db_path = str(tmp_path / "overdue-server.db")
    init_db(db_path)
    with get_conn(db_path) as conn:
        _create_overdue(conn, "server-task", task_type="task")
        _create_overdue(conn, "server-note", task_type="note")

    monkeypatch.setattr(task_server, "_get_write_conn", lambda: get_conn(db_path))
    payload = json.loads(task_server.bump_overdue_priority.__wrapped__("high"))

    assert payload == {"bumped": 1, "target_priority": "high"}
    with get_conn(db_path) as conn:
        assert TaskDAO.get_by_id(conn, "server-task")["priority"] == "high"
        assert TaskDAO.get_by_id(conn, "server-note")["priority"] == "low"


def test_script_rejects_invalid_priority_without_opening_db(tmp_path, capsys):
    assert run(str(tmp_path / "unused.db"), "urgent", dry_run=False) == 1
    assert "invalid priority 'urgent'" in capsys.readouterr().err


def test_script_low_priority_is_an_explicit_noop(tmp_path, capsys):
    assert run(str(tmp_path / "unused.db"), "low", dry_run=False) == 0
    assert "nothing to bump" in capsys.readouterr().out
