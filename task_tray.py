"""Task Tray — SQLite Task Manager.

System tray widget with dual mode: compact popup + full window.
Reads/writes directly to ~/.claude/memory/memory.db.
"""

import sqlite3
import uuid
from datetime import date, datetime, timedelta, timezone

from db_utils import (
    DB_PATH,
    PRIORITY_COLORS,
    TASK_ALLOWED_UPDATE_FIELDS as ALLOWED_FIELDS,
    TASK_HIDDEN_STATUSES as HIDDEN_STATUSES,
    TASK_PRIORITIES,
    TASK_SECTIONS as SECTIONS,
    is_overdue,
    now_iso,
)

PRIORITIES = tuple(reversed(TASK_PRIORITIES))  # descending for UI display

# Upper-case priority colors for UI lookups
_PRIORITY_COLORS_UPPER = {k.upper(): v for k, v in PRIORITY_COLORS.items()}


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

    def get_tasks(self, section=None):
        """Return active tasks (excludes done, archived, cancelled)."""
        excluded = list(HIDDEN_STATUSES) + ["done"]
        placeholders = ",".join("?" for _ in excluded)
        sql = f"SELECT * FROM tasks WHERE status NOT IN ({placeholders})"
        params = list(excluded)
        if section:
            sql += " AND section = ?"
            params.append(section)
        sql += " ORDER BY created_at"
        rows = self._conn.execute(sql, params).fetchall()
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
        overdue = sum(
            1 for t in tasks if is_overdue(t.get("due_date")) and t["status"] != "done"
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
        now = now_iso()
        self._conn.execute(
            "INSERT INTO tasks (id, title, status, section, priority, "
            "due_date, project, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (task_id, title, status, section, priority, due_date, project, now, now),
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
from PyQt6.QtCore import QSettings, Qt, QTimer, QPoint


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
    """Compact popup showing Today + Overdue tasks."""

    def __init__(self, db, on_open_full, parent=None):
        super().__init__(
            parent, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint
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
            QLabel#header { font-size: 15px; font-weight: bold; padding: 10px 14px; }
            QLabel#section-header { font-size: 11px; color: #a0aec0; padding: 6px 14px 2px;
                                    text-transform: uppercase; letter-spacing: 1px; }
            QCheckBox { font-size: 13px; padding: 6px 14px; }
            QCheckBox::indicator { width: 16px; height: 16px; }
            QLabel#priority { font-size: 10px; font-weight: bold; padding: 2px 6px;
                              border-radius: 3px; }
            QLineEdit { background: #2d3748; border: 1px solid #4a5568; border-radius: 4px;
                        color: #f7fafc; padding: 6px 10px; margin: 6px 14px; }
            QPushButton#open-full { background: #2d3748; border: none; color: #a0aec0;
                                    padding: 8px; font-size: 12px; }
            QPushButton#open-full:hover { background: #4a5568; color: #ffffff; }
        """

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Header
        header = QLabel("Tasks")
        header.setObjectName("header")
        layout.addWidget(header)

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

        # Quick add
        self.add_input = QLineEdit()
        self.add_input.setPlaceholderText("+ Quick add task...")
        self.add_input.returnPressed.connect(self._quick_add)
        layout.addWidget(self.add_input)

        # Open full button
        btn = QPushButton("Open Full Window")
        btn.setObjectName("open-full")
        btn.clicked.connect(self.on_open_full)
        layout.addWidget(btn)

    def refresh(self):
        """Reload tasks from DB and rebuild list."""
        while self.task_layout.count():
            item = self.task_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        today_tasks = self.db.get_tasks(section="today")
        overdue = self.db.get_overdue()

        # Remove duplicates (overdue today tasks)
        today_ids = {t["id"] for t in today_tasks}
        overdue_only = [t for t in overdue if t["id"] not in today_ids]

        if today_tasks:
            lbl = QLabel(f"Today ({len(today_tasks)})")
            lbl.setObjectName("section-header")
            self.task_layout.addWidget(lbl)
            for task in today_tasks:
                self.task_layout.addWidget(self._make_task_row(task))

        if overdue_only:
            lbl = QLabel(f"Overdue ({len(overdue_only)})")
            lbl.setObjectName("section-header")
            self.task_layout.addWidget(lbl)
            for task in overdue_only:
                self.task_layout.addWidget(self._make_task_row(task))

        if not today_tasks and not overdue_only:
            lbl = QLabel("All clear!")
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

        return row

    def _on_toggle(self, task_id, checked):
        if checked:
            self.db.mark_done(task_id)
        else:
            self.db.update_task(task_id, status="not_started")
        QTimer.singleShot(300, self.refresh)

    def _quick_add(self):
        text = self.add_input.text().strip()
        if text:
            self.db.add_task(text, section="today")
            self.add_input.clear()
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

    def showEvent(self, event):
        super().showEvent(event)
        self._refresh_timer.start(30000)

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
            "section": self.section_combo.currentText(),
            "priority": self.priority_combo.currentText(),
        }
        vals["due_date"] = self.due_edit.text().strip() or None
        vals["project"] = self.project_edit.text().strip() or None
        return vals


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
            item.setText(f"[{priority}] {task['title']}{due}")
            if task["status"] == "done":
                item.setForeground(QColor("#1a5632"))
            self.addItem(item)
        self.blockSignals(False)

    def _on_double_click(self, item):
        task_id = item.data(Qt.ItemDataRole.UserRole)
        task = next((t for t in self._tasks if t["id"] == task_id), None)
        if task:
            dlg = EditTaskDialog(task, self)
            if dlg.exec() == QDialog.DialogCode.Accepted:
                self.db.update_task(task_id, **dlg.get_values())

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
        delete_action = menu.addAction("Delete")
        action = menu.exec(self.mapToGlobal(pos))
        if action == delete_action:
            self.db.delete_task(task_id)


class FullWindow(QMainWindow):
    """Full task manager window with tabs."""

    def __init__(self, db, parent=None):
        super().__init__(parent)
        self.db = db
        self.setWindowTitle("Task Manager \u2014 SQLite Memory")
        self.resize(800, 600)

        # Center on screen (overridden by saved geometry if available)
        primary = QApplication.primaryScreen()
        if primary:
            screen = primary.availableGeometry()
            self.move(screen.center() - self.rect().center())

        # Restore saved geometry
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
            QStatusBar { background: #e2e8f0; color: #000000; font-weight: bold;
                         border-top: 1px solid #cbd5e0; padding: 2px 8px; }
            QMenuBar { background: #e2e8f0; color: #000000; }
            QMenu { background: #ffffff; color: #000000; border: 1px solid #a0aec0; }
            QMenu::item:selected { background: #1a2332; color: #ffffff; }
        """)

        # Tabs
        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)

        self.tab_lists = {}
        for section in ("today", "inbox", "next", "all", "done"):
            lw = TaskListWidget(self.db)
            lw.itemChanged.connect(lambda item, s=section: self._on_item_changed(item))
            self.tab_lists[section] = lw
            label = (
                section.title() if section not in ("all", "done") else section.title()
            )
            self.tabs.addTab(lw, label)

        # Toolbar
        toolbar = QToolBar()
        toolbar.setMovable(False)
        add_action = QAction("+ Add Task", self)
        add_action.triggered.connect(self._add_task)
        toolbar.addAction(add_action)
        refresh_action = QAction("Refresh", self)
        refresh_action.triggered.connect(self.refresh)
        toolbar.addAction(refresh_action)
        self.addToolBar(toolbar)

        # Status bar
        self.status = QStatusBar()
        self.setStatusBar(self.status)

        # Auto-refresh every 30s (started/stopped in showEvent/closeEvent)
        self._refresh_timer = QTimer(self)
        self._refresh_timer.timeout.connect(self.refresh)

        self.refresh()

    def refresh(self):
        # Auto-purge done tasks older than 30 days
        purged = self.db.purge_old_done(days=30)

        for section, lw in self.tab_lists.items():
            if section == "done":
                lw.load_tasks(self.db.get_done_tasks())
            elif section == "all":
                lw.load_tasks(self.db.get_tasks())
            else:
                lw.load_tasks(self.db.get_tasks(section=section))

        s = self.db.get_summary()
        done_count = len(self.db.get_done_tasks())
        msg = f"Active: {s['total']} | Done: {done_count} | Overdue: {s['overdue']}"
        if purged:
            msg += f" | Purged: {purged}"
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
        self._refresh_timer.start(30000)
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
