"""Read-only debate controls, list, and reader for the native tray.

The module intentionally owns no database handle.  It accepts already-read
records, applies the same client-side controls as the browser board, and emits
navigation/read signals only.  There is no task/debate mutation path here.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date

from PyQt6.QtCore import QSignalBlocker, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QGuiApplication, QKeySequence
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)


_ROLE_KEY = Qt.ItemDataRole.UserRole
_ROLE_TOPIC = Qt.ItemDataRole.UserRole + 1
_ROLE_MSGID = Qt.ItemDataRole.UserRole + 2
_ROLE_COPY = Qt.ItemDataRole.UserRole + 3
_ROLE_READER = Qt.ItemDataRole.UserRole + 4

_PRIORITY_ORDER = {
    "critical": 0,
    "high": 1,
    "medium": 2,
    "low": 3,
    "H": 0,
    "M": 1,
    "L": 2,
    "INFO": 3,
    "": 9,
}

_CONTROL_CONFIG = {
    "waiting": {
        "sorts": (
            ("ts", "Age", -1, "ts"),
            ("priority", "Priority", 1, "priority"),
            ("role", "Role", 1, "text"),
            ("kind", "Kind", 1, "text"),
        ),
        "filters": (
            ("priority", "Priority", ("H", "M", "L", "INFO")),
            ("role", "Role", None),
            ("kind", "Kind", None),
        ),
        "text_fields": ("role", "kind", "line", "body", "fwd"),
    },
    "recent": {
        "sorts": (
            ("ts", "Age", -1, "ts"),
            ("priority", "Priority", 1, "priority"),
            ("role", "Role", 1, "text"),
            ("kind", "Kind", 1, "text"),
        ),
        "filters": (
            ("priority", "Priority", ("H", "M", "L", "INFO")),
            ("role", "Role", None),
            ("kind", "Kind", None),
        ),
        "text_fields": ("role", "kind", "line", "body"),
    },
    "waiting_tasks": {
        "sorts": (
            ("due_date", "Due", 1, "date"),
            ("priority", "Priority", 1, "priority"),
            ("project", "Project", 1, "text"),
            ("section", "Section", 1, "text"),
            ("updated_at", "Updated", -1, "ts"),
        ),
        "filters": (
            ("priority", "Priority", ("critical", "high", "medium", "low")),
            ("project", "Project", None),
            ("section", "Section", ("today", "next")),
            ("due", "Due", ("overdue", "le7", "le21", "none")),
        ),
        "text_fields": ("title", "project", "section", "priority"),
    },
    "topics": {
        "sorts": (),
        "filters": (),
        "text_fields": ("title", "topic_id", "role", "kind", "line", "body"),
    },
}

_RECENT_KIND_PRESETS = (
    (("DECISION", "STATE", "STATUS"), "Decisions + statuses"),
    (("DECISION", "STATE", "STATUS", "A", "Q", "PING"), "All meaningful"),
    (("DECISION",), "Decisions only"),
    (("DECISION", "STATE"), "Decisions + states"),
    (("STATUS",), "Statuses only"),
)

_FILTER_VALUE_LABELS = {
    "overdue": "Overdue",
    "le7": "≤7d",
    "le21": "≤21d",
    "none": "No due date",
}


def _due_buckets(raw_due) -> set[str]:
    if not raw_due:
        return {"none"}
    try:
        due = date.fromisoformat(str(raw_due)[:10])
    except ValueError:
        return {"none"}
    diff = (due - date.today()).days
    buckets = set()
    if diff < 0:
        buckets.add("overdue")
    if 0 <= diff <= 7:
        buckets.add("le7")
    if 0 <= diff <= 21:
        buckets.add("le21")
    return buckets or {"future"}


def default_debate_control_params(key: str) -> dict:
    """Return browser-board-equivalent, JSON-serialisable control defaults."""
    config = _CONTROL_CONFIG.get(key, _CONTROL_CONFIG["topics"])
    sorts = config["sorts"]
    params = {
        "control_text": "",
        "control_sort": sorts[0][0] if sorts else "",
        "control_dir": sorts[0][2] if sorts else 1,
        "control_filters": {
            dimension: [] for dimension, _label, _options in config["filters"]
        },
    }
    if key == "recent":
        # The proven browser board defaults to a useful 24-hour window.
        params["hours"] = 24
        params["kinds"] = list(_RECENT_KIND_PRESETS[0][0])
    if key == "waiting":
        params["section_b_controls"] = default_debate_control_params("waiting_tasks")
    return params


def normalize_debate_control_params(key: str, params: dict | None) -> dict:
    """Validate persisted/native control state without retaining stale fields."""
    raw = dict(params or {})
    out = default_debate_control_params(key)
    config = _CONTROL_CONFIG.get(key, _CONTROL_CONFIG["topics"])
    allowed_sorts = {entry[0]: entry for entry in config["sorts"]}
    requested_sort = str(raw.get("control_sort") or "")
    if requested_sort in allowed_sorts:
        out["control_sort"] = requested_sort
    direction = raw.get("control_dir")
    if direction in (-1, 1):
        out["control_dir"] = int(direction)
    out["control_text"] = str(raw.get("control_text") or "")[:500]

    raw_filters = raw.get("control_filters")
    if not isinstance(raw_filters, dict):
        raw_filters = {}
    for dimension, _label, options in config["filters"]:
        values = raw_filters.get(dimension, [])
        if isinstance(values, str):
            values = [values]
        clean = sorted({str(v) for v in (values or []) if str(v)})
        if options is not None:
            allowed = set(options)
            clean = [value for value in clean if value in allowed]
        out["control_filters"][dimension] = clean

    if key == "recent":
        try:
            hours = int(raw.get("hours", out["hours"]))
        except (TypeError, ValueError):
            hours = out["hours"]
        out["hours"] = hours if hours in (1, 3, 6, 12, 24, 72, 168) else 24
        kinds = raw.get("kinds") or out["kinds"]
        if isinstance(kinds, str):
            kinds = [part.strip() for part in kinds.split(",")]
        allowed_kinds = {kind for preset, _ in _RECENT_KIND_PRESETS for kind in preset}
        clean_kinds = [str(kind) for kind in kinds if str(kind) in allowed_kinds]
        out["kinds"] = clean_kinds or list(_RECENT_KIND_PRESETS[0][0])
        # Migrate the earlier server-side role parameter to the visible client
        # filter instead of silently losing the operator's saved choice.
        legacy_role = str(raw.get("role") or "")
        if legacy_role and not out["control_filters"].get("role"):
            out["control_filters"]["role"] = [legacy_role]
    if key == "waiting":
        out["section_b_controls"] = normalize_debate_control_params(
            "waiting_tasks", raw.get("section_b_controls", {})
        )
    return out


def apply_debate_controls(key: str, items: list[dict], params: dict | None) -> list[dict]:
    """Apply the browser board's AND-across-dimensions, OR-within-dimension UI."""
    state = normalize_debate_control_params(key, params)
    config = _CONTROL_CONFIG.get(key, _CONTROL_CONFIG["topics"])
    filters = state["control_filters"]
    query = state["control_text"].strip().casefold()

    visible = []
    for item in items:
        rejected = False
        for dimension, selected in filters.items():
            if not selected:
                continue
            if dimension == "due":
                if not (_due_buckets(item.get("due_date")) & set(selected)):
                    rejected = True
                    break
            elif str(item.get(dimension) or "") not in set(selected):
                rejected = True
                break
        if rejected:
            continue
        if query:
            haystack = " ".join(
                str(item.get(field) or "") for field in config["text_fields"]
            ).casefold()
            if query not in haystack:
                continue
        visible.append(item)

    sort_key = state["control_sort"]
    sort_spec = next((entry for entry in config["sorts"] if entry[0] == sort_key), None)
    if sort_spec:
        sort_type = sort_spec[3]

        def value(item):
            raw = item.get(sort_key)
            if sort_type == "priority":
                return (_PRIORITY_ORDER.get(str(raw or ""), 99), "")
            if sort_type == "date":
                # Browser comparator uses a high sentinel so empty dates are
                # last for the default ascending order.
                return (1, "") if not raw else (0, str(raw).casefold())
            return (0, str(raw or "").casefold())

        visible = sorted(visible, key=value, reverse=state["control_dir"] < 0)
    return visible


class DebateControlsWidget(QWidget):
    """Visible native port of the browser board's per-view controls."""

    changed = pyqtSignal(object)
    back_requested = pyqtSignal()

    def __init__(self, key: str, parent=None):
        super().__init__(parent)
        self.setObjectName("debate-controls")
        self.key = key
        self._updating = False
        self._selected_filters: dict[str, set[str]] = {}
        self._filter_buttons: dict[str, QToolButton] = {}
        self._filter_options: dict[str, list[str]] = {}

        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(6)

        self.back_btn = QPushButton("← Topics")
        self.back_btn.setVisible(False)
        self.back_btn.clicked.connect(self.back_requested)
        layout.addWidget(self.back_btn)

        if key == "recent":
            layout.addWidget(QLabel("Period:"))
            self.hours_combo = QComboBox()
            for value, label in (
                (1, "1 hour"), (3, "3 hours"), (6, "6 hours"),
                (12, "12 hours"), (24, "24 hours"), (72, "3 days"),
                (168, "7 days"),
            ):
                self.hours_combo.addItem(label, value)
            self.hours_combo.currentIndexChanged.connect(self._emit_now)
            layout.addWidget(self.hours_combo)

            layout.addWidget(QLabel("Show:"))
            self.kinds_combo = QComboBox()
            for kinds, label in _RECENT_KIND_PRESETS:
                self.kinds_combo.addItem(label, list(kinds))
            self.kinds_combo.currentIndexChanged.connect(self._emit_now)
            layout.addWidget(self.kinds_combo)

        config = _CONTROL_CONFIG[key]
        if config["sorts"]:
            layout.addWidget(QLabel("Sort:"))
            self.sort_combo = QComboBox()
            for sort_key, label, default_dir, _sort_type in config["sorts"]:
                self.sort_combo.addItem(label, (sort_key, default_dir))
            self.sort_combo.currentIndexChanged.connect(self._on_sort_changed)
            layout.addWidget(self.sort_combo)
            self.direction_btn = QToolButton()
            self.direction_btn.setToolTip("Reverse sort direction")
            self.direction_btn.clicked.connect(self._toggle_direction)
            layout.addWidget(self.direction_btn)

        for dimension, label, options in config["filters"]:
            button = QToolButton()
            button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
            button.setText(f"{label}: All ▾")
            self._filter_buttons[dimension] = button
            self._selected_filters[dimension] = set()
            self._filter_options[dimension] = list(options or [])
            layout.addWidget(button)

        self.text_input = QLineEdit()
        self.text_input.setClearButtonEnabled(True)
        self.text_input.setPlaceholderText(
            "🔍 Filter by topic…" if key == "topics" else "🔍 Filter within results…"
        )
        self.text_input.setMinimumWidth(190)
        self.text_input.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        layout.addWidget(self.text_input, 1)

        self.count_label = QLabel("0 / 0")
        self.count_label.setMinimumWidth(58)
        layout.addWidget(self.count_label)

        self._text_timer = QTimer(self)
        self._text_timer.setSingleShot(True)
        self._text_timer.setInterval(150)
        self._text_timer.timeout.connect(self._emit_now)
        self.text_input.textChanged.connect(self._text_timer.start)
        self.set_state(default_debate_control_params(key))

    def _current_sort(self):
        if not hasattr(self, "sort_combo"):
            return "", 1
        data = self.sort_combo.currentData() or ("", 1)
        return str(data[0]), int(data[1])

    def state(self) -> dict:
        params = default_debate_control_params(self.key)
        params["control_text"] = self.text_input.text()
        if hasattr(self, "sort_combo"):
            sort_key, _default = self._current_sort()
            params["control_sort"] = sort_key
            params["control_dir"] = getattr(self, "_direction", 1)
        params["control_filters"] = {
            dimension: sorted(values)
            for dimension, values in self._selected_filters.items()
        }
        if self.key == "recent":
            params["hours"] = int(self.hours_combo.currentData())
            params["kinds"] = list(self.kinds_combo.currentData() or [])
        return normalize_debate_control_params(self.key, params)

    def set_state(self, params: dict | None):
        state = normalize_debate_control_params(self.key, params)
        self._updating = True
        blockers = [QSignalBlocker(self.text_input)]
        try:
            self.text_input.setText(state["control_text"])
            if hasattr(self, "sort_combo"):
                blockers.append(QSignalBlocker(self.sort_combo))
                idx = next(
                    (i for i in range(self.sort_combo.count())
                     if (self.sort_combo.itemData(i) or ("",))[0] == state["control_sort"]),
                    0,
                )
                self.sort_combo.setCurrentIndex(idx)
                self._direction = state["control_dir"]
                self._update_direction_label()
            if self.key == "recent":
                blockers.extend((QSignalBlocker(self.hours_combo), QSignalBlocker(self.kinds_combo)))
                hidx = self.hours_combo.findData(state["hours"])
                self.hours_combo.setCurrentIndex(max(0, hidx))
                wanted = tuple(state["kinds"])
                kidx = next(
                    (i for i in range(self.kinds_combo.count())
                     if tuple(self.kinds_combo.itemData(i) or []) == wanted),
                    0,
                )
                self.kinds_combo.setCurrentIndex(kidx)
            for dimension in self._selected_filters:
                self._selected_filters[dimension] = set(
                    state["control_filters"].get(dimension, [])
                )
                self._rebuild_filter_menu(dimension)
        finally:
            del blockers
            self._updating = False

    def set_available(self, items: list[dict]):
        """Refresh dynamic role/kind options without discarding selections."""
        config = _CONTROL_CONFIG[self.key]
        for dimension, _label, fixed in config["filters"]:
            if fixed is None:
                values = {
                    str(item.get(dimension) or "") for item in items
                    if str(item.get(dimension) or "")
                }
                values.update(self._selected_filters.get(dimension, set()))
                self._filter_options[dimension] = sorted(
                    values,
                    key=lambda value: (_PRIORITY_ORDER.get(value, 99), value.casefold()),
                )
            self._rebuild_filter_menu(dimension)

    def set_count(self, visible: int, total: int):
        self.count_label.setText(f"{visible} / {total}")

    def set_thread_mode(self, enabled: bool):
        self.back_btn.setVisible(bool(enabled) and self.key == "topics")

    def _rebuild_filter_menu(self, dimension: str):
        button = self._filter_buttons[dimension]
        menu = QMenu(button)
        clear_action = menu.addAction("All")
        clear_action.triggered.connect(
            lambda _checked=False, d=dimension: self._clear_filter(d)
        )
        menu.addSeparator()
        selected = self._selected_filters[dimension]
        for value in self._filter_options.get(dimension, []):
            action = menu.addAction(_FILTER_VALUE_LABELS.get(value, value))
            action.setCheckable(True)
            action.setChecked(value in selected)
            action.toggled.connect(
                lambda checked, d=dimension, v=value: self._toggle_filter(d, v, checked)
            )
        button.setMenu(menu)
        self._update_filter_button(dimension)

    def _update_filter_button(self, dimension: str):
        button = self._filter_buttons[dimension]
        selected = self._selected_filters[dimension]
        label = next(
            label for dim, label, _options in _CONTROL_CONFIG[self.key]["filters"]
            if dim == dimension
        )
        button.setText(
            f"{label}: {len(selected)} ▾" if selected else f"{label}: All ▾"
        )

    def _clear_filter(self, dimension: str):
        self._selected_filters[dimension].clear()
        self._rebuild_filter_menu(dimension)
        self._emit_now()

    def _toggle_filter(self, dimension: str, value: str, checked: bool):
        if checked:
            self._selected_filters[dimension].add(value)
        else:
            self._selected_filters[dimension].discard(value)
        # The action already carries the correct checked state. Replacing its
        # live menu from inside QAction.toggled is unsafe on some Qt builds;
        # only the summary label needs updating here.
        self._update_filter_button(dimension)
        self._emit_now()

    def _on_sort_changed(self):
        _key, default_direction = self._current_sort()
        self._direction = default_direction
        self._update_direction_label()
        self._emit_now()

    def _toggle_direction(self):
        self._direction = -getattr(self, "_direction", 1)
        self._update_direction_label()
        self._emit_now()

    def _update_direction_label(self):
        if hasattr(self, "direction_btn"):
            self.direction_btn.setText("▲" if getattr(self, "_direction", 1) > 0 else "▼")

    def _emit_now(self):
        if not self._updating:
            self.changed.emit(deepcopy(self.state()))


class DebateReaderDialog(QDialog):
    """Read/select/copy dialog for a message or read-only task record."""

    def __init__(self, payload: dict, parent=None):
        super().__init__(parent)
        self._payload = dict(payload or {})
        self.setWindowTitle(str(self._payload.get("title") or "Viewer"))
        self.resize(820, 560)

        layout = QVBoxLayout(self)
        tabs = QTabWidget()
        self.body_view = QPlainTextEdit()
        self.body_view.setReadOnly(True)
        self.body_view.setPlainText(str(self._payload.get("body") or ""))
        self.record_view = QPlainTextEdit()
        self.record_view.setReadOnly(True)
        self.record_view.setPlainText(str(self._payload.get("record") or ""))
        tabs.addTab(self.body_view, "Text")
        tabs.addTab(self.record_view, "Full record")
        layout.addWidget(tabs)

        buttons_row = QHBoxLayout()
        copy_body = QPushButton("Copy full text")
        copy_body.clicked.connect(self.copy_full_text)
        buttons_row.addWidget(copy_body)
        copy_record = QPushButton("Copy full record")
        copy_record.clicked.connect(self.copy_full_record)
        buttons_row.addWidget(copy_record)
        buttons_row.addStretch(1)
        close_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close_box.rejected.connect(self.reject)
        buttons_row.addWidget(close_box)
        layout.addLayout(buttons_row)

    @staticmethod
    def _copy(text: str):
        clipboard = QGuiApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(str(text or ""))

    def copy_full_text(self):
        self._copy(self._payload.get("body", ""))

    def copy_full_record(self):
        self._copy(self._payload.get("record", ""))


class DebateListWidget(QListWidget):
    """Strictly read-only list: selection, reading, copying, and navigation."""

    navigate_requested = pyqtSignal(str)
    reader_requested = pyqtSignal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("debate-list")
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._context_menu)
        self.itemDoubleClicked.connect(self._on_double_click)

    def clear_rows(self):
        self.clear()

    def add_header(self, text):
        item = QListWidgetItem(text)
        item.setFlags(Qt.ItemFlag.NoItemFlags)
        self.addItem(item)
        return item

    def add_debate_row(
        self,
        msg_id,
        text,
        *,
        topic_id=None,
        copy_payload=None,
        reader_payload=None,
    ):
        item = QListWidgetItem(text)
        item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        item.setData(_ROLE_KEY, f"debate:{msg_id}")
        item.setData(_ROLE_MSGID, str(msg_id))
        if topic_id is not None:
            item.setData(_ROLE_TOPIC, str(topic_id))
        item.setData(_ROLE_COPY, copy_payload if copy_payload is not None else text)
        if reader_payload is not None:
            item.setData(_ROLE_READER, dict(reader_payload))
        self.addItem(item)
        return item

    def _is_debate(self, item):
        data = item.data(_ROLE_KEY) if item is not None else None
        return isinstance(data, str) and data.startswith("debate:")

    def _on_double_click(self, item):
        if not self._is_debate(item):
            return
        reader = item.data(_ROLE_READER)
        if reader:
            self.reader_requested.emit(reader)
            return
        target = item.data(_ROLE_TOPIC) or item.data(_ROLE_MSGID)
        if target:
            self.navigate_requested.emit(str(target))

    @staticmethod
    def _copy(text):
        clipboard = QGuiApplication.clipboard()
        if clipboard is not None and text:
            clipboard.setText(str(text))

    def _copy_items(self, items):
        ordered = sorted((item for item in items if self._is_debate(item)), key=self.row)
        blocks = [str(item.data(_ROLE_COPY) or item.text() or "") for item in ordered]
        self._copy("\n\n".join(block for block in blocks if block))

    def copy_selected(self):
        selected = self.selectedItems()
        self._copy_items(selected if selected else [self.item(i) for i in range(self.count())])

    def copy_all_visible(self):
        self._copy_items([self.item(i) for i in range(self.count())])

    def keyPressEvent(self, event):
        if event.matches(QKeySequence.StandardKey.Copy):
            self.copy_selected()
            event.accept()
            return
        super().keyPressEvent(event)

    def _context_menu(self, pos):
        item = self.itemAt(pos)
        if not self._is_debate(item):
            return
        menu = QMenu(self)
        reader = item.data(_ROLE_READER)
        act_open = menu.addAction("Open and read") if reader else None
        act_copy_id = menu.addAction("Copy ID")
        act_copy_row = menu.addAction("Copy full record")
        act_copy_selected = menu.addAction("Copy selected")
        act_copy_all = menu.addAction("Copy all visible")
        target = item.data(_ROLE_TOPIC)
        act_thread = menu.addAction("Open thread") if target else None
        chosen = menu.exec(self.mapToGlobal(pos))
        if chosen is None:
            return
        if chosen == act_open:
            self.reader_requested.emit(reader)
        elif chosen == act_copy_id:
            self._copy(item.data(_ROLE_MSGID))
        elif chosen == act_copy_row:
            self._copy(item.data(_ROLE_COPY))
        elif chosen == act_copy_selected:
            self.copy_selected()
        elif chosen == act_copy_all:
            self.copy_all_visible()
        elif chosen == act_thread and target:
            self.navigate_requested.emit(str(target))


class DebateTabWidget(QWidget):
    """A compact controls + read-only list page for one native debate tab."""

    def __init__(self, key: str, parent=None):
        super().__init__(parent)
        self.setObjectName("debate-page")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.controls = DebateControlsWidget(key, self)
        self.list_widget = DebateListWidget(self)
        self.secondary_controls = None
        self.secondary_list = None
        if key != "waiting":
            layout.addWidget(self.controls)
            layout.addWidget(self.list_widget, 1)
            return

        # The proven browser view has two independently controlled sections:
        # A) operator asks and B) actionable today/next tasks.  A vertical
        # splitter keeps both useful on a native desktop without conflating
        # their distinct filter/sort state.
        splitter = QSplitter(Qt.Orientation.Vertical, self)
        splitter.setObjectName("debate-splitter")
        upper = QWidget(splitter)
        upper.setObjectName("debate-section")
        upper_layout = QVBoxLayout(upper)
        upper_layout.setContentsMargins(0, 0, 0, 0)
        upper_layout.setSpacing(0)
        upper_layout.addWidget(self.controls)
        upper_layout.addWidget(self.list_widget, 1)
        splitter.addWidget(upper)

        lower = QWidget(splitter)
        lower.setObjectName("debate-section")
        lower_layout = QVBoxLayout(lower)
        lower_layout.setContentsMargins(0, 0, 0, 0)
        lower_layout.setSpacing(0)
        self.secondary_controls = DebateControlsWidget("waiting_tasks", lower)
        self.secondary_list = DebateListWidget(lower)
        lower_layout.addWidget(self.secondary_controls)
        lower_layout.addWidget(self.secondary_list, 1)
        splitter.addWidget(lower)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter, 1)
