"""Tray dialog classes — extracted from task_tray.py.

Contains:
  - Theme system (_T, _fw, style builders)
  - UI helper classes (_ClickableLabel, _TooltipCopyFilter, etc.)
  - All dialog/popup classes (TrayPopup, EditTaskDialog, etc.)
  - TaskListWidget helper utilities (_format_task_text, _apply_task_item_colors, etc.)
"""

import calendar as _cal_mod
import html as _html
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import threading
import time
from datetime import date, datetime, timezone

from db_utils import (
    PRIORITY_COLORS,
    TASK_PRIORITIES,
    TASK_SECTIONS as SECTIONS,
    TaskDAO,
    get_conn,
    is_overdue,
    now_iso,
    priority_sort_key,
)

from PyQt6.QtWidgets import (
    QApplication,
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
    QListWidget,
    QListWidgetItem,
    QDialog,
    QFormLayout,
    QComboBox,
    QDialogButtonBox,
    QDateEdit,
    QCompleter,
    QMessageBox,
    QSpinBox,
    QStyledItemDelegate,
    QDateTimeEdit,
    QFileDialog,
)
from PyQt6.QtGui import QColor, QDesktopServices, QFont, QPainter, QPixmap
from PyQt6.QtCore import (
    QDate,
    QDateTime,
    QEvent,
    QObject,
    Qt,
    QTimer,
    QPoint,
    QTime,
    QUrl,
    pyqtSignal,
)

logger = logging.getLogger("task_tray")

_DIALOG_OPEN_ERRORS = (RuntimeError, sqlite3.Error, OSError, ValueError)
_OPTIONAL_CONTEXT_ERRORS = (
    ImportError,
    sqlite3.Error,
    OSError,
    RuntimeError,
    ValueError,
)

PRIORITIES = tuple(reversed(TASK_PRIORITIES))  # descending for UI display

# Upper-case priority colors for UI lookups
_PRIORITY_COLORS_UPPER = {k.upper(): v for k, v in PRIORITY_COLORS.items()}
_DEFAULT_PRIORITY_COLOR = "#718096"

# Entity type → badge color (shared between TaskReaderDialog + entity search cards)
_ENTITY_TYPE_COLORS = {
    "concept": "#1a3a5c",
    "tool": "#2d6a2e",
    "person": "#8b4513",
    "project": "#4a148c",
    "technology": "#00695c",
    "fact": "#555",
    "claim": "#8b6914",
    "process": "#2e4057",
}
_ENTITY_DEFAULT_COLOR = "#555"

# Columns needed by UI rendering (excludes parent_id, notes, assignee, shared_by, publish_requested_at)
_UI_COLS = "id, title, description, notes, status, section, priority, due_date, project, type, recurring, reminder_at, visibility, updated_at, created_at"

# Auto-refresh interval for TrayPopup and FullWindow refresh timers
_REFRESH_INTERVAL_MS = 30_000


# ── Clipboard helpers ────────────────────────────────────────────────


def _should_render_context_preview(pack_result) -> bool:
    if not isinstance(pack_result, dict):
        return False
    if pack_result.get("items_included", 0) <= 0:
        return False
    if not (pack_result.get("body") or "").strip():
        return False
    return bool(pack_result.get("previewable", True))


_wl_copy_proc = None  # Track wl-copy PID to prevent ghost windows
_HAS_WL_COPY = (
    bool(os.environ.get("WAYLAND_DISPLAY")) and shutil.which("wl-copy") is not None
)


def _clipboard_write(text):
    """Write to clipboard. Wayland-aware with wl-copy fallback."""
    global _wl_copy_proc
    QApplication.clipboard().setText(text)

    if _HAS_WL_COPY:
        if _wl_copy_proc and _wl_copy_proc.poll() is None:
            _wl_copy_proc.kill()
            _wl_copy_proc.wait()
        _wl_copy_proc = subprocess.Popen(
            ["wl-copy", "--", text],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def _format_file_size(size: int | None) -> str:
    """Render a file size for compact attachment labels."""
    value = max(0, int(size or 0))
    units = ("B", "KB", "MB", "GB")
    amount = float(value)
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(amount)} {unit}"
            return f"{amount:.1f} {unit}"
        amount /= 1024.0
    return f"{value} B"


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
_update_theme_colors()


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


# ── Utility functions ────────────────────────────────────────────────


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
            try:
                parts.append(_cal_mod.month_name[int(month)])
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
        with get_conn(db_path) as conn:
            rows = conn.execute(
                "SELECT entity_name, AVG(specificity * 0.35 + falsifiability * 0.25 + "
                "internal_consistency * 0.25 + novelty * 0.15) as iq "
                "FROM knowledge_ratings GROUP BY entity_name"
            ).fetchall()
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
    except sqlite3.Error as exc:
        logger.debug("TruthScore badge refresh failed: %s", exc)
        return _ts_cache  # return stale cache on error


def _get_truth_score_badge(task, db_path=None):
    """Query TruthScore for public entities and return a color-coded badge string."""
    if task.get("visibility") != "public" or not task.get("title"):
        return ""
    cache = _batch_truth_scores(db_path)
    return cache.get(task["title"], "\u2b1c ")  # gray square = unrated


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
    preview_source = desc
    if not preview_source and task.get("notes"):
        preview_source = f"[notes] {task['notes']}"
    preview = (
        f" — {preview_source[:50]}..."
        if len(preview_source) > 50
        else (f" — {preview_source}" if preview_source else "")
    )
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


def _build_rich_tooltip(task):
    """Build consistent rich tooltip for task display."""
    parts = []
    rl = _recurring_label(task.get("recurring"))
    if rl:
        parts.append(f"\U0001f504 {rl}")
    if task.get("description"):
        parts.append(task["description"])
    if task.get("notes"):
        parts.append(f"Notes: {task['notes']}")
    if task.get("priority"):
        parts.append(f"Priority: {task['priority']}")
    if task.get("due_date"):
        parts.append(f"Due: {task['due_date']}")
    if task.get("project"):
        parts.append(f"Project: {task['project']}")
    if task.get("section"):
        parts.append(f"Section: {task['section']}")
    return "\n".join(parts) if parts else None


def _build_detail_sections_from_record(task):
    """Fallback detail sections for premium read-only records."""
    sections = []
    for title, key in (
        ("Description", "description"),
        ("Notes", "notes"),
        ("Project", "project"),
        ("Client", "client_ref"),
        ("Mailbox", "mailbox_key"),
        ("Risk", "risk_level"),
        ("Updated", "updated_at"),
    ):
        value = task.get(key)
        if value:
            sections.append({"title": title, "body": str(value)})
    return sections


def _build_copy_text(task):
    """Build clipboard text for task (title always included)."""
    parts = [task["title"]]
    if task.get("description"):
        parts.append(task["description"])
    if task.get("notes"):
        parts.append(f"Notes: {task['notes']}")
    if task.get("priority"):
        parts.append(f"Priority: {task['priority']}")
    if task.get("due_date"):
        parts.append(f"Due: {task['due_date']}")
    if task.get("project"):
        parts.append(f"Project: {task['project']}")
    if task.get("section"):
        parts.append(f"Section: {task['section']}")
    if task.get("assignee"):
        parts.append(f"Assignee: {task['assignee']}")
    return "\n".join(parts)


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


# ── UI helper classes ────────────────────────────────────────────────


class _ClickableLabel(QLabel):
    """Label that emits clicked signal on mouse press."""

    clicked = pyqtSignal()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


class _TooltipCopyFilter(QObject):
    """Copies full task summary to clipboard when tooltip is about to show."""

    _last_copied_text = None  # class-level debounce

    def __init__(self, task, parent=None):
        super().__init__(parent)
        self._task = task

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.ToolTip:
            copy_text = _build_copy_text(self._task)
            if copy_text != _TooltipCopyFilter._last_copied_text:
                _clipboard_write(copy_text)
                _TooltipCopyFilter._last_copied_text = copy_text
        return False  # let tooltip show normally


class _ListTooltipCopyFilter(QObject):
    """Copies task summary to clipboard when hovering items in TaskListWidget."""

    def __init__(self, list_widget, parent=None):
        super().__init__(parent)
        self._list = list_widget
        self._last_copied_text = None

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.ToolTip:
            pos = event.pos()
            if obj is self._list:
                pos = self._list.viewport().mapFrom(self._list, pos)
            elif obj is not self._list.viewport():
                pos = self._list.viewport().mapFrom(obj, pos)
            item = self._list.itemAt(pos)
            if item:
                task_id = item.data(Qt.ItemDataRole.UserRole)
                if task_id:
                    task = next(
                        (t for t in self._list._tasks if t["id"] == task_id),
                        None,
                    )
                    if task:
                        copy_text = _build_copy_text(task)
                        if copy_text != self._last_copied_text:
                            _clipboard_write(copy_text)
                            self._last_copied_text = copy_text
        return False


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


# ── TrayPopup ───────────────────────────────────────────────────────


class TrayPopup(QWidget):
    """Compact popup showing top suggested tasks."""

    _entity_search_done = pyqtSignal(list, int)  # (entity_results, seq_id)

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
        self._entity_seq_id = 0
        self._open_dialogs = []
        self._entity_search_done.connect(self._on_entity_results)
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
        self._add_desc.setPlaceholderText("Description (main task/note body)...")
        self._add_desc.setMaximumHeight(60)
        form_layout.addWidget(self._add_desc)
        self._add_notes = QTextEdit()
        self._add_notes.setPlaceholderText("Notes (internal / metadata, optional)...")
        self._add_notes.setMaximumHeight(48)
        form_layout.addWidget(self._add_notes)
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
                q, all_tasks, limit=20, conn=None, use_vector=False
            )
            # Show tasks immediately, entities arrive async
            merged = [{**t, "_is_entity": False} for t in tasks]

            # Async entity search (with vector — always on)
            self._entity_seq_id += 1
            _seq = self._entity_seq_id
            _q = q

            def _entity_worker(seq_id=_seq, query=_q):
                results = self.db.search_entities_fast(query, limit=5)
                self._entity_search_done.emit(results, seq_id)

            threading.Thread(target=_entity_worker, daemon=True).start()
        else:
            tasks = self.db.get_suggested_tasks(limit=8)
            merged = None  # no search — use smart grouping

        self._tasks = (
            [m for m in merged if not m.get("_is_entity")] if merged else tasks
        )

        if q and merged:
            # Flat interleaved list when searching
            for item in merged:
                if item.get("_is_entity"):
                    self.task_layout.addWidget(self._make_entity_row(item))
                else:
                    self.task_layout.addWidget(self._make_task_row(item))
        elif q:
            lbl = QLabel("No matches")
            lbl.setObjectName("section-header")
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.task_layout.addWidget(lbl)
        elif tasks:
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
            lbl = QLabel("All clear!")
            lbl.setObjectName("section-header")
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.task_layout.addWidget(lbl)

        self.task_layout.addStretch()

    def _make_entity_row(self, entity):
        """Compact entity card for search results in TrayPopup."""
        etype = (entity.get("entity_type") or "").lower()
        color = _ENTITY_TYPE_COLORS.get(etype, _ENTITY_DEFAULT_COLOR)

        row = QWidget()
        row.setStyleSheet(f"border-left: 3px solid {color}; background: #0d1b2a;")
        row.setCursor(Qt.CursorShape.PointingHandCursor)
        hl = QHBoxLayout(row)
        hl.setContentsMargins(14, 4, 14, 4)

        info = QVBoxLayout()
        info.setSpacing(1)
        # Name + type badge
        name_lbl = QLabel(
            f'<b style="color:#e6edf3;">{_html.escape(entity["name"])}</b>'
            f'  <span style="background:{color}; color:white; padding:1px 6px; '
            f'border-radius:8px; font-size:10px;">{_html.escape(etype or "entity")}</span>'
        )
        name_lbl.setTextFormat(Qt.TextFormat.RichText)
        info.addWidget(name_lbl)
        # Observation preview + task count
        parts = []
        if entity.get("obs_preview"):
            parts.append(_html.escape(entity["obs_preview"][:60]))
        tc = entity.get("task_count", 0)
        if tc:
            parts.append(f"{tc} task{'s' if tc != 1 else ''}")
        if parts:
            sub = QLabel(
                f'<span style="color:#8b949e; font-size:11px; font-style:italic;">'
                f"{' · '.join(parts)}</span>"
            )
            sub.setTextFormat(Qt.TextFormat.RichText)
            info.addWidget(sub)
        hl.addLayout(info)
        hl.addStretch()

        eid = entity["entity_id"]
        row.mousePressEvent = lambda _ev, _eid=eid: self._open_entity_detail(_eid)
        return row

    def _on_entity_results(self, entities: list, seq_id: int):
        """Inject async entity results into existing task layout."""
        if seq_id != self._entity_seq_id:
            return
        if not entities or not self._search_text:
            return
        # Remove existing entity widgets (tagged with _is_entity_row)
        for i in range(self.task_layout.count() - 1, -1, -1):
            item = self.task_layout.itemAt(i)
            if (
                item
                and item.widget()
                and getattr(item.widget(), "_is_entity_row", False)
            ):
                self.task_layout.takeAt(i)
                item.widget().deleteLater()
        # Remove trailing stretch
        last = self.task_layout.count() - 1
        if last >= 0:
            item = self.task_layout.itemAt(last)
            if item and item.spacerItem():
                self.task_layout.takeAt(last)
        # Append entity widgets
        for ent in entities:
            row = self._make_entity_row(ent)
            row._is_entity_row = True
            self.task_layout.addWidget(row)
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
        plbl.setStyleSheet(
            f"color: {_PRIORITY_COLORS_UPPER.get(priority, _DEFAULT_PRIORITY_COLOR)};"
        )
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

    def _track_dialog(self, dlg):
        self._open_dialogs.append(dlg)

        def _cleanup(*_args):
            self._open_dialogs = [d for d in self._open_dialogs if d is not dlg]

        dlg.destroyed.connect(_cleanup)

    def _show_dialog_deferred(self, factory, *, label: str):
        """Open dialogs after the tray input cycle to avoid Windows COM crashes."""

        def _open():
            try:
                self.hide()
                dlg = factory()
                if dlg is None:
                    return
                dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
                self._track_dialog(dlg)
                dlg.show()
                dlg.raise_()
                dlg.activateWindow()
            except _DIALOG_OPEN_ERRORS as exc:
                logger.error(
                    "Failed to open %s from popup: %s",
                    label,
                    exc,
                    exc_info=True,
                )

        QTimer.singleShot(0, _open)

    def _open_reader(self, task_id):
        task = TaskDAO.get_by_id(self.db._conn, task_id, columns=_UI_COLS)
        if not task:
            task = next((t for t in self._tasks if t["id"] == task_id), None)
        if task:
            self._show_dialog_deferred(
                lambda task=task: TaskReaderDialog(task, self.db, None),
                label=f"task reader for {task_id}",
            )

    def _open_entity_detail(self, entity_id: int):
        self._show_dialog_deferred(
            lambda entity_id=entity_id: EntityDetailDialog(self.db, entity_id, None),
            label=f"entity detail for {entity_id}",
        )

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
        notes = self._add_notes.toPlainText().strip()
        due = self._add_due.text().strip()
        if due:
            kwargs["due_date"] = due
        task_type = self._add_type.currentText().lower()
        task_id = self.db.add_task(
            title,
            type=task_type,
            description=desc or None,
            notes=notes or None,
            **kwargs,
        )
        self._add_title.clear()
        self._add_desc.clear()
        self._add_notes.clear()
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


# ── EditTaskDialog ───────────────────────────────────────────────────


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
        self._task_id = task.get("id")
        self._attachment_items: list[dict] = []
        self._removed_attachment_ids: set[str] = set()
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
        self.desc_edit.setPlaceholderText("Description (main task/note body)...")
        layout.addRow("Description:", self.desc_edit)

        self.notes_edit = QTextEdit()
        self.notes_edit.setPlainText(task.get("notes", "") or "")
        self.notes_edit.setMaximumHeight(70)
        self.notes_edit.setPlaceholderText("Notes (internal / metadata, optional)...")
        layout.addRow("Notes:", self.notes_edit)

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
            from PyQt6.QtWidgets import QMenu, QToolButton

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

        self.attachments_list = QListWidget()
        self.attachments_list.setMaximumHeight(120)
        self.attachments_list.itemSelectionChanged.connect(
            self._update_attachment_buttons
        )
        self.attachments_list.itemDoubleClicked.connect(
            lambda _item: self._open_selected_attachment()
        )
        att_row = QVBoxLayout()
        att_row.setSpacing(6)
        att_row.addWidget(self.attachments_list)
        att_btns = QHBoxLayout()
        self.attach_add_btn = QPushButton("Attach…")
        self.attach_add_btn.clicked.connect(self._on_add_attachments)
        self.attach_open_btn = QPushButton("Open")
        self.attach_open_btn.clicked.connect(self._open_selected_attachment)
        self.attach_remove_btn = QPushButton("Remove")
        self.attach_remove_btn.clicked.connect(self._remove_selected_attachment)
        att_btns.addWidget(self.attach_add_btn)
        att_btns.addWidget(self.attach_open_btn)
        att_btns.addWidget(self.attach_remove_btn)
        att_btns.addStretch()
        att_row.addLayout(att_btns)
        layout.addRow("Attachments:", att_row)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

        if self._task_id and self._db:
            self._attachment_items = [
                {**meta, "_kind": "existing"}
                for meta in self._db.get_task_attachments(self._task_id)
            ]
        self._refresh_attachment_list()

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

    def _refresh_attachment_list(self):
        self.attachments_list.clear()
        for attachment in self._attachment_items:
            label = attachment.get("file_name") or "attachment"
            if attachment.get("_kind") == "pending":
                detail = attachment.get("source_path") or ""
                text = f"{label}  [pending]"
                if detail:
                    text += f" — {detail}"
            else:
                text = f"{label}  ({_format_file_size(attachment.get('file_size'))})"
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, attachment)
            self.attachments_list.addItem(item)
        if self.attachments_list.count() == 0:
            placeholder = QListWidgetItem("No attachments")
            placeholder.setFlags(Qt.ItemFlag.NoItemFlags)
            placeholder.setForeground(QColor("#888"))
            self.attachments_list.addItem(placeholder)
        self._update_attachment_buttons()

    def _update_attachment_buttons(self):
        current = self._selected_attachment()
        self.attach_open_btn.setEnabled(bool(current))
        self.attach_remove_btn.setEnabled(bool(current))

    def _selected_attachment(self):
        items = self.attachments_list.selectedItems()
        if not items:
            return None
        return items[0].data(Qt.ItemDataRole.UserRole)

    def _on_add_attachments(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "Attach Files")
        existing_pending = {
            item.get("source_path")
            for item in self._attachment_items
            if item.get("_kind") == "pending"
        }
        existing_names = {
            (item.get("file_name"), item.get("_kind"), item.get("attachment_id"))
            for item in self._attachment_items
        }
        for path in paths:
            if not path or path in existing_pending:
                continue
            name = os.path.basename(path) or "attachment"
            key = (name, "pending", None)
            if key in existing_names:
                continue
            self._attachment_items.append(
                {
                    "_kind": "pending",
                    "file_name": name,
                    "source_path": path,
                    "file_size": os.path.getsize(path) if os.path.exists(path) else 0,
                }
            )
            existing_pending.add(path)
            existing_names.add(key)
        self._refresh_attachment_list()

    def _open_selected_attachment(self):
        attachment = self._selected_attachment()
        if not attachment:
            return
        if attachment.get("_kind") == "pending":
            path = attachment.get("source_path")
        elif self._db:
            path = self._db.resolve_attachment_path(attachment)
        else:
            path = None
        if not path or not os.path.exists(path):
            QMessageBox.warning(self, "Attachment Missing", "File not found.")
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(path)):
            QMessageBox.warning(self, "Open Failed", path)

    def _remove_selected_attachment(self):
        attachment = self._selected_attachment()
        if not attachment:
            return
        if attachment.get("_kind") == "existing" and attachment.get("attachment_id"):
            self._removed_attachment_ids.add(attachment["attachment_id"])
        self._attachment_items = [
            item for item in self._attachment_items if item is not attachment
        ]
        self._refresh_attachment_list()

    def get_values(self):
        vals = {
            "title": self.title_edit.text().strip(),
            "description": self.desc_edit.toPlainText().strip() or None,
            "notes": self.notes_edit.toPlainText().strip() or None,
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

    def get_attachment_changes(self):
        return {
            "add_paths": [
                item["source_path"]
                for item in self._attachment_items
                if item.get("_kind") == "pending"
            ],
            "remove_ids": sorted(self._removed_attachment_ids),
        }


# ── EntityLinkDialog ─────────────────────────────────────────────────


class EntityLinkDialog(QDialog):
    """Dialog for searching and linking knowledge graph entities to a task."""

    _search_done = pyqtSignal(list, str)

    def __init__(self, db, task_id: str, parent=None):
        super().__init__(parent)
        self.db = db
        self.task_id = task_id
        self._debounce_timer: int | None = None
        self._pending_query = ""
        self._search_done.connect(self._on_search_done)
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

        def _worker(q=query):
            res = self.db.search_entities_fast(q, limit=10)
            self._search_done.emit(res, q)

        threading.Thread(target=_worker, daemon=True).start()

    def _on_search_done(self, results: list, query: str):
        # In case the user typed more while we were searching
        if query != self._pending_query:
            return

        self._results_list.clear()
        current_ids: set[int] = set()
        for i in range(self._current_list.count()):
            eid = self._current_list.item(i).data(Qt.ItemDataRole.UserRole)
            if eid is not None:
                current_ids.add(eid)

        for r in results:
            if r["entity_id"] in current_ids:
                continue
            tc = r.get("task_count", 0)
            text = f"{r['name']}  ({r['entity_type']})  — {r['obs_count']} obs  · {tc} task{'s' if tc != 1 else ''}"
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, r["entity_id"])
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


# ── EntityDetailDialog ───────────────────────────────────────────────


class EntityDetailDialog(QDialog):
    """Detail popup for a knowledge graph entity — observations, relations, linked tasks."""

    def __init__(self, db, entity_id: int, parent=None):
        super().__init__(parent)
        self.db = db
        self.entity_id = entity_id
        self.setWindowTitle("Entity Detail")
        self.setMinimumSize(520, 420)
        self.resize(600, 520)
        self.setStyleSheet(
            "QDialog { background: #0d1117; color: #e6edf3; }"
            "QScrollArea { border: none; background: #0d1117; }"
            "QLabel { background: transparent; }"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        self._content = QLabel()
        self._content.setWordWrap(True)
        self._content.setTextFormat(Qt.TextFormat.RichText)
        self._content.setAlignment(Qt.AlignmentFlag.AlignTop)
        self._content.setContentsMargins(20, 16, 20, 16)
        self._content.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.LinksAccessibleByMouse
        )
        self._content.linkActivated.connect(self._on_link_click)
        scroll.setWidget(self._content)
        layout.addWidget(scroll)

        close_btn = QPushButton("Close")
        close_btn.setStyleSheet(
            "QPushButton { padding: 8px 24px; background: #21262d; color: #e6edf3; "
            "border: 1px solid #30363d; border-radius: 4px; margin: 8px 20px 12px; }"
            "QPushButton:hover { background: #30363d; }"
        )
        close_btn.clicked.connect(self.accept)
        layout.addWidget(close_btn, alignment=Qt.AlignmentFlag.AlignRight)

        self._load_data()

    def _load_data(self):
        with get_conn(self.db.db_path) as conn:
            self._load_data_inner(conn)

    def _load_data_inner(self, conn):
        eid = self.entity_id

        # Entity info
        ent = conn.execute("SELECT * FROM entities WHERE id = ?", (eid,)).fetchone()
        if not ent:
            self._content.setText("<h3>Entity not found</h3>")
            return

        name = _html.escape(ent["name"])
        etype = (ent["entity_type"] or "").lower()
        color = _ENTITY_TYPE_COLORS.get(etype, _ENTITY_DEFAULT_COLOR)
        badge = (
            f'<span style="background:{color}; color:white; padding:3px 10px; '
            f'border-radius:12px; font-size:11px;">{_html.escape(etype or "entity")}</span>'
        )

        html_parts = [
            f'<h2 style="margin:0 0 8px 0; color:#e6edf3;">{name} {badge}</h2>'
        ]

        # Observations
        obs_rows = conn.execute(
            "SELECT content, created_at FROM observations WHERE entity_id = ? ORDER BY id",
            (eid,),
        ).fetchall()
        if obs_rows:
            html_parts.append(
                '<div style="margin-top:12px; font-weight:bold; color:#8b949e; '
                'font-size:12px; text-transform:uppercase;">Observations</div>'
            )
            for obs in obs_rows:
                ts = obs["created_at"] or ""
                if ts:
                    ts = f' <span style="color:#484f58; font-size:11px;">{_html.escape(ts[:16])}</span>'
                html_parts.append(
                    f'<p style="margin:6px 0; padding:8px 12px; background:#161b22; '
                    f"border-left:3px solid {color}; border-radius:2px; font-size:13px; "
                    f'color:#c9d1d9;">{_html.escape(obs["content"])}{ts}</p>'
                )

        # Relations
        rel_rows = conn.execute(
            "SELECT r.relation_type, e1.name AS from_name, e1.id AS from_id, "
            "e2.name AS to_name, e2.id AS to_id "
            "FROM relations r JOIN entities e1 ON e1.id = r.from_id "
            "JOIN entities e2 ON e2.id = r.to_id "
            "WHERE r.from_id = ? OR r.to_id = ?",
            (eid, eid),
        ).fetchall()
        if rel_rows:
            html_parts.append(
                '<div style="margin-top:16px; font-weight:bold; color:#8b949e; '
                'font-size:12px; text-transform:uppercase;">Relations</div>'
            )
            for rel in rel_rows:
                fn = _html.escape(rel["from_name"])
                tn = _html.escape(rel["to_name"])
                rt = _html.escape(rel["relation_type"])
                fid, tid = rel["from_id"], rel["to_id"]
                other_id = tid if fid == eid else fid
                html_parts.append(
                    f'<p style="margin:4px 0; font-size:13px; color:#c9d1d9;">'
                    f'{fn} <span style="color:#58a6ff;">─{rt}→</span> {tn} '
                    f'<a href="entity:{other_id}" style="color:#58a6ff; font-size:11px;">[open]</a></p>'
                )

        # Extracted Canonical Facts (Intelligence Graph)
        try:
            facts = conn.execute(
                "SELECT predicate, object_text, confidence FROM canonical_facts "
                "WHERE subject COLLATE NOCASE = ? OR object_text COLLATE NOCASE = ? "
                "ORDER BY confidence DESC LIMIT 20",
                (ent["name"], ent["name"]),
            ).fetchall()
            if facts:
                html_parts.append(
                    '<div style="margin-top:16px; font-weight:bold; color:#7ee787; '
                    'font-size:12px; text-transform:uppercase;">Extracted Facts (AI)</div>'
                )
                for f in facts:
                    subj = _html.escape(ent["name"])
                    pred = _html.escape(f["predicate"])
                    obj = _html.escape(f["object_text"])
                    conf = f["confidence"] * 100
                    html_parts.append(
                        f'<p style="margin:4px 0; font-size:13px; color:#c9d1d9;">'
                        f'<span style="color:#7ee787;">⚡</span> {subj} <strong style="color:#a5d6ff;">{pred}</strong> {obj} '
                        f'<span style="color:#484f58; font-size:11px;">({conf:.0f}% conf)</span></p>'
                    )
        except sqlite3.OperationalError:
            pass

        # Linked tasks
        try:
            task_rows = TaskDAO.get_entity_tasks(conn, eid)
        except sqlite3.Error as exc:
            logger.debug("Linked task lookup failed for entity %s: %s", eid, exc)
            task_rows = []
        if task_rows:
            html_parts.append(
                '<div style="margin-top:16px; font-weight:bold; color:#8b949e; '
                'font-size:12px; text-transform:uppercase;">Linked Tasks</div>'
            )
            for t in task_rows:
                status = t.get("status", "")
                s_color = (
                    "#3fb950"
                    if status == "done"
                    else "#d29922"
                    if status == "in_progress"
                    else "#8b949e"
                )
                s_badge = (
                    f'<span style="background:{s_color}; color:white; padding:1px 6px; '
                    f'border-radius:8px; font-size:10px;">{_html.escape(status)}</span>'
                )
                title = _html.escape(t.get("title", ""))
                tid = t.get("id", "")
                html_parts.append(
                    f'<p style="margin:4px 0; font-size:13px; color:#c9d1d9;">'
                    f"{s_badge} {title} "
                    f'<a href="task:{tid}" style="color:#58a6ff; font-size:11px;">[open]</a></p>'
                )

        self._content.setText("".join(html_parts))

    def _on_link_click(self, url: str):
        if url.startswith("entity:"):
            eid = int(url.split(":", 1)[1])
            EntityDetailDialog(self.db, eid, self.parent()).exec()
        elif url.startswith("task:"):
            tid = url.split(":", 1)[1]
            with get_conn(self.db.db_path) as _conn:
                task_row = _conn.execute(
                    f"SELECT {_UI_COLS} FROM tasks WHERE id = ?", (tid,)
                ).fetchone()
                if task_row:
                    TaskReaderDialog(dict(task_row), self.db, self.parent()).exec()


# ── TaskReaderDialog ─────────────────────────────────────────────────


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

        self._attachments_frame = QFrame()
        attachments_layout = QVBoxLayout(self._attachments_frame)
        attachments_layout.setContentsMargins(16, 8, 16, 8)
        attachments_layout.setSpacing(6)
        attachments_label = QLabel("Attachments")
        attachments_label.setObjectName("reader-meta")
        attachments_layout.addWidget(attachments_label)
        self._attachment_list = QListWidget()
        self._attachment_list.setMaximumHeight(140)
        self._attachment_list.itemDoubleClicked.connect(
            lambda _item: self._open_selected_attachment()
        )
        self._attachment_list.itemSelectionChanged.connect(
            self._update_attachment_actions
        )
        attachments_layout.addWidget(self._attachment_list)
        attachments_btns = QHBoxLayout()
        self._attachment_open_btn = QPushButton("Open")
        self._attachment_open_btn.clicked.connect(self._open_selected_attachment)
        attachments_btns.addWidget(self._attachment_open_btn)
        attachments_btns.addStretch()
        attachments_layout.addLayout(attachments_btns)
        layout.addWidget(self._attachments_frame)

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
        color = _PRIORITY_COLORS_UPPER.get(priority, _DEFAULT_PRIORITY_COLOR)
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
        notes = self.task.get("notes") or ""
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

        if notes:
            notes_html = _html.escape(notes).replace("\n", "<br>")
            body_html += (
                '<div style="margin-top:16px; padding-top:12px; border-top:1px solid #444;">'
                '<div style="font-weight:bold; color:#a0aec0; margin-bottom:8px; '
                'font-size:12px;">Notes</div>'
                f'<div style="font-family: Segoe UI; font-size: {_font_size}px; '
                f'line-height: 160%; color: #cbd5e1;">{notes_html}</div></div>'
            )

        # Linked entities section
        links = self.db.get_task_links(self.task.get("id", ""))
        if links:
            badges = []
            for lk in links:
                c = _ENTITY_TYPE_COLORS.get(
                    (lk.get("entity_type") or "").lower(), _ENTITY_DEFAULT_COLOR
                )
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

        # AI Enrich Context (Context Packs)
        try:
            with get_conn(self.db.db_path) as conn:
                tid = self.task.get("id")
                from context_packer import build_context_pack as _build_context_pack

                pack_result = _build_context_pack(
                    conn,
                    "executor",
                    target_ref=tid,
                    token_budget=1600,
                    persist=False,
                )
                if _should_render_context_preview(pack_result):
                    match_score = (pack_result.get("relevance_score") or 0.0) * 100
                    trust_score = (pack_result.get("quality_score") or 0.0) * 100
                    freshness_score = (pack_result.get("freshness_score") or 0.0) * 100
                    ai_text = _html.escape(pack_result["body"]).replace("\n", "<br>")
                    body_html += (
                        f'<div style="margin-top:25px; padding:15px; background:rgba(40, 60, 90, 0.3); '
                        f'border-left: 4px solid #58a6ff; border-radius:3px;">'
                        f'<div style="color:#58a6ff; font-weight:bold; font-size:12px; margin-bottom:8px; text-transform:uppercase;">'
                        f"⬡ Intelligence Context (Match: {match_score:.0f}% • Trust: {trust_score:.0f}% • Fresh: {freshness_score:.0f}%)</div>"
                        f'<div style="color:#c9d1d9; font-size:13px; line-height:1.5;">{ai_text}</div></div>'
                    )
        except _OPTIONAL_CONTEXT_ERRORS as exc:
            logger.debug(
                "Context pack unavailable for task %s: %s",
                self.task.get("id"),
                exc,
            )

        self._body_label.setText(body_html)
        self._refresh_attachments()

    def _refresh_attachments(self):
        self._attachment_list.clear()
        attachments = self.db.get_task_attachments(self.task.get("id", ""))
        if not attachments:
            self._attachments_frame.hide()
            return
        for attachment in attachments:
            text = (
                f"{attachment.get('file_name', 'attachment')}  "
                f"({_format_file_size(attachment.get('file_size'))})"
            )
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, attachment)
            self._attachment_list.addItem(item)
        self._attachments_frame.show()
        self._update_attachment_actions()

    def _update_attachment_actions(self):
        items = self._attachment_list.selectedItems()
        self._attachment_open_btn.setEnabled(bool(items))

    def _open_selected_attachment(self):
        items = self._attachment_list.selectedItems()
        if not items:
            return
        attachment = items[0].data(Qt.ItemDataRole.UserRole)
        path = self.db.resolve_attachment_path(attachment)
        if not path or not os.path.exists(path):
            QMessageBox.warning(self, "Attachment Missing", "File not found.")
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(path)):
            QMessageBox.warning(self, "Open Failed", path)

    def _on_edit(self):
        dlg = EditTaskDialog(self.task, self, db=self.db)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            vals = dlg.get_values()
            attachment_changes = dlg.get_attachment_changes()
            if vals:
                self.db.update_task(self.task["id"], **vals)
            if attachment_changes["add_paths"] or attachment_changes["remove_ids"]:
                self.db.apply_attachment_changes(
                    self.task["id"],
                    add_paths=attachment_changes["add_paths"],
                    remove_ids=attachment_changes["remove_ids"],
                )
            self.task.update(vals)
            self._refresh_display()


class PremiumRecordDialog(QDialog):
    """Read-only dialog for premium memory records rendered in the tray."""

    def __init__(self, record, parent=None):
        super().__init__(parent)
        self.record = record
        self.setWindowTitle((record.get("title") or "Premium Record")[:80])
        self.resize(720, 680)
        self.setMinimumSize(640, 560)
        self.setStyleSheet(_build_dialog_style())

        layout = QVBoxLayout(self)

        title = QLabel(record.get("title") or "Premium Record")
        title.setStyleSheet(f"font-size: {_font_size + 3}px; font-weight: bold;")
        title.setWordWrap(True)
        layout.addWidget(title)

        meta_parts = []
        for value in (
            record.get("_premium_kind"),
            record.get("client_ref"),
            record.get("mailbox_key"),
            record.get("risk_level"),
            record.get("updated_at"),
        ):
            if value:
                meta_parts.append(str(value))
        if meta_parts:
            meta = QLabel(" | ".join(meta_parts))
            meta.setStyleSheet(
                f"color: {_T()['text2']}; font-size: {_font_size - 1}px;"
            )
            meta.setWordWrap(True)
            layout.addWidget(meta)

        body = QTextEdit()
        body.setReadOnly(True)
        sections = record.get("_detail_sections") or []
        if not sections:
            sections = _build_detail_sections_from_record(record)
        fragments = []
        for section in sections:
            heading = _html.escape(str(section.get("title") or "Section"))
            content = _html.escape(str(section.get("body") or "")).replace("\n", "<br>")
            fragments.append(
                f'<div style="margin: 0 0 16px 0;">'
                f'<div style="font-weight: bold; margin-bottom: 6px;">{heading}</div>'
                f"<div>{content}</div></div>"
            )
        body.setHtml("".join(fragments))
        layout.addWidget(body, 1)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.close)
        layout.addWidget(close_btn)


class CustomDesignDialog(QDialog):
    """Parameter editor for the premium Custom Design task-tray tab."""

    _FOCUS_CHOICES = (
        ("mixed", "Mixed"),
        ("communication", "Communication"),
        ("governance", "Governance"),
        ("history", "History"),
        ("facts", "Facts"),
        ("followup", "Follow-up"),
        ("action", "Action"),
        ("signals", "Signals"),
    )
    _GROUP_CHOICES = (
        ("smart", "Smart"),
        ("project", "Project"),
        ("client", "Client"),
        ("mailbox", "Mailbox"),
        ("risk", "Risk"),
        ("kind", "Kind"),
    )

    def __init__(self, params: dict[str, object] | None = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Custom Design")
        self.setMinimumWidth(420)
        self.setStyleSheet(_build_dialog_style())
        values = dict(params or {})

        layout = QFormLayout(self)

        self.focus_combo = QComboBox()
        for key, label in self._FOCUS_CHOICES:
            self.focus_combo.addItem(label, key)
        self._set_combo_value(self.focus_combo, str(values.get("focus") or "mixed"))
        layout.addRow("Focus:", self.focus_combo)

        self.group_combo = QComboBox()
        for key, label in self._GROUP_CHOICES:
            self.group_combo.addItem(label, key)
        self._set_combo_value(self.group_combo, str(values.get("group_by") or "smart"))
        layout.addRow("Group By:", self.group_combo)

        self.limit_spin = QSpinBox()
        self.limit_spin.setRange(1, 100)
        self.limit_spin.setValue(int(values.get("limit") or 25))
        layout.addRow("Limit:", self.limit_spin)

        self.mailbox_edit = QLineEdit(str(values.get("mailbox_key") or ""))
        layout.addRow("Mailbox:", self.mailbox_edit)

        self.client_edit = QLineEdit(str(values.get("client_ref") or ""))
        layout.addRow("Client:", self.client_edit)

        self.risk_combo = QComboBox()
        self.risk_combo.addItem("Any", "")
        for value in ("critical", "high", "medium", "low"):
            self.risk_combo.addItem(value.title(), value)
        self._set_combo_value(self.risk_combo, str(values.get("risk_level") or ""))
        layout.addRow("Risk:", self.risk_combo)

        self.only_followup = QCheckBox("Only open follow-up")
        self.only_followup.setChecked(bool(values.get("only_followup")))
        layout.addRow(self.only_followup)

        self.include_threads = QCheckBox("Include threads")
        self.include_threads.setChecked(bool(values.get("include_threads", True)))
        layout.addRow(self.include_threads)

        self.include_notes = QCheckBox("Include history notes")
        self.include_notes.setChecked(bool(values.get("include_notes", True)))
        layout.addRow(self.include_notes)

        self.include_snapshots = QCheckBox("Include action snapshots")
        self.include_snapshots.setChecked(bool(values.get("include_snapshots", True)))
        layout.addRow(self.include_snapshots)

        self.include_facts = QCheckBox("Include canonical facts")
        self.include_facts.setChecked(bool(values.get("include_facts", True)))
        layout.addRow(self.include_facts)

        self.include_extractions = QCheckBox("Include extracted signals")
        self.include_extractions.setChecked(
            bool(values.get("include_extractions", True))
        )
        layout.addRow(self.include_extractions)

        self.protected_enabled = QCheckBox("Password-protected premium view")
        self.protected_enabled.setChecked(bool(values.get("protected_view_enabled")))
        self.protected_enabled.toggled.connect(self._on_protection_toggled)
        layout.addRow(self.protected_enabled)

        self.protected_label = QLineEdit(str(values.get("protected_view_label") or ""))
        self.protected_label.setPlaceholderText("Protected View")
        layout.addRow("Protected label:", self.protected_label)

        self.protected_hint = QLineEdit(str(values.get("protected_view_hint") or ""))
        self.protected_hint.setPlaceholderText("Optional hint for trusted operators")
        layout.addRow("Password hint:", self.protected_hint)

        self.protected_password = QLineEdit()
        self.protected_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.protected_password.setPlaceholderText(
            "Leave blank to keep current password"
        )
        layout.addRow("Set password:", self.protected_password)

        self.protected_password_confirm = QLineEdit()
        self.protected_password_confirm.setEchoMode(QLineEdit.EchoMode.Password)
        self.protected_password_confirm.setPlaceholderText("Confirm new password")
        layout.addRow("Confirm password:", self.protected_password_confirm)

        self.protected_unlock = QLineEdit()
        self.protected_unlock.setEchoMode(QLineEdit.EchoMode.Password)
        self.protected_unlock.setPlaceholderText(
            "Unlock this view for the current session"
        )
        layout.addRow("Unlock password:", self.protected_unlock)

        self._protected_hash = str(values.get("protected_view_password_sha256") or "")
        self._on_protection_toggled(self.protected_enabled.isChecked())

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    @staticmethod
    def _set_combo_value(combo: QComboBox, value: str) -> None:
        idx = combo.findData(value)
        combo.setCurrentIndex(idx if idx >= 0 else 0)

    def _on_protection_toggled(self, checked: bool) -> None:
        for widget in (
            self.protected_label,
            self.protected_hint,
            self.protected_password,
            self.protected_password_confirm,
            self.protected_unlock,
        ):
            widget.setEnabled(checked)
        if checked and not self.protected_label.text().strip():
            self.protected_label.setText("Protected View")

    def get_params(self) -> dict[str, object]:
        return {
            "focus": self.focus_combo.currentData(),
            "group_by": self.group_combo.currentData(),
            "limit": self.limit_spin.value(),
            "mailbox_key": self.mailbox_edit.text().strip(),
            "client_ref": self.client_edit.text().strip(),
            "risk_level": self.risk_combo.currentData(),
            "only_followup": self.only_followup.isChecked(),
            "include_threads": self.include_threads.isChecked(),
            "include_notes": self.include_notes.isChecked(),
            "include_snapshots": self.include_snapshots.isChecked(),
            "include_facts": self.include_facts.isChecked(),
            "include_extractions": self.include_extractions.isChecked(),
            "protected_view_enabled": self.protected_enabled.isChecked(),
            "protected_view_label": self.protected_label.text().strip(),
            "protected_view_hint": self.protected_hint.text().strip(),
            "protected_view_password_sha256": self._protected_hash,
            "protected_view_password": self.protected_password.text(),
            "protected_view_password_confirm": self.protected_password_confirm.text(),
            "protected_view_unlock_password": self.protected_unlock.text(),
        }


# ── RecurringDialog ──────────────────────────────────────────────────


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
        self.month_combo.addItems([_cal_mod.month_name[i] for i in range(1, 13)])
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


# ── ReminderDateTimeDialog ───────────────────────────────────────────


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
                parsed = datetime.fromisoformat(existing_reminder)
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
            self.dt_edit.setDateTime(QDateTime(tomorrow.date(), QTime(9, 0)))
        elif minutes == -2:  # Next Monday 9:00
            days_until_monday = (8 - now.date().dayOfWeek()) % 7
            if days_until_monday == 0:
                days_until_monday = 7
            monday = now.addDays(days_until_monday)
            self.dt_edit.setDateTime(QDateTime(monday.date(), QTime(9, 0)))
        else:
            self.dt_edit.setDateTime(now.addSecs(minutes * 60))

    def get_reminder_at(self) -> str:
        """Return ISO datetime string (UTC)."""
        qdt = self.dt_edit.dateTime().toUTC()
        return datetime(
            qdt.date().year(),
            qdt.date().month(),
            qdt.date().day(),
            qdt.time().hour(),
            qdt.time().minute(),
            tzinfo=timezone.utc,
        ).isoformat()


# ── ReminderPopupDialog ──────────────────────────────────────────────


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
        color = PRIORITY_COLORS.get(priority, _DEFAULT_PRIORITY_COLOR)
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


# ── TaskListWidget ───────────────────────────────────────────────────


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
        self._open_dialogs = []
        self._tooltip_copy_filter = _ListTooltipCopyFilter(self, self)
        self.installEventFilter(self._tooltip_copy_filter)
        self.viewport().installEventFilter(self._tooltip_copy_filter)

    @staticmethod
    def _build_tooltip(task):
        return _build_rich_tooltip(task)

    @staticmethod
    def _fingerprint(tasks):
        return tuple((t["id"], t.get("updated_at", "")) for t in tasks)

    @staticmethod
    def _item_is_readonly(task):
        task_id = task.get("id")
        return bool(task.get("_readonly")) or (
            isinstance(task_id, str) and task_id.startswith("premium:")
        )

    def _apply_item_state(self, item, task, *, prefix="", include_project=True):
        item.setData(Qt.ItemDataRole.UserRole, task["id"])
        if self._item_is_readonly(task):
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
        else:
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                Qt.CheckState.Checked
                if task["status"] == "done"
                else Qt.CheckState.Unchecked
            )
        item.setText(
            _format_task_text(task, include_project=include_project, prefix=prefix)
        )
        tip = self._build_tooltip(task)
        if tip:
            item.setToolTip(tip)
        _apply_task_item_colors(item, task)

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
            self._apply_item_state(item, task)
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
                self._apply_item_state(
                    item,
                    task,
                    prefix="  ",
                    include_project=False,
                )
                self.addItem(item)

        self.blockSignals(False)

    def load_grouped_by_field(self, tasks, field_name, *, empty_label="Other"):
        """Load tasks grouped by a caller-selected field."""
        from collections import OrderedDict

        fp = (self._fingerprint(tasks), field_name)
        if fp == self._last_fp:
            return
        self._last_fp = fp
        self._tasks = tasks
        self.blockSignals(True)
        self.clear()

        groups: OrderedDict[str, list] = OrderedDict()
        for task in tasks:
            key = task.get(field_name) or empty_label
            groups.setdefault(str(key), []).append(task)

        for group_name, group_tasks in groups.items():
            header = QListWidgetItem(f"── {group_name} ({len(group_tasks)}) ──")
            header.setFlags(Qt.ItemFlag.NoItemFlags)
            header.setBackground(_CLR_HEADER_BG)
            header.setForeground(_CLR_HEADER_FG)
            font = header.font()
            font.setBold(True)
            header.setFont(font)
            self.addItem(header)
            for task in group_tasks:
                item = QListWidgetItem()
                self._apply_item_state(item, task, prefix="  ")
                self.addItem(item)

        self.blockSignals(False)

    def load_smart_grouped(self, tasks, entities=None):
        """Load tasks with smart grouping: Overdue → Urgent → By Project → Rest."""
        fp = self._fingerprint(tasks)
        ent_fp = (
            tuple((e["entity_id"], e["name"]) for e in entities) if entities else ()
        )
        combined_fp = (fp, ent_fp)
        if combined_fp == self._last_fp:
            return
        self._last_fp = combined_fp
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
                self._apply_item_state(item, task, prefix="  ")
                self.addItem(item)
        # Append entity results after task groups
        if entities:
            sep = QListWidgetItem("── Related Knowledge ──")
            sep.setFlags(Qt.ItemFlag.NoItemFlags)
            sep.setBackground(_CLR_HEADER_BG)
            sep.setForeground(QColor("#607080"))
            font = sep.font()
            font.setBold(True)
            sep.setFont(font)
            self.addItem(sep)
            for ent in entities:
                item = QListWidgetItem()
                item.setData(Qt.ItemDataRole.UserRole, f"entity:{ent['entity_id']}")
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
                etype = (ent.get("entity_type") or "").lower()
                parts = [ent["name"]]
                if etype:
                    parts.append(f"[{etype}]")
                if ent.get("obs_preview"):
                    parts.append(f"— {ent['obs_preview'][:60]}")
                tc = ent.get("task_count", 0)
                if tc:
                    parts.append(f"({tc} task{'s' if tc != 1 else ''})")
                item.setText("  ".join(parts))
                item.setBackground(QColor("#0d1b2a"))
                item.setForeground(QColor("#a0cfff"))
                self.addItem(item)
        self.blockSignals(False)

    def _dialog_parent(self):
        """Use a stable top-level parent; popup-launched readers should stand alone."""
        host = self.window()
        if isinstance(host, TrayPopup):
            host.hide()
            return None
        return host if isinstance(host, QWidget) else None

    def _track_dialog(self, dlg):
        self._open_dialogs.append(dlg)

        def _cleanup(*_args):
            self._open_dialogs = [d for d in self._open_dialogs if d is not dlg]

        dlg.destroyed.connect(_cleanup)

    def _show_dialog_deferred(self, factory, *, label: str):
        """Open dialogs outside the immediate Qt input signal to avoid tray crashes."""

        def _open():
            try:
                dlg = factory()
                if dlg is None:
                    return
                dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
                self._track_dialog(dlg)
                dlg.show()
                dlg.raise_()
                dlg.activateWindow()
            except _DIALOG_OPEN_ERRORS as exc:
                logger.error("Failed to open %s: %s", label, exc, exc_info=True)

        QTimer.singleShot(0, _open)

    def _open_reader(self, task_id):
        task = TaskDAO.get_by_id(self.db._conn, task_id, columns=_UI_COLS)
        if not task:
            task = next((t for t in self._tasks if t["id"] == task_id), None)
        if not task:
            logger.warning("Double-click open skipped; task not found: %s", task_id)
            return
        if hasattr(self, "_search_engine"):
            self._search_engine.record_open(task)
        self._show_dialog_deferred(
            lambda task=task: TaskReaderDialog(task, self.db, self._dialog_parent()),
            label=f"task reader for {task_id}",
        )

    def _open_entity_detail(self, entity_id: int):
        self._show_dialog_deferred(
            lambda entity_id=entity_id: EntityDetailDialog(
                self.db, entity_id, self._dialog_parent()
            ),
            label=f"entity detail for {entity_id}",
        )

    def _open_premium_detail(self, record):
        self._show_dialog_deferred(
            lambda record=record: PremiumRecordDialog(record, self._dialog_parent()),
            label=f"premium record detail for {record.get('id')}",
        )

    def _on_double_click(self, item):
        data = item.data(Qt.ItemDataRole.UserRole)
        if not data:
            return
        if isinstance(data, str) and data.startswith("entity:"):
            entity_id = int(data.split(":", 1)[1])
            self._open_entity_detail(entity_id)
            return
        if isinstance(data, str) and data.startswith("premium:"):
            record = next((t for t in self._tasks if t["id"] == data), None)
            if record:
                self._open_premium_detail(record)
            return
        self._open_reader(data)

    def _context_menu(self, pos):
        from PyQt6.QtWidgets import QMenu

        item = self.itemAt(pos)
        if not item:
            return
        data = item.data(Qt.ItemDataRole.UserRole)
        if not data:
            return
        # Entity items get a simplified context menu
        if isinstance(data, str) and data.startswith("entity:"):
            entity_id = int(data.split(":", 1)[1])
            menu = QMenu(self)
            menu.setStyleSheet(_build_menu_style())
            view_action = menu.addAction("View Entity")
            action = menu.exec(self.mapToGlobal(pos))
            if action == view_action:
                self._open_entity_detail(entity_id)
            return
        if isinstance(data, str) and data.startswith("premium:"):
            record = next((t for t in self._tasks if t["id"] == data), None)
            if not record:
                return
            menu = QMenu(self)
            menu.setStyleSheet(_build_menu_style())
            view_action = menu.addAction("View Premium Record")
            action = menu.exec(self.mapToGlobal(pos))
            if action == view_action:
                self._open_premium_detail(record)
            return
        task_id = data
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
