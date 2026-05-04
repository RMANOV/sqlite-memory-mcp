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


def test_install_doctor_reports_missing_claude_mcp_servers(monkeypatch):
    class Result:
        stdout = "claude.ai Gmail: https://gmailmcp.googleapis.com/mcp/v1 - ✓ Connected"
        stderr = ""

    monkeypatch.setattr(install_doctor.shutil, "which", lambda name: "/usr/bin/claude")
    monkeypatch.setattr(
        install_doctor.subprocess,
        "run",
        lambda *args, **kwargs: Result(),
    )

    check = install_doctor._check_claude_mcp_registration()

    assert check["name"] == "claude_mcp"
    assert check["ok"] is False
    assert check["required"] is False
    assert "missing local sqlite MCP servers" in check["detail"]
    assert "sqlite_memory" in check["detail"]


def test_install_doctor_accepts_registered_claude_mcp_servers(monkeypatch):
    class Result:
        stdout = "\n".join(install_doctor._EXPECTED_LOCAL_MCP_SERVERS)
        stderr = ""

    monkeypatch.setattr(install_doctor.shutil, "which", lambda name: "/usr/bin/claude")
    monkeypatch.setattr(
        install_doctor.subprocess,
        "run",
        lambda *args, **kwargs: Result(),
    )

    check = install_doctor._check_claude_mcp_registration()

    assert check["ok"] is True
    assert check["detail"] == "sqlite MCP servers registered"


def test_install_doctor_reports_missing_codex_mcp_servers(monkeypatch):
    class Result:
        stdout = "No MCP servers configured yet."
        stderr = ""

    monkeypatch.setattr(install_doctor.shutil, "which", lambda name: "/usr/bin/codex")
    monkeypatch.setattr(
        install_doctor.subprocess,
        "run",
        lambda *args, **kwargs: Result(),
    )

    check = install_doctor._check_codex_mcp_registration()

    assert check["name"] == "codex_mcp"
    assert check["ok"] is False
    assert check["required"] is False
    assert "missing local sqlite MCP servers" in check["detail"]
    assert "codex mcp add sqlite_memory -- sqlite-memory-core" in check["detail"]


def test_install_doctor_accepts_registered_codex_mcp_servers(monkeypatch):
    class Result:
        stdout = "\n".join(install_doctor._EXPECTED_LOCAL_MCP_SERVERS)
        stderr = ""

    monkeypatch.setattr(install_doctor.shutil, "which", lambda name: "/usr/bin/codex")
    monkeypatch.setattr(
        install_doctor.subprocess,
        "run",
        lambda *args, **kwargs: Result(),
    )

    check = install_doctor._check_codex_mcp_registration()

    assert check["name"] == "codex_mcp"
    assert check["ok"] is True
    assert check["detail"] == "sqlite MCP servers registered"


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
