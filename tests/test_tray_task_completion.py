"""Native completion controls for task/note records in the three new tabs."""

from __future__ import annotations

import os
import sqlite3

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def qapp():
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


def test_trailing_checkbox_emits_exact_payload_at_row_end(qapp):
    from PyQt6.QtCore import Qt
    from PyQt6.QtTest import QTest
    from PyQt6.QtWidgets import QStyleOptionViewItem
    from debate_list_widget import DebateListWidget

    payload = {
        "id": "task-1",
        "type": "task",
        "expected_status": "not_started",
        "expected_order": 42,
        "expected_event_id": "event-42",
    }
    widget = DebateListWidget()
    widget.resize(720, 120)
    item = widget.add_task_row(
        "task-1",
        "A task that can be completed",
        copy_payload="full task record",
        reader_payload={"title": "Task", "body": "Body", "record": "Record"},
        completion_payload=payload,
    )
    seen = []
    widget.task_completion_requested.connect(
        lambda value, checked: seen.append((value, checked))
    )
    widget.show()
    qapp.processEvents()
    try:
        index = widget.indexFromItem(item)
        option = QStyleOptionViewItem()
        option.widget = widget
        option.rect = widget.visualRect(index)
        rect = widget.itemDelegate().check_rect(option, index)
        assert rect.isValid()
        assert rect.center().x() > option.rect.center().x(), "checkbox must trail text"

        QTest.mouseClick(
            widget.viewport(), Qt.MouseButton.LeftButton, pos=rect.center()
        )
        qapp.processEvents()
        assert item.checkState() == Qt.CheckState.Checked
        assert seen == [(payload, True)]
    finally:
        widget.close()


def test_debate_rows_stay_readonly_while_task_rows_keep_reader_and_copy(qapp):
    from PyQt6.QtCore import Qt
    from debate_list_widget import DebateListWidget

    widget = DebateListWidget()
    debate = widget.add_debate_row(
        "msg-1",
        "debate row",
        copy_payload="full debate",
        reader_payload={"title": "Debate", "body": "D", "record": "DR"},
    )
    task = widget.add_task_row(
        "task-1",
        "task row",
        copy_payload="full task",
        reader_payload={"title": "Task", "body": "T", "record": "TR"},
        completion_payload={"id": "task-1"},
    )
    assert not (debate.flags() & Qt.ItemFlag.ItemIsUserCheckable)
    assert task.flags() & Qt.ItemFlag.ItemIsUserCheckable

    opened = []
    widget.reader_requested.connect(lambda payload: opened.append(payload["title"]))
    widget._on_double_click(task)
    assert opened == ["Task"]
    task.setSelected(True)
    widget.copy_selected()
    assert qapp.clipboard().text() == "full task"
    widget.close()


def _status(db_path, task_id):
    con = sqlite3.connect(db_path)
    try:
        return con.execute(
            "SELECT status FROM tasks WHERE id=?", (task_id,)
        ).fetchone()[0]
    finally:
        con.close()


def _status_event_count(db_path, task_id):
    con = sqlite3.connect(db_path)
    try:
        return con.execute(
            "SELECT COUNT(*) FROM memory_events "
            "WHERE aggregate_kind='task' AND aggregate_id=? AND field_name='status'",
            (task_id,),
        ).fetchone()[0]
    finally:
        con.close()


def test_fullwindow_exposes_controls_in_all_three_tabs_and_completes_note(
    qapp, tmp_path, monkeypatch
):
    import task_tray
    from PyQt6.QtCore import QSettings, Qt
    from db_utils import create_task_with_ledger, get_conn_immediate
    from debate_list_widget import _ROLE_COMPLETION, _ROLE_KEY
    from schema import init_db

    db_path = str(tmp_path / "tray-completion.db")
    init_db(db_path)
    with get_conn_immediate(db_path) as con:
        create_task_with_ledger(
            con,
            "task-open",
            "completionprobe task",
            "2026-07-19T20:00:00+00:00",
            status="not_started",
            type="task",
            section="today",
            priority="high",
            actor_id="test",
        )
        create_task_with_ledger(
            con,
            "note-open",
            "completionprobe note",
            "2026-07-19T20:00:01+00:00",
            status="in_progress",
            type="note",
            section="today",
            priority="high",
            actor_id="test",
        )

    ini = str(tmp_path / "tray.ini")
    monkeypatch.setattr(
        task_tray,
        "QSettings",
        lambda *args, **kwargs: QSettings(ini, QSettings.Format.IniFormat),
    )
    monkeypatch.setattr(
        task_tray.FullWindow,
        "_restore_profile_from_bridge",
        lambda self: None,
        raising=False,
    )

    db = task_tray.TaskDB(db_path)
    window = task_tray.FullWindow(db, sync_host=None)
    try:
        # Global task/note search is rendered in each of the three new tabs.
        window._search_text = "completionprobe"
        window._debate_source_cache.clear()
        window._filtered_cache.clear()
        for key in window._DEBATE_TABS:
            rows = window._build_debate_rows(key)
            window._load_debate_tab(key, rows)
            list_widget = window.tab_lists[key]
            task_rows = [
                list_widget.item(i)
                for i in range(list_widget.count())
                if str(list_widget.item(i).data(_ROLE_KEY) or "").startswith("task:")
            ]
            assert {row.data(_ROLE_KEY) for row in task_rows} == {
                "task:task-open",
                "task:note-open",
            }
            assert all(
                row.flags() & Qt.ItemFlag.ItemIsUserCheckable for row in task_rows
            )

        # Waiting on Me section B uses the same control outside search.
        window._search_text = ""
        window._debate_source_cache.clear()
        window._filtered_cache.clear()
        window._load_tab("waiting")
        note_item = next(
            window._waiting_task_list.item(i)
            for i in range(window._waiting_task_list.count())
            if window._waiting_task_list.item(i).data(_ROLE_KEY) == "task:note-open"
        )
        payload = note_item.data(_ROLE_COMPLETION)
        assert payload["type"] == "note"
        before_events = _status_event_count(db_path, "note-open")

        window._apply_debate_task_completion(dict(payload))

        assert _status(db_path, "note-open") == "done"
        assert _status(db_path, "task-open") == "not_started"
        assert _status_event_count(db_path, "note-open") == before_events + 1
        window._load_tab("waiting")
        remaining = {
            window._waiting_task_list.item(i).data(_ROLE_KEY)
            for i in range(window._waiting_task_list.count())
        }
        assert "task:note-open" not in remaining
        assert "task:task-open" in remaining
    finally:
        if getattr(window, "_debate_dao", None) is not None:
            window._debate_dao.close()
        window.close()
        db._conn.close()


def test_stale_completion_token_preserves_foreign_change(tmp_path):
    from db_utils import apply_task_mutation, create_task_with_ledger, get_conn_immediate
    from task_status_cas import StatusToken, status_token, transition_status
    from schema import init_db

    db_path = str(tmp_path / "stale.db")
    init_db(db_path)
    with get_conn_immediate(db_path) as con:
        create_task_with_ledger(
            con,
            "stale-note",
            "stale note",
            "2026-07-19T20:00:00+00:00",
            status="not_started",
            type="note",
            actor_id="test",
        )
    con = sqlite3.connect(db_path)
    try:
        token = status_token(con, "stale-note")
    finally:
        con.close()
    assert isinstance(token, StatusToken)
    with get_conn_immediate(db_path) as con:
        apply_task_mutation(
            con,
            "stale-note",
            {"status": "in_progress"},
            actor_id="foreign",
            tool_name="test.foreign",
        )

    result = transition_status(db_path, token, "done", forbid_path=None)

    assert result["outcome"] == "conflict"
    assert _status(db_path, "stale-note") == "in_progress"
