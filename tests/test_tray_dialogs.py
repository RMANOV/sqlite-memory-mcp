import os
import sqlite3
import sys

import pytest
from PyQt6.QtWidgets import QApplication

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import tray_dialogs


class _BrokenTruthScoreConn:
    def execute(self, sql, params=()):
        raise sqlite3.OperationalError("knowledge_ratings unavailable")


class _BrokenTruthScoreConnCtx:
    def __enter__(self):
        return _BrokenTruthScoreConn()

    def __exit__(self, exc_type, exc_val, exc_tb):
        return False


class _FakeTaskDb:
    db_path = ":memory:"

    def get_project_names(self):
        return ["general"]

    def get_task_attachments(self, task_id, include_removed=False):
        return []

    def get_task_links(self, task_id):
        return []

    def resolve_attachment_path(self, attachment):
        return None


class _FakeTraySearchEngine:
    def __init__(self):
        self.indexed = []

    def rebuild_index(self, tasks):
        self.indexed = tasks

    def search(self, query, tasks, limit=20, conn=None, use_vector=False):
        return []


class _FakeTrayPopupDb(_FakeTaskDb):
    def __init__(self):
        self.search_engine = _FakeTraySearchEngine()
        self.created = []
        self.active = []
        self.done = []

    def promote_due_today(self):
        return []

    def get_suggested_tasks(self, limit=8):
        return []

    def get_all_active(self):
        return self.active

    def get_done_tasks(self):
        return self.done

    def search_entities_fast(self, query, limit=5):
        return []

    def add_task(self, title, **kwargs):
        self.created.append((title, kwargs))
        return "task-created"


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_batch_truth_scores_returns_stale_cache_on_sqlite_error(monkeypatch):
    stale_cache = {"EntityA": "🟡 "}
    monkeypatch.setattr(tray_dialogs, "_ts_cache", stale_cache.copy())
    monkeypatch.setattr(tray_dialogs, "_ts_cache_time", 0.0)
    monkeypatch.setattr(
        tray_dialogs,
        "get_conn",
        lambda db_path=None: _BrokenTruthScoreConnCtx(),
    )

    assert tray_dialogs._batch_truth_scores() == stale_cache


def test_should_render_context_preview_hides_weak_executor_pack():
    assert (
        tray_dialogs._should_render_context_preview(
            {
                "items_included": 1,
                "body": "## Context Fragments\nweak raw chunk",
                "previewable": False,
            }
        )
        is False
    )


def test_should_render_context_preview_allows_strong_executor_pack():
    assert (
        tray_dialogs._should_render_context_preview(
            {
                "items_included": 1,
                "body": "## Canonical Facts\nhigh-confidence fact",
                "previewable": True,
            }
        )
        is True
    )


def test_format_task_text_falls_back_to_notes_when_description_missing():
    text = tray_dialogs._format_task_text(
        {
            "title": "Task",
            "priority": "medium",
            "notes": "Internal preview",
            "description": None,
        }
    )

    assert "[notes] Internal preview" in text


def test_build_rich_tooltip_includes_notes():
    tooltip = tray_dialogs._build_rich_tooltip(
        {
            "title": "Task",
            "description": "Main body",
            "notes": "Internal details",
            "priority": "high",
        }
    )

    assert "Main body" in tooltip
    assert "Notes: Internal details" in tooltip


def test_edit_task_dialog_roundtrips_notes(qapp):
    dlg = tray_dialogs.EditTaskDialog(
        {
            "title": "Task",
            "description": "Main body",
            "notes": "Internal details",
            "type": "task",
            "status": "not_started",
            "section": "inbox",
            "priority": "medium",
            "project": "general",
        },
        db=_FakeTaskDb(),
    )

    vals = dlg.get_values()

    assert vals["description"] == "Main body"
    assert vals["notes"] == "Internal details"
    dlg.close()


def test_task_reader_title_and_body_copy_exact_mixed_text_without_mutation(qapp):
    from PyQt6.QtCore import Qt
    from PyQt6.QtTest import QTest

    title = "Дълго BG заглавие / mixed ASCII-Cyr 123 — exact"
    description = "Първи ред на български.\nSecond ASCII line: []{} / exact."
    task = {
        "id": "task-copy-demo",
        "title": title,
        "description": description,
        "notes": None,
        "status": "not_started",
        "priority": "critical",
        "section": "today",
    }
    before = task.copy()
    dlg = tray_dialogs.TaskReaderDialog(task, _FakeTaskDb())
    dlg.show()
    qapp.processEvents()

    required = (
        Qt.TextInteractionFlag.TextSelectableByMouse
        | Qt.TextInteractionFlag.TextSelectableByKeyboard
    )
    assert dlg._title_label.textInteractionFlags() & required == required
    assert dlg._body_label.textInteractionFlags() & required == required

    dlg._title_label.setFocus()
    QTest.keyClick(dlg._title_label, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
    QTest.keyClick(dlg._title_label, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
    qapp.processEvents()
    assert qapp.clipboard().text() == title

    dlg._body_label.setFocus()
    QTest.keyClick(dlg._body_label, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
    QTest.keyClick(dlg._body_label, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
    qapp.processEvents()
    assert qapp.clipboard().text() == description
    assert task == before
    dlg.close()


def test_reminder_copy_shortcuts_are_read_only_and_buttons_still_work(qapp):
    from PyQt6.QtCore import Qt
    from PyQt6.QtTest import QTest
    from PyQt6.QtWidgets import QLabel, QPushButton

    dlg = tray_dialogs.ReminderPopupDialog(
        "task-reminder",
        "Напомняне mixed 42",
        "critical",
        "Описание BG / ASCII",
    )
    snoozed = []
    dismissed = []
    dlg.snoozed.connect(lambda task_id, minutes: snoozed.append((task_id, minutes)))
    dlg.dismissed.connect(dismissed.append)

    required = (
        Qt.TextInteractionFlag.TextSelectableByMouse
        | Qt.TextInteractionFlag.TextSelectableByKeyboard
    )
    labels = dlg.findChildren(QLabel)
    assert len(labels) == 3
    assert all(label.textInteractionFlags() & required == required for label in labels)

    dlg.show()
    dlg.activateWindow()
    qapp.processEvents()
    title_label = next(
        label for label in labels if label.text() == "Напомняне mixed 42"
    )
    title_label.setSelection(0, len("Напомняне"))
    title_label.setFocus()
    QTest.keyClick(title_label, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
    qapp.processEvents()
    assert qapp.clipboard().text() == "Напомняне"

    qapp.clipboard().setText("unchanged until copy")
    QTest.keyClick(title_label, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
    qapp.processEvents()
    assert qapp.clipboard().text() == "unchanged until copy"
    assert all(label.selectedText() == label.text() for label in labels)
    QTest.keyClick(title_label, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
    qapp.processEvents()
    assert qapp.clipboard().text() == dlg._copy_payload
    assert snoozed == []
    assert dismissed == []
    dlg.close()

    snooze_dlg = tray_dialogs.ReminderPopupDialog(
        "task-snooze", "Snooze", "high", "body"
    )
    snooze_seen = []
    snooze_dlg.snoozed.connect(
        lambda task_id, minutes: snooze_seen.append((task_id, minutes))
    )
    snooze_buttons = {
        button.text(): button for button in snooze_dlg.findChildren(QPushButton)
    }
    snooze_buttons["Snooze 5 min"].click()
    qapp.processEvents()
    assert snooze_seen == [("task-snooze", 5)]

    dismiss_dlg = tray_dialogs.ReminderPopupDialog(
        "task-dismiss", "Dismiss", "medium", "body"
    )
    dismiss_seen = []
    dismiss_dlg.dismissed.connect(dismiss_seen.append)
    dismiss_buttons = {
        button.text(): button for button in dismiss_dlg.findChildren(QPushButton)
    }
    dismiss_buttons["Dismiss"].click()
    qapp.processEvents()
    assert dismiss_seen == ["task-dismiss"]


def test_edit_task_dialog_roundtrips_reminder_and_recurring(qapp):
    recurring = '{"every":"week","day":"monday"}'
    dlg = tray_dialogs.EditTaskDialog(
        {
            "title": "Task",
            "description": "Main body",
            "notes": "Internal details",
            "type": "task",
            "status": "not_started",
            "section": "inbox",
            "priority": "medium",
            "project": "general",
            "reminder_at": "2026-04-25T15:30:00+00:00",
            "recurring": recurring,
        },
        db=_FakeTaskDb(),
    )

    vals = dlg.get_values()

    assert vals["reminder_at"] == "2026-04-25T15:30:00+00:00"
    assert vals["recurring"] == recurring
    dlg.close()


def test_edit_task_dialog_can_clear_reminder_and_recurring(qapp):
    dlg = tray_dialogs.EditTaskDialog(
        {
            "title": "Task",
            "type": "task",
            "status": "not_started",
            "section": "inbox",
            "priority": "medium",
            "project": "general",
            "reminder_at": "2026-04-25T15:30:00+00:00",
            "recurring": '{"every":"day"}',
        },
        db=_FakeTaskDb(),
    )

    dlg._clear_reminder()
    dlg._clear_recurring()
    vals = dlg.get_values()

    assert vals["reminder_at"] is None
    assert vals["recurring"] is None
    dlg.close()


def test_tray_popup_add_form_can_create_note_with_reminder(qapp):
    db = _FakeTrayPopupDb()
    popup = tray_dialogs.TrayPopup(db, lambda: None)

    popup._add_title.setText("Reminder note")
    popup._add_type.setCurrentText("Note")
    popup._apply_add_reminder(60)
    popup._submit_task()

    assert len(db.created) == 1
    title, kwargs = db.created[0]
    assert title == "Reminder note"
    assert kwargs["type"] == "note"
    assert kwargs["reminder_at"].endswith("+00:00")
    assert popup._add_reminder_enabled.isChecked() is False
    popup.close()


def test_tray_popup_search_uses_bounded_lightweight_index(qapp, monkeypatch):
    monkeypatch.setattr(tray_dialogs, "_POPUP_SEARCH_INDEX_LIMIT", 2)
    monkeypatch.setattr(tray_dialogs, "_POPUP_INDEX_TEXT_CHARS", 5)
    db = _FakeTrayPopupDb()
    db.active = [
        {"id": "a", "title": "needle", "description": "0123456789"},
        {"id": "b", "title": "needle two", "notes": "abcdefghij"},
        {"id": "c", "title": "needle three"},
    ]
    popup = tray_dialogs.TrayPopup(db, lambda: None)
    popup._search_text = "needle"

    popup.refresh()

    assert [row["id"] for row in db.search_engine.indexed] == ["a", "b"]
    assert db.search_engine.indexed[0]["description"] == "01234"
    assert db.search_engine.indexed[1]["notes"] == "abcde"
    popup.close()


def test_entity_dialogs_use_shared_light_theme(qapp, monkeypatch):
    monkeypatch.setattr(tray_dialogs, "_theme_name", "light")
    monkeypatch.setattr(
        tray_dialogs.EntityDetailDialog, "_load_data", lambda self: None
    )

    link_dialog = tray_dialogs.EntityLinkDialog(_FakeTaskDb(), "task-1")
    detail_dialog = tray_dialogs.EntityDetailDialog(_FakeTaskDb(), 1)

    expected = tray_dialogs._build_entity_dialog_style()
    assert link_dialog.styleSheet() == expected
    assert detail_dialog.styleSheet() == expected
    assert tray_dialogs._T()["bg"] in expected
    assert "#0d1117" not in expected

    link_dialog.close()
    detail_dialog.close()


def test_entity_detail_rich_text_uses_active_theme(qapp, monkeypatch):
    monkeypatch.setattr(tray_dialogs, "_theme_name", "light")
    monkeypatch.setattr(
        tray_dialogs.EntityDetailDialog, "_load_data", lambda self: None
    )
    dialog = tray_dialogs.EntityDetailDialog(_FakeTaskDb(), 1)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        "CREATE TABLE entities (id INTEGER PRIMARY KEY, name TEXT, entity_type TEXT);"
        "CREATE TABLE observations (id INTEGER PRIMARY KEY, entity_id INTEGER, "
        "content TEXT, created_at TEXT);"
        "CREATE TABLE relations (from_id INTEGER, to_id INTEGER, relation_type TEXT);"
        "INSERT INTO entities VALUES (1, 'Entity A', 'concept');"
        "INSERT INTO observations VALUES (1, 1, 'Theme-aware fact', '2026-07-22');"
    )

    dialog._load_data_inner(conn)
    html = dialog._content.text()

    assert tray_dialogs._T()["text"] in html
    assert tray_dialogs._T()["bg2"] in html
    assert "#e6edf3" not in html
    assert "#161b22" not in html
    conn.close()
    dialog.close()


def test_task_reader_dialog_renders_notes_section(qapp, monkeypatch):
    monkeypatch.setattr(
        tray_dialogs,
        "get_conn",
        lambda db_path=None: _BrokenTruthScoreConnCtx(),
    )
    dlg = tray_dialogs.TaskReaderDialog(
        {
            "id": "task-1",
            "title": "Task",
            "description": "",
            "notes": "Internal details",
            "priority": "medium",
            "section": "inbox",
        },
        _FakeTaskDb(),
    )

    body_html = dlg._body_label.text()

    assert "Notes" in body_html
    assert "Internal details" in body_html
    dlg.close()


# ── v3.9.1: Mark-Done checkbox + 5s optimistic undo ────────────────────


class _FakeTaskDbWithStatus(_FakeTaskDb):
    """FakeTaskDb extension that records mark_done/update_task calls."""

    def __init__(self):
        self.mark_done_calls = []
        self.update_calls = []

    def mark_done(self, task_id):
        self.mark_done_calls.append(task_id)
        return True

    def update_task(self, task_id, **fields):
        self.update_calls.append((task_id, fields))
        return True


def _make_reader(qapp, monkeypatch, status="not_started"):
    monkeypatch.setattr(
        tray_dialogs,
        "get_conn",
        lambda db_path=None: _BrokenTruthScoreConnCtx(),
    )
    db = _FakeTaskDbWithStatus()
    dlg = tray_dialogs.TaskReaderDialog(
        {
            "id": "task-mark-1",
            "title": "Reader test",
            "description": "body",
            "priority": "medium",
            "section": "inbox",
            "status": status,
        },
        db,
    )
    return dlg, db


def test_reader_checkbox_initially_unchecked_for_not_started(qapp, monkeypatch):
    dlg, _db = _make_reader(qapp, monkeypatch, status="not_started")
    assert dlg._done_checkbox.isChecked() is False
    assert dlg._undo_banner.isHidden()
    assert dlg._pending_done is False
    dlg.close()


def test_reader_checkbox_initially_checked_for_done_status(qapp, monkeypatch):
    dlg, _db = _make_reader(qapp, monkeypatch, status="done")
    assert dlg._done_checkbox.isChecked() is True
    assert dlg._undo_banner.isHidden()  # no pending action; just reflects state
    assert dlg._pending_done is False
    dlg.close()


def test_reader_check_arms_timer_and_shows_banner_no_db_yet(qapp, monkeypatch):
    dlg, db = _make_reader(qapp, monkeypatch)
    dlg._done_checkbox.setChecked(True)
    assert dlg._pending_done is True
    # isVisible() needs full window chain shown; isHidden() reads the
    # explicit-hidden flag and is correct for headless tests.
    assert dlg._undo_banner.isHidden() is False
    assert dlg._undo_timer.isActive()
    # No DB write yet — that's the optimistic-UI contract.
    assert db.mark_done_calls == []
    dlg.close()


def test_reader_undo_within_window_cancels_timer_zero_writes(qapp, monkeypatch):
    dlg, db = _make_reader(qapp, monkeypatch)
    dlg._done_checkbox.setChecked(True)
    assert dlg._undo_timer.isActive()
    dlg._on_undo_clicked()
    assert dlg._pending_done is False
    assert dlg._undo_timer.isActive() is False
    assert dlg._done_checkbox.isChecked() is False
    assert dlg._undo_banner.isHidden()
    assert db.mark_done_calls == []  # zero DB writes on undo
    dlg.close()


def test_reader_timer_fire_writes_mark_done_once(qapp, monkeypatch):
    dlg, db = _make_reader(qapp, monkeypatch)
    dlg._done_checkbox.setChecked(True)
    # Simulate timer fire without waiting 5s — call the slot directly.
    dlg._commit_mark_done()
    assert dlg._pending_done is False
    assert dlg._undo_banner.isHidden()
    assert dlg._done_checkbox.isChecked() is True  # commit keeps checked
    assert db.mark_done_calls == ["task-mark-1"]
    assert dlg.task["status"] == "done"
    dlg.close()


def test_reader_close_while_pending_commits_immediately(qapp, monkeypatch):
    dlg, db = _make_reader(qapp, monkeypatch)
    dlg._done_checkbox.setChecked(True)
    assert dlg._pending_done is True
    dlg.close()
    # closeEvent path should commit the pending mark-done.
    assert db.mark_done_calls == ["task-mark-1"]


def test_reader_uncheck_already_done_writes_revert_immediately(qapp, monkeypatch):
    dlg, db = _make_reader(qapp, monkeypatch, status="done")
    assert dlg._done_checkbox.isChecked() is True
    dlg._done_checkbox.setChecked(False)
    # Reverting an already-done task is NOT undoable — writes immediately.
    assert db.update_calls == [("task-mark-1", {"status": "not_started"})]
    assert dlg._pending_done is False
    dlg.close()


def test_reader_double_check_while_pending_is_noop(qapp, monkeypatch):
    """Defensive: re-toggling the checkbox while a timer is pending must
    not stack timers or restart the window."""
    dlg, db = _make_reader(qapp, monkeypatch)
    dlg._done_checkbox.setChecked(True)
    first_active = dlg._undo_timer.isActive()
    # Force-set checked=True again via the slot (real Qt would suppress
    # the duplicate signal, but defensive code path still must be safe).
    dlg._on_done_toggled(True)
    assert dlg._undo_timer.isActive() == first_active
    assert dlg._pending_done is True
    assert db.mark_done_calls == []
    dlg.close()


def test_reader_edit_while_pending_commits_before_dialog_opens(qapp, monkeypatch):
    """When user clicks Edit while a 5s mark-done is pending, the
    pending action MUST commit to DB BEFORE EditTaskDialog opens.

    Avoids ambiguity between the auto-mark-done timer and the explicit
    edit intent — the user pressing Edit is treated as 'I'm done with
    this dialog state' (matches close-while-pending behavior).
    """
    dlg, db = _make_reader(qapp, monkeypatch)
    dlg._done_checkbox.setChecked(True)
    assert dlg._pending_done is True
    assert dlg._undo_timer.isActive()

    edit_open_state = {}

    class _FakeEditDialog:
        def __init__(self, *args, **kwargs):
            # Capture mark_done call state at the exact moment Edit opens.
            edit_open_state["mark_done_calls_at_open"] = list(db.mark_done_calls)

        def exec(self):
            from PyQt6.QtWidgets import QDialog

            return QDialog.DialogCode.Rejected

    monkeypatch.setattr(tray_dialogs, "EditTaskDialog", _FakeEditDialog)

    dlg._on_edit()

    # The single mark_done call must have landed BEFORE the edit dialog
    # constructor ran — i.e., commit-then-open ordering, not vice versa.
    assert edit_open_state["mark_done_calls_at_open"] == ["task-mark-1"]
    assert db.mark_done_calls == ["task-mark-1"]  # exactly one, no duplicate
    assert dlg._pending_done is False
    assert dlg._undo_timer.isActive() is False
    dlg.close()
