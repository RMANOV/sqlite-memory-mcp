"""Read-only list widget for debate rows (BUILD STEP 1).

Implements the §2.0 read-only UI isolation contract of
BOARD-TO-NATIVE-TRAY-SPEC-2026-07-18.md. This is a *dedicated* widget, not a
flag on ``TaskListWidget``:

* it holds **no** ``db`` reference — structurally it cannot issue any DB write;
* it never connects ``itemChanged`` to a mutation callback (no checkbox);
* ``_on_double_click`` performs **in-app navigation only** (emits a signal) —
  never a task reader / ``_open_reader`` / mutation path;
* ``_context_menu`` exposes **only** Copy msg_id / Copy row / Open thread —
  never Convert / Recurring / Reminder / Publish / Link / Delete;
* every row carries the typed ``debate:<msg_id>`` ``UserRole`` namespace, which
  the task-side handlers additionally guard (defense in depth).

Rows are inert with respect to TaskDB: select / copy / double-click /
context-menu can never reach ``apply_task_mutation`` / ``update_task`` /
``mark_done`` / ``delete_task``. Falsified by the negative tests (spec T7).
"""
from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QGuiApplication
from PyQt6.QtWidgets import QAbstractItemView, QListWidget, QListWidgetItem, QMenu

# typed data roles (kept local; UserRole payload is always ``debate:<msg_id>``)
_ROLE_KEY = Qt.ItemDataRole.UserRole           # "debate:<msg_id>"
_ROLE_TOPIC = Qt.ItemDataRole.UserRole + 1     # topic_id for "open thread"
_ROLE_MSGID = Qt.ItemDataRole.UserRole + 2     # bare msg_id for "copy msg_id"
_ROLE_COPY = Qt.ItemDataRole.UserRole + 3      # formatted block for "copy row"


class DebateListWidget(QListWidget):
    """A strictly read-only list for debate/topic rows.

    Emits ``navigate_requested(topic_id_or_msg_id)`` for in-app navigation
    (e.g. jump to the ``topics`` tab on a ``topic_id``). It performs no DB
    access of any kind.
    """

    navigate_requested = pyqtSignal(str)  # topic_id (preferred) or msg_id

    def __init__(self, parent=None):
        super().__init__(parent)
        # NOTE: deliberately NO ``db`` attribute and NO ``itemChanged`` wiring.
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._context_menu)
        self.itemDoubleClicked.connect(self._on_double_click)

    # ---- population --------------------------------------------------------
    def clear_rows(self):
        self.clear()

    def add_header(self, text):
        item = QListWidgetItem(text)
        item.setFlags(Qt.ItemFlag.NoItemFlags)  # non-interactive
        self.addItem(item)
        return item

    def add_debate_row(self, msg_id, text, *, topic_id=None, copy_payload=None):
        """Add one read-only debate row.

        The row is selectable/copyable but NOT checkable/editable, so no
        interaction can toggle task state. ``UserRole`` is the typed
        ``debate:<msg_id>`` namespace.
        """
        item = QListWidgetItem(text)
        item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        item.setData(_ROLE_KEY, f"debate:{msg_id}")
        item.setData(_ROLE_MSGID, str(msg_id))
        if topic_id is not None:
            item.setData(_ROLE_TOPIC, str(topic_id))
        item.setData(_ROLE_COPY, copy_payload if copy_payload is not None else text)
        self.addItem(item)
        return item

    # ---- read-only interactions -------------------------------------------
    def _is_debate(self, item):
        data = item.data(_ROLE_KEY) if item is not None else None
        return isinstance(data, str) and data.startswith("debate:")

    def _on_double_click(self, item):
        # In-app navigation ONLY. No task reader, no mutation, no DB.
        if not self._is_debate(item):
            return
        target = item.data(_ROLE_TOPIC) or item.data(_ROLE_MSGID)
        if target:
            self.navigate_requested.emit(str(target))

    def _copy(self, text):
        cb = QGuiApplication.clipboard()
        if cb is not None and text:
            cb.setText(str(text))

    def _context_menu(self, pos):
        item = self.itemAt(pos)
        if not self._is_debate(item):
            return  # inert for anything that is not a debate row
        menu = QMenu(self)
        act_copy_id = menu.addAction("Copy msg_id")
        act_copy_row = menu.addAction("Copy row")
        act_open = menu.addAction("Open thread")
        chosen = menu.exec(self.mapToGlobal(pos))
        if chosen is None:
            return
        if chosen == act_copy_id:
            self._copy(item.data(_ROLE_MSGID))
        elif chosen == act_copy_row:
            self._copy(item.data(_ROLE_COPY))
        elif chosen == act_open:
            target = item.data(_ROLE_TOPIC) or item.data(_ROLE_MSGID)
            if target:
                self.navigate_requested.emit(str(target))
