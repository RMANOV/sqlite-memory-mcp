"""Task Tray — SQLite Task Manager.

System tray widget with dual mode: compact popup + full window.
Reads/writes directly to ~/.claude/memory/memory.db.
"""

import base64
import html as _html
import json
import os
import socket
import sqlite3
import subprocess
import threading
import uuid
import calendar as _cal_mod
import time
from datetime import date, datetime, timedelta, timezone

from task_search import TaskSearchEngine

from db_utils import (
    DB_PATH,
    PRIORITY_COLORS,
    TASK_ACTIVE_EXCLUSIONS,
    TASK_ALLOWED_UPDATE_FIELDS as ALLOWED_FIELDS,
    TASK_PRIORITIES,
    TASK_SECTIONS as SECTIONS,
    build_priority_order_sql,
    is_overdue,
    now_iso,
    parse_iso_date,
    priority_sort_key,
)

PRIORITIES = tuple(reversed(TASK_PRIORITIES))  # descending for UI display

# Upper-case priority colors for UI lookups
_PRIORITY_COLORS_UPPER = {k.upper(): v for k, v in PRIORITY_COLORS.items()}

# SQL fragment for active-task exclusion (reused across queries)
_ACTIVE_PH = ",".join("?" for _ in TASK_ACTIVE_EXCLUSIONS)
_ACTIVE_PARAMS = list(TASK_ACTIVE_EXCLUSIONS)

# Columns needed by UI rendering (excludes parent_id, notes, assignee, shared_by, publish_requested_at)
_UI_COLS = "id, title, description, status, section, priority, due_date, project, type, recurring, visibility, updated_at, created_at"

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
        self._conn.execute("PRAGMA busy_timeout=10000")
        self._ensure_table()

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

    def close(self):
        self._conn.close()

    def promote_due_today(self):
        """Auto-move tasks with due_date <= today from inbox/next to today."""
        cur = self._conn.execute(
            "UPDATE tasks SET section = 'today' "
            "WHERE due_date <= date('now') AND section IN ('inbox', 'next') "
            "AND status <> 'done' AND type = 'task'"
        )
        if cur.rowcount:
            self._conn.commit()
        return cur.rowcount

    def get_all_active(self):
        """Return all active tasks (excludes done, archived, cancelled)."""
        rows = self._conn.execute(
            f"SELECT {_UI_COLS} FROM tasks WHERE status NOT IN ({_ACTIVE_PH}) "
            "ORDER BY created_at",
            _ACTIVE_PARAMS,
        ).fetchall()
        return [dict(r) for r in rows]

    def get_done_tasks(self):
        """Return completed tasks, newest first."""
        rows = self._conn.execute(
            f"SELECT {_UI_COLS} FROM tasks WHERE status = 'done' ORDER BY updated_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def purge_old_done(self, days=30):
        """Delete done tasks older than `days` days. Returns count deleted."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        cur = self._conn.execute(
            "DELETE FROM tasks WHERE status = 'done' AND type = 'task' AND updated_at < ?",
            (cutoff,),
        )
        if cur.rowcount:
            self._conn.commit()
        return cur.rowcount

    def get_suggested_tasks(self, limit=20):
        """Return prioritized mix: overdue + high/critical + nearest due."""
        pri_sql = build_priority_order_sql()
        rows = self._conn.execute(
            f"SELECT * FROM tasks WHERE status NOT IN ({_ACTIVE_PH}) "
            "ORDER BY "
            "CASE WHEN due_date IS NOT NULL AND due_date < date('now') THEN 0 ELSE 1 END, "
            f"{pri_sql}, "
            "CASE WHEN due_date IS NULL THEN 1 ELSE 0 END, due_date, "
            "created_at DESC "
            "LIMIT ?",
            _ACTIVE_PARAMS + [limit],
        ).fetchall()
        return [dict(r) for r in rows]

    def get_all_notes(self):
        """All notes (never-deleted). Excludes archived/cancelled."""
        pri_sql = build_priority_order_sql()
        rows = self._conn.execute(
            "SELECT * FROM tasks WHERE type = 'note' "
            "AND status NOT IN ('archived', 'cancelled') "
            f"ORDER BY {pri_sql}, updated_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_project_names(self):
        """Return project names sorted by active task count (most first)."""
        rows = self._conn.execute(
            "SELECT project, COUNT(*) as cnt FROM tasks "
            "WHERE project IS NOT NULL AND status NOT IN ('archived','cancelled') "
            "GROUP BY project ORDER BY cnt DESC"
        ).fetchall()
        return [r["project"] for r in rows]

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
        self._conn.execute(
            "INSERT INTO tasks (id, title, description, status, section, priority, "
            "due_date, project, type, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                task_id,
                title,
                description,
                status,
                section,
                priority,
                due_date,
                project,
                type,
                now,
                now,
            ),
        )
        self._conn.commit()
        if self.on_change:
            self.on_change()
        return task_id

    def mark_done(self, task_id):
        """Set status=done."""
        now = now_iso()
        self._conn.execute(
            "UPDATE tasks SET status='done', updated_at=? WHERE id=?",
            (now, task_id),
        )
        self._conn.commit()
        if self.on_change:
            self.on_change()

    def update_task(self, task_id, **fields):
        """Update arbitrary fields on a task."""
        if not fields:
            return
        invalid = set(fields) - ALLOWED_FIELDS
        if invalid:
            raise ValueError(f"Unknown task fields: {invalid}")
        fields["updated_at"] = now_iso()
        sets = ", ".join(f"{k}=?" for k in fields)
        vals = list(fields.values()) + [task_id]
        self._conn.execute(f"UPDATE tasks SET {sets} WHERE id=?", vals)
        self._conn.commit()
        if self.on_change:
            self.on_change()

    def delete_task(self, task_id):
        """Hard delete a task."""
        self._conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))
        self._conn.commit()
        if self.on_change:
            self.on_change()


# ── UI Layer ────────────────────────────────────────────────────────

import sys
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
        "overdue_bg": "#3b1c1c",
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
        "overdue_bg": "#fff5f5",
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
    if every == "day":
        return "Daily"
    if every == "week":
        return f"Weekly ({cfg.get('day', '?').title()})"
    if every == "month":
        return f"Monthly (day {cfg.get('day', '?')})"
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
        item.setBackground(_CLR_OVERDUE_BG)
        item.setForeground(_CLR_OVERDUE_FG)


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
        self.setFixedWidth(380)
        self.setMaximumHeight(500)
        self.setStyleSheet(self._stylesheet())
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
        self._search_input.setPlaceholderText("Search tasks...")
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

        tasks = self.db.get_suggested_tasks(limit=8)

        # Apply search filter if active
        q = self._search_text
        if q:
            scored = [(t, score_task(t, q)) for t in tasks]
            tasks = [t for t, s in sorted(scored, key=lambda x: -x[1]) if s > 0]

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

        cb = QCheckBox(task["title"])
        cb.setChecked(task["status"] == "done")
        if task["status"] == "done":
            cb.setStyleSheet(f"color: {_T()['done']}; text-decoration: line-through;")
        task_id = task["id"]
        cb.toggled.connect(lambda checked, tid=task_id: self._on_toggle(tid, checked))
        hl.addWidget(cb, 1)

        priority = (task.get("priority") or "medium").upper()
        plbl = QLabel(priority)
        plbl.setObjectName("priority")
        plbl.setStyleSheet(f"color: {_PRIORITY_COLORS_UPPER.get(priority, '#718096')};")
        hl.addWidget(plbl)

        desc = task.get("description")
        if desc:
            row.setToolTip(desc)

        return row

    def _on_toggle(self, task_id, checked):
        if checked:
            self.db.mark_done(task_id)
        else:
            self.db.update_task(task_id, status="not_started")
        QTimer.singleShot(300, self.refresh)

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
        task_id = self.db.add_task(title, type=task_type, **kwargs)
        if desc:
            self.db.update_task(task_id, description=desc)
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
        """Auto-adjust due date when section changes."""
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
        return vals


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
            body_html = "".join(
                f"<p>{p.replace(chr(10), '<br>')}</p>" for p in paragraphs
            )
            self._body_label.setText(
                f'<div style="font-family: Segoe UI; font-size: 13px; '
                f'line-height: 160%; color: #e2e8f0;">{body_html}</div>'
            )
        else:
            self._body_label.setText(
                '<div style="font-family: Segoe UI; font-size: 13px; '
                'color: #4a5568; font-style: italic;">No description</div>'
            )

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
        self.setStyleSheet(_build_list_style())
        self.itemDoubleClicked.connect(self._on_double_click)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._context_menu)
        self._tasks = []
        self._last_fp = None  # fingerprint for skip-if-unchanged

    @staticmethod
    def _build_tooltip(task):
        parts = []
        rl = _recurring_label(task.get("recurring"))
        if rl:
            parts.append(f"\U0001f504 {rl}")
        desc = task.get("description")
        if desc:
            parts.append(desc)
        return "\n".join(parts) if parts else None

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
        self._open_reader(item.data(Qt.ItemDataRole.UserRole))

    def _context_menu(self, pos):
        item = self.itemAt(pos)
        if not item:
            return
        task_id = item.data(Qt.ItemDataRole.UserRole)
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
        elif publish_action and action == publish_action:
            self._publish_task(task_id)
        elif unpublish_action and action == unpublish_action:
            self._cancel_publish_task(task_id)
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
        self.every_combo.addItems(["Daily", "Weekly", "Monthly"])
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
                else:
                    self.every_combo.setCurrentText("Daily")
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
        self.day_of_week.setVisible(is_weekly)
        self._dow_label.setVisible(is_weekly)
        self.day_of_month.setVisible(is_monthly)
        self._dom_label.setVisible(is_monthly)

    def get_config(self) -> str:
        """Return recurring config as JSON string."""
        every = self.every_combo.currentText()
        if every == "Daily":
            return json.dumps({"every": "day"})
        elif every == "Weekly":
            return json.dumps(
                {"every": "week", "day": self.day_of_week.currentText().lower()}
            )
        else:
            return json.dumps({"every": "month", "day": self.day_of_month.value()})


_REFRESH_INTERVAL_MS = 30_000
_PURGE_INTERVAL_MS = 3_600_000  # 1 hour


class FullWindow(QMainWindow):
    """Full task manager window with tabs, search, sort, and suggested view."""

    _bridge_done = pyqtSignal(str)
    _bridge_progress = pyqtSignal(int, str)  # (percent, step_label)

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
        self._search_engine = TaskSearchEngine()
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

        # Restore sort/tab/filter state from QSettings
        self._sort_mode = self._settings.value("sort_mode", "priority")
        if self._sort_mode not in self._SORT_MODES:
            self._sort_mode = "priority"
        self._saved_active_tab = int(self._settings.value("active_tab", 0))
        try:
            raw = self._settings.value("active_filters", "{}")
            parsed = json.loads(raw) if isinstance(raw, str) else {}
            self._active_filters = {
                k: set(parsed.get(k, [])) for k in ("priority", "due", "project")
            }
            raw_ex = self._settings.value("excluded_filters", "{}")
            parsed_ex = json.loads(raw_ex) if isinstance(raw_ex, str) else {}
            self._excluded_filters = {
                k: set(parsed_ex.get(k, [])) for k in ("priority", "due", "project")
            }
        except (json.JSONDecodeError, TypeError, ValueError):
            pass  # defaults already set above

        # First-run recovery: if QSettings has no sort_mode, try bridge profile
        if self._settings.value("sort_mode") is None:
            self._restore_profile_from_bridge()

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

        # Restore saved active tab
        if hasattr(self, "_saved_active_tab"):
            self.tabs.setCurrentIndex(
                min(self._saved_active_tab, len(self._tab_keys) - 1)
            )
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
        except Exception:
            pass  # silent — never break startup/sync

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
        """Persist all UI state to QSettings."""
        self._settings.setValue("sort_mode", self._sort_mode)
        self._settings.setValue("active_tab", self.tabs.currentIndex())
        self._settings.setValue(
            "active_filters",
            json.dumps({k: list(v) for k, v in self._active_filters.items()}),
        )
        self._settings.setValue(
            "excluded_filters",
            json.dumps({k: list(v) for k, v in self._excluded_filters.items()}),
        )

    def _restore_profile_from_bridge(self):
        """First-run recovery: load UI state from bridge shared.json profile."""
        shared_path = Path(self._BRIDGE_DIR) / "shared.json"
        if not shared_path.exists():
            return
        try:
            data = json.loads(shared_path.read_text(encoding="utf-8"))
            profiles = data.get("ui_profiles", {})
            profile = profiles.get(socket.gethostname())
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
            if profile.get("sort_mode") in self._SORT_MODES:
                self._sort_mode = profile["sort_mode"]
            if isinstance(profile.get("active_tab"), int):
                self._saved_active_tab = profile["active_tab"]
            if isinstance(profile.get("active_filters"), dict):
                self._active_filters = {
                    k: set(profile["active_filters"].get(k, []))
                    for k in ("priority", "due", "project")
                }
            if isinstance(profile.get("excluded_filters"), dict):
                self._excluded_filters = {
                    k: set(profile["excluded_filters"].get(k, []))
                    for k in ("priority", "due", "project")
                }
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
        """Sync memory bridge via bridge_sync_worker (pull + push + shared.js)."""
        if not os.path.isdir(self._BRIDGE_DIR):
            self.status.showMessage("Bridge dir not found", 3000)
            return

        def _run():
            try:
                _hooks_dir = os.path.expanduser("~/.claude/hooks")
                if _hooks_dir not in sys.path:
                    sys.path.insert(0, _hooks_dir)
                import bridge_sync_worker

                bridge_sync_worker.main(
                    progress_callback=lambda pct, label: self._bridge_progress.emit(
                        pct, label
                    )
                )

                # Patch UI profile into shared.json (tray-specific, no extra commit)
                self._patch_ui_profile()

                self._bridge_done.emit("Synced OK")
            except Exception as exc:
                self._bridge_done.emit(f"Sync error: {exc}")

        threading.Thread(target=_run, daemon=True).start()

    def _patch_ui_profile(self):
        """Write own UI profile into shared.json (persisted on next sync cycle)."""
        shared_path = Path(self._BRIDGE_DIR) / "shared.json"
        if not shared_path.exists():
            return
        try:
            data = json.loads(shared_path.read_text(encoding="utf-8"))
            profiles = data.get("ui_profiles", {})
            profiles[socket.gethostname()] = {
                "theme": _theme_name,
                "font_size": _font_size,
                "bold": _bold,
                "sort_mode": self._settings.value("sort_mode", "priority"),
                "active_tab": int(self._settings.value("active_tab", 0)),
                "active_filters": json.loads(
                    self._settings.value("active_filters", "{}")
                ),
                "updated_at": now_iso(),
            }
            geo = self._settings.value("geometry")
            if geo:
                profiles[socket.gethostname()]["geometry_b64"] = base64.b64encode(
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
        conn = conn or self.db._conn
        conn.execute("BEGIN")
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
        conn.commit()

    def _sort_tasks(self, tasks):
        """Sort tasks by current sort mode."""
        mode = self._sort_mode
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
        for f in due_filters:
            # "today" matches by due_date OR by section (user intent = "work today")
            if f == "today" and (due == today or task.get("section") == "today"):
                return True
            if due is None:
                continue
            if f == "overdue" and due < today:
                return True
            if f == "week" and week_start <= due <= week_end:
                return True
        return False

    def _filter(self, tasks):
        """Apply search OR chip filters. Search bypasses chip filters."""
        q = self._search_text
        if q:
            # SmartKey fuzzy search (falls back to substring if unavailable)
            return self._search_engine.search(q, tasks)

        # ── Include filters (AND between dims, OR within dim) ──
        if self._active_filters["priority"]:
            tasks = [
                t
                for t in tasks
                if t.get("priority", "medium") in self._active_filters["priority"]
            ]

        due_inc = self._active_filters["due"]
        due_exc = self._excluded_filters["due"]
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

        if self._active_filters["project"]:
            tasks = [
                t for t in tasks if t.get("project") in self._active_filters["project"]
            ]

        # ── Exclude filters (remove matching) ──
        if self._excluded_filters["priority"]:
            tasks = [
                t
                for t in tasks
                if t.get("priority", "medium") not in self._excluded_filters["priority"]
            ]

        if self._excluded_filters["project"]:
            tasks = [
                t
                for t in tasks
                if t.get("project") not in self._excluded_filters["project"]
            ]

        return tasks

    def refresh(self):
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
            "today": [
                t
                for t in all_active
                if t.get("section") == "today" and t.get("type", "task") != "note"
            ],
            "inbox": [
                t
                for t in all_active
                if t.get("section") == "inbox" and t.get("type", "task") != "note"
            ],
            "next": [
                t
                for t in all_active
                if t.get("section") == "next" and t.get("type", "task") != "note"
            ],
            "notes": notes,
            "projects": [t for t in all_active if t.get("type", "task") != "note"],
            "all": all_active,
            "done": done,
        }

        # Pre-compute filtered+sorted data for all tabs (cheap Python ops)
        self._filtered_cache = {}
        if self._search_text:
            # Global search: search ALL tasks, then distribute into tabs
            all_tasks = all_active + done
            global_results = self._search_engine.search(self._search_text, all_tasks)
            global_ids = {id(t) for t in global_results}
            for key in self._tab_keys:
                matched = [t for t in raw[key] if id(t) in global_ids]
                self._filtered_cache[key] = self._sort_tasks(matched)
        else:
            for key in self._tab_keys:
                self._filtered_cache[key] = self._sort_tasks(self._filter(raw[key]))

        # Update tab visibility (suggested, notes, projects always visible)
        always_visible = ("suggested", "notes", "projects")
        for i, key in enumerate(self._tab_keys):
            count = len(self._filtered_cache[key])
            self.tabs.setTabVisible(i, count > 0 or key in always_visible)

        # Auto-switch to first tab with results when searching
        current_idx = self.tabs.currentIndex()
        if self._search_text:
            current_key = (
                self._tab_keys[current_idx] if current_idx < len(self._tab_keys) else ""
            )
            if not self._filtered_cache.get(current_key):
                for i, key in enumerate(self._tab_keys):
                    if self._filtered_cache.get(key):
                        self.tabs.setCurrentIndex(i)
                        current_idx = i
                        break

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
        """Handle tab switch: save state + lazy-load the newly visible tab."""
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
        if item.checkState() == Qt.CheckState.Checked:
            self.db.mark_done(task_id)
        else:
            self.db.update_task(task_id, status="not_started")
        QTimer.singleShot(300, self.refresh)

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
        self.refresh()

    def closeEvent(self, event):
        self._settings.setValue("geometry", self.saveGeometry())
        self._save_ui_state()
        self._search_engine.save()
        self._refresh_timer.stop()
        event.ignore()
        self.hide()


# ── App Controller ──────────────────────────────────────────────────


class TaskTrayApp:
    """Main application controller."""

    def __init__(self):
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

    def _refresh_all(self):
        """Update tray icon badge + tooltip after any change."""
        summary = self.db.get_summary()
        self._update_icon(summary)
        self.tray.setToolTip(self._tooltip(summary))
        if self.popup and self.popup.isVisible():
            self.popup.refresh()
        if self.full_window and self.full_window.isVisible():
            self.full_window.refresh()

    def _on_quit(self):
        self.db.close()

    def run(self):
        return self.app.exec()


def main():
    app = TaskTrayApp()
    sys.exit(app.run())


if __name__ == "__main__":
    main()
