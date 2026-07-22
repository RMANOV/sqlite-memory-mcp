import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import task_tray


def test_bounded_tray_limit_rejects_unbounded_limit():
    assert task_tray._bounded_tray_limit(None, 50) == 50
    assert task_tray._bounded_tray_limit(5000, 50) == 50
    assert task_tray._bounded_tray_limit(0, 50) == 1
    assert task_tray._bounded_tray_limit("bad", 50) == 50


def test_tray_search_index_rows_are_bounded_and_trimmed(monkeypatch):
    monkeypatch.setattr(task_tray, "_TRAY_SEARCH_INDEX_LIMIT", 2)
    monkeypatch.setattr(task_tray, "_TRAY_INDEX_TEXT_CHARS", 5)
    rows = [
        {"id": "a", "title": "alpha", "description": "0123456789"},
        {"id": "b", "title": "bravo", "notes": "abcdefghij"},
        {"id": "c", "title": "charlie"},
    ]

    indexed = task_tray._tray_search_index_rows(rows)

    assert [row["id"] for row in indexed] == ["a", "b"]
    assert indexed[0]["description"] == "01234"
    assert indexed[1]["notes"] == "abcde"


def test_full_window_search_index_rebuild_is_lazy_and_idempotent(monkeypatch):
    monkeypatch.setattr(task_tray, "_TRAY_SEARCH_INDEX_LIMIT", 2)
    calls = []
    window = SimpleNamespace(
        _search_index_dirty=True,
        _raw_cache={
            "all": [
                {"id": "a", "title": "alpha"},
                {"id": "b", "title": "bravo"},
                {"id": "c", "title": "charlie"},
            ]
        },
        _premium_tray_extension=None,
        _search_engine=SimpleNamespace(rebuild_index=lambda rows: calls.append(rows)),
    )

    task_tray.FullWindow._ensure_search_index(window)
    task_tray.FullWindow._ensure_search_index(window)

    assert len(calls) == 1
    assert [row["id"] for row in calls[0]] == ["a", "b"]
    assert window._search_index_dirty is False


def test_get_suggested_tasks_caps_none_limit(monkeypatch):
    captured = {}

    def fake_suggested_ready(tasks, *, include_readings, limit):
        captured["tasks"] = tasks
        captured["include_readings"] = include_readings
        captured["limit"] = limit
        return tasks[:limit]

    db = SimpleNamespace(
        get_all_active=lambda: [{"id": "active"}],
        get_ready_review_tasks=lambda limit: [{"id": f"review-{limit}"}],
    )
    monkeypatch.setattr(task_tray, "suggested_ready", fake_suggested_ready)

    result = task_tray.TaskDB.get_suggested_tasks(db, limit=None)

    assert captured["include_readings"] is False
    assert captured["limit"] == task_tray._TRAY_SUGGESTED_LIMIT
    assert result[1]["id"] == f"review-{task_tray._TRAY_READY_REVIEW_LIMIT}"


def test_build_tab_rows_caps_heavy_tabs_but_keeps_total_count():
    rows = [{"id": str(i), "title": f"Task {i}"} for i in range(250)]
    window = SimpleNamespace(
        _raw_cache={"done": rows},
        _filtered_cache={},
        _tab_total_counts={},
        _search_text="",
        _tab_views={},
        _filter=lambda source, *args: source,
        _sort_tasks=lambda source, sort_mode=None: source,
    )

    rendered = task_tray.FullWindow._build_tab_rows(window, "done")

    assert len(rendered) == task_tray._TAB_PAGE_SIZE
    assert window._tab_total_counts["done"] == 250


def test_memory_watchdog_restarts_above_exit_threshold(monkeypatch):
    calls = []
    app = SimpleNamespace(
        _rss_restart_requested=False,
        _rss_next_log_at=0.0,
        _restart_due_to_memory=lambda rss_mb: calls.append(rss_mb),
    )
    monkeypatch.setattr(task_tray, "_current_rss_mb", lambda: 4096.0)
    monkeypatch.setattr(task_tray, "_TRAY_RSS_LOG_MB", 512)
    monkeypatch.setattr(task_tray, "_TRAY_RSS_EXIT_MB", 1024)

    task_tray.TaskTrayApp._check_memory_budget(app)

    assert calls == [4096.0]


def test_current_rss_uses_windows_api_on_windows(monkeypatch):
    monkeypatch.setattr(task_tray.os, "name", "nt")
    monkeypatch.setattr(task_tray, "_windows_rss_mb", lambda: 123.5)

    assert task_tray._current_rss_mb() == 123.5


def test_memory_restart_cleans_up_then_quits_if_exec_fails(monkeypatch):
    class Stopper:
        def __init__(self):
            self.stopped = False

        def stop(self):
            self.stopped = True

    class SearchEngine:
        def __init__(self):
            self.saved = False

        def save(self):
            self.saved = True

    class Db:
        def __init__(self):
            self.search_engine = SearchEngine()
            self.closed = False

        def close(self):
            self.closed = True

    class App:
        def __init__(self):
            self.quitted = False

        def quit(self):
            self.quitted = True

    class Socket:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    db = Db()
    socket = Socket()
    app = SimpleNamespace(
        _rss_restart_requested=False,
        _enrich_timer=Stopper(),
        _rss_timer=Stopper(),
        _audit_timer=None,
        _reminder_timer=Stopper(),
        _purge_timer=Stopper(),
        _periodic_pull_timer=Stopper(),
        _auto_sync_timer=Stopper(),
        _db_refresh_debounce=Stopper(),
        _instance_socket=socket,
        db=db,
        app=App(),
    )
    monkeypatch.setattr(
        task_tray.os, "execv", lambda *args: (_ for _ in ()).throw(OSError())
    )

    task_tray.TaskTrayApp._restart_due_to_memory(app, 4096.0)

    assert app._rss_restart_requested is True
    assert db.search_engine.saved is True
    assert db.closed is True
    assert socket.closed is True
    assert app.app.quitted is True
