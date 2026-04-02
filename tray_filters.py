"""Filter/search mixin for FullWindow.

All methods here operate on the filter chips, search bar, and task filtering logic.
This is a mixin — it requires the following from the host class (FullWindow):

Qt widgets (set up in FullWindow.__init__):
    self._filter_bar:   QToolBar
    self._filter_chips: dict  — {(dimension, value): QToolButton}
    self.tabs:          QTabWidget
    self.db:            TaskDB

Instance attributes (set up in FullWindow.__init__):
    self._search_text:       str
    self._search_timer:      QTimer
    self._search_engine:     TaskSearchEngine
    self._pre_search_tab:    int | None
    self._active_filters:    dict[str, set]
    self._excluded_filters:  dict[str, set]
    self._minus_mode:        bool

Methods called on self (must exist on FullWindow):
    self._save_ui_state()
    self.refresh()

Module-level globals from tray_dialogs (accessed via import):
    PRIORITIES, _T, _font_size
"""

from datetime import date, timedelta

from PyQt6.QtWidgets import QToolButton

from db_utils import PRIORITY_COLORS, normalize_project_name, parse_iso_date
from tray_dialogs import PRIORITIES, _T, _font_size


class FilterMixin:
    """Filter chip and search methods for FullWindow. Mixin — requires Qt widgets and state."""

    # ── Search ───────────────────────────────────────────────────────────

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

    # ── Filter chips ─────────────────────────────────────────────────────

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
                f"QToolButton:checked {{ background: {used_color}; border-color: {used_color}; color: #fff; }}"
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
                f"QToolButton:checked {{ background: {used_color}; border-color: {used_color}; color: #fff; }}"
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
                f"QToolButton:checked {{ background: {used_color}; border-color: {used_color}; color: #fff; }}"
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

    # ── Filtering logic ──────────────────────────────────────────────────

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
            wanted = {
                normalized
                for normalized in (normalize_project_name(p) for p in af["project"])
                if normalized
            }
            tasks = [
                t for t in tasks if normalize_project_name(t.get("project")) in wanted
            ]

        # ── Exclude filters (remove matching) ──
        if ef["priority"]:
            tasks = [
                t for t in tasks if t.get("priority", "medium") not in ef["priority"]
            ]

        if ef["project"]:
            unwanted = {
                normalized
                for normalized in (normalize_project_name(p) for p in ef["project"])
                if normalized
            }
            tasks = [
                t
                for t in tasks
                if normalize_project_name(t.get("project")) not in unwanted
            ]

        return tasks
