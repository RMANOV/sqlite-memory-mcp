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
    def rebuild_index(self, tasks):
        pass

    def search(self, query, tasks, limit=20, conn=None, use_vector=False):
        return []


class _FakeTrayPopupDb(_FakeTaskDb):
    def __init__(self):
        self.search_engine = _FakeTraySearchEngine()
        self.created = []

    def promote_due_today(self):
        return []

    def get_suggested_tasks(self, limit=8):
        return []

    def get_all_active(self):
        return []

    def get_done_tasks(self):
        return []

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
