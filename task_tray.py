"""Task Tray — SQLite Task Manager.

System tray widget with dual mode: compact popup + full window.
Reads/writes directly to ~/.claude/memory/memory.db.
"""

import html as _html
import os
import sqlite3
import subprocess
import threading
import uuid
from datetime import datetime, timedelta, timezone

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
    priority_sort_key,
)

PRIORITIES = tuple(reversed(TASK_PRIORITIES))  # descending for UI display

# Upper-case priority colors for UI lookups
_PRIORITY_COLORS_UPPER = {k.upper(): v for k, v in PRIORITY_COLORS.items()}

# SQL fragment for active-task exclusion (reused across queries)
_ACTIVE_PH = ",".join("?" for _ in TASK_ACTIVE_EXCLUSIONS)
_ACTIVE_PARAMS = list(TASK_ACTIVE_EXCLUSIONS)


class TaskDB:
    """Direct sqlite3 wrapper for tasks table."""

    def __init__(self, db_path=None):
        self.db_path = db_path or DB_PATH
        self.on_change = None
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

    def get_all_active(self):
        """Return all active tasks (excludes done, archived, cancelled)."""
        rows = self._conn.execute(
            f"SELECT * FROM tasks WHERE status NOT IN ({_ACTIVE_PH}) "
            "ORDER BY created_at",
            _ACTIVE_PARAMS,
        ).fetchall()
        return [dict(r) for r in rows]

    def get_done_tasks(self):
        """Return completed tasks, newest first."""
        rows = self._conn.execute(
            "SELECT * FROM tasks WHERE status = 'done' ORDER BY updated_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def purge_old_done(self, days=30):
        """Delete done tasks older than `days` days. Returns count deleted."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        cur = self._conn.execute(
            "DELETE FROM tasks WHERE status = 'done' AND updated_at < ?", (cutoff,)
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
    ):
        """Insert new task, return its ID."""
        task_id = str(uuid.uuid4())
        now = now_iso()
        self._conn.execute(
            "INSERT INTO tasks (id, title, description, status, section, priority, "
            "due_date, project, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                task_id,
                title,
                description,
                status,
                section,
                priority,
                due_date,
                project,
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
    QStatusBar,
    QDialog,
    QFormLayout,
    QComboBox,
    QDialogButtonBox,
)
from PyQt6.QtGui import QIcon, QAction, QPixmap, QPainter, QColor, QFont
from PyQt6.QtCore import QEvent, QSettings, Qt, QTimer, QPoint, pyqtSignal


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

    def _stylesheet(self):
        return """
            QWidget { background: #1a2332; color: #f7fafc; font-family: 'Segoe UI'; }
            QLabel#header { font-size: 15px; font-weight: bold; padding: 10px 0 10px 14px; }
            QLabel#section-header { font-size: 11px; color: #a0aec0; padding: 6px 14px 2px;
                                    text-transform: uppercase; letter-spacing: 1px; }
            QCheckBox { font-size: 13px; padding: 6px 14px; }
            QCheckBox::indicator { width: 16px; height: 16px; }
            QLabel#priority { font-size: 10px; font-weight: bold; padding: 2px 6px;
                              border-radius: 3px; }
            QLineEdit { background: #2d3748; border: 1px solid #4a5568; border-radius: 4px;
                        color: #f7fafc; padding: 6px 10px; margin: 2px 14px; }
            QTextEdit { background: #2d3748; border: 1px solid #4a5568; border-radius: 4px;
                        color: #f7fafc; padding: 6px 10px; margin: 2px 14px; font-family: 'Segoe UI';
                        font-size: 13px; }
            QComboBox { background: #2d3748; border: 1px solid #4a5568; border-radius: 4px;
                        color: #f7fafc; padding: 4px 8px; margin: 2px 14px; }
            QComboBox QAbstractItemView { background: #2d3748; color: #f7fafc;
                                          selection-background-color: #4a5568; }
            QPushButton#add-btn { background: transparent; border: none; color: #a0aec0;
                                  font-size: 18px; font-weight: bold; padding: 4px 10px; }
            QPushButton#add-btn:hover { color: #ffffff; }
            QPushButton#submit-btn { background: #2d3748; border: 1px solid #4a5568;
                                     border-radius: 4px; color: #f7fafc; padding: 6px;
                                     margin: 2px 14px; font-weight: bold; }
            QPushButton#submit-btn:hover { background: #4a5568; }
            QPushButton#open-full { background: #2d3748; border: none; color: #a0aec0;
                                    padding: 8px; font-size: 12px; }
            QPushButton#open-full:hover { background: #4a5568; color: #ffffff; }
        """

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
        while self.task_layout.count():
            item = self.task_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        tasks = self.db.get_suggested_tasks(limit=8)

        # Apply search filter if active
        q = self._search_text
        if q:
            tasks = [
                t
                for t in tasks
                if q
                in (
                    f"{t.get('title', '')} {t.get('description', '')} "
                    f"{t.get('priority', '')} "
                    f"{t.get('project', '')} {t.get('due_date', '')}"
                ).lower()
            ]

        if tasks:
            label = f"Suggested ({len(tasks)})"
            lbl = QLabel(label)
            lbl.setObjectName("section-header")
            self.task_layout.addWidget(lbl)
            for task in tasks:
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
            row.setStyleSheet(
                "border-left: 3px solid #e53e3e; background: rgba(229,62,62,0.05);"
            )
        hl = QHBoxLayout(row)
        hl.setContentsMargins(14, 2, 14, 2)

        cb = QCheckBox(task["title"])
        cb.setChecked(task["status"] == "done")
        if task["status"] == "done":
            cb.setStyleSheet("color: #276749; text-decoration: line-through;")
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
        task_id = self.db.add_task(title, **kwargs)
        if desc:
            self.db.update_task(task_id, description=desc)
        self._add_title.clear()
        self._add_desc.clear()
        self._add_due.clear()
        self._add_priority.setCurrentText("medium")
        self._add_form.setVisible(False)
        self._add_btn.setText("+")
        self.refresh()

    def _on_search(self, text):
        self._search_text = text.strip().lower()
        self.refresh()

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


class EditTaskDialog(QDialog):
    """Dialog for editing task fields."""

    def __init__(self, task, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Edit Task")
        self.setMinimumWidth(350)
        self.setStyleSheet("""
            QDialog { background: #ffffff; color: #000000; }
            QLabel { color: #000000; font-weight: bold; }
            QLineEdit { background: #ffffff; color: #000000; border: 2px solid #a0aec0;
                        border-radius: 4px; padding: 6px; }
            QLineEdit:focus { border-color: #1a2332; }
            QTextEdit { background: #ffffff; color: #000000; border: 2px solid #a0aec0;
                        border-radius: 4px; padding: 6px; font-family: 'Segoe UI'; font-size: 13px; }
            QTextEdit:focus { border-color: #1a2332; }
            QComboBox { background: #ffffff; color: #000000; border: 2px solid #a0aec0;
                        border-radius: 4px; padding: 4px 8px; }
            QComboBox:focus { border-color: #1a2332; }
            QComboBox QAbstractItemView { background: #ffffff; color: #000000;
                                          selection-background-color: #1a2332;
                                          selection-color: #ffffff; }
            QPushButton { background: #e2e8f0; color: #000000; border: 1px solid #a0aec0;
                          border-radius: 4px; padding: 6px 16px; font-weight: bold; }
            QPushButton:hover { background: #1a2332; color: #ffffff; }
        """)
        layout = QFormLayout(self)

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
        layout.addRow("Section:", self.section_combo)

        self.priority_combo = QComboBox()
        self.priority_combo.addItems(PRIORITIES)
        self.priority_combo.setCurrentText(task.get("priority", "medium"))
        layout.addRow("Priority:", self.priority_combo)

        self.due_edit = QLineEdit(task.get("due_date", "") or "")
        self.due_edit.setPlaceholderText("YYYY-MM-DD")
        layout.addRow("Due Date:", self.due_edit)

        self.project_edit = QLineEdit(task.get("project", "") or "")
        layout.addRow("Project:", self.project_edit)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def get_values(self):
        vals = {
            "title": self.title_edit.text().strip(),
            "description": self.desc_edit.toPlainText().strip() or None,
            "section": self.section_combo.currentText(),
            "priority": self.priority_combo.currentText(),
        }
        vals["due_date"] = self.due_edit.text().strip() or None
        vals["project"] = self.project_edit.text().strip() or None
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

        self.setStyleSheet("""
            QDialog { background: #ffffff; }
            QLabel#reader-title { color: #000000; font-size: 18px; font-weight: bold;
                                  padding: 12px 16px 4px; }
            QLabel#reader-meta { color: #4a5568; font-size: 12px; padding: 2px 6px; }
            QLabel#reader-priority { font-size: 11px; font-weight: bold; padding: 2px 8px;
                                     border-radius: 3px; }
            QScrollArea { background: #ffffff; border: none; }
            QLabel#reader-body { color: #1a202c; font-size: 13px; padding: 16px;
                                 background: #ffffff; }
            QFrame#reader-header { background: #f7fafc; border-bottom: 1px solid #e2e8f0; }
            QPushButton { background: #e2e8f0; color: #000000; border: 1px solid #a0aec0;
                          border-radius: 4px; padding: 8px 20px; font-weight: bold;
                          font-size: 13px; }
            QPushButton:hover { background: #1a2332; color: #ffffff; }
        """)

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
            f"color: #ffffff; background: {color}; font-size: 11px; "
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
                f'line-height: 160%; color: #1a202c;">{body_html}</div>'
            )
        else:
            self._body_label.setText(
                '<div style="font-family: Segoe UI; font-size: 13px; '
                'color: #a0aec0; font-style: italic;">No description</div>'
            )

    def _on_edit(self):
        dlg = EditTaskDialog(self.task, self)
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
        self.setStyleSheet("""
            QListWidget { background: #ffffff; color: #000000; border: none;
                          font-size: 13px; }
            QListWidget::item { padding: 8px 12px; border-bottom: 1px solid #cbd5e0;
                                color: #000000; background: #ffffff; }
            QListWidget::item:selected { background: #dbeafe; color: #000000; }
            QListWidget::item:hover { background: #f0f4f8; }
            QListWidget::indicator { width: 18px; height: 18px; }
            QListWidget::indicator:unchecked { border: 2px solid #1a2332;
                                               background: #ffffff; border-radius: 3px; }
            QListWidget::indicator:checked { border: 2px solid #1a2332;
                                             background: #1a2332; border-radius: 3px; }
        """)
        self.itemDoubleClicked.connect(self._on_double_click)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._context_menu)
        self._tasks = []

    def load_tasks(self, tasks):
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
            priority = (task.get("priority") or "medium").upper()
            due = f" | Due: {task['due_date']}" if task.get("due_date") else ""
            desc = task.get("description") or ""
            preview = (
                f" — {desc[:50]}..."
                if len(desc) > 50
                else (f" — {desc}" if desc else "")
            )
            item.setText(f"[{priority}] {task['title']}{due}{preview}")
            if desc:
                item.setToolTip(desc)
            if task["status"] == "done":
                item.setForeground(QColor("#1a5632"))
            self.addItem(item)
        self.blockSignals(False)

    def _open_reader(self, task_id):
        task = next((t for t in self._tasks if t["id"] == task_id), None)
        if task:
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
        menu.setStyleSheet(
            "QMenu { background: #ffffff; color: #000000; border: 1px solid #a0aec0; }"
            "QMenu::item:selected { background: #1a2332; color: #ffffff; }"
        )
        view_action = menu.addAction("View")
        delete_action = menu.addAction("Delete")
        action = menu.exec(self.mapToGlobal(pos))
        if action == view_action:
            self._open_reader(task_id)
        elif action == delete_action:
            self.db.delete_task(task_id)


_REFRESH_INTERVAL_MS = 30_000
_PURGE_INTERVAL_MS = 3_600_000  # 1 hour


class FullWindow(QMainWindow):
    """Full task manager window with tabs, search, sort, and suggested view."""

    _bridge_done = pyqtSignal(str)

    # Sort modes cycle: priority → due → created → priority ...
    _SORT_MODES = ("priority", "due", "created")
    _SORT_LABELS = {
        "priority": "Sort: Priority",
        "due": "Sort: Due Date",
        "created": "Sort: Created",
    }

    def __init__(self, db, parent=None):
        super().__init__(parent)
        self.db = db
        self._sort_mode = "priority"
        self._search_text = ""
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

        self.setStyleSheet("""
            QMainWindow { background: #ffffff; color: #000000; }
            QTabWidget::pane { border: none; background: #ffffff; }
            QTabBar { background: #e2e8f0; }
            QTabBar::tab { padding: 8px 20px; font-weight: bold;
                           background: #e2e8f0; color: #000000;
                           border: 1px solid #cbd5e0; border-bottom: none;
                           margin-right: 2px; }
            QTabBar::tab:selected { background: #1a2332; color: #ffffff; }
            QTabBar::tab:hover:!selected { background: #cbd5e0; color: #000000; }
            QToolBar { background: #e2e8f0; border-bottom: 1px solid #cbd5e0; spacing: 4px; }
            QToolBar QToolButton { background: #ffffff; color: #000000; border: 1px solid #a0aec0;
                                   padding: 4px 12px; font-weight: bold; }
            QToolBar QToolButton:hover { background: #1a2332; color: #ffffff; }
            QToolBar QToolButton:checked { background: #1a2332; color: #ffffff; }
            QStatusBar { background: #e2e8f0; color: #000000; font-weight: bold;
                         border-top: 1px solid #cbd5e0; padding: 2px 8px; }
            QMenu { background: #ffffff; color: #000000; border: 1px solid #a0aec0; }
            QMenu::item:selected { background: #1a2332; color: #ffffff; }
            QLineEdit#search { background: #ffffff; color: #000000; border: 2px solid #a0aec0;
                               border-radius: 4px; padding: 4px 8px; min-width: 200px; }
            QLineEdit#search:focus { border-color: #1a2332; }
        """)

        # Central widget with tabs
        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)

        # Tab order: Suggested, Today, Inbox, Next, All, Done
        self._tab_keys = ["suggested", "today", "inbox", "next", "all", "done"]
        self._tab_labels = {
            "suggested": "Suggested",
            "today": "Today",
            "inbox": "Inbox",
            "next": "Next",
            "all": "All",
            "done": "Done",
        }
        self.tab_lists = {}
        for key in self._tab_keys:
            lw = TaskListWidget(self.db)
            lw.itemChanged.connect(lambda item, k=key: self._on_item_changed(item))
            self.tab_lists[key] = lw
            self.tabs.addTab(lw, self._tab_labels[key])

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

        # Sort button (click to cycle modes)
        self._sort_action = QAction(self._SORT_LABELS[self._sort_mode], self)
        self._sort_action.triggered.connect(self._cycle_sort)
        toolbar.addAction(self._sort_action)
        toolbar.addSeparator()

        # Instant search bar
        self._search_input = QLineEdit()
        self._search_input.setObjectName("search")
        self._search_input.setPlaceholderText("Search tasks...")
        self._search_input.setClearButtonEnabled(True)
        self._search_input.textChanged.connect(self._on_search)
        toolbar.addWidget(self._search_input)

        self.addToolBar(toolbar)

        # Status bar
        self.status = QStatusBar()
        self.setStatusBar(self.status)

        # Bridge sync signal (thread-safe → main thread)
        self._bridge_done.connect(lambda msg: self.status.showMessage(msg, 5000))

        # Auto-refresh every 30s
        self._refresh_timer = QTimer(self)
        self._refresh_timer.timeout.connect(self.refresh)

        # Purge done tasks once at startup, then hourly
        self._last_purged = self.db.purge_old_done(days=30)
        self._purge_timer = QTimer(self)
        self._purge_timer.timeout.connect(self._run_purge)
        self._purge_timer.start(_PURGE_INTERVAL_MS)

        self.refresh()

    def _run_purge(self):
        self._last_purged = self.db.purge_old_done(days=30)

    # ── Bridge sync ────────────────────────────────────────────────────

    _BRIDGE_DIR = os.path.expanduser("~/.claude/memory/bridge")

    def _refresh_and_sync(self):
        """Refresh task list then sync memory bridge to GitHub."""
        self.refresh()
        self._sync_bridge()

    def _sync_bridge(self):
        """Git add/commit/push the memory bridge in a background thread."""
        if not os.path.isdir(self._BRIDGE_DIR):
            self.status.showMessage("Bridge dir not found", 3000)
            return

        self.status.showMessage("Syncing bridge to GitHub...")

        def _do_sync():
            try:
                subprocess.run(
                    ["git", "add", "-A"],
                    cwd=self._BRIDGE_DIR, capture_output=True, timeout=10,
                )
                result = subprocess.run(
                    ["git", "commit", "-m", "bridge: sync from task tray"],
                    cwd=self._BRIDGE_DIR, capture_output=True, timeout=10,
                )
                if result.returncode == 0:
                    subprocess.run(
                        ["git", "push"],
                        cwd=self._BRIDGE_DIR, capture_output=True, timeout=30,
                    )
                    return "Bridge synced to GitHub"
                return "Bridge: nothing to sync"
            except Exception as exc:
                return f"Bridge sync error: {exc}"

        def _run():
            msg = _do_sync()
            self._bridge_done.emit(msg)

        threading.Thread(target=_run, daemon=True).start()

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
        # mode == "created"
        return sorted(tasks, key=lambda t: t.get("created_at") or "", reverse=True)

    def _cycle_sort(self):
        """Cycle to next sort mode and refresh."""
        idx = self._SORT_MODES.index(self._sort_mode)
        self._sort_mode = self._SORT_MODES[(idx + 1) % len(self._SORT_MODES)]
        self._sort_action.setText(self._SORT_LABELS[self._sort_mode])
        self.refresh()

    def _on_search(self, text):
        """Instant search filter."""
        self._search_text = text.strip().lower()
        self.refresh()

    def _filter(self, tasks):
        """Apply search filter to task list."""
        q = self._search_text
        if not q:
            return tasks
        return [
            t
            for t in tasks
            if q
            in (
                f"{t.get('title', '')} {t.get('description', '')} "
                f"{t.get('priority', '')} {t.get('project', '')} "
                f"{t.get('due_date', '')} {t.get('section', '')} {t.get('status', '')}"
            ).lower()
        ]

    def refresh(self):
        # Single query for all active tasks, then filter by section in Python
        all_active = self.db.get_all_active()
        done = self.db.get_done_tasks()
        suggested = self.db.get_suggested_tasks()

        raw = {
            "suggested": suggested,
            "today": [t for t in all_active if t.get("section") == "today"],
            "inbox": [t for t in all_active if t.get("section") == "inbox"],
            "next": [t for t in all_active if t.get("section") == "next"],
            "all": all_active,
            "done": done,
        }

        # Apply filter + sort, load into widgets
        for key in self._tab_keys:
            tasks = self._filter(raw[key])
            tasks = self._sort_tasks(tasks) if key != "done" else tasks
            self.tab_lists[key].load_tasks(tasks)

        # Hide empty tabs (suggested always visible)
        for i, key in enumerate(self._tab_keys):
            count = self.tab_lists[key].count()
            self.tabs.setTabVisible(i, count > 0 or key == "suggested")

        # Status bar — derive summary from already-fetched data
        s = self.db.get_summary(all_active)
        done_count = len(done)
        msg = f"Active: {s['total']} | Done: {done_count} | Overdue: {s['overdue']}"
        if self._search_text:
            msg += f" | Filter: '{self._search_text}'"
        self.status.showMessage(msg)

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
        dlg = EditTaskDialog(task, self)
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
        dlg = EditTaskDialog(task)
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
