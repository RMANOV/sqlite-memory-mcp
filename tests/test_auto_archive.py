"""Regression tests for the standalone auto-archive maintenance script."""

from datetime import datetime, timedelta, timezone

from auto_archive import archive, dry_run
from db_utils import TaskDAO, get_conn, now_iso
from schema import init_db


def _old_timestamp(days: int = 30) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _create_done(conn, task_id: str, *, task_type: str) -> None:
    timestamp = now_iso()
    TaskDAO.create(
        conn,
        task_id,
        task_id,
        timestamp,
        status="done",
        type=task_type,
    )
    conn.execute(
        "UPDATE tasks SET updated_at = ? WHERE id = ?",
        (_old_timestamp(), task_id),
    )


def test_archive_and_dry_run_never_archive_notes(tmp_path, capsys):
    db_path = str(tmp_path / "auto-archive.db")
    init_db(db_path)

    with get_conn(db_path) as conn:
        _create_done(conn, "old-task", task_type="task")
        _create_done(conn, "old-note", task_type="note")

        dry_run(conn, 7)
        preview = capsys.readouterr().out
        assert "old-task" in preview
        assert "old-note" not in preview

        archive(conn, 7)
        assert TaskDAO.get_by_id(conn, "old-task")["status"] == "archived"
        assert TaskDAO.get_by_id(conn, "old-note")["status"] == "done"
        version = conn.execute(
            "SELECT new_value, source_event_id FROM task_field_versions "
            "WHERE task_id = 'old-task' AND field_name = 'status'"
        ).fetchone()
        assert version["new_value"] == "archived"
        event = conn.execute(
            "SELECT event_type, tool_name, new_value FROM memory_events "
            "WHERE event_id = ?",
            (version["source_event_id"],),
        ).fetchone()
        assert event["event_type"] == "task_field_set"
        assert event["tool_name"] == "db_utils.TaskDAO.archive_done"
        assert event["new_value"] == "archived"

    assert "Archived 1 tasks" in capsys.readouterr().out
