"""Task Tray — SQLite Task Manager.

System tray widget with dual mode: compact popup + full window.
Reads/writes directly to ~/.claude/memory/memory.db.
"""

import atexit
import base64
import copy
import faulthandler
import html as _html
import json
import logging
import os
import socket as _socket
import sqlite3
import subprocess
import sys
import threading
import uuid
import calendar as _cal_mod
import time
from datetime import date, datetime, timedelta, timezone

_log_dir = os.path.expanduser("~/.claude/mcp_servers/sqlite_kb")
os.makedirs(_log_dir, exist_ok=True)
_crash_log = open(os.path.join(_log_dir, "crash.log"), "a")
atexit.register(_crash_log.close)
faulthandler.enable(file=_crash_log)
logging.basicConfig(
    filename=os.path.join(_log_dir, "task_tray.log"),
    level=logging.WARNING,
    format="%(asctime)s %(levelname)s %(message)s",
)

from task_search import TaskSearchEngine

from db_utils import (
    DB_PATH,
    MERGEABLE_FIELDS,
    PRIORITY_COLORS,
    TASK_ALLOWED_UPDATE_FIELDS as ALLOWED_FIELDS,
    TASK_PRIORITIES,
    TASK_SECTIONS as SECTIONS,
    TaskDAO,
    get_conn,
    is_overdue,
    now_iso,
    parse_iso_date,
    priority_sort_key,
    upsert_field_versions,
)

PRIORITIES = tuple(reversed(TASK_PRIORITIES))  # descending for UI display

# Upper-case priority colors for UI lookups
_PRIORITY_COLORS_UPPER = {k.upper(): v for k, v in PRIORITY_COLORS.items()}

# Columns needed by UI rendering (excludes parent_id, notes, assignee, shared_by, publish_requested_at)
_UI_COLS = "id, title, description, notes, status, section, priority, due_date, project, type, recurring, reminder_at, visibility, updated_at, created_at"

# Page size cap for "All" and "Done" tabs to keep QListWidget responsive
_TAB_PAGE_SIZE = 200


class TaskDB:
    """Direct sqlite3 wrapper for tasks table."""

    def __init__(self, db_path=None):
        self.db_path = db_path or DB_PATH
        self.on_change = None
        self._conn = sqlite3.connect(self.db_path, isolation_level=None, timeout=10)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=10000")
        self._ensure_table()
        self._repair_fts_if_needed()

        self._last_promote_time: float = 0.0
        self.search_engine = TaskSearchEngine()

        self._wal_timer = QTimer(QApplication.instance())
        self._wal_timer.timeout.connect(self._wal_checkpoint)
        self._wal_timer.start(300_000)  # 5 minutes

    class _transact:
        """Explicit transaction block for multi-statement atomic writes.

        In autocommit mode (isolation_level=None), each execute() auto-commits.
        This context manager groups multiple statements into a single transaction.
        """

        def __init__(self, conn):
            self._conn = conn

        def __enter__(self):
            self._conn.execute("BEGIN")
            return self._conn

        def __exit__(self, exc_type, exc_val, exc_tb):
            if exc_type is None:
                self._conn.execute("COMMIT")
            else:
                try:
                    self._conn.execute("ROLLBACK")
                except Exception:
                    pass
            return False

    def _wal_checkpoint(self):
        try:
            self._conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
        except Exception:
            pass

    def _repair_fts_if_needed(self):
        """Check FTS5 indexes and rebuild if corrupted."""
        for fts_table in ("tasks_fts", "memory_fts"):
            try:
                self._conn.execute(
                    f"INSERT INTO {fts_table}({fts_table}, rank) VALUES('integrity-check', 1)"
                )
            except Exception:
                try:
                    self._conn.execute(
                        f"INSERT INTO {fts_table}({fts_table}) VALUES('rebuild')"
                    )
                    self._conn.commit()
                    logging.getLogger("task_tray").warning(
                        "Repaired corrupted FTS index: %s", fts_table
                    )
                except Exception:
                    pass  # FTS table may not exist yet

    def _ensure_table(self):
        """Create tasks table if missing; migrate existing table to v0.5.0 schema."""
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
                type TEXT NOT NULL DEFAULT 'task',
                assignee TEXT,
                shared_by TEXT,
                visibility TEXT DEFAULT 'private',
                publish_requested_at TEXT,
                created_at TEXT,
                updated_at TEXT
            )
        """)
        # Migrate existing DBs: add columns that v0.5.0 requires
        existing = {
            r[1] for r in self._conn.execute("PRAGMA table_info('tasks')").fetchall()
        }
        for col, sql in [
            ("type", "ALTER TABLE tasks ADD COLUMN type TEXT NOT NULL DEFAULT 'task'"),
            ("assignee", "ALTER TABLE tasks ADD COLUMN assignee TEXT DEFAULT NULL"),
            ("shared_by", "ALTER TABLE tasks ADD COLUMN shared_by TEXT DEFAULT NULL"),
            (
                "description",
                "ALTER TABLE tasks ADD COLUMN description TEXT DEFAULT NULL",
            ),
            (
                "visibility",
                "ALTER TABLE tasks ADD COLUMN visibility TEXT DEFAULT 'private'",
            ),
            (
                "publish_requested_at",
                "ALTER TABLE tasks ADD COLUMN publish_requested_at TEXT DEFAULT NULL",
            ),
            (
                "reminder_at",
                "ALTER TABLE tasks ADD COLUMN reminder_at TEXT DEFAULT NULL",
            ),
        ]:
            if col not in existing:
                self._conn.execute(sql)
        # Backfill null IDs
        nulls = self._conn.execute(
            "SELECT rowid FROM tasks WHERE id IS NULL"
        ).fetchall()
        for r in nulls:
            self._conn.execute(
                "UPDATE tasks SET id=? WHERE rowid=?", (str(uuid.uuid4()), r[0])
            )
        self._conn.commit()
        # Composite indices for common UI query patterns
        for idx_sql in (
            "CREATE INDEX IF NOT EXISTS idx_tasks_status_type ON tasks(status, type)",
            "CREATE INDEX IF NOT EXISTS idx_tasks_status_due ON tasks(status, due_date)",
            "CREATE INDEX IF NOT EXISTS idx_tasks_project_status ON tasks(project, status)",
        ):
            self._conn.execute(idx_sql)
        # v0.6.0+: supporting tables for per-field LWW and entity links
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS task_field_versions ("
            "task_id TEXT NOT NULL, field_name TEXT NOT NULL, "
            "updated_at TEXT NOT NULL, updated_by TEXT NOT NULL DEFAULT '', "
            "PRIMARY KEY (task_id, field_name), "
            "FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE)"
        )
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS task_entity_links ("
            "task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE, "
            "entity_id INTEGER NOT NULL, "
            "link_type TEXT NOT NULL DEFAULT 'manual', "
            "score REAL DEFAULT NULL, "
            "created_at TEXT NOT NULL, "
            "PRIMARY KEY (task_id, entity_id))"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tel_entity ON task_entity_links(entity_id)"
        )
        self._conn.commit()

    def close(self):
        self._wal_timer.stop()
        self._conn.close()

    def promote_due_today(self):
        """Auto-move tasks with due_date <= today (throttled to 60s)."""
        now = time.monotonic()
        if now - self._last_promote_time < 60:
            return 0
        self._last_promote_time = now
        return TaskDAO.promote_due_today(self._conn)

    def get_all_active(self):
        """Return all active tasks (excludes done, archived, cancelled)."""
        return TaskDAO.get_active(self._conn, columns=_UI_COLS)

    def get_done_tasks(self):
        """Return completed tasks, newest first."""
        return TaskDAO.get_done(self._conn, columns=_UI_COLS)

    def purge_old_done(self, days=30):
        """Delete done tasks older than `days` days. Returns count deleted."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        return TaskDAO.purge_done(self._conn, cutoff)

    def get_suggested_tasks(self, limit=20):
        """Return prioritized mix: overdue + high/critical + nearest due."""
        return TaskDAO.get_suggested(self._conn, limit)

    def get_all_notes(self):
        """All notes (never-deleted). Excludes archived/cancelled."""
        return TaskDAO.get_notes(self._conn)

    def get_project_names(self):
        """Return project names sorted by active task count (most first)."""
        return TaskDAO.get_project_names(self._conn)

    def get_summary(self, tasks=None):
        """Return dict with total, overdue counts. Accepts pre-fetched tasks."""
        if tasks is None:
            tasks = self.get_all_active()
        overdue = sum(1 for t in tasks if is_overdue(t.get("due_date")))
        return {"total": len(tasks), "overdue": overdue}

    def add_task(
        self,
        title,
        section="inbox",
        priority="medium",
        due_date=None,
        project=None,
        status="not_started",
        description=None,
        type="task",
    ):
        """Insert new task, return its ID."""
        task_id = str(uuid.uuid4())
        now = now_iso()
        with self._transact(self._conn):
            TaskDAO.create(
                self._conn,
                task_id,
                title,
                now,
                description=description,
                status=status,
                section=section,
                priority=priority,
                due_date=due_date,
                project=project,
                type=type,
            )
            upsert_field_versions(self._conn, task_id, MERGEABLE_FIELDS, now)
        if self.on_change:
            self.on_change()
        return task_id

    def mark_done(self, task_id):
        """Set status=done."""
        now = now_iso()
        with self._transact(self._conn):
            TaskDAO.update(self._conn, task_id, {"status": "done", "updated_at": now})
            upsert_field_versions(self._conn, task_id, ("status",), now)
        if self.on_change:
            self.on_change()

    def update_task(self, task_id, **fields):
        """Update arbitrary fields on a task."""
        if not fields:
            return
        invalid = set(fields) - ALLOWED_FIELDS
        if invalid:
            raise ValueError(f"Unknown task fields: {invalid}")
        now = now_iso()
        changed = tuple(k for k in fields if k in MERGEABLE_FIELDS)
        fields["updated_at"] = now
        with self._transact(self._conn):
            TaskDAO.update(self._conn, task_id, fields)
            if changed:
                upsert_field_versions(self._conn, task_id, changed, now)
        if self.on_change:
            self.on_change()

    def delete_task(self, task_id):
        """Soft-delete: cancel task (creates tombstone for bridge sync).
        For recurring tasks, also cancel done siblings to stop respawn cycle."""
        now = now_iso()
        with self._transact(self._conn):
            # Read task metadata before cancelling
            row = TaskDAO.get_by_id(self._conn, task_id, "title, recurring, project")
            # Cancel the target task
            TaskDAO.update(
                self._conn, task_id, {"status": "cancelled", "updated_at": now}
            )
            upsert_field_versions(self._conn, task_id, ("status",), now)
            # For recurring tasks: cancel all done siblings to break spawn cycle
            if row and row["recurring"]:
                sibling_ids = [
                    s["id"]
                    for s in self._conn.execute(
                        "SELECT id FROM tasks WHERE title=? AND status='done' "
                        "AND recurring IS NOT NULL AND id!=? AND project IS ?",
                        (row["title"], task_id, row["project"]),
                    ).fetchall()
                ]
                if sibling_ids:
                    ph = ",".join("?" * len(sibling_ids))
                    self._conn.execute(
                        f"UPDATE tasks SET status='cancelled', updated_at=? "
                        f"WHERE id IN ({ph})",
                        [now, *sibling_ids],
                    )
                    for sid in sibling_ids:
                        upsert_field_versions(self._conn, sid, ("status",), now)
        if self.on_change:
            self.on_change()

    # ── Entity Link helpers (v2.2.0) ─────────────────────────────────

    def search_entities(self, query: str, limit: int = 10) -> list[dict]:
        """FTS5 search for entities (for autocomplete in link dialog)."""
        if not query or len(query.strip()) < 2:
            return []
        words = query.strip().split()
        fts_q = " OR ".join(f'"{w}"' for w in words if w)
        if not fts_q:
            return []
        rows = self._conn.execute(
            "SELECT rowid, name, entity_type, "
            "(SELECT COUNT(*) FROM observations WHERE entity_id = memory_fts.rowid) AS obs_count "
            "FROM memory_fts WHERE memory_fts MATCH ? LIMIT ?",
            (fts_q, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def link_task_entity(
        self, task_id: str, entity_id: int, link_type: str = "manual"
    ) -> bool:
        """Create a manual link between a task and an entity."""
        now = now_iso()
        try:
            TaskDAO.link_entity(
                self._conn, task_id, entity_id, link_type, created_at=now
            )
            self._conn.commit()
            return True
        except Exception:
            return False

    def get_task_links(self, task_id: str) -> list[dict]:
        """Get all entities linked to a task."""
        try:
            return TaskDAO.get_task_links(self._conn, task_id)
        except Exception:
            return []

    def unlink_task_entity(self, task_id: str, entity_id: int) -> bool:
        """Remove a link between a task and an entity."""
        removed = TaskDAO.unlink_entity(self._conn, task_id, entity_id)
        self._conn.commit()
        return removed > 0


# ── UI Layer ────────────────────────────────────────────────────────

from PyQt6.QtWidgets import (
    QApplication,
    QSystemTrayIcon,
    QMenu,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QCheckBox,
    QLineEdit,
    QTextEdit,
    QPushButton,
    QScrollArea,
    QFrame,
    QMainWindow,
    QTabWidget,
    QListWidget,
    QListWidgetItem,
    QToolBar,
    QToolButton,
    QStatusBar,
    QDialog,
    QFormLayout,
    QComboBox,
    QDialogButtonBox,
    QProgressBar,
    QDateEdit,
    QCompleter,
    QMessageBox,
    QSpinBox,
    QStyledItemDelegate,
)
from PyQt6.QtGui import QIcon, QAction, QActionGroup, QPixmap, QPainter, QColor, QFont
from PyQt6.QtCore import (
    QDate,
    QEvent,
    QFileSystemWatcher,
    QObject,
    QSettings,
    Qt,
    QTimer,
    QPoint,
    pyqtSignal,
)
from pathlib import Path


class _ClickableLabel(QLabel):
    """Label that emits clicked signal on mouse press."""

    clicked = pyqtSignal()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


class _TooltipCopyFilter(QObject):
    """Copies full task summary to clipboard when tooltip is about to show."""

    _last_copied_id = None  # class-level debounce

    def __init__(self, task, parent=None):
        super().__init__(parent)
        self._task = task

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.ToolTip:
            t = self._task
            tid = t.get("id")
            if tid != _TooltipCopyFilter._last_copied_id:
                QApplication.clipboard().setText(_build_copy_text(t))
                _TooltipCopyFilter._last_copied_id = tid
        return False  # let tooltip show normally


def _build_rich_tooltip(task):
    """Build consistent rich tooltip for task display."""
    parts = []
    rl = _recurring_label(task.get("recurring"))
    if rl:
        parts.append(f"\U0001f504 {rl}")
    if task.get("description"):
        parts.append(task["description"])
    if task.get("priority"):
        parts.append(f"Priority: {task['priority']}")
    if task.get("due_date"):
        parts.append(f"Due: {task['due_date']}")
    if task.get("project"):
        parts.append(f"Project: {task['project']}")
    if task.get("section"):
        parts.append(f"Section: {task['section']}")
    return "\n".join(parts) if parts else None


def _build_copy_text(task):
    """Build clipboard text for task (title always included)."""
    parts = [task["title"]]
    if task.get("description"):
        parts.append(task["description"])
    if task.get("priority"):
        parts.append(f"Priority: {task['priority']}")
    if task.get("due_date"):
        parts.append(f"Due: {task['due_date']}")
    if task.get("project"):
        parts.append(f"Project: {task['project']}")
    if task.get("section"):
        parts.append(f"Section: {task['section']}")
    return "\n".join(parts)


class _ListTooltipCopyFilter(QObject):
    """Copies task summary to clipboard when hovering items in TaskListWidget."""

    def __init__(self, list_widget, parent=None):
        super().__init__(parent)
        self._list = list_widget
        self._last_copied_id = None

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.ToolTip:
            item = self._list.itemAt(event.pos())
            if item:
                task_id = item.data(Qt.ItemDataRole.UserRole)
                if task_id and task_id != self._last_copied_id:
                    task = next(
                        (t for t in self._list._tasks if t["id"] == task_id),
                        None,
                    )
                    if task:
                        QApplication.clipboard().setText(_build_copy_text(task))
                        self._last_copied_id = task_id
        return False


def create_tray_icon_pixmap(overdue_count=0):
    """Generate a 64x64 tray icon with optional overdue badge."""
    pm = QPixmap(64, 64)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)

    # Base: dark navy circle
    p.setBrush(QColor("#1a2332"))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawEllipse(4, 4, 56, 56)

    # Checkmark
    p.setPen(QColor("#ffffff"))
    p.setFont(QFont("Segoe UI", 28, QFont.Weight.Bold))
    p.drawText(pm.rect(), Qt.AlignmentFlag.AlignCenter, "\u2713")

    # Overdue badge (red circle top-right)
    if overdue_count > 0:
        p.setBrush(QColor("#e53e3e"))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(38, 0, 26, 26)
        p.setPen(QColor("#ffffff"))
        p.setFont(QFont("Segoe UI", 12, QFont.Weight.Bold))
        text = str(overdue_count) if overdue_count < 10 else "9+"
        p.drawText(38, 0, 26, 26, Qt.AlignmentFlag.AlignCenter, text)

    p.end()
    return pm


# ── Theme System ─────────────────────────────────────────────────────

_THEMES = {
    "black": {
        "bg": "#000000",
        "bg2": "#0a0a0a",
        "bg3": "#1a1a1a",
        "text": "#e2e8f0",
        "text2": "#a0aec0",
        "border": "#2d2d2d",
        "accent": "#3182ce",
        "accent_hover": "#4299e1",
        "danger": "#e53e3e",
        "done": "#38a169",
        "note_bg": "#0d1a0d",
        "overdue_bg": "#1a0000",
        "overdue_fg": "#fc8181",
        "header_bg": "#111111",
        "urgent_bg": "#2d1a00",
        "urgent_fg": "#f6ad55",
    },
    "blue": {
        "bg": "#0f1923",
        "bg2": "#1a2332",
        "bg3": "#2d3748",
        "text": "#e2e8f0",
        "text2": "#a0aec0",
        "border": "#4a5568",
        "accent": "#3182ce",
        "accent_hover": "#4299e1",
        "danger": "#e53e3e",
        "done": "#38a169",
        "note_bg": "#1e2d3d",
        "overdue_bg": "#291e26",
        "overdue_fg": "#fc8181",
        "header_bg": "#1e2836",
        "urgent_bg": "#3b2c1c",
        "urgent_fg": "#f6ad55",
    },
    "light": {
        "bg": "#f7fafc",
        "bg2": "#edf2f7",
        "bg3": "#e2e8f0",
        "text": "#1a202c",
        "text2": "#4a5568",
        "border": "#cbd5e0",
        "accent": "#3182ce",
        "accent_hover": "#2b6cb0",
        "danger": "#e53e3e",
        "done": "#38a169",
        "note_bg": "#ebf8ff",
        "overdue_bg": "#f5e4e5",
        "overdue_fg": "#c53030",
        "header_bg": "#e2e8f0",
        "urgent_bg": "#fffbeb",
        "urgent_fg": "#c05621",
    },
}

_theme_name = "blue"
_font_size = 13
_bold = False


def _T():
    """Current theme palette."""
    return _THEMES[_theme_name]


def _fw():
    """Current font-weight CSS value."""
    return "bold" if _bold else "normal"


def _update_theme_colors():
    """Refresh module-level QColor variables from current theme."""
    global _CLR_DONE, _CLR_NOTE_BG, _CLR_OVERDUE_BG, _CLR_OVERDUE_FG
    global _CLR_HEADER_BG, _CLR_HEADER_FG, _CLR_OVERDUE_HDR_BG, _CLR_OVERDUE_HDR_FG
    global _CLR_URGENT_HDR_BG, _CLR_URGENT_HDR_FG
    t = _T()
    _CLR_DONE = QColor(t["done"])
    _CLR_NOTE_BG = QColor(t["note_bg"])
    _CLR_OVERDUE_BG = QColor(t["overdue_bg"])
    _CLR_OVERDUE_FG = QColor(t["overdue_fg"])
    _CLR_HEADER_BG = QColor(t["header_bg"])
    _CLR_HEADER_FG = QColor(t["text2"])
    _CLR_OVERDUE_HDR_BG = QColor(t["overdue_bg"])
    _CLR_OVERDUE_HDR_FG = QColor(t["overdue_fg"])
    _CLR_URGENT_HDR_BG = QColor(t["urgent_bg"])
    _CLR_URGENT_HDR_FG = QColor(t["urgent_fg"])


# Initialize with default theme
_t = _T()
_CLR_DONE = QColor(_t["done"])
_CLR_NOTE_BG = QColor(_t["note_bg"])
_CLR_OVERDUE_BG = QColor(_t["overdue_bg"])
_CLR_OVERDUE_FG = QColor(_t["overdue_fg"])
_CLR_HEADER_BG = QColor(_t["header_bg"])
_CLR_HEADER_FG = QColor(_t["text2"])
_CLR_OVERDUE_HDR_BG = QColor(_t["overdue_bg"])
_CLR_OVERDUE_HDR_FG = QColor(_t["overdue_fg"])
_CLR_URGENT_HDR_BG = QColor(_t["urgent_bg"])
_CLR_URGENT_HDR_FG = QColor(_t["urgent_fg"])
del _t


# ── Stylesheet builders ─────────────────────────────────────────────


def _build_main_style():
    """Build FullWindow stylesheet from current theme."""
    t, fs = _T(), _font_size
    return f"""
        QMainWindow {{ background: {t["bg"]}; color: {t["text"]}; }}
        QTabWidget::pane {{ border: none; background: {t["bg"]}; }}
        QTabBar {{ background: {t["bg2"]}; }}
        QTabBar::tab {{ padding: 8px 20px; font-weight: bold; font-size: {fs}px;
                       background: {t["bg2"]}; color: {t["text2"]};
                       border: 1px solid {t["bg3"]}; border-bottom: none;
                       margin-right: 2px; }}
        QTabBar::tab:selected {{ background: {t["accent"]}; color: #ffffff; }}
        QTabBar::tab:hover:!selected {{ background: {t["bg3"]}; color: {t["text"]}; }}
        QToolBar {{ background: {t["bg2"]}; border-bottom: 1px solid {t["bg3"]}; spacing: 4px; }}
        QToolBar QToolButton {{ background: {t["bg3"]}; color: {t["text"]}; border: 1px solid {t["border"]};
                               padding: 4px 12px; font-weight: bold; font-size: {fs}px; }}
        QToolBar QToolButton:hover {{ background: {t["accent"]}; color: #ffffff; }}
        QToolBar QToolButton:checked {{ background: {t["accent"]}; color: #ffffff; }}
        QToolBar QToolButton#enrich_quick,
        QToolBar QToolButton#enrich_standard,
        QToolBar QToolButton#enrich_deep {{ background: {t["accent"]}; color: #ffffff; border: 1px solid {t["border"]}; font-weight: bold; }}
        QToolBar QToolButton#enrich_quick:hover,
        QToolBar QToolButton#enrich_standard:hover,
        QToolBar QToolButton#enrich_deep:hover {{ background: #1a5cb0; color: #ffffff; }}
        QStatusBar {{ background: {t["bg2"]}; color: {t["text2"]}; font-weight: bold;
                     border-top: 1px solid {t["bg3"]}; padding: 2px 8px; font-size: {fs - 1}px; }}
        QMenu {{ background: {t["bg2"]}; color: {t["text"]}; border: 1px solid {t["border"]}; }}
        QMenu::item:selected {{ background: {t["accent"]}; color: #ffffff; }}
        QLineEdit#search {{ background: {t["bg3"]}; color: {t["text"]}; border: 2px solid {t["border"]};
                           border-radius: 4px; padding: 4px 8px; min-width: 200px; font-size: {fs}px; }}
        QLineEdit#search:focus {{ border-color: {t["accent"]}; }}
    """


def _build_filter_style():
    """Build filter bar stylesheet from current theme."""
    t, fs = _T(), _font_size
    return f"""
        QToolBar {{ background: {t["bg"]}; border-bottom: 1px solid {t["bg3"]}; spacing: 3px; padding: 2px 4px; }}
        QToolButton {{ border-radius: 10px; padding: 2px 8px; font-size: {fs - 2}px; font-weight: 600;
                      border: 1px solid {t["border"]}; background: {t["bg3"]}; color: {t["text2"]}; }}
        QToolButton:hover {{ background: {t["bg3"]}; color: {t["text"]}; }}
        QToolButton:checked {{ background: {t["accent"]}; color: #fff; border-color: {t["accent"]}; }}
    """


def _build_list_style():
    """Build TaskListWidget stylesheet from current theme."""
    t, fs, fw = _T(), _font_size, _fw()
    return f"""
        QListWidget {{ background: {t["bg"]}; color: {t["text"]}; border: none;
                      font-size: {fs}px; font-weight: {fw}; }}
        QListWidget::item {{ padding: 8px 12px; border-bottom: 1px solid {t["bg3"]};
                            color: {t["text"]}; background: {t["bg"]}; }}
        QListWidget::item:selected {{ background: {t["bg3"]}; color: #ffffff; }}
        QListWidget::item:hover {{ background: {t["bg2"]}; }}
        QListWidget::indicator {{ width: 18px; height: 18px; }}
        QListWidget::indicator:unchecked {{ border: 2px solid {t["border"]};
                                           background: {t["bg2"]}; border-radius: 3px; }}
        QListWidget::indicator:checked {{ border: 2px solid {t["accent"]};
                                         background: {t["accent"]}; border-radius: 3px; }}
    """


def _build_popup_style():
    """Build TrayPopup stylesheet from current theme."""
    t, fs, fw = _T(), _font_size, _fw()
    return f"""
        QWidget {{ background: {t["bg2"]}; color: {t["text"]}; font-family: 'Segoe UI'; font-weight: {fw}; }}
        QLabel#header {{ font-size: {fs + 2}px; font-weight: bold; padding: 10px 0 10px 14px; }}
        QLabel#section-header {{ font-size: {fs - 2}px; color: {t["text2"]}; padding: 6px 14px 2px;
                                text-transform: uppercase; letter-spacing: 1px; }}
        QCheckBox {{ font-size: {fs}px; padding: 6px 14px; }}
        QCheckBox::indicator {{ width: 16px; height: 16px; }}
        QLabel#priority {{ font-size: {fs - 3}px; font-weight: bold; padding: 2px 6px;
                          border-radius: 3px; }}
        QLineEdit {{ background: {t["bg3"]}; border: 1px solid {t["border"]}; border-radius: 4px;
                    color: {t["text"]}; padding: 6px 10px; margin: 2px 14px; }}
        QTextEdit {{ background: {t["bg3"]}; border: 1px solid {t["border"]}; border-radius: 4px;
                    color: {t["text"]}; padding: 6px 10px; margin: 2px 14px; font-family: 'Segoe UI';
                    font-size: {fs}px; }}
        QComboBox {{ background: {t["bg3"]}; border: 1px solid {t["border"]}; border-radius: 4px;
                    color: {t["text"]}; padding: 4px 8px; margin: 2px 14px; }}
        QComboBox QAbstractItemView {{ background: {t["bg3"]}; color: {t["text"]};
                                      selection-background-color: {t["border"]}; }}
        QPushButton#add-btn {{ background: transparent; border: none; color: {t["text2"]};
                              font-size: {fs + 5}px; font-weight: bold; padding: 4px 10px; }}
        QPushButton#add-btn:hover {{ color: #ffffff; }}
        QPushButton#submit-btn {{ background: {t["bg3"]}; border: 1px solid {t["border"]};
                                 border-radius: 4px; color: {t["text"]}; padding: 6px;
                                 margin: 2px 14px; font-weight: bold; }}
        QPushButton#submit-btn:hover {{ background: {t["border"]}; }}
        QPushButton#open-full {{ background: {t["bg3"]}; border: none; color: {t["text2"]};
                                padding: 8px; font-size: {fs - 1}px; }}
        QPushButton#open-full:hover {{ background: {t["border"]}; color: #ffffff; }}
    """


def _build_dialog_style():
    """Build EditTaskDialog stylesheet from current theme."""
    t, fs, fw = _T(), _font_size, _fw()
    return f"""
        QDialog {{ background: {t["bg"]}; color: {t["text"]}; font-weight: {fw}; }}
        QLabel {{ color: {t["text2"]}; font-weight: bold; }}
        QLineEdit {{ background: {t["bg2"]}; color: {t["text"]}; border: 2px solid {t["border"]};
                    border-radius: 4px; padding: 6px; font-size: {fs}px; }}
        QLineEdit:focus {{ border-color: {t["accent"]}; }}
        QTextEdit {{ background: {t["bg2"]}; color: {t["text"]}; border: 2px solid {t["border"]};
                    border-radius: 4px; padding: 6px; font-size: {fs}px; }}
        QComboBox {{ background: {t["bg2"]}; color: {t["text"]}; border: 2px solid {t["border"]};
                    border-radius: 4px; padding: 6px; font-size: {fs}px; }}
        QComboBox QAbstractItemView {{ background: {t["bg2"]}; color: {t["text"]};
                                      selection-background-color: {t["border"]}; }}
        QDateEdit {{ background: {t["bg2"]}; color: {t["text"]}; border: 2px solid {t["border"]};
                    border-radius: 4px; padding: 6px; font-size: {fs}px; }}
        QDateEdit::drop-down {{ border: none; }}
        QPushButton {{ background: {t["bg3"]}; color: {t["text"]}; border: 1px solid {t["border"]};
                      border-radius: 4px; padding: 6px 16px; font-weight: bold; font-size: {fs}px; }}
        QPushButton:hover {{ background: {t["accent"]}; color: #ffffff; }}
        QMenu {{ background: {t["bg2"]}; color: {t["text"]}; border: 1px solid {t["border"]}; }}
        QMenu::item:selected {{ background: {t["accent"]}; color: #ffffff; }}
    """


def _build_reader_style():
    """Build TaskReaderDialog stylesheet from current theme."""
    t, fs, fw = _T(), _font_size, _fw()
    return f"""
        QDialog {{ background: {t["bg"]}; font-weight: {fw}; }}
        QLabel#reader-title {{ color: {t["text"]}; font-size: {fs + 5}px; font-weight: bold;
                              padding: 12px 16px 4px; }}
        QLabel#reader-meta {{ color: {t["text2"]}; font-size: {fs - 1}px; padding: 2px 6px; }}
        QLabel#reader-priority {{ font-size: {fs - 2}px; font-weight: bold; padding: 2px 8px;
                                 border-radius: 3px; }}
        QScrollArea {{ background: {t["bg"]}; border: none; }}
        QLabel#reader-body {{ color: {t["text"]}; font-size: {fs}px; padding: 16px;
                             background: {t["bg"]}; }}
        QFrame#reader-header {{ background: {t["bg2"]}; border-bottom: 1px solid {t["bg3"]}; }}
        QPushButton {{ background: {t["bg3"]}; color: {t["text"]}; border: 1px solid {t["border"]};
                      border-radius: 4px; padding: 8px 20px; font-weight: bold;
                      font-size: {fs}px; }}
        QPushButton:hover {{ background: {t["accent"]}; color: #ffffff; }}
    """


def _build_menu_style():
    """Build context menu stylesheet from current theme."""
    t = _T()
    return (
        f"QMenu {{ background: {t['bg2']}; color: {t['text']}; border: 1px solid {t['border']}; }}"
        f"QMenu::item:selected {{ background: {t['accent']}; color: #ffffff; }}"
    )


def _recurring_label(raw: str | None) -> str:
    """Human-readable label for recurring config JSON."""
    if not raw:
        return ""
    try:
        cfg = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return ""
    every = cfg.get("every", "").lower()
    try:
        interval = int(cfg.get("interval", 1))
    except (ValueError, TypeError):
        interval = 1
    if every == "day":
        return "Daily" if interval == 1 else f"Every {interval} days"
    if every == "week":
        day = cfg.get("day", "?").title()
        if interval == 1:
            return f"Weekly ({day})"
        if interval == 2:
            return f"Biweekly ({day})"
        return f"Every {interval} weeks ({day})"
    if every == "month":
        day = cfg.get("day", "?")
        if interval == 1:
            return f"Monthly (day {day})"
        return f"Every {interval} months (day {day})"
    if every == "year":
        month = cfg.get("month")
        day = cfg.get("day")
        parts = []
        if month:
            import calendar

            try:
                parts.append(calendar.month_name[int(month)])
            except (ValueError, TypeError, IndexError):
                parts.append(str(month))
        if day:
            parts.append(str(day))
        suffix = " ".join(parts) if parts else ""
        if interval == 1:
            return f"Yearly ({suffix})" if suffix else "Yearly"
        return (
            f"Every {interval} years ({suffix})"
            if suffix
            else f"Every {interval} years"
        )
    return ""


def _get_truth_score_badge(task, db_path=None):
    """Query TruthScore for public entities and return a color-coded badge string."""
    if task.get("visibility") != "public" or not task.get("title"):
        return ""
    cache = _batch_truth_scores(db_path)
    return cache.get(task["title"], "\u2b1c ")  # gray square = unrated


# Module-level TruthScore cache (refreshed every 30s)
_ts_cache: dict[str, str] = {}
_ts_cache_time: float = 0.0


def _batch_truth_scores(db_path=None):
    """Single query for all TruthScore badges. Cached for 30s."""
    global _ts_cache, _ts_cache_time
    now = time.monotonic()
    if now - _ts_cache_time < 30:
        return _ts_cache
    try:
        conn = sqlite3.connect(db_path or DB_PATH, isolation_level=None, timeout=5)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT entity_name, AVG(specificity * 0.35 + falsifiability * 0.25 + "
            "internal_consistency * 0.25 + novelty * 0.15) as iq "
            "FROM knowledge_ratings GROUP BY entity_name"
        ).fetchall()
        conn.close()
        result = {}
        for r in rows:
            avg = r["iq"] or 0.0
            if avg > 0.7:
                result[r["entity_name"]] = "\U0001f7e2 "  # green
            elif avg >= 0.4:
                result[r["entity_name"]] = "\U0001f7e1 "  # yellow
            else:
                result[r["entity_name"]] = "\U0001f534 "  # red
        _ts_cache = result
        _ts_cache_time = now
        return result
    except Exception:
        return _ts_cache  # return stale cache on error


def _format_task_text(task, include_project=True, prefix=""):
    """Build display text: [N] [🔄] [⏳/🌐] [TS] [PRIORITY] title | Due: date | project — preview."""
    type_prefix = "[N] " if task.get("type") == "note" else ""
    recur = "\U0001f504 " if task.get("recurring") else ""
    vis = task.get("visibility", "private")
    vis_badge = (
        "\u23f3 "
        if vis == "pending_public"
        else ("\U0001f310 " if vis == "public" else "")
    )
    ts_badge = _get_truth_score_badge(task) if vis == "public" else ""
    priority = (task.get("priority") or "medium").upper()
    due = f" | Due: {task['due_date']}" if task.get("due_date") else ""
    proj = f" | {task['project']}" if include_project and task.get("project") else ""
    desc = task.get("description") or ""
    preview = f" — {desc[:50]}..." if len(desc) > 50 else (f" — {desc}" if desc else "")
    return f"{prefix}{type_prefix}{recur}{vis_badge}{ts_badge}[{priority}] {task['title']}{due}{proj}{preview}"


def _apply_task_item_colors(item, task):
    """Apply state-based colors to a QListWidgetItem (done, note, overdue)."""
    if task["status"] == "done":
        item.setForeground(_CLR_DONE)
    if task.get("type") == "note":
        item.setBackground(_CLR_NOTE_BG)
    if is_overdue(task.get("due_date")) and task["status"] != "done":
        item.setData(_OVERDUE_ROLE, True)
        item.setBackground(_CLR_OVERDUE_BG)
        item.setForeground(_CLR_OVERDUE_FG)


_OVERDUE_ROLE = Qt.ItemDataRole.UserRole + 10


class _OverdueDelegate(QStyledItemDelegate):
    """Paints a 3px red left border on overdue task items."""

    def paint(self, painter, option, index):
        super().paint(painter, option, index)
        if index.data(_OVERDUE_ROLE):
            painter.save()
            painter.setPen(Qt.PenStyle.NoPen)
            painter.fillRect(
                option.rect.x(),
                option.rect.y(),
                3,
                option.rect.height(),
                QColor(_T()["danger"]),
            )
            painter.restore()


def _suggested_sort_key(t):
    """Python sort key replicating get_suggested_tasks() SQL ordering."""
    dd = t.get("due_date")
    today_str = date.today().isoformat()
    return (
        0 if (dd and dd < today_str) else 1,  # overdue first
        priority_sort_key(t)[0],  # priority (critical first)
        0 if dd else 1,  # has due date first
        dd or "9999-99-99",  # due date ascending
    )


def _smart_group(tasks):
    """Group tasks intelligently: Overdue → Critical/High → By Project (due soon) → Rest.

    Returns list of (label, task_list) tuples. Each task appears in exactly one group.
    """
    overdue = []
    urgent = []
    by_project: dict[str, list] = {}
    rest = []

    for t in tasks:
        if is_overdue(t.get("due_date")) and t["status"] != "done":
            overdue.append(t)
        elif t.get("priority", "medium") in ("critical", "high"):
            urgent.append(t)
        elif t.get("project"):
            by_project.setdefault(t["project"], []).append(t)
        else:
            rest.append(t)

    groups = []
    if overdue:
        groups.append(("⚠ Overdue", overdue))
    if urgent:
        groups.append(("Urgent", urgent))
    for proj_name in sorted(by_project, key=lambda p: len(by_project[p]), reverse=True):
        groups.append((proj_name, by_project[proj_name]))
    if rest:
        groups.append(("Other", rest))
    return groups


# ── TrayPopup ───────────────────────────────────────────────────────


class TrayPopup(QWidget):
    """Compact popup showing top suggested tasks."""

    def __init__(self, db, on_open_full, parent=None):
        super().__init__(
            parent,
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint,
        )
        self.db = db
        self.on_open_full = on_open_full
        self._tasks = []
        self.setFixedWidth(380)
        self.setMaximumHeight(500)
        self.setStyleSheet(self._stylesheet())
        self._search_engine = db.search_engine
        self._build_ui()

        # Auto-refresh timer (only ticks when visible)
        self._refresh_timer = QTimer(self)
        self._refresh_timer.timeout.connect(self.refresh)

        # Search debounce (300ms)
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(300)
        self._search_timer.timeout.connect(self.refresh)

    def _stylesheet(self):
        return _build_popup_style()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Header row: "Tasks" + "+" button
        header_row = QHBoxLayout()
        header_row.setContentsMargins(0, 0, 8, 0)
        header = QLabel("Tasks")
        header.setObjectName("header")
        header_row.addWidget(header)
        header_row.addStretch()
        self._add_btn = QPushButton("+")
        self._add_btn.setObjectName("add-btn")
        self._add_btn.setFixedSize(30, 30)
        self._add_btn.clicked.connect(self._toggle_add_form)
        header_row.addWidget(self._add_btn)
        layout.addLayout(header_row)

        # Collapsible add-task form (hidden by default)
        self._add_form = QWidget()
        self._add_form.setVisible(False)
        form_layout = QVBoxLayout(self._add_form)
        form_layout.setContentsMargins(0, 0, 0, 4)
        form_layout.setSpacing(0)
        self._add_title = QLineEdit()
        self._add_title.setPlaceholderText("Title...")
        form_layout.addWidget(self._add_title)
        self._add_desc = QTextEdit()
        self._add_desc.setPlaceholderText("Description...")
        self._add_desc.setMaximumHeight(60)
        form_layout.addWidget(self._add_desc)
        self._add_due = QLineEdit()
        self._add_due.setPlaceholderText("Due date (YYYY-MM-DD)")
        form_layout.addWidget(self._add_due)
        self._add_priority = QComboBox()
        self._add_priority.addItems(PRIORITIES)
        self._add_priority.setCurrentText("medium")
        form_layout.addWidget(self._add_priority)
        self._add_type = QComboBox()
        self._add_type.addItems(["Task", "Note"])
        form_layout.addWidget(self._add_type)
        submit = QPushButton("Add Task")
        submit.setObjectName("submit-btn")
        submit.clicked.connect(self._submit_task)
        self._add_title.returnPressed.connect(self._submit_task)
        form_layout.addWidget(submit)
        layout.addWidget(self._add_form)

        # Scroll area for tasks
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.task_container = QWidget()
        self.task_layout = QVBoxLayout(self.task_container)
        self.task_layout.setContentsMargins(0, 0, 0, 0)
        self.task_layout.setSpacing(0)
        self.scroll.setWidget(self.task_container)
        layout.addWidget(self.scroll)

        # Search bar (bottom)
        self._search_input = QLineEdit()
        self._search_input.setObjectName("search")
        self._search_input.setPlaceholderText("Search tasks...")
        self._search_input.setClearButtonEnabled(True)
        self._search_input.textChanged.connect(self._on_search)
        layout.addWidget(self._search_input)

        # Open full button
        btn = QPushButton("Open Full Window")
        btn.setObjectName("open-full")
        btn.clicked.connect(self.on_open_full)
        layout.addWidget(btn)

        self._search_text = ""

    def refresh(self):
        """Reload tasks from DB and rebuild list."""
        self.db.promote_due_today()
        while self.task_layout.count():
            item = self.task_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        q = self._search_text
        if q:
            # Search ALL tasks (same as FullWindow) via SmartKey engine
            all_tasks = self.db.get_all_active() + self.db.get_done_tasks()
            self._search_engine.rebuild_index(all_tasks)
            tasks = self._search_engine.search(
                q, all_tasks, limit=20, conn=self.db._conn, use_vector=False
            )
        else:
            tasks = self.db.get_suggested_tasks(limit=8)

        self._tasks = tasks  # cache for _open_reader lookup

        if tasks:
            groups = _smart_group(tasks)
            for group_label, group_tasks in groups:
                if not group_tasks:
                    continue
                lbl = QLabel(f"{group_label} ({len(group_tasks)})")
                lbl.setObjectName("section-header")
                self.task_layout.addWidget(lbl)
                for task in group_tasks:
                    self.task_layout.addWidget(self._make_task_row(task))
        else:
            msg = "No matches" if q else "All clear!"
            lbl = QLabel(msg)
            lbl.setObjectName("section-header")
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.task_layout.addWidget(lbl)

        self.task_layout.addStretch()

    def _make_task_row(self, task):
        overdue = is_overdue(task.get("due_date")) and task["status"] != "done"
        row = QWidget()
        if overdue:
            t = _T()
            row.setStyleSheet(
                f"border-left: 3px solid {t['danger']}; background: rgba(229,62,62,0.12);"
            )
        hl = QHBoxLayout(row)
        hl.setContentsMargins(14, 2, 14, 2)

        # Checkbox — no text, only the square
        cb = QCheckBox()
        cb.setChecked(task["status"] == "done")
        task_id = task["id"]
        cb.toggled.connect(lambda checked, tid=task_id: self._on_toggle(tid, checked))
        hl.addWidget(cb)

        # Clickable title label — opens TaskReaderDialog
        title_lbl = _ClickableLabel(task["title"])
        title_lbl.setCursor(Qt.CursorShape.PointingHandCursor)
        if task["status"] == "done":
            title_lbl.setStyleSheet(
                f"color: {_T()['done']}; text-decoration: line-through;"
            )
        title_lbl.clicked.connect(lambda tid=task_id: self._open_reader(tid))
        hl.addWidget(title_lbl, 1)

        priority = (task.get("priority") or "medium").upper()
        plbl = QLabel(priority)
        plbl.setObjectName("priority")
        plbl.setStyleSheet(f"color: {_PRIORITY_COLORS_UPPER.get(priority, '#718096')};")
        hl.addWidget(plbl)

        tip = _build_rich_tooltip(task)
        if tip:
            row.setToolTip(tip)
        row.installEventFilter(_TooltipCopyFilter(task, row))

        return row

    def _on_toggle(self, task_id, checked):
        # Defer DB write — on_change() triggers _refresh_all() which rebuilds
        # this popup's widgets; doing it inside the signal can crash.
        QTimer.singleShot(0, lambda: self._apply_toggle(task_id, checked))

    def _apply_toggle(self, task_id, checked):
        if checked:
            self.db.mark_done(task_id)
        else:
            self.db.update_task(task_id, status="not_started")

    def _open_reader(self, task_id):
        task = next((t for t in self._tasks if t["id"] == task_id), None)
        if task:
            dlg = TaskReaderDialog(task, self.db, self)
            dlg.exec()

    def _toggle_add_form(self):
        visible = not self._add_form.isVisible()
        self._add_form.setVisible(visible)
        self._add_btn.setText("\u2212" if visible else "+")
        if visible:
            self._add_title.setFocus()
        self.adjustSize()

    def _submit_task(self):
        title = self._add_title.text().strip()
        if not title:
            return
        kwargs = {"section": "inbox", "priority": self._add_priority.currentText()}
        desc = self._add_desc.toPlainText().strip()
        due = self._add_due.text().strip()
        if due:
            kwargs["due_date"] = due
        task_type = self._add_type.currentText().lower()
        task_id = self.db.add_task(
            title, type=task_type, description=desc or None, **kwargs
        )
        self._add_title.clear()
        self._add_desc.clear()
        self._add_due.clear()
        self._add_priority.setCurrentText("medium")
        self._add_type.setCurrentText("Task")
        self._add_form.setVisible(False)
        self._add_btn.setText("+")
        self.refresh()

    def _on_search(self, text):
        self._search_text = text.strip().lower()
        self._search_timer.start()  # debounce: resets 300ms countdown

    def show_near_tray(self, tray_geometry):
        """Position popup near the tray icon."""
        self.refresh()
        self.adjustSize()
        x = tray_geometry.x() - self.width() // 2
        y = tray_geometry.y() - self.height()
        primary = QApplication.primaryScreen()
        if primary is None:
            self.move(QPoint(x, y))
            self.show()
            self.activateWindow()
            return
        screen = primary.availableGeometry()
        x = max(screen.left(), min(x, screen.right() - self.width()))
        y = max(screen.top(), min(y, screen.bottom() - self.height()))
        self.move(QPoint(x, y))
        self.show()
        self.activateWindow()

    def changeEvent(self, event):
        # Dismiss on deactivation (replaces Popup auto-dismiss behavior)
        if event.type() == QEvent.Type.ActivationChange and not self.isActiveWindow():
            self.hide()
        super().changeEvent(event)

    def showEvent(self, event):
        super().showEvent(event)
        self._refresh_timer.start(_REFRESH_INTERVAL_MS)

    def hideEvent(self, event):
        super().hideEvent(event)
        self._refresh_timer.stop()


# ── FullWindow ──────────────────────────────────────────────────────


class _CalendarShowFilter(QObject):
    """Open calendar at today's page when no date is set."""

    def __init__(self, due_edit, parent=None):
        super().__init__(parent)
        self._due_edit = due_edit

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.Show:
            if self._due_edit.date() == self._due_edit.minimumDate():
                today = QDate.currentDate()
                obj.setCurrentPage(today.year(), today.month())
        return False


class EditTaskDialog(QDialog):
    """Dialog for editing task fields with smart defaults."""

    # Section → date intelligence
    _SECTION_DATE = {
        "inbox": 1,  # tomorrow
        "today": 0,  # today
        "next": 1,  # tomorrow
        "someday": None,  # no date
        "waiting": 7,  # +1 week
    }

    def __init__(self, task, parent=None, db=None):
        super().__init__(parent)
        self._db = db
        self.setWindowTitle("Edit Task")
        self.setMinimumWidth(380)
        self.setStyleSheet(_build_dialog_style())
        layout = QFormLayout(self)

        self.type_combo = QComboBox()
        self.type_combo.addItems(["Task", "Note"])
        self.type_combo.setCurrentText(task.get("type", "task").title())
        layout.addRow("Type:", self.type_combo)

        self.status_check = QCheckBox()
        self.status_check.setChecked(task.get("status") == "done")
        layout.addRow("Completed:", self.status_check)

        self.title_edit = QLineEdit(task.get("title", ""))
        layout.addRow("Title:", self.title_edit)

        self.desc_edit = QTextEdit()
        self.desc_edit.setPlainText(task.get("description", "") or "")
        self.desc_edit.setMaximumHeight(80)
        self.desc_edit.setPlaceholderText("Description...")
        layout.addRow("Description:", self.desc_edit)

        self.section_combo = QComboBox()
        self.section_combo.addItems(SECTIONS)
        self.section_combo.setCurrentText(task.get("section", "inbox"))
        self.section_combo.currentTextChanged.connect(self._on_section_changed)
        layout.addRow("Section:", self.section_combo)

        self.priority_combo = QComboBox()
        self.priority_combo.addItems(PRIORITIES)
        self.priority_combo.setCurrentText(task.get("priority", "medium"))
        layout.addRow("Priority:", self.priority_combo)

        # Due date — QDateEdit with calendar popup, DD.MM.YYYY format
        self.due_edit = QDateEdit()
        self.due_edit.setCalendarPopup(True)
        self.due_edit.setDisplayFormat("dd.MM.yyyy")
        self.due_edit.setSpecialValueText("—")  # shown when "no date"
        self._due_cleared = False  # track if user explicitly cleared date
        existing_due = task.get("due_date", "") or ""
        if existing_due:
            parsed = QDate.fromString(existing_due, "yyyy-MM-dd")
            if parsed.isValid():
                self.due_edit.setDate(parsed)
            else:
                self._set_smart_date(task.get("section", "inbox"))
        else:
            self._set_smart_date(task.get("section", "inbox"))

        due_row = QHBoxLayout()
        due_row.addWidget(self.due_edit, 1)
        self.due_clear_btn = QPushButton("✕")
        self.due_clear_btn.setFixedWidth(28)
        self.due_clear_btn.setToolTip("Clear date")
        self.due_clear_btn.clicked.connect(self._clear_due)
        due_row.addWidget(self.due_clear_btn)
        layout.addRow("Due Date:", due_row)

        # Calendar: open at today, block past dates, dropdown month/year nav
        cal = self.due_edit.calendarWidget()
        if cal:
            self._cal_filter = _CalendarShowFilter(self.due_edit, self)
            cal.installEventFilter(self._cal_filter)

            def _on_cal_clicked(qdate):
                if qdate < QDate.currentDate():

                    def _snap():
                        self.due_edit.setDate(self.due_edit.minimumDate())
                        self._due_cleared = True

                    QTimer.singleShot(0, _snap)

            cal.clicked.connect(_on_cal_clicked)

            # Dropdown menus for month/year navigation buttons
            month_names = [_cal_mod.month_name[i] for i in range(1, 13)]
            for btn in cal.findChildren(QToolButton):
                txt = btn.text().strip()
                if not txt:
                    continue
                if txt.isdigit() and len(txt) == 4:
                    menu = QMenu(btn)
                    cur_year = QDate.currentDate().year()
                    for y in range(cur_year, cur_year + 3):
                        act = menu.addAction(str(y))
                        act.triggered.connect(
                            lambda checked, yr=y: cal.setCurrentPage(
                                yr, cal.monthShown()
                            )
                        )
                    btn.setMenu(menu)
                    btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
                elif len(txt) > 2:
                    menu = QMenu(btn)
                    for i, name in enumerate(month_names, 1):
                        act = menu.addAction(name)
                        act.triggered.connect(
                            lambda checked, m=i: cal.setCurrentPage(cal.yearShown(), m)
                        )
                    btn.setMenu(menu)
                    btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)

        # Project — editable combo with autocomplete from existing projects
        self.project_combo = QComboBox()
        self.project_combo.setEditable(True)
        existing_projects = db.get_project_names() if db else []
        if "general" not in existing_projects:
            existing_projects.insert(0, "general")
        self.project_combo.addItems(existing_projects)
        completer = QCompleter(existing_projects)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        completer.setFilterMode(Qt.MatchFlag.MatchContains)
        self.project_combo.setCompleter(completer)
        current_project = task.get("project", "") or "general"
        self.project_combo.setCurrentText(current_project)
        layout.addRow("Project:", self.project_combo)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def _set_smart_date(self, section):
        """Set due date based on section intelligence."""
        offset = self._SECTION_DATE.get(section)
        if offset is not None:
            self.due_edit.setDate(QDate.currentDate().addDays(offset))
            self._due_cleared = False
        else:
            # "someday" / unknown → minimum date = visual "no date"
            self.due_edit.setDate(self.due_edit.minimumDate())
            self._due_cleared = True

    def _on_section_changed(self, section):
        """Auto-adjust due date when section changes (only if no manual date set)."""
        if self._due_cleared or self.due_edit.date() == self.due_edit.minimumDate():
            self._set_smart_date(section)

    def _clear_due(self):
        """Clear due date (set to minimum = special value)."""
        self.due_edit.setDate(self.due_edit.minimumDate())
        self._due_cleared = True

    def get_values(self):
        vals = {
            "title": self.title_edit.text().strip(),
            "description": self.desc_edit.toPlainText().strip() or None,
            "section": self.section_combo.currentText(),
            "priority": self.priority_combo.currentText(),
        }
        # Due date: None if cleared, else YYYY-MM-DD for DB storage
        if self._due_cleared or self.due_edit.date() == self.due_edit.minimumDate():
            vals["due_date"] = None
        else:
            vals["due_date"] = self.due_edit.date().toString("yyyy-MM-dd")
        project = self.project_combo.currentText().strip()
        vals["project"] = project if project else None
        vals["type"] = self.type_combo.currentText().lower()
        vals["status"] = "done" if self.status_check.isChecked() else "not_started"
        return vals


class EntityLinkDialog(QDialog):
    """Dialog for searching and linking knowledge graph entities to a task."""

    def __init__(self, db: TaskDB, task_id: str, parent=None):
        super().__init__(parent)
        self.db = db
        self.task_id = task_id
        self._debounce_timer: int | None = None
        self._pending_query = ""
        self.setWindowTitle("Link to Entity")
        self.setMinimumSize(500, 450)
        self.setModal(True)
        self._build_ui()
        self._load_current_links()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.setContentsMargins(16, 16, 16, 16)

        # Current links section
        layout.addWidget(QLabel("<b>Current Links:</b>"))
        self._current_list = QListWidget()
        self._current_list.setMaximumHeight(120)
        self._current_list.setStyleSheet(
            "QListWidget { border: 1px solid #555; border-radius: 4px; }"
            "QListWidget::item { padding: 4px 8px; }"
        )
        layout.addWidget(self._current_list)

        self._unlink_btn = QPushButton("Unlink Selected")
        self._unlink_btn.setEnabled(False)
        self._unlink_btn.clicked.connect(self._on_unlink)
        self._current_list.itemSelectionChanged.connect(
            lambda: self._unlink_btn.setEnabled(
                bool(self._current_list.selectedItems())
            )
        )
        layout.addWidget(self._unlink_btn)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("color: #555;")
        layout.addWidget(sep)

        # Search section
        layout.addWidget(QLabel("<b>Search Entities:</b>"))
        self._search_input = QLineEdit()
        self._search_input.setPlaceholderText("Type to search entities...")
        self._search_input.setStyleSheet(
            "QLineEdit { padding: 6px 10px; border: 1px solid #666; "
            "border-radius: 4px; font-size: 13px; }"
        )
        self._search_input.textChanged.connect(self._on_search_changed)
        layout.addWidget(self._search_input)

        self._results_list = QListWidget()
        self._results_list.setStyleSheet(
            "QListWidget { border: 1px solid #555; border-radius: 4px; }"
            "QListWidget::item { padding: 6px 8px; }"
            "QListWidget::item:selected { background: #1a3a5c; color: white; }"
        )
        layout.addWidget(self._results_list)

        # Buttons
        btn_layout = QHBoxLayout()
        self._link_btn = QPushButton("Link Selected")
        self._link_btn.setEnabled(False)
        self._link_btn.setStyleSheet(
            "QPushButton { background: #1a3a5c; color: white; padding: 8px 20px; "
            "border-radius: 4px; font-weight: bold; }"
            "QPushButton:hover { background: #254d73; }"
            "QPushButton:disabled { background: #555; }"
        )
        self._link_btn.clicked.connect(self._on_link)
        self._results_list.itemSelectionChanged.connect(
            lambda: self._link_btn.setEnabled(bool(self._results_list.selectedItems()))
        )

        close_btn = QPushButton("Close")
        close_btn.setStyleSheet(
            "QPushButton { padding: 8px 20px; border: 1px solid #666; "
            "border-radius: 4px; }"
            "QPushButton:hover { background: #333; }"
        )
        close_btn.clicked.connect(self.accept)

        btn_layout.addStretch()
        btn_layout.addWidget(self._link_btn)
        btn_layout.addWidget(close_btn)
        layout.addLayout(btn_layout)

    def _load_current_links(self):
        self._current_list.clear()
        links = self.db.get_task_links(self.task_id)
        for link in links:
            text = f"{link['entity_name']}  ({link['entity_type']})"
            if link.get("link_type") == "auto":
                text += "  [auto]"
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, link["entity_id"])
            self._current_list.addItem(item)

        if not links:
            item = QListWidgetItem("No linked entities")
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            item.setForeground(QColor("#999"))
            self._current_list.addItem(item)

    def _on_search_changed(self, text: str):
        """Debounced search — 300ms delay."""
        if self._debounce_timer is not None:
            self.killTimer(self._debounce_timer)
        self._debounce_timer = self.startTimer(300)
        self._pending_query = text

    def timerEvent(self, event):
        if event.timerId() == self._debounce_timer:
            self.killTimer(self._debounce_timer)
            self._debounce_timer = None
            self._do_search(self._pending_query)

    def _do_search(self, query: str):
        self._results_list.clear()
        if len(query.strip()) < 2:
            return

        results = self.db.search_entities(query)
        current_ids: set[int] = set()
        for i in range(self._current_list.count()):
            eid = self._current_list.item(i).data(Qt.ItemDataRole.UserRole)
            if eid is not None:
                current_ids.add(eid)

        for r in results:
            if r["rowid"] in current_ids:
                continue
            text = f"{r['name']}  ({r['entity_type']})  — {r['obs_count']} obs"
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, r["rowid"])
            self._results_list.addItem(item)

        if self._results_list.count() == 0:
            item = QListWidgetItem("No matching entities found")
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            item.setForeground(QColor("#999"))
            self._results_list.addItem(item)

    def _on_link(self):
        items = self._results_list.selectedItems()
        if not items:
            return
        entity_id = items[0].data(Qt.ItemDataRole.UserRole)
        if entity_id is None:
            return
        if self.db.link_task_entity(self.task_id, entity_id):
            self._load_current_links()
            self._do_search(self._search_input.text())

    def _on_unlink(self):
        items = self._current_list.selectedItems()
        if not items:
            return
        entity_id = items[0].data(Qt.ItemDataRole.UserRole)
        if entity_id is None:
            return
        if self.db.unlink_task_entity(self.task_id, entity_id):
            self._load_current_links()


class TaskReaderDialog(QDialog):
    """Read-only view for task descriptions with comfortable reading layout."""

    def __init__(self, task, db, parent=None):
        super().__init__(parent)
        self.task = task
        self.db = db

        # Size: 60% x 85% of screen or 700x900 minimum
        screen = QApplication.primaryScreen()
        if screen:
            sg = screen.availableGeometry()
            w = max(700, int(sg.width() * 0.6))
            h = max(900, int(sg.height() * 0.85))
        else:
            w, h = 700, 900
        self.resize(w, h)
        self.setMinimumSize(700, 900)

        title_text = (task.get("title") or "")[:60]
        self.setWindowTitle(title_text)

        self.setStyleSheet(_build_reader_style())

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Header
        self._header = QFrame()
        self._header.setObjectName("reader-header")
        header_layout = QVBoxLayout(self._header)
        header_layout.setContentsMargins(0, 0, 0, 8)
        header_layout.setSpacing(4)

        self._title_label = QLabel()
        self._title_label.setObjectName("reader-title")
        self._title_label.setWordWrap(True)
        header_layout.addWidget(self._title_label)

        self._meta_layout = QHBoxLayout()
        self._meta_layout.setContentsMargins(16, 0, 16, 0)
        self._meta_layout.setSpacing(8)
        header_layout.addLayout(self._meta_layout)

        layout.addWidget(self._header)

        # Body scroll area
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._body_label = QLabel()
        self._body_label.setObjectName("reader-body")
        self._body_label.setWordWrap(True)
        self._body_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self._body_label.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
        )
        scroll.setWidget(self._body_label)
        self._scroll = scroll
        layout.addWidget(scroll, 1)

        # Button bar
        btn_bar = QHBoxLayout()
        btn_bar.setContentsMargins(16, 8, 16, 8)
        btn_bar.addStretch()
        edit_btn = QPushButton("Edit")
        edit_btn.clicked.connect(self._on_edit)
        btn_bar.addWidget(edit_btn)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.close)
        btn_bar.addWidget(close_btn)
        layout.addLayout(btn_bar)

        self._refresh_display()

        # Center on screen
        if screen:
            sg = screen.availableGeometry()
            self.move(sg.center() - self.rect().center())

    def _refresh_display(self):
        self._title_label.setText(self.task.get("title") or "Untitled")

        # Clear old meta labels
        while self._meta_layout.count():
            item = self._meta_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        # Priority badge
        priority = (self.task.get("priority") or "medium").upper()
        plbl = QLabel(priority)
        plbl.setObjectName("reader-priority")
        color = _PRIORITY_COLORS_UPPER.get(priority, "#718096")
        plbl.setStyleSheet(
            f"color: #ffffff; background: {color}; font-size: {_font_size - 2}px; "
            f"font-weight: bold; padding: 2px 8px; border-radius: 3px;"
        )
        self._meta_layout.addWidget(plbl)

        # Optional meta items
        for key, label in [
            ("section", "Section"),
            ("due_date", "Due"),
            ("project", "Project"),
        ]:
            val = self.task.get(key)
            if val:
                mlbl = QLabel(f"{label}: {val}")
                mlbl.setObjectName("reader-meta")
                self._meta_layout.addWidget(mlbl)

        rl = _recurring_label(self.task.get("recurring"))
        if rl:
            rlbl = QLabel(f"\U0001f504 {rl}")
            rlbl.setObjectName("reader-meta")
            self._meta_layout.addWidget(rlbl)

        self._meta_layout.addStretch()

        # Body
        desc = self.task.get("description") or ""
        if desc:
            escaped = _html.escape(desc)
            paragraphs = escaped.split("\n\n")
            reading = len(paragraphs) > 1

            if reading:
                font = f"{_font_size + 8}px"
                style = (
                    f"font-family: Georgia, 'Noto Serif', serif; font-size: {font}; "
                    f"line-height: 230%; color: #d4d4d4; max-width: 680px; "
                    f"margin: 0 auto; letter-spacing: 0.3px;"
                )
            else:
                style = (
                    f"font-family: Segoe UI; font-size: {_font_size}px; "
                    f"line-height: 160%; color: #e2e8f0;"
                )

            if reading:
                inner = "".join(
                    f'<p style="margin: 0 0 1.2em 0;">{p.replace(chr(10), "<br>")}</p>'
                    for p in paragraphs
                )
            else:
                inner = "".join(
                    f"<p>{p.replace(chr(10), '<br>')}</p>" for p in paragraphs
                )
            body_html = f'<div style="{style}">{inner}</div>'

            # Reading mode: Kindle Dark background override
            if reading:
                self._body_label.setStyleSheet(
                    f"QLabel#reader-body {{ background: #0a0a0a; color: #d4d4d4; "
                    f"font-size: {_font_size + 8}px; padding: 40px 32px; }}"
                )
                self._scroll.setStyleSheet("background: #0a0a0a;")
            else:
                self._body_label.setStyleSheet("")
                self._scroll.setStyleSheet("")
        else:
            body_html = (
                '<div style="font-family: Segoe UI; font-size: 13px; '
                'color: #4a5568; font-style: italic;">No description</div>'
            )
            self._body_label.setStyleSheet("")
            self._scroll.setStyleSheet("")

        # Linked entities section
        links = self.db.get_task_links(self.task.get("id", ""))
        if links:
            _type_colors = {
                "concept": "#1a3a5c",
                "tool": "#2d6a2e",
                "person": "#8b4513",
                "project": "#4a148c",
                "technology": "#00695c",
            }
            badges = []
            for lk in links:
                c = _type_colors.get((lk.get("entity_type") or "").lower(), "#555")
                b = (
                    f'<span style="display:inline-block; background:{c}; color:white; '
                    f"padding:3px 10px; border-radius:12px; font-size:11px; "
                    f'margin:2px 4px 2px 0;">{_html.escape(lk["entity_name"])}'
                )
                if lk.get("entity_type"):
                    b += f' <span style="opacity:0.7;">({_html.escape(lk["entity_type"])})</span>'
                if lk.get("link_type") == "auto":
                    b += ' <span style="opacity:0.5;">[auto]</span>'
                b += "</span>"
                badges.append(b)
            body_html += (
                '<div style="margin-top:16px; padding-top:12px; border-top:1px solid #444;">'
                '<div style="font-weight:bold; color:#a0aec0; margin-bottom:8px; '
                'font-size:12px;">Linked Entities</div>'
                f"<div>{''.join(badges)}</div></div>"
            )

        self._body_label.setText(body_html)

    def _on_edit(self):
        dlg = EditTaskDialog(self.task, self, db=self.db)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            vals = dlg.get_values()
            self.db.update_task(self.task["id"], **vals)
            self.task.update(vals)
            self._refresh_display()


class TaskListWidget(QListWidget):
    """Custom list widget for tasks with checkbox + priority badge."""

    def __init__(self, db, parent=None):
        super().__init__(parent)
        self.db = db
        self.setItemDelegate(_OverdueDelegate(self))
        self.setStyleSheet(_build_list_style())
        self.itemDoubleClicked.connect(self._on_double_click)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._context_menu)
        self._tasks = []
        self._last_fp = None  # fingerprint for skip-if-unchanged
        self.installEventFilter(_ListTooltipCopyFilter(self, self))

    @staticmethod
    def _build_tooltip(task):
        return _build_rich_tooltip(task)

    @staticmethod
    def _fingerprint(tasks):
        return tuple((t["id"], t.get("updated_at", "")) for t in tasks)

    def load_tasks(self, tasks):
        fp = self._fingerprint(tasks)
        if fp == self._last_fp:
            return
        self._last_fp = fp
        self._tasks = tasks
        self.blockSignals(True)
        self.clear()
        for task in tasks:
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, task["id"])
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                Qt.CheckState.Checked
                if task["status"] == "done"
                else Qt.CheckState.Unchecked
            )
            item.setText(_format_task_text(task))
            tip = self._build_tooltip(task)
            if tip:
                item.setToolTip(tip)
            _apply_task_item_colors(item, task)
            self.addItem(item)
        self.blockSignals(False)

    def load_grouped_by_project(self, tasks):
        """Load tasks grouped by project with section headers."""
        from collections import OrderedDict

        fp = self._fingerprint(tasks)
        if fp == self._last_fp:
            return
        self._last_fp = fp
        self._tasks = tasks
        self.blockSignals(True)
        self.clear()

        groups: OrderedDict[str, list] = OrderedDict()
        for t in tasks:
            proj = t.get("project") or "(no project)"
            groups.setdefault(proj, []).append(t)

        for proj_name, proj_tasks in groups.items():
            # Project header item (non-interactive)
            header = QListWidgetItem(f"── {proj_name} ({len(proj_tasks)}) ──")
            header.setFlags(Qt.ItemFlag.NoItemFlags)
            header.setBackground(_CLR_HEADER_BG)
            header.setForeground(_CLR_HEADER_FG)
            font = header.font()
            font.setBold(True)
            header.setFont(font)
            self.addItem(header)

            # Tasks under this project
            for task in proj_tasks:
                item = QListWidgetItem()
                item.setData(Qt.ItemDataRole.UserRole, task["id"])
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(
                    Qt.CheckState.Checked
                    if task["status"] == "done"
                    else Qt.CheckState.Unchecked
                )
                item.setText(
                    _format_task_text(task, include_project=False, prefix="  ")
                )
                tip = self._build_tooltip(task)
                if tip:
                    item.setToolTip(tip)
                _apply_task_item_colors(item, task)
                self.addItem(item)

        self.blockSignals(False)

    def load_smart_grouped(self, tasks):
        """Load tasks with smart grouping: Overdue → Urgent → By Project → Rest."""
        fp = self._fingerprint(tasks)
        if fp == self._last_fp:
            return
        self._last_fp = fp
        self._tasks = tasks
        self.blockSignals(True)
        self.clear()
        groups = _smart_group(tasks)
        for group_label, group_tasks in groups:
            if not group_tasks:
                continue
            header = QListWidgetItem(f"── {group_label} ({len(group_tasks)}) ──")
            header.setFlags(Qt.ItemFlag.NoItemFlags)
            header.setBackground(_CLR_HEADER_BG)
            header.setForeground(_CLR_HEADER_FG)
            font = header.font()
            font.setBold(True)
            header.setFont(font)
            if group_label == "⚠ Overdue":
                header.setBackground(_CLR_OVERDUE_HDR_BG)
                header.setForeground(_CLR_OVERDUE_HDR_FG)
            elif group_label == "Urgent":
                header.setBackground(_CLR_URGENT_HDR_BG)
                header.setForeground(_CLR_URGENT_HDR_FG)
            self.addItem(header)
            for task in group_tasks:
                item = QListWidgetItem()
                item.setData(Qt.ItemDataRole.UserRole, task["id"])
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(
                    Qt.CheckState.Checked
                    if task["status"] == "done"
                    else Qt.CheckState.Unchecked
                )
                item.setText(_format_task_text(task, prefix="  "))
                tip = self._build_tooltip(task)
                if tip:
                    item.setToolTip(tip)
                _apply_task_item_colors(item, task)
                self.addItem(item)
        self.blockSignals(False)

    def _open_reader(self, task_id):
        task = next((t for t in self._tasks if t["id"] == task_id), None)
        if task:
            if hasattr(self, "_search_engine"):
                self._search_engine.record_open(task)
            dlg = TaskReaderDialog(task, self.db, self)
            dlg.exec()

    def _on_double_click(self, item):
        task_id = item.data(Qt.ItemDataRole.UserRole)
        if not task_id:
            return
        self._open_reader(task_id)

    def _context_menu(self, pos):
        item = self.itemAt(pos)
        if not item:
            return
        task_id = item.data(Qt.ItemDataRole.UserRole)
        if not task_id:
            return
        menu = QMenu(self)
        menu.setStyleSheet(_build_menu_style())
        view_action = menu.addAction("View")
        task = next((t for t in self._tasks if t["id"] == task_id), None)
        current_type = task.get("type", "task") if task else "task"
        target_type = "note" if current_type == "task" else "task"
        convert_action = menu.addAction(f"Convert to {target_type.title()}")
        has_recurring = bool(task.get("recurring")) if task else False
        recurring_label = "Edit Recurring..." if has_recurring else "Set Recurring..."
        recurring_action = menu.addAction(recurring_label)
        if has_recurring:
            clear_recurring_action = menu.addAction("Clear Recurring")
        else:
            clear_recurring_action = None
        # Reminder actions
        has_reminder = bool(task.get("reminder_at")) if task else False
        reminder_label = "Edit Reminder..." if has_reminder else "Set Reminder..."
        reminder_action = menu.addAction(reminder_label)
        if has_reminder:
            clear_reminder_action = menu.addAction("Clear Reminder")
        else:
            clear_reminder_action = None
        menu.addSeparator()
        # v0.7.0: Publish / Unpublish
        task_vis = task.get("visibility", "private") if task else "private"
        publish_action = unpublish_action = None
        if task_vis == "public":
            unpublish_action = menu.addAction("\U0001f310 Unpublish")
        elif task_vis == "pending_public":
            unpublish_action = menu.addAction("\u23f3 Cancel Publish")
        else:
            publish_action = menu.addAction("Publish...")
        menu.addSeparator()
        # v2.2.0: Entity links
        task_links = self.db.get_task_links(task_id)
        if task_links:
            link_action = menu.addAction(f"Manage Links ({len(task_links)})...")
        else:
            link_action = menu.addAction("Link to Entity...")
        menu.addSeparator()
        delete_action = menu.addAction("Delete")
        action = menu.exec(self.mapToGlobal(pos))
        if action == view_action:
            self._open_reader(task_id)
        elif action == convert_action:
            self.db.update_task(task_id, type=target_type)
        elif action == recurring_action:
            self._show_recurring_dialog(task_id, task)
        elif action == clear_recurring_action:
            self.db.update_task(task_id, recurring=None)
        elif action == reminder_action:
            self._show_reminder_dialog(task_id, task)
        elif action == clear_reminder_action:
            self.db.update_task(task_id, reminder_at=None)
        elif publish_action and action == publish_action:
            self._publish_task(task_id)
        elif unpublish_action and action == unpublish_action:
            self._cancel_publish_task(task_id)
        elif action == link_action:
            EntityLinkDialog(self.db, task_id, self).exec()
        elif action == delete_action:
            self.db.delete_task(task_id)

    def _publish_task(self, task_id):
        """Two-warning gate before setting task to pending_public."""
        # Warning 1: visibility scope
        msg1 = QMessageBox(self)
        msg1.setWindowTitle("Publish Task")
        msg1.setText(
            "Are you sure you want to publish this content?\n"
            "It will be visible to ALL Claude instances."
        )
        btn_no = msg1.addButton("Don't Publish", QMessageBox.ButtonRole.RejectRole)
        btn_yes = msg1.addButton("Publish", QMessageBox.ButtonRole.AcceptRole)
        msg1.setDefaultButton(btn_no)
        msg1.exec()
        if msg1.clickedButton() != btn_yes:
            return

        # Warning 2: harm/safety check
        msg2 = QMessageBox(self)
        msg2.setWindowTitle("Safety Check")
        msg2.setText(
            "Are you sure the content will NOT harm,\n"
            "endanger, or compromise the safety of any person?"
        )
        btn_cancel = msg2.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        btn_confirm = msg2.addButton("Confirm Safe", QMessageBox.ButtonRole.AcceptRole)
        msg2.setDefaultButton(btn_cancel)
        msg2.exec()
        if msg2.clickedButton() != btn_confirm:
            return

        self.db.update_task(
            task_id,
            visibility="pending_public",
            publish_requested_at=now_iso(),
        )

    def _cancel_publish_task(self, task_id):
        """Revert pending_public or public → private."""
        self.db.update_task(
            task_id,
            visibility="private",
            publish_requested_at=None,
        )

    def _show_recurring_dialog(self, task_id, task):
        """Show dialog to set/edit recurring schedule."""
        dlg = RecurringDialog(task, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            config = dlg.get_config()
            self.db.update_task(task_id, recurring=config)

    def _show_reminder_dialog(self, task_id, task):
        """Show dialog to set/edit reminder."""
        existing = task.get("reminder_at") if task else None
        dlg = ReminderDateTimeDialog(existing, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            reminder_at = dlg.get_reminder_at()
            self.db.update_task(task_id, reminder_at=reminder_at)


class RecurringDialog(QDialog):
    """Dialog to configure recurring schedule for a task."""

    _WEEKDAYS = (
        "Monday",
        "Tuesday",
        "Wednesday",
        "Thursday",
        "Friday",
        "Saturday",
        "Sunday",
    )

    def __init__(self, task, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Recurring Schedule")
        self.setMinimumWidth(300)
        self.setStyleSheet(_build_dialog_style())
        layout = QFormLayout(self)

        self.every_combo = QComboBox()
        self.every_combo.addItems(["Daily", "Weekly", "Monthly", "Yearly"])
        self.every_combo.currentTextChanged.connect(self._on_every_changed)
        layout.addRow("Repeat:", self.every_combo)

        self.day_of_week = QComboBox()
        self.day_of_week.addItems(self._WEEKDAYS)
        self._dow_label = QLabel("Day:")
        layout.addRow(self._dow_label, self.day_of_week)

        self.day_of_month = QSpinBox()
        self.day_of_month.setRange(1, 31)
        self._dom_label = QLabel("Day of month:")
        layout.addRow(self._dom_label, self.day_of_month)

        self.interval_spin = QSpinBox()
        self.interval_spin.setRange(1, 52)
        self.interval_spin.setValue(1)
        self._interval_label = QLabel("Every N:")
        layout.addRow(self._interval_label, self.interval_spin)

        self.month_combo = QComboBox()
        import calendar

        self.month_combo.addItems([calendar.month_name[i] for i in range(1, 13)])
        self._month_label = QLabel("Month:")
        layout.addRow(self._month_label, self.month_combo)

        # Restore from existing config
        raw = task.get("recurring") if task else None
        if raw:
            try:
                cfg = json.loads(raw)
                every = cfg.get("every", "day").lower()
                if every == "week":
                    self.every_combo.setCurrentText("Weekly")
                    day_name = cfg.get("day", "monday").title()
                    if day_name in self._WEEKDAYS:
                        self.day_of_week.setCurrentText(day_name)
                elif every == "month":
                    self.every_combo.setCurrentText("Monthly")
                    self.day_of_month.setValue(int(cfg.get("day", 1)))
                elif every == "year":
                    self.every_combo.setCurrentText("Yearly")
                    month = cfg.get("month")
                    if month:
                        self.month_combo.setCurrentIndex(int(month) - 1)
                    if cfg.get("day"):
                        self.day_of_month.setValue(int(cfg.get("day", 1)))
                else:
                    self.every_combo.setCurrentText("Daily")
                self.interval_spin.setValue(int(cfg.get("interval", 1)))
            except (json.JSONDecodeError, TypeError, ValueError):
                pass

        self._on_every_changed(self.every_combo.currentText())

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def _on_every_changed(self, text):
        is_weekly = text == "Weekly"
        is_monthly = text == "Monthly"
        is_yearly = text == "Yearly"
        self.day_of_week.setVisible(is_weekly)
        self._dow_label.setVisible(is_weekly)
        self.day_of_month.setVisible(is_monthly or is_yearly)
        self._dom_label.setVisible(is_monthly or is_yearly)
        self.month_combo.setVisible(is_yearly)
        self._month_label.setVisible(is_yearly)
        if is_yearly:
            self._dom_label.setText("Day:")
        else:
            self._dom_label.setText("Day of month:")

    def get_config(self) -> str:
        """Return recurring config as JSON string."""
        every = self.every_combo.currentText()
        interval = self.interval_spin.value()
        base = {}
        if every == "Daily":
            base = {"every": "day"}
        elif every == "Weekly":
            base = {"every": "week", "day": self.day_of_week.currentText().lower()}
        elif every == "Monthly":
            base = {"every": "month", "day": self.day_of_month.value()}
        elif every == "Yearly":
            base = {
                "every": "year",
                "month": self.month_combo.currentIndex() + 1,
                "day": self.day_of_month.value(),
            }
        if interval > 1:
            base["interval"] = interval
        return json.dumps(base)


from PyQt6.QtWidgets import QDateTimeEdit
from PyQt6.QtCore import QDateTime, QTime


class ReminderDateTimeDialog(QDialog):
    """Dialog to set a one-time reminder datetime."""

    def __init__(self, existing_reminder: str | None = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Set Reminder")
        self.setMinimumWidth(320)
        self.setStyleSheet(_build_dialog_style())
        layout = QVBoxLayout(self)

        # Quick shortcuts
        shortcuts_layout = QHBoxLayout()
        for label, minutes in [
            ("1h", 60),
            ("3h", 180),
            ("Tomorrow 9:00", -1),
            ("Next Monday 9:00", -2),
        ]:
            btn = QPushButton(label)
            btn.clicked.connect(lambda checked, m=minutes: self._apply_shortcut(m))
            shortcuts_layout.addWidget(btn)
        layout.addLayout(shortcuts_layout)

        # DateTime picker
        self.dt_edit = QDateTimeEdit()
        self.dt_edit.setCalendarPopup(True)
        self.dt_edit.setDisplayFormat("dd.MM.yyyy HH:mm")
        now = QDateTime.currentDateTime()
        self.dt_edit.setMinimumDateTime(now)
        if existing_reminder:
            try:
                from datetime import datetime as _dt

                parsed = _dt.fromisoformat(existing_reminder)
                self.dt_edit.setDateTime(
                    QDateTime(
                        QDate(parsed.year, parsed.month, parsed.day),
                        QTime(parsed.hour, parsed.minute),
                    )
                )
            except (ValueError, TypeError):
                self.dt_edit.setDateTime(now.addSecs(3600))
        else:
            self.dt_edit.setDateTime(now.addSecs(3600))
        layout.addWidget(self.dt_edit)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _apply_shortcut(self, minutes):
        now = QDateTime.currentDateTimeUtc()
        if minutes == -1:  # Tomorrow 9:00
            tomorrow = now.addDays(1)
            tomorrow.setTime(QTime(9, 0))
            self.dt_edit.setDateTime(tomorrow)
        elif minutes == -2:  # Next Monday 9:00
            days_until_monday = (8 - now.date().dayOfWeek()) % 7
            if days_until_monday == 0:
                days_until_monday = 7
            monday = now.addDays(days_until_monday)
            monday.setTime(QTime(9, 0))
            self.dt_edit.setDateTime(monday)
        else:
            self.dt_edit.setDateTime(now.addSecs(minutes * 60))

    def get_reminder_at(self) -> str:
        """Return ISO datetime string (UTC)."""
        qdt = self.dt_edit.dateTime()
        return datetime(
            qdt.date().year(),
            qdt.date().month(),
            qdt.date().day(),
            qdt.time().hour(),
            qdt.time().minute(),
            tzinfo=timezone.utc,
        ).isoformat()


class ReminderPopupDialog(QDialog):
    """Always-on-top popup for overdue/critical reminders with snooze."""

    snoozed = pyqtSignal(str, int)  # task_id, minutes
    dismissed = pyqtSignal(str)  # task_id

    def __init__(
        self,
        task_id: str,
        title: str,
        priority: str,
        description: str | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self.task_id = task_id
        self.setWindowTitle("Task Reminder")
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)
        self.setMinimumWidth(350)
        self.setStyleSheet(_build_dialog_style())
        layout = QVBoxLayout(self)

        # Priority badge
        color = PRIORITY_COLORS.get(priority, "#718096")
        badge = QLabel(priority.upper())
        badge.setStyleSheet(
            f"background: {color}; color: white; padding: 2px 8px; "
            f"border-radius: 3px; font-weight: bold; font-size: 11px;"
        )
        layout.addWidget(badge)

        # Title
        title_lbl = QLabel(title)
        title_lbl.setStyleSheet("font-size: 15px; font-weight: bold; padding: 4px 0;")
        title_lbl.setWordWrap(True)
        layout.addWidget(title_lbl)

        # Description preview
        if description:
            desc_lbl = QLabel(
                description[:200] + ("..." if len(description) > 200 else "")
            )
            desc_lbl.setWordWrap(True)
            desc_lbl.setStyleSheet("color: #666; padding: 4px 0;")
            layout.addWidget(desc_lbl)

        # Snooze buttons
        snooze_layout = QHBoxLayout()
        for label, minutes in [
            ("5 min", 5),
            ("15 min", 15),
            ("1 hour", 60),
            ("Tomorrow", 1440),
        ]:
            btn = QPushButton(f"Snooze {label}")
            btn.clicked.connect(lambda checked, m=minutes: self._snooze(m))
            snooze_layout.addWidget(btn)
        layout.addLayout(snooze_layout)

        # Dismiss
        dismiss_btn = QPushButton("Dismiss")
        dismiss_btn.clicked.connect(self._dismiss)
        layout.addWidget(dismiss_btn)

    def _snooze(self, minutes):
        self.snoozed.emit(self.task_id, minutes)
        self.accept()

    def _dismiss(self):
        self.dismissed.emit(self.task_id)
        self.accept()


_REFRESH_INTERVAL_MS = 30_000
_PURGE_INTERVAL_MS = 3_600_000  # 1 hour

# Per-tab sort/filter constants
_FIXED_VIEW_TABS = frozenset({"suggested", "projects"})
_DEFAULT_TAB_VIEW = {
    "sort": "priority",
    "active": {"priority": set(), "due": set(), "project": set()},
    "excluded": {"priority": set(), "due": set(), "project": set()},
}


class FullWindow(QMainWindow):
    """Full task manager window with tabs, search, sort, and suggested view."""

    _bridge_done = pyqtSignal(str)
    _bridge_progress = pyqtSignal(int, str)  # (percent, step_label)
    _enrich_done = pyqtSignal(str)
    _enrich_running = pyqtSignal(str)

    # Sort modes cycle: priority → due → created → priority ...
    _SORT_MODES = ("priority", "due", "created", "project")
    _SORT_LABELS = {
        "priority": "Sort: Priority",
        "due": "Sort: Due Date",
        "created": "Sort: Created",
        "project": "Sort: Project",
    }

    def __init__(self, db, parent=None):
        super().__init__(parent)
        self.db = db
        self._sort_mode = "priority"
        self._search_text = ""
        self._pre_search_tab: int | None = None  # tab to restore after search clears
        self._active_filters = {"priority": set(), "due": set(), "project": set()}
        self._excluded_filters = {"priority": set(), "due": set(), "project": set()}
        self._minus_mode = False
        self._filter_chips = {}
        self._last_projects = None
        self._project_cache_time: float = 0.0  # monotonic time of last project query
        self._filtered_cache: dict[str, list] = {}  # lazy tab rendering cache
        self._search_engine = db.search_engine
        self.setWindowTitle("Task Manager \u2014 SQLite Memory")
        self.resize(800, 600)

        primary = QApplication.primaryScreen()
        if primary:
            screen = primary.availableGeometry()
            self.move(screen.center() - self.rect().center())

        self._settings = QSettings("TaskTray", "FullWindow")
        geometry = self._settings.value("geometry")
        if geometry:
            self.restoreGeometry(geometry)

        # Restore appearance settings
        global _theme_name, _font_size, _bold
        _theme_name = self._settings.value("theme", "blue")
        if _theme_name not in _THEMES:
            _theme_name = "blue"
        _font_size = int(self._settings.value("font_size", 13))
        _bold = self._settings.value("bold", "false") == "true"
        _update_theme_colors()

        # First-run recovery: if QSettings has no tab_views, try bridge profile
        # (deferred — _tab_keys not yet defined; called after tab init below)

        self.setStyleSheet(_build_main_style())

        # Central widget with tabs
        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)

        # Tab order: Suggested, Today, Inbox, Next, Notes, All, Done
        self._tab_keys = [
            "suggested",
            "today",
            "inbox",
            "next",
            "projects",
            "notes",
            "all",
            "done",
        ]
        self._tab_labels = {
            "suggested": "Suggested",
            "today": "Today",
            "inbox": "Inbox",
            "next": "Next",
            "projects": "Projects",
            "notes": "Notes",
            "all": "All",
            "done": "Done",
        }
        self.tab_lists = {}
        for key in self._tab_keys:
            lw = TaskListWidget(self.db)
            lw._search_engine = self._search_engine
            lw.itemChanged.connect(lambda item, k=key: self._on_item_changed(item))
            self.tab_lists[key] = lw
            self.tabs.addTab(lw, self._tab_labels[key])

        # B1: Per-tab view state dict (sort + filters per tab)
        self._tab_views = {
            key: copy.deepcopy(_DEFAULT_TAB_VIEW) for key in self._tab_keys
        }
        self._current_tab_idx = 0  # track for state swapping on tab change

        # B2: Restore per-tab state from QSettings
        parsed = {}
        try:
            raw_views = self._settings.value("tab_views", "{}")
            parsed = json.loads(raw_views) if isinstance(raw_views, str) else {}
            for key, view in parsed.items():
                if key in self._tab_views:
                    if view.get("sort") in self._SORT_MODES:
                        self._tab_views[key]["sort"] = view["sort"]
                    self._tab_views[key]["active"] = {
                        k: set(view.get("active", {}).get(k, []))
                        for k in ("priority", "due", "project")
                    }
                    self._tab_views[key]["excluded"] = {
                        k: set(view.get("excluded", {}).get(k, []))
                        for k in ("priority", "due", "project")
                    }
        except (json.JSONDecodeError, TypeError, ValueError, AttributeError):
            pass

        # Backward compat: if no tab_views saved, load old scalar values as defaults
        if not parsed:
            old_sort = self._settings.value("sort_mode", "priority")
            if old_sort in self._SORT_MODES:
                for v in self._tab_views.values():
                    v["sort"] = old_sort
            try:
                raw = self._settings.value("active_filters", "{}")
                old_af = json.loads(raw) if isinstance(raw, str) else {}
                raw_ex = self._settings.value("excluded_filters", "{}")
                old_ef = json.loads(raw_ex) if isinstance(raw_ex, str) else {}
                for v in self._tab_views.values():
                    v["active"] = {
                        k: set(old_af.get(k, []))
                        for k in ("priority", "due", "project")
                    }
                    v["excluded"] = {
                        k: set(old_ef.get(k, []))
                        for k in ("priority", "due", "project")
                    }
            except (json.JSONDecodeError, TypeError, ValueError):
                pass

        # Set working state from the initial tab
        self._saved_active_tab = int(self._settings.value("active_tab", 0))
        initial_key = self._tab_keys[
            min(self._saved_active_tab, len(self._tab_keys) - 1)
        ]
        if initial_key in self._tab_views:
            v = self._tab_views[initial_key]
            self._sort_mode = v["sort"]
            self._active_filters = v["active"]
            self._excluded_filters = v["excluded"]

        # First-run recovery: if QSettings has no tab_views, try bridge profile
        if self._settings.value("tab_views") is None:
            self._restore_profile_from_bridge()

        # Restore saved active tab
        if hasattr(self, "_saved_active_tab"):
            self.tabs.setCurrentIndex(
                min(self._saved_active_tab, len(self._tab_keys) - 1)
            )
        self._current_tab_idx = self.tabs.currentIndex()
        self.tabs.currentChanged.connect(self._on_tab_changed)

        # Toolbar: actions + search + sort
        toolbar = QToolBar()
        toolbar.setMovable(False)
        add_action = QAction("+ Add Task", self)
        add_action.triggered.connect(self._add_task)
        toolbar.addAction(add_action)
        refresh_action = QAction("Refresh + Sync", self)
        refresh_action.triggered.connect(self._refresh_and_sync)
        toolbar.addAction(refresh_action)
        toolbar.addSeparator()

        # ── Intelligence v2 enrich buttons (amber→orange→red gradient) ──
        for obj_name, label, depth in [
            ("enrich_quick", "\u26a1 Quick", "quick"),
            ("enrich_standard", "\U0001f52c Std", "standard"),
            ("enrich_deep", "\U0001f9e0 Deep", "deep"),
        ]:
            btn = QToolButton()
            btn.setText(label)
            btn.setObjectName(obj_name)
            btn.setToolTip(f"Enrich context: {depth}")
            btn.clicked.connect(lambda checked, d=depth: self._run_enrich(d))
            toolbar.addWidget(btn)
        toolbar.addSeparator()

        # Instant search bar (debounced 300ms)
        self._search_input = QLineEdit()
        self._search_input.setObjectName("search")
        self._search_input.setPlaceholderText("Search tasks...")
        self._search_input.setClearButtonEnabled(True)
        self._search_input.textChanged.connect(self._on_search)
        toolbar.addWidget(self._search_input)
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(300)
        self._search_timer.timeout.connect(self.refresh)

        toolbar.addSeparator()

        # Sort ▾ mega-button
        self._sort_btn = QToolButton()
        self._sort_btn.setText(f"{self._SORT_LABELS[self._sort_mode]} \u25be")
        self._sort_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        sort_menu = QMenu(self)
        self._sort_action_group = QActionGroup(self)
        self._sort_action_group.setExclusive(True)
        self._sort_actions = {}
        for mode in self._SORT_MODES:
            act = QAction(self._SORT_LABELS[mode].replace("Sort: ", ""), self)
            act.setCheckable(True)
            act.setChecked(mode == self._sort_mode)
            act.triggered.connect(lambda checked, m=mode: self._set_sort(m))
            self._sort_action_group.addAction(act)
            sort_menu.addAction(act)
            self._sort_actions[mode] = act
        sort_menu.addSeparator()
        reset_sort_act = QAction("Reset Sort && Filters", self)
        reset_sort_act.triggered.connect(self._reset_sort_filters)
        sort_menu.addAction(reset_sort_act)
        self._sort_btn.setMenu(sort_menu)
        toolbar.addWidget(self._sort_btn)

        toolbar.addSeparator()

        # View ▾ mega-button
        self._view_btn = QToolButton()
        self._view_btn.setText("View \u25be")
        self._view_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        view_menu = QMenu(self)

        # Theme sub-section
        self._theme_action_group = QActionGroup(self)
        self._theme_action_group.setExclusive(True)
        self._theme_actions = {}
        theme_items = [
            ("blue", "\u25c6 Blue (default)"),
            ("black", "\u25fc True Black"),
            ("light", "\u25fb Light"),
        ]
        for name, label in theme_items:
            act = QAction(label, self)
            act.setCheckable(True)
            act.setChecked(name == _theme_name)
            act.triggered.connect(lambda checked, n=name: self._set_theme(n))
            self._theme_action_group.addAction(act)
            view_menu.addAction(act)
            self._theme_actions[name] = act
        view_menu.addSeparator()

        font_down_act = QAction("A\u2212  Smaller Font", self)
        font_down_act.triggered.connect(self._font_down)
        view_menu.addAction(font_down_act)
        font_up_act = QAction("A+  Larger Font", self)
        font_up_act.triggered.connect(self._font_up)
        view_menu.addAction(font_up_act)
        self._bold_action = QAction("Bold", self)
        self._bold_action.setCheckable(True)
        self._bold_action.setChecked(_bold)
        self._bold_action.triggered.connect(self._toggle_bold)
        view_menu.addAction(self._bold_action)
        view_menu.addSeparator()

        # ── Auto-Enrich depth (Intelligence v2) ──
        enrich_header = QAction("Auto-Enrich before Sync:", self)
        enrich_header.setEnabled(False)
        view_menu.addAction(enrich_header)
        self._enrich_depth_group = QActionGroup(self)
        self._enrich_depth_group.setExclusive(True)
        saved_depth = self._settings.value("auto_enrich_depth", "off")
        for depth_val, depth_label in [
            ("off", "Off"),
            ("quick", "\u26a1 Quick"),
            ("standard", "\U0001f52c Standard"),
            ("deep", "\U0001f9e0 Deep"),
        ]:
            act = QAction(depth_label, self)
            act.setCheckable(True)
            act.setChecked(depth_val == saved_depth)
            act.triggered.connect(
                lambda checked, d=depth_val: self._set_enrich_depth(d)
            )
            self._enrich_depth_group.addAction(act)
            view_menu.addAction(act)
        view_menu.addSeparator()

        reset_view_act = QAction("Reset View", self)
        reset_view_act.triggered.connect(self._reset_view)
        view_menu.addAction(reset_view_act)
        self._view_btn.setMenu(view_menu)
        toolbar.addWidget(self._view_btn)

        self.addToolBar(toolbar)

        # Filter chip bar
        self._filter_bar = QToolBar("Filters")
        self._filter_bar.setMovable(False)
        self._filter_bar.setStyleSheet(_build_filter_style())
        self._build_filter_chips()
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, self._filter_bar)

        # Hide sort/filter UI for fixed tabs on initial load
        _initial_key = self._tab_keys[
            min(self._saved_active_tab, len(self._tab_keys) - 1)
        ]
        _is_fixed_initial = _initial_key in _FIXED_VIEW_TABS
        self._sort_btn.setVisible(not _is_fixed_initial)

        # Status bar
        self.status = QStatusBar()
        self.setStatusBar(self.status)

        # Last sync timestamp label (permanent, right corner)
        self._sync_label = QLabel("")
        self._sync_label.setStyleSheet(
            f"color: {_T()['text2']}; font-size: {_font_size - 2}px; padding-right: 8px;"
        )
        self.status.addPermanentWidget(self._sync_label)

        # Bridge sync progress bar (hidden by default, replaces label visually)
        self._sync_bar = QProgressBar()
        self._sync_bar.setFixedWidth(280)
        self._sync_bar.setTextVisible(True)
        self._sync_bar.setRange(0, 100)
        self._sync_bar.hide()
        self.status.addPermanentWidget(self._sync_bar)

        # Bridge sync signals (thread-safe → main thread)
        self._bridge_progress.connect(self._on_sync_progress)
        self._bridge_done.connect(self._on_sync_done)

        # Intelligence v2 enrich signals
        self._enrich_in_progress = False
        self._enrich_running.connect(lambda msg: self.status.showMessage(msg, 30000))
        self._enrich_done.connect(self._on_enrich_done)

        # Auto-refresh every 30s
        self._refresh_timer = QTimer(self)
        self._refresh_timer.timeout.connect(self.refresh)

        # Purge done tasks once at startup, then hourly
        self._last_purged = self.db.purge_old_done(days=30)
        self._purge_timer = QTimer(self)
        self._purge_timer.timeout.connect(self._run_purge)
        self._purge_timer.start(_PURGE_INTERVAL_MS)

        # Auto-sync: watch memory.db for changes
        self._db_watcher = QFileSystemWatcher([str(Path(self.db.db_path))], self)
        self._db_watcher.fileChanged.connect(self._on_db_changed)
        self._auto_sync_timer = QTimer(self)
        self._auto_sync_timer.setSingleShot(True)
        self._auto_sync_timer.setInterval(60_000)  # 60s debounce
        self._auto_sync_timer.timeout.connect(self._auto_sync_triggered)
        self._db_refresh_debounce = QTimer(self)
        self._db_refresh_debounce.setSingleShot(True)
        self._db_refresh_debounce.setInterval(500)  # 500ms UI debounce
        self._db_refresh_debounce.timeout.connect(self.refresh)

        # Process recurring tasks at startup
        self._process_recurring()

        self.refresh()

    def _on_db_changed(self, path):
        """DB file changed — start/restart debounce timers."""
        self._auto_sync_timer.start()  # 60s bridge sync debounce
        self._db_refresh_debounce.start()  # 500ms UI refresh debounce
        # Re-add path (Qt removes watched files after change notification)
        if not self._db_watcher.files():
            self._db_watcher.addPath(path)

    def _auto_sync_triggered(self):
        """Debounce elapsed — run bridge sync."""
        self._sync_bridge()

    def _run_purge(self):
        self._last_purged = self.db.purge_old_done(days=30)

    def _process_recurring(self):
        """Process recurring tasks silently (idempotent)."""
        try:
            from recurring_tasks import process_recurring

            with get_conn() as conn:
                created = process_recurring(conn, dry_run=False)
            if created:
                self.refresh()
        except Exception as exc:
            logging.getLogger("task_tray").warning("recurring: %s", exc)

    # ── Appearance ─────────────────────────────────────────────────────

    def _apply_appearance(self):
        """Rebuild all stylesheets from current theme/font/bold state."""
        _update_theme_colors()
        self.setStyleSheet(_build_main_style())
        self._filter_bar.setStyleSheet(_build_filter_style())
        for lw in self.tab_lists.values():
            lw.setStyleSheet(_build_list_style())
            lw.viewport().update()
        self._settings.setValue("theme", _theme_name)
        self._settings.setValue("font_size", _font_size)
        self._settings.setValue("bold", "true" if _bold else "false")
        self._build_filter_chips()
        self._save_ui_state()
        self.refresh()

    def _save_ui_state(self):
        """Persist all UI state to QSettings (per-tab views)."""
        # Sync working state back to current tab before serializing
        idx = getattr(self, "_current_tab_idx", 0)
        if idx < len(self._tab_keys):
            key = self._tab_keys[idx]
            if key in self._tab_views:
                self._tab_views[key] = {
                    "sort": self._sort_mode,
                    "active": copy.deepcopy(self._active_filters),
                    "excluded": copy.deepcopy(self._excluded_filters),
                }

        # Serialize per-tab views
        serializable = {}
        for key, view in self._tab_views.items():
            serializable[key] = {
                "sort": view["sort"],
                "active": {k: list(v) for k, v in view["active"].items()},
                "excluded": {k: list(v) for k, v in view["excluded"].items()},
            }
        self._settings.setValue("tab_views", json.dumps(serializable))
        self._settings.setValue("active_tab", self.tabs.currentIndex())

    def _restore_profile_from_bridge(self):
        """First-run recovery: load UI state from bridge shared.json profile."""
        shared_path = Path(self._BRIDGE_DIR) / "shared.json"
        if not shared_path.exists():
            return
        try:
            data = json.loads(shared_path.read_text(encoding="utf-8"))
            profiles = data.get("ui_profiles", {})
            profile = profiles.get(_socket.gethostname())
            if not profile:
                return
            global _theme_name, _font_size, _bold
            if profile.get("theme") in _THEMES:
                _theme_name = profile["theme"]
            if (
                isinstance(profile.get("font_size"), int)
                and 10 <= profile["font_size"] <= 20
            ):
                _font_size = profile["font_size"]
            _bold = bool(profile.get("bold", False))
            if isinstance(profile.get("active_tab"), int):
                self._saved_active_tab = profile["active_tab"]

            # Load per-tab views from bridge profile (new format)
            bridge_tab_views = profile.get("tab_views")
            if isinstance(bridge_tab_views, dict):
                for key, view in bridge_tab_views.items():
                    if key in self._tab_views:
                        if view.get("sort") in self._SORT_MODES:
                            self._tab_views[key]["sort"] = view["sort"]
                        self._tab_views[key]["active"] = {
                            k: set(view.get("active", {}).get(k, []))
                            for k in ("priority", "due", "project")
                        }
                        self._tab_views[key]["excluded"] = {
                            k: set(view.get("excluded", {}).get(k, []))
                            for k in ("priority", "due", "project")
                        }
                # Sync working state from current tab
                cur_key = self._tab_keys[
                    min(getattr(self, "_saved_active_tab", 0), len(self._tab_keys) - 1)
                ]
                if cur_key in self._tab_views:
                    v = self._tab_views[cur_key]
                    self._sort_mode = v["sort"]
                    self._active_filters = v["active"]
                    self._excluded_filters = v["excluded"]
            else:
                # Backward compat: old format had flat sort_mode/active_filters
                if profile.get("sort_mode") in self._SORT_MODES:
                    for v in self._tab_views.values():
                        v["sort"] = profile["sort_mode"]
                    self._sort_mode = profile["sort_mode"]
                if isinstance(profile.get("active_filters"), dict):
                    af = {
                        k: set(profile["active_filters"].get(k, []))
                        for k in ("priority", "due", "project")
                    }
                    for v in self._tab_views.values():
                        v["active"] = copy.deepcopy(af)
                    self._active_filters = af
                if isinstance(profile.get("excluded_filters"), dict):
                    ef = {
                        k: set(profile["excluded_filters"].get(k, []))
                        for k in ("priority", "due", "project")
                    }
                    for v in self._tab_views.values():
                        v["excluded"] = copy.deepcopy(ef)
                    self._excluded_filters = ef

            geo_b64 = profile.get("geometry_b64")
            if geo_b64:
                from PyQt6.QtCore import QByteArray

                self.restoreGeometry(QByteArray(base64.b64decode(geo_b64)))
            _update_theme_colors()
            self._save_ui_state()
        except (json.JSONDecodeError, OSError, KeyError, ValueError):
            pass

    def _font_down(self):
        global _font_size
        if _font_size > 10:
            _font_size -= 1
            self._apply_appearance()

    def _font_up(self):
        global _font_size
        if _font_size < 20:
            _font_size += 1
            self._apply_appearance()

    def _toggle_bold(self, checked):
        global _bold
        _bold = checked
        if hasattr(self, "_bold_action"):
            self._bold_action.setChecked(_bold)
        self._apply_appearance()

    def _set_theme(self, name):
        global _theme_name
        _theme_name = name
        if hasattr(self, "_theme_actions") and name in self._theme_actions:
            self._theme_actions[name].setChecked(True)
        self._apply_appearance()

    def _set_enrich_depth(self, depth):
        """Set auto-enrich depth for pre-sync pipeline."""
        self._settings.setValue("auto_enrich_depth", depth)

    def _set_sort(self, mode):
        """Set sort mode from mega-button menu."""
        self._sort_mode = mode
        self._sort_btn.setText(f"{self._SORT_LABELS[mode]} \u25be")
        self._save_ui_state()
        self.refresh()

    def _reset_sort_filters(self):
        """Reset sort to priority and clear all filter chips."""
        self._sort_mode = "priority"
        self._sort_btn.setText(f"{self._SORT_LABELS['priority']} \u25be")
        if hasattr(self, "_sort_actions") and "priority" in self._sort_actions:
            self._sort_actions["priority"].setChecked(True)
        for s in self._active_filters.values():
            s.clear()
        for s in self._excluded_filters.values():
            s.clear()
        if hasattr(self, "_minus_btn"):
            self._minus_btn.setChecked(False)
        self._minus_mode = False
        for btn in self._filter_chips.values():
            btn.setChecked(False)
        self._update_clear_btn()
        self._save_ui_state()
        self.refresh()

    def _reset_view(self):
        """Reset theme=blue, font=13, bold=off."""
        global _theme_name, _font_size, _bold
        _theme_name = "blue"
        _font_size = 13
        _bold = False
        if hasattr(self, "_theme_actions") and "blue" in self._theme_actions:
            self._theme_actions["blue"].setChecked(True)
        if hasattr(self, "_bold_action"):
            self._bold_action.setChecked(False)
        self._apply_appearance()

    # ── Bridge sync ────────────────────────────────────────────────────

    _BRIDGE_DIR = os.path.expanduser("~/.claude/memory/bridge")

    def _refresh_and_sync(self):
        """Refresh task list then sync memory bridge to GitHub."""
        self.refresh()
        self._sync_bridge()

    # Suppress console windows on Windows
    _SP_FLAGS = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

    def _on_sync_progress(self, pct, label):
        self._sync_bar.setValue(pct)
        self._sync_bar.setFormat(f"{pct}%  {label}")
        self._sync_label.hide()
        self._sync_bar.show()

    def _on_sync_done(self, msg):
        is_error = msg.startswith("Sync error")
        self._sync_bar.setValue(100)
        self._sync_bar.setFormat(msg[:50])
        hide_ms = 15000 if is_error else 4000
        QTimer.singleShot(hide_ms, self._show_last_sync_time)
        self.status.showMessage(msg, hide_ms)
        if not is_error:
            from datetime import datetime

            self._last_sync_at = datetime.now()
            self._process_recurring()
            self.refresh()

    def _show_last_sync_time(self):
        self._sync_bar.hide()
        if hasattr(self, "_last_sync_at") and self._last_sync_at:
            if self._last_sync_at.date() == date.today():
                ts = self._last_sync_at.strftime("%H:%M:%S")
            else:
                ts = self._last_sync_at.strftime("%a %d.%m, %H:%M:%S")
            self._sync_label.setText(f"Synced: {ts}")
        self._sync_label.show()

    def _sync_bridge(self):
        """Sync memory bridge (pull + push + shared.json)."""
        if not os.path.isdir(self._BRIDGE_DIR):
            self.status.showMessage("Bridge dir not found", 3000)
            return

        def _run():
            try:
                import bridge_sync_worker

                stats = bridge_sync_worker.main(
                    progress_callback=lambda pct, label: self._bridge_progress.emit(
                        pct, label
                    )
                )

                # Patch UI profile into shared.json (tray-specific, no extra commit)
                self._patch_ui_profile()

                if stats.get("skipped"):
                    self._bridge_done.emit("Already in sync — no changes to push")
                else:
                    n_ent = stats.get("entities", 0)
                    n_tasks = stats.get("tasks", 0)
                    self._bridge_done.emit(f"Synced: {n_ent} entities, {n_tasks} tasks")
            except Exception as exc:
                self._bridge_done.emit(f"Sync error: {exc}")

        threading.Thread(target=_run, daemon=True).start()

    def _run_enrich(self, depth: str = "quick"):
        """Run Intelligence v2 enrich pipeline in background thread."""
        if getattr(self, "_enrich_in_progress", False):
            self.status.showMessage("Enrich already running...", 2000)
            return
        self._enrich_in_progress = True
        self._enrich_running.emit(f"Enriching ({depth})...")

        def _work():
            try:
                from intelligence_v2 import assess_context as _assess
                from claim_graph import extract_candidate_claims as _extract
                from context_packer import build_context_pack as _pack
                from impact_graph import explain_impact as _impact

                with get_conn(self.db.db_path) as conn:
                    # First: assess no_enrich chunks to unlock them
                    pending = conn.execute(
                        "SELECT chunk_id FROM context_chunks "
                        "WHERE state = 'no_enrich' LIMIT 50"
                    ).fetchall()
                    for row in pending:
                        _assess(conn, row["chunk_id"])

                    # Now fetch all enrichable chunks (including freshly unlocked)
                    enrichable = conn.execute(
                        "SELECT chunk_id FROM context_chunks "
                        "WHERE state = 'enrichable' LIMIT 20"
                    ).fetchall()
                    assessed = 0
                    for row in enrichable:
                        _assess(conn, row["chunk_id"])
                        assessed += 1

                    _pack(conn, "executor")

                    claims = 0
                    promoted = 0
                    if depth in ("standard", "deep"):
                        from claim_graph import auto_promote_layer1

                        all_claims: list = []
                        for row in enrichable:
                            cr = _extract(conn, row["chunk_id"])
                            claims += cr.get("claims_extracted", 0)
                            all_claims.extend(cr.get("claims", []))

                        promoted_results = auto_promote_layer1(conn, all_claims)
                        promoted = len(promoted_results)

                    impacts = 0
                    if depth == "deep":
                        recent = conn.execute(
                            "SELECT fact_id FROM canonical_facts "
                            "WHERE updated_at >= datetime('now', '-7 days') "
                            "LIMIT 10"
                        ).fetchall()
                        for f in recent:
                            _impact(conn, "fact", f["fact_id"])
                            impacts += 1

                self._enrich_done.emit(
                    f"Enriched: {assessed} assessed, {claims} claims, "
                    f"{promoted} promoted, {impacts} impacts"
                )
            except Exception as exc:
                self._enrich_done.emit(f"Enrich error: {exc}")

        threading.Thread(target=_work, daemon=True).start()

    def _on_enrich_done(self, msg):
        self._enrich_in_progress = False  # reset on GUI thread via signal
        is_error = msg.startswith("Enrich error")
        self.status.showMessage(msg, 10000 if is_error else 5000)

    def _patch_ui_profile(self):
        """Write own UI profile into shared.json (persisted on next sync cycle)."""
        shared_path = Path(self._BRIDGE_DIR) / "shared.json"
        if not shared_path.exists():
            return
        try:
            data = json.loads(shared_path.read_text(encoding="utf-8"))
            profiles = data.get("ui_profiles", {})

            # Serialize per-tab views for bridge profile
            serializable_views = {}
            for key, view in self._tab_views.items():
                serializable_views[key] = {
                    "sort": view["sort"],
                    "active": {k: list(v) for k, v in view["active"].items()},
                    "excluded": {k: list(v) for k, v in view["excluded"].items()},
                }

            profiles[_socket.gethostname()] = {
                "theme": _theme_name,
                "font_size": _font_size,
                "bold": _bold,
                "active_tab": int(self._settings.value("active_tab", 0)),
                "tab_views": serializable_views,
                "updated_at": now_iso(),
            }
            geo = self._settings.value("geometry")
            if geo:
                profiles[_socket.gethostname()]["geometry_b64"] = base64.b64encode(
                    bytes(geo)
                ).decode("ascii")
            data["ui_profiles"] = profiles
            shared_path.write_text(
                json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        except (json.JSONDecodeError, OSError):
            pass  # non-critical — UI profiles sync on next cycle

    def _import_remote_entities(self, remote_entities, conn=None):
        """Import entities from remote shared.json that don't exist locally."""
        _owned = conn is None
        if _owned:
            conn = sqlite3.connect(self.db.db_path, isolation_level=None, timeout=10)
            conn.row_factory = sqlite3.Row
        conn.execute("BEGIN")
        try:
            for e in remote_entities:
                existing = conn.execute(
                    "SELECT id FROM entities WHERE name = ?", (e["name"],)
                ).fetchone()
                if existing:
                    continue
                now = now_iso()
                eid = conn.execute(
                    "INSERT INTO entities (name, entity_type, project, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        e["name"],
                        e["entityType"],
                        e.get("project") or "shared:bridge",
                        now,
                        now,
                    ),
                ).lastrowid
                for o in e.get("observations", []):
                    conn.execute(
                        "INSERT INTO observations (entity_id, content, created_at) "
                        "VALUES (?, ?, ?)",
                        (eid, o["content"], o.get("createdAt", now)),
                    )
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            if _owned:
                conn.close()

    def _sort_tasks(self, tasks, sort_mode=None):
        """Sort tasks by given sort mode (or current working sort mode)."""
        mode = sort_mode or self._sort_mode
        if mode == "priority":
            return sorted(tasks, key=priority_sort_key)
        if mode == "due":
            return sorted(
                tasks,
                key=lambda t: (
                    0 if t.get("due_date") else 1,
                    t.get("due_date") or "9999-12-31",
                    priority_sort_key(t),
                ),
            )
        if mode == "project":
            return sorted(
                tasks,
                key=lambda t: (
                    t.get("project") or "zzz_none",
                    priority_sort_key(t),
                ),
            )
        # mode == "created"
        return sorted(tasks, key=lambda t: t.get("created_at") or "", reverse=True)

    def _cycle_sort(self):
        """Cycle to next sort mode and refresh."""
        idx = self._SORT_MODES.index(self._sort_mode)
        self._sort_mode = self._SORT_MODES[(idx + 1) % len(self._SORT_MODES)]
        self._sort_btn.setText(f"{self._SORT_LABELS[self._sort_mode]} \u25be")
        if hasattr(self, "_sort_actions") and self._sort_mode in self._sort_actions:
            self._sort_actions[self._sort_mode].setChecked(True)
        self._save_ui_state()
        self.refresh()

    def _on_search(self, text):
        """Debounced search filter (300ms)."""
        new_text = text.strip().lower()
        # Save tab before first search keystroke
        if new_text and not self._search_text:
            self._pre_search_tab = self.tabs.currentIndex()
        # Restore tab when search is cleared
        if not new_text and self._search_text and self._pre_search_tab is not None:
            self.tabs.setCurrentIndex(self._pre_search_tab)
            self._pre_search_tab = None
        self._search_text = new_text
        self._search_timer.start()  # resets 300ms countdown

    def _build_filter_chips(self):
        """Populate the filter bar with priority, due, and project chips."""
        self._filter_bar.clear()
        self._filter_chips.clear()
        t = _T()

        # Minus (exclude) mode toggle
        self._minus_btn = QToolButton()
        self._minus_btn.setText("\u2212")  # Unicode minus sign
        self._minus_btn.setCheckable(True)
        self._minus_btn.setChecked(self._minus_mode)
        self._minus_btn.setToolTip("Exclude mode: click chips to exclude them")
        self._minus_btn.setStyleSheet(
            f"QToolButton {{ font-size: {_font_size}px; font-weight: 900; padding: 2px 6px; "
            f"border: 1px solid {t['border']}; background: {t['bg3']}; color: {t['text2']}; border-radius: 10px; }}"
            f"QToolButton:checked {{ background: {t['danger']}; border-color: {t['danger']}; color: #fff; }}"
        )
        self._minus_btn.toggled.connect(self._on_minus_toggled)
        self._filter_bar.addWidget(self._minus_btn)
        self._filter_bar.addSeparator()

        # Priority chips
        for pri in PRIORITIES:
            btn = QToolButton()
            btn.setText(pri.capitalize())
            btn.setCheckable(True)
            color = PRIORITY_COLORS.get(pri, "#3182ce")
            excluded = pri in self._excluded_filters["priority"]
            btn.setChecked(excluded or pri in self._active_filters["priority"])
            used_color = t["danger"] if excluded else color
            btn.setStyleSheet(
                btn.styleSheet()
                + f"QToolButton:checked {{ background: {used_color}; border-color: {used_color}; color: #fff; }}"
            )
            btn.clicked.connect(
                lambda checked, p=pri: self._toggle_filter("priority", p)
            )
            self._filter_bar.addWidget(btn)
            self._filter_chips[("priority", pri)] = btn

        self._filter_bar.addSeparator()

        # Due chips
        due_chips = [
            ("overdue", "Overdue", t["danger"]),
            ("today", "Today", t["accent"]),
            ("week", "This Week", t["accent"]),
        ]
        for value, label, color in due_chips:
            btn = QToolButton()
            btn.setText(label)
            btn.setCheckable(True)
            excluded = value in self._excluded_filters["due"]
            btn.setChecked(excluded or value in self._active_filters["due"])
            used_color = t["danger"] if excluded else color
            btn.setStyleSheet(
                btn.styleSheet()
                + f"QToolButton:checked {{ background: {used_color}; border-color: {used_color}; color: #fff; }}"
            )
            btn.clicked.connect(lambda checked, v=value: self._toggle_filter("due", v))
            self._filter_bar.addWidget(btn)
            self._filter_chips[("due", value)] = btn

        self._filter_bar.addSeparator()

        # Clear all button (before project chips for quick access)
        self._clear_btn = QToolButton()
        self._clear_btn.setText("Clear")
        self._clear_btn.setStyleSheet(
            f"QToolButton {{ border: 1px solid {t['border']}; background: {t['bg3']}; color: {t['text']}; "
            f"padding: 4px 12px; font-size: {_font_size - 2}px; font-weight: bold; }}"
            f"QToolButton:hover {{ background: {t['danger']}; color: #fff; border-color: {t['danger']}; }}"
            f"QToolButton:disabled {{ color: {t['border']}; }}"
        )
        self._clear_btn.clicked.connect(self._clear_all_filters)
        self._filter_bar.addWidget(self._clear_btn)
        self._update_clear_btn()

        self._filter_bar.addSeparator()

        # Project chips (dynamic, sorted by task count descending)
        projects = self.db.get_project_names()
        for proj in projects:
            btn = QToolButton()
            btn.setText(proj)
            btn.setCheckable(True)
            excluded = proj in self._excluded_filters["project"]
            btn.setChecked(excluded or proj in self._active_filters["project"])
            used_color = t["danger"] if excluded else t["accent"]
            btn.setStyleSheet(
                btn.styleSheet()
                + f"QToolButton:checked {{ background: {used_color}; border-color: {used_color}; color: #fff; }}"
            )
            btn.clicked.connect(
                lambda checked, p=proj: self._toggle_filter("project", p)
            )
            self._filter_bar.addWidget(btn)
            self._filter_chips[("project", proj)] = btn

    def _on_minus_toggled(self, checked):
        """Toggle exclude mode on/off."""
        self._minus_mode = checked

    def _toggle_filter(self, dimension, value):
        """Cycle chip state: off/include/exclude based on minus mode."""
        inc = self._active_filters[dimension]
        exc = self._excluded_filters[dimension]

        if self._minus_mode:
            if value in exc:
                exc.discard(value)  # exclude → off
            else:
                inc.discard(value)  # remove include if any
                exc.add(value)  # → exclude
        else:
            if value in exc:
                exc.discard(value)  # exclude → off (normal click clears)
            elif value in inc:
                inc.discard(value)  # include → off
            else:
                inc.add(value)  # off → include

        # Update chip visual
        chip = self._filter_chips.get((dimension, value))
        if chip:
            is_active = value in inc or value in exc
            chip.setChecked(is_active)
            t = _T()
            if value in exc:
                color = t["danger"]
            elif dimension == "priority":
                color = PRIORITY_COLORS.get(value, "#3182ce")
            elif dimension == "due":
                color = {
                    "overdue": t["danger"],
                    "today": t["accent"],
                    "week": t["accent"],
                }.get(value, t["accent"])
            else:
                color = t["accent"]
            chip.setStyleSheet(
                f"QToolButton:checked {{ background: {color}; border-color: {color}; color: #fff; }}"
            )

        self._update_clear_btn()
        self._save_ui_state()
        self.refresh()

    def _clear_all_filters(self):
        """Remove all active and excluded chip filters."""
        for s in self._active_filters.values():
            s.clear()
        for s in self._excluded_filters.values():
            s.clear()
        for btn in self._filter_chips.values():
            btn.setChecked(False)
        if hasattr(self, "_minus_btn"):
            self._minus_btn.setChecked(False)
        self._minus_mode = False
        self._update_clear_btn()
        self._save_ui_state()
        self.refresh()

    def _update_clear_btn(self):
        """Dim the Clear button when no filters are active."""
        active = any(self._active_filters.values()) or any(
            self._excluded_filters.values()
        )
        if hasattr(self, "_clear_btn"):
            self._clear_btn.setEnabled(active)

    @staticmethod
    def _matches_due_filter(task, due_filters, today, week_start, week_end):
        """Check if task matches any active due filter (OR within)."""
        due = parse_iso_date(task.get("due_date"))
        section = task.get("section")
        for f in due_filters:
            # "today" matches by due_date OR by section (user intent = "work today")
            if f == "today" and (due == today or section == "today"):
                return True
            # "week" matches by due_date range OR section=today (today ⊂ this week)
            if f == "week" and (
                section == "today"
                or (due is not None and week_start <= due <= week_end)
            ):
                return True
            if due is None:
                continue
            if f == "overdue" and due < today:
                return True
        return False

    def _filter(self, tasks, active_filters=None, excluded_filters=None):
        """Apply chip filters. Accepts explicit filter dicts or falls back to working state."""
        q = self._search_text
        if q:
            # SmartKey fuzzy search (falls back to substring if unavailable)
            return self._search_engine.search(
                q, tasks, conn=self.db._conn, use_vector=False
            )

        af = active_filters if active_filters is not None else self._active_filters
        ef = (
            excluded_filters if excluded_filters is not None else self._excluded_filters
        )

        # ── Include filters (AND between dims, OR within dim) ──
        if af["priority"]:
            tasks = [t for t in tasks if t.get("priority", "medium") in af["priority"]]

        due_inc = af["due"]
        due_exc = ef["due"]
        if due_inc or due_exc:
            today = date.today()
            week_start = today - timedelta(days=today.weekday())
            week_end = week_start + timedelta(days=6)
            if due_inc:
                tasks = [
                    t
                    for t in tasks
                    if self._matches_due_filter(t, due_inc, today, week_start, week_end)
                ]
            if due_exc:
                tasks = [
                    t
                    for t in tasks
                    if not self._matches_due_filter(
                        t, due_exc, today, week_start, week_end
                    )
                ]

        if af["project"]:
            tasks = [t for t in tasks if t.get("project") in af["project"]]

        # ── Exclude filters (remove matching) ──
        if ef["priority"]:
            tasks = [
                t for t in tasks if t.get("priority", "medium") not in ef["priority"]
            ]

        if ef["project"]:
            tasks = [t for t in tasks if t.get("project") not in ef["project"]]

        return tasks

    def refresh(self):
        if getattr(self, "_refreshing", False):
            return
        self._refreshing = True
        try:
            self._do_refresh()
        finally:
            self._refreshing = False

    def _do_refresh(self):
        # Auto-promote tasks whose due date has arrived
        self.db.promote_due_today()

        # Rebuild project chips (cached 60s)
        now_mono = time.monotonic()
        if now_mono - self._project_cache_time >= 60:
            self._project_cache_time = now_mono
            projects = self.db.get_project_names()
            if self._last_projects != projects:
                self._last_projects = projects
                self._build_filter_chips()

        # 2 DB queries instead of 4: derive suggested & notes in Python
        all_active = self.db.get_all_active()
        done = self.db.get_done_tasks()

        # Rebuild SmartKey search index (skips if fingerprint unchanged)
        self._search_engine.rebuild_index(all_active + done)

        suggested = sorted(all_active, key=_suggested_sort_key)[:20]
        notes = [t for t in all_active if t.get("type") == "note"] + [
            t for t in done if t.get("type") == "note"
        ]

        raw = {
            "suggested": suggested,
            "today": [t for t in all_active if t.get("section") == "today"],
            "inbox": [t for t in all_active if t.get("section") == "inbox"],
            "next": [t for t in all_active if t.get("section") == "next"],
            "notes": notes,
            "projects": all_active,
            "all": all_active + done,
            "done": done,
        }

        # Pre-compute filtered+sorted data for all tabs (cheap Python ops)
        self._filtered_cache = {}
        if self._search_text:
            # Global search: search ALL tasks, then distribute into tabs
            all_tasks = all_active + done
            global_results = self._search_engine.search(
                self._search_text, all_tasks, conn=self.db._conn, use_vector=False
            )
            global_ids = {t["id"] for t in global_results}
            for key in self._tab_keys:
                if key == "suggested":
                    # Use full search results, not the pre-limited top-20 list
                    source = global_results
                else:
                    source = [t for t in raw[key] if t["id"] in global_ids]
                if key in self._tab_views:
                    v = self._tab_views[key]
                    self._filtered_cache[key] = self._sort_tasks(source, v["sort"])
                else:
                    self._filtered_cache[key] = self._sort_tasks(source)
        else:
            for key in self._tab_keys:
                if key in self._tab_views:
                    v = self._tab_views[key]
                    filtered = self._filter(raw[key], v["active"], v["excluded"])
                    self._filtered_cache[key] = self._sort_tasks(filtered, v["sort"])
                else:
                    self._filtered_cache[key] = self._sort_tasks(self._filter(raw[key]))

        # Update tab visibility (suggested, notes, projects always visible)
        always_visible = ("suggested", "today", "notes", "projects")
        for i, key in enumerate(self._tab_keys):
            count = len(self._filtered_cache[key])
            self.tabs.setTabVisible(i, count > 0 or key in always_visible)

        # Auto-switch to first tab with results when searching
        if self._search_text:
            cur = self.tabs.currentIndex()
            current_key = self._tab_keys[cur] if cur < len(self._tab_keys) else ""
            if not self._filtered_cache.get(current_key):
                for i, key in enumerate(self._tab_keys):
                    if self._filtered_cache.get(key):
                        self.tabs.setCurrentIndex(i)
                        break

        # Actual current index AFTER all tab visibility and search changes
        current_idx = self.tabs.currentIndex()

        # Lazy rendering: only load the currently active tab
        if 0 <= current_idx < len(self._tab_keys):
            self._load_tab(self._tab_keys[current_idx])

        # Status bar — derive summary from already-fetched data
        s = self.db.get_summary(all_active)
        task_count = sum(1 for t in all_active if t.get("type", "task") == "task")
        note_count = len(notes)
        done_count = len(done)
        msg = f"Tasks: {task_count} | Notes: {note_count} | Done: {done_count} | Overdue: {s['overdue']}"
        if self._search_text:
            msg += f" | Filter: '{self._search_text}'"
        inc_count = sum(len(v) for v in self._active_filters.values())
        exc_count = sum(len(v) for v in self._excluded_filters.values())
        if inc_count or exc_count:
            parts = []
            if inc_count:
                parts.append(f"{inc_count} include")
            if exc_count:
                parts.append(f"{exc_count} exclude")
            msg += f" | Filters: {', '.join(parts)}"
        self.status.showMessage(msg)

    def _on_tab_changed(self, idx):
        """Handle tab switch: save current tab's view, load new tab's view."""
        # Save outgoing tab's state
        old_idx = getattr(self, "_current_tab_idx", 0)
        if old_idx < len(self._tab_keys):
            old_key = self._tab_keys[old_idx]
            if old_key in self._tab_views:
                self._tab_views[old_key] = {
                    "sort": self._sort_mode,
                    "active": copy.deepcopy(self._active_filters),
                    "excluded": copy.deepcopy(self._excluded_filters),
                }

        # Load incoming tab's state
        self._current_tab_idx = idx
        if idx < len(self._tab_keys):
            new_key = self._tab_keys[idx]
            if new_key in self._tab_views:
                v = self._tab_views[new_key]
                self._sort_mode = v["sort"]
                self._active_filters = copy.deepcopy(v["active"])
                self._excluded_filters = copy.deepcopy(v["excluded"])
            else:
                self._sort_mode = "priority"
                self._active_filters = {
                    "priority": set(),
                    "due": set(),
                    "project": set(),
                }
                self._excluded_filters = {
                    "priority": set(),
                    "due": set(),
                    "project": set(),
                }

            # Update sort button
            self._sort_btn.setText(f"{self._SORT_LABELS[self._sort_mode]} \u25be")
            if hasattr(self, "_sort_actions") and self._sort_mode in self._sort_actions:
                self._sort_actions[self._sort_mode].setChecked(True)

            # Rebuild filter chips for new tab
            self._build_filter_chips()

            # Hide sort/filter UI for fixed tabs
            is_fixed = new_key in _FIXED_VIEW_TABS
            self._sort_btn.setVisible(not is_fixed)

        self._save_ui_state()
        if idx < len(self._tab_keys):
            self._load_tab(self._tab_keys[idx])

    def _load_tab(self, key):
        """Render a single tab from cached data. Caps All/Done at 200 items."""
        tasks = self._filtered_cache.get(key)
        if tasks is None:
            return
        cap_msg = ""
        if key in ("all", "done") and len(tasks) > _TAB_PAGE_SIZE:
            cap_msg = f"── {len(tasks) - _TAB_PAGE_SIZE} more items... ──"
            tasks = tasks[:_TAB_PAGE_SIZE]

        lw = self.tab_lists[key]
        if key == "suggested":
            lw.load_smart_grouped(tasks)
        elif key == "projects":
            proj_sorted = sorted(tasks, key=lambda t: t.get("project") or "zzz_none")
            lw.load_grouped_by_project(proj_sorted)
        else:
            lw.load_tasks(tasks)

        if cap_msg:
            sentinel = QListWidgetItem(cap_msg)
            sentinel.setFlags(Qt.ItemFlag.NoItemFlags)
            sentinel.setForeground(QColor("#888"))
            lw.addItem(sentinel)

    def _on_item_changed(self, item):
        task_id = item.data(Qt.ItemDataRole.UserRole)
        if not task_id:
            return
        checked = item.checkState() == Qt.CheckState.Checked
        # Defer DB write out of signal handler — immediate clear() during
        # itemChanged dispatch deletes the C++ QListWidgetItem, causing segfault.
        QTimer.singleShot(10, lambda: self._apply_check_change(task_id, checked))

    def _apply_check_change(self, task_id, checked):
        try:
            if checked:
                self.db.mark_done(task_id)
            else:
                self.db.update_task(task_id, status="not_started")
            # on_change() triggers _refresh_all → full_window.refresh(),
            # but refresh explicitly as safety net (re-entrancy guard makes it cheap).
            self.refresh()
        except Exception:
            import traceback

            err = traceback.format_exc()
            logging.getLogger("task_tray").error(
                "Error toggling task %s: %s", task_id, err
            )
            # DB write failed — revert checkbox visual state + notify user
            self._revert_checkbox(task_id, checked)
            self.status.showMessage(
                f"DB error — task not saved. {err.splitlines()[-1]}", 8000
            )

    def _revert_checkbox(self, task_id, was_checked):
        """Revert checkbox to opposite state after a failed DB write."""
        revert_to = Qt.CheckState.Unchecked if was_checked else Qt.CheckState.Checked
        for lw in self.tab_lists.values():
            lw.blockSignals(True)
            for i in range(lw.count()):
                item = lw.item(i)
                if item and item.data(Qt.ItemDataRole.UserRole) == task_id:
                    item.setCheckState(revert_to)
            lw.blockSignals(False)

    def _add_task(self):
        task = {"title": "", "section": "inbox", "priority": "medium"}
        dlg = EditTaskDialog(task, self, db=self.db)
        dlg.setWindowTitle("Add Task")
        if dlg.exec() == QDialog.DialogCode.Accepted:
            vals = dlg.get_values()
            title = vals.pop("title", "")
            if title:
                self.db.add_task(title, **vals)
                self.refresh()

    def showEvent(self, event):
        super().showEvent(event)
        self._refresh_timer.start(_REFRESH_INTERVAL_MS)
        self._purge_timer.start(_PURGE_INTERVAL_MS)
        self._auto_sync_timer.start()
        self.refresh()

    def closeEvent(self, event):
        self._settings.setValue("geometry", self.saveGeometry())
        self._save_ui_state()
        self._search_engine.save()
        self._refresh_timer.stop()
        self._purge_timer.stop()
        self._auto_sync_timer.stop()
        self._db_refresh_debounce.stop()
        self._search_timer.stop()
        event.ignore()
        self.hide()


# ── App Controller ──────────────────────────────────────────────────


class TaskTrayApp:
    """Main application controller."""

    def __init__(self, instance_socket=None):
        self.app = QApplication(sys.argv)
        self.app.setQuitOnLastWindowClosed(False)
        self.db = TaskDB()
        self.db.on_change = self._refresh_all
        self.app.aboutToQuit.connect(self._on_quit)

        # Tray icon
        self.tray = QSystemTrayIcon()
        self._update_icon()
        self.tray.setToolTip(self._tooltip())
        self.tray.activated.connect(self._on_tray_activated)

        # Context menu
        menu = QMenu()
        open_action = QAction("Open Full Window", menu)
        open_action.triggered.connect(self._open_full)
        menu.addAction(open_action)
        add_task_action = QAction("Add Task", menu)
        add_task_action.triggered.connect(self._quick_add_from_tray)
        menu.addAction(add_task_action)
        menu.addSeparator()
        quit_action = QAction("Quit", menu)
        quit_action.triggered.connect(self.app.quit)
        menu.addAction(quit_action)
        self.tray.setContextMenu(menu)

        self.tray.show()
        self.popup = None
        self.full_window = None

        # Reminder timer — check every 60s for due reminders
        self._reminder_timer = QTimer(self.app)
        self._reminder_timer.timeout.connect(self._check_reminders)
        self._reminder_timer.start(60_000)
        self._shown_reminder_ids: dict[str, float] = {}  # task_id → monotonic ts
        self._active_reminder_dlgs: list = []  # keep dialogs alive until closed
        QTimer.singleShot(5000, self._check_reminders)  # initial check after startup

        # Single-instance socket polling
        self._instance_socket = instance_socket
        if instance_socket:
            self._instance_timer = QTimer(self.app)
            self._instance_timer.timeout.connect(self._poll_instance_socket)
            self._instance_timer.start(500)

    def _update_icon(self, summary=None):
        if summary is None:
            summary = self.db.get_summary()
        pm = create_tray_icon_pixmap(summary["overdue"])
        self.tray.setIcon(QIcon(pm))

    def _tooltip(self, summary=None):
        if summary is None:
            summary = self.db.get_summary()
        return f"Tasks: {summary['total']} | Overdue: {summary['overdue']}"

    def _on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self._toggle_popup()

    def _toggle_popup(self):
        if self.popup and self.popup.isVisible():
            self.popup.hide()
            return
        if not self.popup:
            self.popup = TrayPopup(self.db, self._open_full)
        geo = self.tray.geometry()
        self.popup.show_near_tray(geo)

    def _quick_add_from_tray(self):
        task = {"title": "", "section": "today", "priority": "medium"}
        dlg = EditTaskDialog(task, db=self.db)
        dlg.setWindowTitle("Add Task")
        if dlg.exec() == QDialog.DialogCode.Accepted:
            vals = dlg.get_values()
            title = vals.pop("title", "")
            if title:
                self.db.add_task(title, **vals)

    def _open_full(self):
        if self.popup:
            self.popup.hide()
        if not self.full_window:
            self.full_window = FullWindow(self.db)
        self.full_window.show()
        self.full_window.raise_()
        self.full_window.activateWindow()

    def _poll_instance_socket(self):
        """Check if another instance sent a SHOW command."""
        if not self._instance_socket:
            return
        try:
            conn, _ = self._instance_socket.accept()
            data = conn.recv(64)
            conn.close()
            if data == b"SHOW":
                self._open_full()
        except BlockingIOError:
            pass
        except OSError:
            pass

    def _refresh_all(self):
        """Update tray icon badge + tooltip after any change."""
        summary = self.db.get_summary()
        self._update_icon(summary)
        self.tray.setToolTip(self._tooltip(summary))
        if self.popup and self.popup.isVisible():
            self.popup.refresh()
        if self.full_window and self.full_window.isVisible():
            self.full_window.refresh()

    def _check_reminders(self):
        """Check for tasks with due reminders and show notifications."""
        # Auto-cleanup: evict entries older than 5 min so reminders can re-fire
        cutoff = time.monotonic() - 300
        self._shown_reminder_ids = {
            k: v for k, v in self._shown_reminder_ids.items() if v > cutoff
        }

        try:
            now_str = datetime.now(timezone.utc).isoformat()
            conn = sqlite3.connect(self.db.db_path, isolation_level=None, timeout=5)
            try:
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA busy_timeout=5000")
                rows = conn.execute(
                    "SELECT id, title, description, priority, reminder_at FROM tasks "
                    "WHERE reminder_at IS NOT NULL AND reminder_at <= ? "
                    "AND status NOT IN ('done', 'archived', 'cancelled')",
                    (now_str,),
                ).fetchall()
            finally:
                conn.close()
        except Exception as exc:
            logging.getLogger("task_tray").warning("reminder check: %s", exc)
            return

        for row in rows:
            tid = row["id"]
            if tid in self._shown_reminder_ids:
                continue
            self._shown_reminder_ids[tid] = time.monotonic()

            # Critical priority or >1h overdue → popup dialog
            is_critical = row["priority"] == "critical"
            try:
                reminder_dt = datetime.fromisoformat(row["reminder_at"])
                overdue_minutes = (
                    datetime.now(timezone.utc) - reminder_dt
                ).total_seconds() / 60
                is_very_overdue = overdue_minutes > 60
            except (ValueError, TypeError):
                is_very_overdue = False

            if is_critical or is_very_overdue:
                dlg = ReminderPopupDialog(
                    tid,
                    row["title"],
                    row["priority"],
                    row["description"],
                )
                dlg.snoozed.connect(self._snooze_reminder)
                dlg.dismissed.connect(self._dismiss_reminder)
                self._active_reminder_dlgs.append(dlg)
                dlg.finished.connect(
                    lambda _, d=dlg: (
                        self._active_reminder_dlgs.remove(d)
                        if d in self._active_reminder_dlgs
                        else None
                    )
                )
                dlg.show()
            else:
                self.tray.showMessage(
                    "Task Reminder",
                    row["title"],
                    QSystemTrayIcon.MessageIcon.Information,
                    10_000,
                )

    def _snooze_reminder(self, task_id: str, minutes: int):
        """Reschedule reminder to NOW + minutes."""
        new_time = datetime.now(timezone.utc) + timedelta(minutes=minutes)
        self.db.update_task(task_id, reminder_at=new_time.isoformat())
        self._shown_reminder_ids.pop(task_id, None)

    def _dismiss_reminder(self, task_id: str):
        """Clear the reminder."""
        self.db.update_task(task_id, reminder_at=None)
        self._shown_reminder_ids.pop(task_id, None)

    def _on_quit(self):
        self._reminder_timer.stop()
        if self._instance_socket:
            self._instance_socket.close()
        self.db.close()

    def run(self):
        return self.app.exec()


_INSTANCE_TCP_PORT = 47831  # localhost-only, for Windows (no AF_UNIX)
_USE_UNIX = hasattr(_socket, "AF_UNIX")


def _try_single_instance():
    """If another instance is running, send SHOW signal and return None.
    Otherwise, bind the socket and return the server socket."""
    if _USE_UNIX:
        return _try_single_instance_unix()
    return _try_single_instance_tcp()


def _try_single_instance_unix():
    """AF_UNIX variant (Linux): abstract namespace, zero cleanup."""
    addr = "\0TaskTray_SingleInstance"
    try:
        client = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        client.connect(addr)
        client.send(b"SHOW")
        client.close()
        return None
    except (ConnectionRefusedError, FileNotFoundError, OSError):
        pass
    srv = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    try:
        srv.bind(addr)
        srv.listen(1)
        srv.setblocking(False)
        return srv
    except OSError:
        srv.close()
        raise


def _try_single_instance_tcp():
    """TCP localhost variant (Windows): no AF_UNIX available."""
    addr = ("127.0.0.1", _INSTANCE_TCP_PORT)
    try:
        client = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        client.connect(addr)
        client.send(b"SHOW")
        client.close()
        return None
    except (ConnectionRefusedError, OSError):
        pass
    srv = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    srv.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
    try:
        srv.bind(addr)
        srv.listen(1)
        srv.setblocking(False)
        return srv
    except OSError:
        srv.close()
        raise


def main():
    srv = _try_single_instance()
    if srv is None:
        sys.exit(0)
    app = TaskTrayApp(instance_socket=srv)
    if "--show" in sys.argv:
        app._open_full()
    sys.exit(app.run())


if __name__ == "__main__":
    main()
