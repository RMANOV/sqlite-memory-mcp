"""The silent-accept audit stays bounded inside Waiting on Me."""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def qapp():
    from PyQt6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_waiting_tab_renders_reversible_auto_link(qapp, tmp_path, monkeypatch):
    import task_tray
    from db_utils import TaskDAO, fts_sync_entity
    from link_suggestions import auto_accept_high_confidence_links
    from PyQt6.QtCore import QSettings

    path = str(tmp_path / "tray-links.db")
    db = task_tray.TaskDB(path)
    now = "2026-07-23T09:00:00+00:00"
    entity_id = int(
        db._conn.execute(
            "INSERT INTO entities "
            "(name, entity_type, project, created_at, updated_at) "
            "VALUES ('Alpha Systems', 'organization', 'alpha', ?, ?)",
            (now, now),
        ).lastrowid
    )
    fts_sync_entity(db._conn, entity_id)
    TaskDAO.create(
        db._conn,
        "task-alpha",
        "Alpha Systems rollout",
        now,
        project="alpha",
        priority="high",
        section="today",
    )
    auto_accept_high_confidence_links(db._conn)

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
    window = task_tray.FullWindow(db, sync_host=None)
    try:
        window._load_tab("waiting")
        texts = [
            window.tab_lists["waiting"].item(index).text()
            for index in range(window.tab_lists["waiting"].count())
        ]
        assert sum("[AUTO-LINK]" in text for text in texts) == 1
        assert any("silence keeps the link" in text for text in texts)
    finally:
        window.close()
        db.close()
