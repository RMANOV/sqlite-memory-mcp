"""Tests for the standalone daily digest formatter."""

import sqlite3

from daily_digest import run_digest


def test_digest_coalesces_null_note_priority(tmp_path):
    db_path = str(tmp_path / "digest.db")
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE tasks ("
        "id TEXT PRIMARY KEY, title TEXT, status TEXT, priority TEXT, "
        "section TEXT, due_date TEXT, project TEXT, type TEXT, updated_at TEXT)"
    )
    conn.execute(
        "INSERT INTO tasks VALUES "
        "('note-1', 'Null priority note', 'not_started', NULL, 'inbox', "
        "NULL, NULL, 'note', datetime('now'))"
    )
    conn.commit()
    conn.close()

    digest = run_digest(
        db_path,
        sections=["today"],
        include_overdue=False,
        limit=10,
        include_notes=True,
    )

    assert "### NOTES (1)" in digest
    assert "- Null priority note" in digest
