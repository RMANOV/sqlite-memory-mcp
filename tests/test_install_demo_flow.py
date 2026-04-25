import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import demo_flow
import install_doctor


def test_install_doctor_initializes_and_checks_demo_db(tmp_path, capsys):
    db_path = tmp_path / "doctor.db"

    rc = install_doctor.main(["--db", str(db_path), "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert payload["ok"] is True
    assert payload["db_path"] == str(db_path)
    assert {item["name"] for item in payload["checks"]} >= {
        "python",
        "sqlite3",
        "fastmcp",
        "schema",
        "db_write_lock",
    }


def test_demo_flow_seeds_task_note_and_reminder(tmp_path, capsys):
    db_path = tmp_path / "demo.db"

    rc = demo_flow.main(["--db", str(db_path), "--reset", "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert payload["db_path"] == str(db_path)
    assert payload["project"] == demo_flow.DEMO_PROJECT

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT type, reminder_at, recurring FROM tasks WHERE project = ?",
            (demo_flow.DEMO_PROJECT,),
        ).fetchall()
    finally:
        conn.close()

    assert {row["type"] for row in rows} == {"task", "note"}
    task = next(row for row in rows if row["type"] == "task")
    assert task["reminder_at"]
    assert task["recurring"]
