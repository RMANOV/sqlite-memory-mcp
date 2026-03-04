"""Task Tray — SQLite Task Manager.

System tray widget with dual mode: compact popup + full window.
Reads/writes directly to ~/.claude/memory/memory.db.
"""

import os
import sqlite3
import uuid
from datetime import UTC, date, datetime


DB_PATH = os.path.expanduser("~/.claude/memory/memory.db")

SECTIONS = ("today", "inbox", "next", "waiting", "someday")
PRIORITIES = ("critical", "high", "medium", "low")
STATUSES = ("not_started", "in_progress", "pending", "done", "archived", "cancelled")
HIDDEN_STATUSES = ("archived", "cancelled")


class TaskDB:
    """Direct sqlite3 wrapper for tasks table."""

    def __init__(self, db_path=None):
        self.db_path = db_path or DB_PATH
        self._conn = sqlite3.connect(self.db_path, timeout=10)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._ensure_table()

    def _ensure_table(self):
        """Create tasks table if it doesn't exist (for test DBs)."""
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                description TEXT,
                status TEXT DEFAULT 'not_started',
                section TEXT DEFAULT 'inbox',
                priority TEXT DEFAULT 'medium',
                due_date TEXT,
                project TEXT,
                parent_id TEXT,
                notes TEXT,
                recurring TEXT,
                created_at TEXT,
                updated_at TEXT
            )
        """)
        self._conn.commit()

    def close(self):
        self._conn.close()

    def get_tasks(self, section=None):
        """Return non-hidden tasks, optionally filtered by section."""
        placeholders = ",".join("?" for _ in HIDDEN_STATUSES)
        sql = f"SELECT * FROM tasks WHERE status NOT IN ({placeholders})"
        params = list(HIDDEN_STATUSES)
        if section:
            sql += " AND section = ?"
            params.append(section)
        sql += " ORDER BY created_at"
        rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def get_overdue(self):
        """Return non-hidden tasks with due_date in the past."""
        today = date.today().isoformat()
        placeholders = ",".join("?" for _ in HIDDEN_STATUSES)
        sql = (
            f"SELECT * FROM tasks WHERE status NOT IN ({placeholders}) "
            "AND due_date IS NOT NULL AND due_date < ? AND status <> 'done' "
            "ORDER BY due_date"
        )
        params = list(HIDDEN_STATUSES) + [today]
        return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def get_summary(self):
        """Return dict with total, overdue counts."""
        tasks = self.get_tasks()
        today = date.today().isoformat()
        overdue = sum(
            1
            for t in tasks
            if t.get("due_date") and t["due_date"] < today and t["status"] != "done"
        )
        return {"total": len(tasks), "overdue": overdue}

    def add_task(
        self,
        title,
        section="inbox",
        priority="medium",
        due_date=None,
        project=None,
        status="not_started",
    ):
        """Insert new task, return its ID."""
        task_id = str(uuid.uuid4())
        now = datetime.now(UTC).isoformat()
        self._conn.execute(
            "INSERT INTO tasks (id, title, status, section, priority, "
            "due_date, project, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (task_id, title, status, section, priority, due_date, project, now, now),
        )
        self._conn.commit()
        return task_id

    def mark_done(self, task_id):
        """Set status=done."""
        now = datetime.now(UTC).isoformat()
        self._conn.execute(
            "UPDATE tasks SET status='done', updated_at=? WHERE id=?",
            (now, task_id),
        )
        self._conn.commit()

    def update_task(self, task_id, **fields):
        """Update arbitrary fields on a task."""
        if not fields:
            return
        fields["updated_at"] = datetime.now(UTC).isoformat()
        sets = ", ".join(f"{k}=?" for k in fields)
        vals = list(fields.values()) + [task_id]
        self._conn.execute(f"UPDATE tasks SET {sets} WHERE id=?", vals)
        self._conn.commit()

    def delete_task(self, task_id):
        """Hard delete a task."""
        self._conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))
        self._conn.commit()
