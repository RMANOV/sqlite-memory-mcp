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
