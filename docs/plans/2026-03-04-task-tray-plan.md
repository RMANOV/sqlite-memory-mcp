# Task Tray Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** System tray task manager with dual mode — compact popup (daily) + full window (planning) — reading/writing directly to SQLite Memory DB.

**Architecture:** Single-file PyQt6 app (`task_tray.py`). TaskDB class wraps sqlite3 CRUD. TrayPopup shows Today+Overdue. FullWindow has tabbed views. Auto-refresh on focus. Bridge sync handled by existing hook.

**Tech Stack:** Python 3.14, PyQt6 6.10.2, sqlite3 (stdlib)

**Design doc:** `docs/plans/2026-03-04-task-tray-design.md`

**DANGER:** pytest + Python 3.14 = freeze risk. Run ONLY focused tests via `safe-test` skill or `python -m pytest <specific_file> -k <pattern>` with PYTHONOPTIMIZE=0.

---

### Task 1: TaskDB Data Layer

**Files:**
- Create: `task_tray.py` (start with data layer only)
- Create: `tests/test_task_db.py`

**Step 1: Write failing tests for TaskDB**

```python
# tests/test_task_db.py
import os
import sqlite3
import pytest
from datetime import date

# Will import from parent dir
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


@pytest.fixture
def db(tmp_path):
    """Fresh in-memory-like DB for each test."""
    db_path = str(tmp_path / "test.db")
    from task_tray import TaskDB
    tdb = TaskDB(db_path)
    return tdb


class TestTaskDB:
    def test_get_tasks_empty(self, db):
        assert db.get_tasks() == []

    def test_add_task_minimal(self, db):
        task_id = db.add_task("Test task")
        assert task_id is not None
        tasks = db.get_tasks()
        assert len(tasks) == 1
        assert tasks[0]["title"] == "Test task"
        assert tasks[0]["section"] == "inbox"
        assert tasks[0]["priority"] == "medium"
        assert tasks[0]["status"] == "not_started"

    def test_add_task_full(self, db):
        task_id = db.add_task(
            "Full task", section="today", priority="high",
            due_date="2026-03-04", project="test-proj"
        )
        tasks = db.get_tasks()
        assert tasks[0]["section"] == "today"
        assert tasks[0]["priority"] == "high"
        assert tasks[0]["due_date"] == "2026-03-04"

    def test_mark_done(self, db):
        tid = db.add_task("To complete")
        db.mark_done(tid)
        tasks = db.get_tasks()
        assert tasks[0]["status"] == "done"

    def test_update_task(self, db):
        tid = db.add_task("Original")
        db.update_task(tid, title="Updated", section="next", priority="low")
        t = db.get_tasks()[0]
        assert t["title"] == "Updated"
        assert t["section"] == "next"

    def test_delete_task(self, db):
        tid = db.add_task("To delete")
        db.delete_task(tid)
        assert db.get_tasks() == []

    def test_get_by_section(self, db):
        db.add_task("A", section="today")
        db.add_task("B", section="inbox")
        db.add_task("C", section="today")
        today = db.get_tasks(section="today")
        assert len(today) == 2

    def test_get_overdue(self, db):
        db.add_task("Past", due_date="2020-01-01", section="today")
        db.add_task("Future", due_date="2099-01-01", section="today")
        db.add_task("No date", section="today")
        overdue = db.get_overdue()
        assert len(overdue) == 1
        assert overdue[0]["title"] == "Past"

    def test_get_summary(self, db):
        db.add_task("A", section="today")
        db.add_task("B", due_date="2020-01-01")
        db.add_task("C", status="done")  # direct insert
        s = db.get_summary()
        assert s["total"] >= 2
        assert s["overdue"] >= 1
```

**Step 2: Run tests — expect FAIL (no task_tray.py yet)**

```bash
cd ~/.claude/mcp_servers/sqlite_memory
python -m pytest tests/test_task_db.py -v --no-header 2>&1 | head -30
```
Expected: ImportError — `task_tray` module not found.

**Step 3: Implement TaskDB**

```python
# task_tray.py — top of file
"""Task Tray — SQLite Task Manager.

System tray widget with dual mode: compact popup + full window.
Reads/writes directly to ~/.claude/memory/memory.db.
"""

import os
import sqlite3
import uuid
from datetime import date, datetime


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
            "AND due_date IS NOT NULL AND due_date < ? "
            "ORDER BY due_date"
        )
        params = list(HIDDEN_STATUSES) + [today]
        return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def get_summary(self):
        """Return dict with total, overdue, by-section counts."""
        tasks = self.get_tasks()
        today = date.today().isoformat()
        overdue = sum(
            1 for t in tasks
            if t.get("due_date") and t["due_date"] < today and t["status"] != "done"
        )
        return {"total": len(tasks), "overdue": overdue}

    def add_task(self, title, section="inbox", priority="medium",
                 due_date=None, project=None, status="not_started"):
        """Insert new task, return its ID."""
        task_id = str(uuid.uuid4())
        now = datetime.utcnow().isoformat()
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
        now = datetime.utcnow().isoformat()
        self._conn.execute(
            "UPDATE tasks SET status='done', updated_at=? WHERE id=?",
            (now, task_id),
        )
        self._conn.commit()

    def update_task(self, task_id, **fields):
        """Update arbitrary fields on a task."""
        if not fields:
            return
        fields["updated_at"] = datetime.utcnow().isoformat()
        sets = ", ".join(f"{k}=?" for k in fields)
        vals = list(fields.values()) + [task_id]
        self._conn.execute(f"UPDATE tasks SET {sets} WHERE id=?", vals)
        self._conn.commit()

    def delete_task(self, task_id):
        """Hard delete a task."""
        self._conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))
        self._conn.commit()
```

**Step 4: Run tests — expect PASS**

```bash
cd ~/.claude/mcp_servers/sqlite_memory
PYTHONOPTIMIZE=0 python -m pytest tests/test_task_db.py -v --no-header
```
Expected: 10/10 PASS

**Step 5: Commit**

```bash
git add task_tray.py tests/test_task_db.py
git commit -m "feat(task-tray): add TaskDB data layer with tests"
```

---

### Task 2: App Shell + System Tray Icon

**Files:**
- Modify: `task_tray.py` (add QApplication + QSystemTrayIcon)

**Step 1: Add tray icon setup**

After TaskDB class, add:

```python
import sys
from PyQt6.QtWidgets import QApplication, QSystemTrayIcon, QMenu
from PyQt6.QtGui import QIcon, QAction, QPixmap, QPainter, QColor, QFont
from PyQt6.QtCore import Qt


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


class TaskTrayApp:
    """Main application controller."""

    def __init__(self):
        self.app = QApplication(sys.argv)
        self.app.setQuitOnLastWindowClosed(False)
        self.db = TaskDB()

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
        menu.addSeparator()
        quit_action = QAction("Quit", menu)
        quit_action.triggered.connect(self.app.quit)
        menu.addAction(quit_action)
        self.tray.setContextMenu(menu)

        self.tray.show()
        self.popup = None
        self.full_window = None

    def _update_icon(self):
        summary = self.db.get_summary()
        pm = create_tray_icon_pixmap(summary["overdue"])
        self.tray.setIcon(QIcon(pm))

    def _tooltip(self):
        s = self.db.get_summary()
        return f"Tasks: {s['total']} | Overdue: {s['overdue']}"

    def _on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self._toggle_popup()

    def _toggle_popup(self):
        # Placeholder — Task 3 implements TrayPopup
        pass

    def _open_full(self):
        # Placeholder — Task 5 implements FullWindow
        pass

    def run(self):
        return self.app.exec()


def main():
    app = TaskTrayApp()
    sys.exit(app.run())


if __name__ == "__main__":
    main()
```

**Step 2: Manual test — run the app, verify tray icon appears**

```bash
cd ~/.claude/mcp_servers/sqlite_memory
python task_tray.py
```
Expected: Tray icon appears with checkmark. Right-click shows menu. "Quit" exits.

**Step 3: Commit**

```bash
git add task_tray.py
git commit -m "feat(task-tray): add system tray icon with context menu"
```

---

### Task 3: TrayPopup — Task Display

**Files:**
- Modify: `task_tray.py` (add TrayPopup class)

**Step 1: Implement TrayPopup widget**

Add before TaskTrayApp class:

```python
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QCheckBox, QLineEdit, QPushButton, QScrollArea, QFrame,
)
from PyQt6.QtCore import QTimer, QPoint


class TrayPopup(QWidget):
    """Compact popup showing Today + Overdue tasks."""

    def __init__(self, db, on_open_full, parent=None):
        super().__init__(parent, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self.db = db
        self.on_open_full = on_open_full
        self.setFixedWidth(380)
        self.setMaximumHeight(500)
        self.setStyleSheet(self._stylesheet())
        self._build_ui()

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
        # Clear existing
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
        row = QWidget()
        hl = QHBoxLayout(row)
        hl.setContentsMargins(14, 2, 14, 2)

        cb = QCheckBox(task["title"])
        cb.setChecked(task["status"] == "done")
        task_id = task["id"]
        cb.toggled.connect(lambda checked, tid=task_id: self._on_toggle(tid, checked))
        hl.addWidget(cb, 1)

        priority = (task.get("priority") or "medium").upper()
        colors = {"CRITICAL": "#e53e3e", "HIGH": "#dd6b20", "MEDIUM": "#2b6cb0", "LOW": "#718096"}
        plbl = QLabel(priority)
        plbl.setObjectName("priority")
        plbl.setStyleSheet(f"color: {colors.get(priority, '#718096')};")
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
        # Position above tray icon (Windows taskbar is typically at bottom)
        x = tray_geometry.x() - self.width() // 2
        y = tray_geometry.y() - self.height()
        # Clamp to screen
        screen = QApplication.primaryScreen().availableGeometry()
        x = max(screen.left(), min(x, screen.right() - self.width()))
        y = max(screen.top(), min(y, screen.bottom() - self.height()))
        self.move(QPoint(x, y))
        self.show()
        self.activateWindow()
```

**Step 2: Wire popup into TaskTrayApp**

Update `_toggle_popup` in TaskTrayApp:

```python
def _toggle_popup(self):
    if self.popup and self.popup.isVisible():
        self.popup.hide()
        return
    if not self.popup:
        self.popup = TrayPopup(self.db, self._open_full)
    geo = self.tray.geometry()
    self.popup.show_near_tray(geo)
```

**Step 3: Manual test**

```bash
python task_tray.py
```
Expected: Click tray icon -> popup shows Today + Overdue tasks. Checkbox marks done. Quick add works.

**Step 4: Commit**

```bash
git add task_tray.py
git commit -m "feat(task-tray): add compact popup with today/overdue display"
```

---

### Task 4: FullWindow — Tabbed Task Manager

**Files:**
- Modify: `task_tray.py` (add FullWindow class)

**Step 1: Implement FullWindow**

Add after TrayPopup class:

```python
from PyQt6.QtWidgets import (
    QMainWindow, QTabWidget, QListWidget, QListWidgetItem,
    QToolBar, QStatusBar, QDialog, QFormLayout, QComboBox,
    QDialogButtonBox, QMessageBox,
)


class TaskListWidget(QListWidget):
    """Custom list widget for tasks with checkbox + priority badge."""

    def __init__(self, db, parent=None):
        super().__init__(parent)
        self.db = db
        self.setStyleSheet("""
            QListWidget { background: #f0f4f8; border: none; }
            QListWidget::item { padding: 8px 12px; border-bottom: 1px solid #e2e8f0; }
            QListWidget::item:selected { background: #ebf8ff; }
        """)
        self.itemDoubleClicked.connect(self._on_double_click)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._context_menu)
        self._tasks = []

    def load_tasks(self, tasks):
        self._tasks = tasks
        self.clear()
        for task in tasks:
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, task["id"])
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                Qt.CheckState.Checked if task["status"] == "done"
                else Qt.CheckState.Unchecked
            )
            priority = (task.get("priority") or "medium").upper()
            due = f" | Due: {task['due_date']}" if task.get("due_date") else ""
            item.setText(f"[{priority}] {task['title']}{due}")
            self.addItem(item)

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
        delete_action = menu.addAction("Delete")
        action = menu.exec(self.mapToGlobal(pos))
        if action == delete_action:
            self.db.delete_task(task_id)


class EditTaskDialog(QDialog):
    """Dialog for editing task fields."""

    def __init__(self, task, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Edit Task")
        self.setMinimumWidth(350)
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
        due = self.due_edit.text().strip()
        if due:
            vals["due_date"] = due
        proj = self.project_edit.text().strip()
        if proj:
            vals["project"] = proj
        return vals


class FullWindow(QMainWindow):
    """Full task manager window with tabs."""

    def __init__(self, db, parent=None):
        super().__init__(parent)
        self.db = db
        self.setWindowTitle("Task Manager — SQLite Memory")
        self.resize(800, 600)
        self.setStyleSheet("""
            QMainWindow { background: #f0f4f8; }
            QTabWidget::pane { border: none; }
            QTabBar::tab { padding: 8px 20px; font-weight: bold; }
            QTabBar::tab:selected { background: #1a2332; color: white; }
        """)

        # Tabs
        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)

        self.tab_lists = {}
        for section in ("today", "inbox", "next", "all"):
            lw = TaskListWidget(self.db)
            lw.itemChanged.connect(lambda item, s=section: self._on_item_changed(item))
            self.tab_lists[section] = lw
            label = section.title() if section != "all" else "All"
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

        # Auto-refresh on focus
        self._refresh_timer = QTimer(self)
        self._refresh_timer.timeout.connect(self.refresh)
        self._refresh_timer.start(30000)  # 30s

        self.refresh()

    def refresh(self):
        for section, lw in self.tab_lists.items():
            if section == "all":
                tasks = self.db.get_tasks()
            else:
                tasks = self.db.get_tasks(section=section)
            lw.load_tasks(tasks)

        s = self.db.get_summary()
        self.status.showMessage(f"Tasks: {s['total']} | Overdue: {s['overdue']}")

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
        self.refresh()

    def closeEvent(self, event):
        event.ignore()
        self.hide()  # Hide don't quit
```

**Step 2: Wire into TaskTrayApp**

Update `_open_full`:

```python
def _open_full(self):
    if self.popup:
        self.popup.hide()
    if not self.full_window:
        self.full_window = FullWindow(self.db)
    self.full_window.show()
    self.full_window.raise_()
    self.full_window.activateWindow()
```

**Step 3: Manual test**

```bash
python task_tray.py
```
Expected: Tray -> popup -> "Open Full Window" -> full window with tabs. Double-click edits. Right-click deletes. Checkbox toggles done. Add task works.

**Step 4: Commit**

```bash
git add task_tray.py
git commit -m "feat(task-tray): add full window with tabs, edit, add, delete"
```

---

### Task 5: Auto-Refresh + Icon Badge + Polish

**Files:**
- Modify: `task_tray.py`

**Step 1: Add icon badge update after every DB write**

In TaskTrayApp, add a method and call it from popup/window callbacks:

```python
def _refresh_all(self):
    """Update tray icon badge + tooltip after any change."""
    self._update_icon()
    self.tray.setToolTip(self._tooltip())
    if self.popup and self.popup.isVisible():
        self.popup.refresh()
    if self.full_window and self.full_window.isVisible():
        self.full_window.refresh()
```

Wire into TaskDB by adding a callback pattern:

```python
# In TaskTrayApp.__init__, after creating db:
self.db.on_change = self._refresh_all
```

In TaskDB, add at end of add_task, mark_done, update_task, delete_task:

```python
if hasattr(self, 'on_change') and self.on_change:
    self.on_change()
```

**Step 2: Add startup shortcut info to tooltip**

Update tooltip to show overdue count prominently.

**Step 3: Manual test full workflow**

1. Start app -> tray icon appears
2. Click tray -> popup with today/overdue
3. Check a task -> icon badge updates
4. Quick add -> task appears
5. Open full -> tabs work, edit, delete
6. Close full -> hides to tray (doesn't quit)
7. Right-click tray -> Quit

**Step 4: Commit**

```bash
git add task_tray.py
git commit -m "feat(task-tray): add auto-refresh, icon badge, polish"
```

---

### Task 6: Final Tests + Cleanup

**Files:**
- Modify: `tests/test_task_db.py` (add edge case tests)

**Step 1: Add edge case tests**

```python
def test_mark_done_then_undo(self, db):
    tid = db.add_task("Toggle")
    db.mark_done(tid)
    db.update_task(tid, status="not_started")
    assert db.get_tasks()[0]["status"] == "not_started"

def test_add_task_to_each_section(self, db):
    for section in ("today", "inbox", "next", "waiting", "someday"):
        db.add_task(f"Task in {section}", section=section)
    assert len(db.get_tasks()) == 5

def test_hidden_statuses_filtered(self, db):
    db.add_task("Visible")
    tid = db.add_task("Hidden")
    db.update_task(tid, status="archived")
    assert len(db.get_tasks()) == 1
```

**Step 2: Run all tests**

```bash
PYTHONOPTIMIZE=0 python -m pytest tests/test_task_db.py -v --no-header
```
Expected: All pass.

**Step 3: Final commit**

```bash
git add -A
git commit -m "test(task-tray): add edge case tests for TaskDB"
```

---

## Summary

| Task | What | Est. Lines |
|------|------|-----------|
| 1 | TaskDB data layer + tests | ~120 |
| 2 | App shell + tray icon | ~80 |
| 3 | TrayPopup (today/overdue) | ~150 |
| 4 | FullWindow (tabs/edit/add) | ~200 |
| 5 | Auto-refresh + badge | ~30 |
| 6 | Edge case tests | ~20 |
| **Total** | | **~600** |

Single file: `task_tray.py` (~550 lines code) + `tests/test_task_db.py` (~80 lines)
