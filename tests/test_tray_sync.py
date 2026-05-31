import json
import os
import sys
import threading
import time
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from bridge_sync_worker import _ui_profile_changed
import task_tray
import tray_sync
from tray_sync import BridgeSyncMixin


_FILTER_KEYS = ("priority", "due", "project")


def _empty_filters():
    return {key: set() for key in _FILTER_KEYS}


def _default_tab_views(*keys):
    return {
        key: {
            "sort": "priority",
            "active": _empty_filters(),
            "excluded": _empty_filters(),
            "params": {},
        }
        for key in keys
    }


class _DummyWindow(BridgeSyncMixin):
    _SORT_MODES = ["priority", "due", "updated"]

    def __init__(self, bridge_dir):
        self._BRIDGE_DIR = str(bridge_dir)
        self._tab_keys = ["today", "inbox"]
        self._tab_views = _default_tab_views(*self._tab_keys)
        self._saved_active_tab = 0
        self._sort_mode = "priority"
        self._active_filters = _empty_filters()
        self._excluded_filters = _empty_filters()
        self._sync_run_active = False
        self._sync_cooldown_until = 0.0
        self._initial_auto_sync_pending = True
        self.saved_ui_state = False
        self.restored_geometry = None

    def _save_ui_state(self):
        self.saved_ui_state = True

    def restoreGeometry(self, geometry):
        self.restored_geometry = geometry
        return True


class _CaptureStatus:
    def __init__(self):
        self.messages = []

    def showMessage(self, message, timeout):
        self.messages.append((message, timeout))


@pytest.fixture
def bridge_env(tmp_path, monkeypatch):
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    monkeypatch.setattr("socket.gethostname", lambda: "test-host")
    monkeypatch.setattr(task_tray, "_theme_name", "blue")
    monkeypatch.setattr(task_tray, "_font_size", 13)
    monkeypatch.setattr(task_tray, "_bold", False)
    monkeypatch.setattr(task_tray, "_update_theme_colors", lambda: None)
    return bridge_dir


def test_restore_profile_from_bridge_uses_tab_views_and_ignores_legacy_flat_fields(
    bridge_env,
):
    profile = {
        "theme": "blue",
        "font_size": 14,
        "bold": True,
        "active_tab": 1,
        "tab_views": {
            "inbox": {
                "sort": "due",
                "active": {
                    "priority": ["high"],
                    "due": ["today"],
                    "project": ["mapping_studio"],
                },
                "excluded": {
                    "priority": ["low"],
                    "due": ["overdue"],
                    "project": ["smartkey"],
                },
                "params": {
                    "focus": "communication",
                    "group_by": "mailbox",
                    "limit": 15,
                },
            }
        },
        "sort_mode": "updated",
        "active_filters": {"priority": ["legacy"], "due": [], "project": []},
        "excluded_filters": {
            "priority": [],
            "due": ["legacy"],
            "project": [],
        },
    }
    (bridge_env / "shared.json").write_text(
        json.dumps({"ui_profiles": {"test-host": profile}}), encoding="utf-8"
    )

    window = _DummyWindow(bridge_env)

    window._restore_profile_from_bridge()

    assert window.saved_ui_state is True
    assert window._saved_active_tab == 1
    assert task_tray._font_size == 14
    assert task_tray._bold is True
    assert window._tab_views["today"]["sort"] == "priority"
    assert window._tab_views["inbox"]["sort"] == "due"
    assert window._tab_views["inbox"]["active"] == {
        "priority": {"high"},
        "due": {"today"},
        "project": {"mapping-studio"},
    }
    assert window._tab_views["inbox"]["excluded"] == {
        "priority": {"low"},
        "due": {"overdue"},
        "project": {"SmartKey"},
    }
    assert window._tab_views["inbox"]["params"] == {
        "focus": "communication",
        "group_by": "mailbox",
        "limit": 15,
    }
    assert window._sort_mode == "due"
    assert window._active_filters == window._tab_views["inbox"]["active"]
    assert window._excluded_filters == window._tab_views["inbox"]["excluded"]


def test_restore_profile_from_bridge_leaves_defaults_when_tab_views_missing(bridge_env):
    profile = {
        "theme": "blue",
        "font_size": 15,
        "bold": True,
        "active_tab": 1,
        "sort_mode": "updated",
        "active_filters": {
            "priority": ["high"],
            "due": ["today"],
            "project": ["ops"],
        },
        "excluded_filters": {
            "priority": ["low"],
            "due": ["overdue"],
            "project": ["archive"],
        },
    }
    (bridge_env / "shared.json").write_text(
        json.dumps({"ui_profiles": {"test-host": profile}}), encoding="utf-8"
    )

    window = _DummyWindow(bridge_env)

    window._restore_profile_from_bridge()

    assert window.saved_ui_state is True
    assert window._saved_active_tab == 1
    assert task_tray._font_size == 15
    assert task_tray._bold is True
    assert window._tab_views == _default_tab_views("today", "inbox")
    assert window._sort_mode == "priority"
    assert window._active_filters == _empty_filters()
    assert window._excluded_filters == _empty_filters()


def test_build_ui_profile_serializes_current_window_state(bridge_env, monkeypatch):
    window = _DummyWindow(bridge_env)
    window._settings = SimpleNamespace(
        value=lambda key, default=None: {
            "active_tab": 1,
            "geometry": b"geo-bytes",
        }.get(key, default)
    )
    window._tab_views["inbox"]["sort"] = "due"
    window._tab_views["inbox"]["active"]["priority"] = {"high"}
    window._tab_views["inbox"]["active"]["project"] = {"mapping_studio"}
    window._tab_views["inbox"]["excluded"]["project"] = {"smartkey"}
    window._tab_views["inbox"]["params"] = {
        "focus": "history",
        "group_by": "client",
        "limit": 20,
    }
    monkeypatch.setattr(task_tray, "_theme_name", "blue")
    monkeypatch.setattr(task_tray, "_font_size", 15)
    monkeypatch.setattr(task_tray, "_bold", True)

    profile = window._build_ui_profile()

    assert profile["theme"] == "blue"
    assert profile["font_size"] == 15
    assert profile["bold"] is True
    assert profile["active_tab"] == 1
    assert profile["tab_views"]["inbox"]["sort"] == "due"
    assert profile["tab_views"]["inbox"]["active"]["priority"] == ["high"]
    assert profile["tab_views"]["inbox"]["active"]["project"] == ["mapping-studio"]
    assert profile["tab_views"]["inbox"]["excluded"]["project"] == ["SmartKey"]
    assert profile["tab_views"]["inbox"]["params"] == {
        "focus": "history",
        "group_by": "client",
        "limit": 20,
    }
    assert profile["geometry_b64"]


def test_ui_profile_diff_ignores_updated_at(bridge_env):
    profile = {
        "theme": "blue",
        "font_size": 14,
        "bold": True,
        "active_tab": 1,
        "tab_views": {
            "today": {"sort": "priority", "active": {}, "excluded": {}, "params": {}}
        },
        "updated_at": "2026-04-23T09:00:00+00:00",
    }
    (bridge_env / "shared.json").write_text(
        json.dumps({"ui_profiles": {"test-host": profile}}),
        encoding="utf-8",
    )

    current = dict(profile)
    current["updated_at"] = "2026-04-23T09:05:00+00:00"

    assert (
        _ui_profile_changed(bridge_env / "shared.json", "test-host", current) is False
    )


def test_sync_bridge_skips_when_thread_already_running(bridge_env):
    window = _DummyWindow(bridge_env)
    window.status = _CaptureStatus()
    window._bridge_thread_lock.acquire()

    try:
        window._sync_bridge()
    finally:
        window._bridge_thread_lock.release()

    assert window.status.messages[-1][0] == "Sync already running"


def test_periodic_pull_uses_pull_only_mode(bridge_env, monkeypatch):
    captured = {}
    refreshes = []
    bridge_sync_worker = SimpleNamespace(
        main=lambda **kwargs: (
            captured.update(kwargs) or {"imported_new": 1, "imported_updated": 0}
        )
    )
    monkeypatch.setitem(sys.modules, "bridge_sync_worker", bridge_sync_worker)
    heads = ["old-head", "new-head"]
    monkeypatch.setattr(tray_sync, "_bridge_head", lambda repo_dir: heads.pop(0))
    monkeypatch.setattr(
        tray_sync,
        "_bridge_git_pull",
        lambda repo_dir: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )

    window = _DummyWindow(bridge_env)
    window.status = _CaptureStatus()
    window._db_refresh_debounce = SimpleNamespace(
        start=lambda: refreshes.append("timer")
    )
    window._bridge_refresh_requested = SimpleNamespace(
        emit=lambda: refreshes.append("signal")
    )
    window._bridge_progress = SimpleNamespace(emit=lambda *args: None)
    window._bridge_done = SimpleNamespace(emit=lambda *args: None)
    monkeypatch.setattr(
        window,
        "_start_bridge_sync_thread",
        lambda target, busy_message=None, **kwargs: (target(), True)[1],
    )

    window._periodic_pull()

    assert captured["pull_only"] is True
    assert refreshes == ["signal"]


def test_periodic_pull_skips_heavy_import_when_git_head_unchanged(
    bridge_env, monkeypatch
):
    refreshes = []
    bridge_sync_worker = SimpleNamespace(
        main=lambda **kwargs: pytest.fail("heavy worker should not run")
    )
    monkeypatch.setitem(sys.modules, "bridge_sync_worker", bridge_sync_worker)
    monkeypatch.setattr(tray_sync, "_bridge_head", lambda repo_dir: "same-head")
    monkeypatch.setattr(
        tray_sync,
        "_bridge_git_pull",
        lambda repo_dir: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )

    window = _DummyWindow(bridge_env)
    window.status = _CaptureStatus()
    window._db_refresh_debounce = SimpleNamespace(
        start=lambda: refreshes.append("timer")
    )
    window._bridge_refresh_requested = SimpleNamespace(
        emit=lambda: refreshes.append("signal")
    )
    window._bridge_progress = SimpleNamespace(emit=lambda *args: None)
    window._bridge_done = SimpleNamespace(emit=lambda *args: None)
    monkeypatch.setattr(
        window,
        "_start_bridge_sync_thread",
        lambda target, busy_message=None, **kwargs: (target(), True)[1],
    )

    window._periodic_pull()

    assert refreshes == []


def test_bootstrap_pull_imports_remote_changes_when_git_head_changes(
    bridge_env, monkeypatch
):
    """Regression: bootstrap auto-sync must IMPORT remote changes after a HEAD
    advance, not early-return with imported=0 leaving local SQLite stale.

    Bootstrap now follows the same path as periodic pull: when HEAD advanced,
    it runs bridge_sync_worker.main(pull_only=True) so the local DB absorbs
    remote tombstones/edits, then requests a UI refresh."""
    captured = {}
    refreshes = []
    bridge_sync_worker = SimpleNamespace(
        main=lambda **kwargs: (
            captured.update(kwargs) or {"imported_new": 2, "imported_updated": 1}
        )
    )
    monkeypatch.setitem(sys.modules, "bridge_sync_worker", bridge_sync_worker)
    heads = ["old-head", "new-head"]
    monkeypatch.setattr(tray_sync, "_bridge_head", lambda repo_dir: heads.pop(0))
    monkeypatch.setattr(
        tray_sync,
        "_bridge_git_pull",
        lambda repo_dir: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )

    window = _DummyWindow(bridge_env)
    window.status = _CaptureStatus()
    window._db_refresh_debounce = SimpleNamespace(
        start=lambda: refreshes.append("timer")
    )
    window._bridge_refresh_requested = SimpleNamespace(
        emit=lambda: refreshes.append("signal")
    )
    window._bridge_progress = SimpleNamespace(emit=lambda *args: None)
    window._bridge_done = SimpleNamespace(emit=lambda *args: None)
    monkeypatch.setattr(
        window,
        "_start_bridge_sync_thread",
        lambda target, busy_message=None, **kwargs: (target(), True)[1],
    )

    window._periodic_pull(initiator="bootstrap")

    # Heavy import worker ran in pull-only mode (not an imported=0 early-return).
    assert captured["pull_only"] is True
    # Imported updates trigger a UI refresh so the tray reflects remote state.
    assert refreshes == ["signal"]


def test_bootstrap_pull_skips_heavy_import_when_git_head_unchanged(
    bridge_env, monkeypatch
):
    """Bootstrap still avoids the heavy import worker when nothing arrived
    (HEAD unchanged) — the import only runs when the pull advanced HEAD."""
    bridge_sync_worker = SimpleNamespace(
        main=lambda **kwargs: pytest.fail(
            "bootstrap must not import when HEAD is unchanged"
        )
    )
    monkeypatch.setitem(sys.modules, "bridge_sync_worker", bridge_sync_worker)
    monkeypatch.setattr(tray_sync, "_bridge_head", lambda repo_dir: "same-head")
    monkeypatch.setattr(
        tray_sync,
        "_bridge_git_pull",
        lambda repo_dir: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )

    window = _DummyWindow(bridge_env)
    window.status = _CaptureStatus()
    window._db_refresh_debounce = SimpleNamespace(start=lambda: None)
    window._bridge_refresh_requested = SimpleNamespace(emit=lambda: None)
    window._bridge_progress = SimpleNamespace(emit=lambda *args: None)
    window._bridge_done = SimpleNamespace(emit=lambda *args: None)
    monkeypatch.setattr(
        window,
        "_start_bridge_sync_thread",
        lambda target, busy_message=None, **kwargs: (target(), True)[1],
    )

    window._periodic_pull(initiator="bootstrap")


def test_request_db_refresh_from_worker_falls_back_to_timer(bridge_env):
    refreshes = []
    window = _DummyWindow(bridge_env)
    window._db_refresh_debounce = SimpleNamespace(
        start=lambda: refreshes.append("timer")
    )

    window._request_db_refresh_from_worker()

    assert refreshes == ["timer"]


def test_start_bridge_sync_thread_serializes_background_db_writers(bridge_env):
    order = []
    window = _DummyWindow(bridge_env)
    window._auto_sync_timer = SimpleNamespace(stop=lambda: None)
    window._background_db_write_lock = threading.Lock()
    window._background_db_write_lock.acquire()

    assert window._start_bridge_sync_thread(
        lambda: order.append("target"),
        initiator="test",
        mode="sync",
    )
    time.sleep(0.05)
    assert order == []

    window._background_db_write_lock.release()
    deadline = time.monotonic() + 1
    while not order and time.monotonic() < deadline:
        time.sleep(0.01)

    assert order == ["target"]


def test_start_bridge_sync_thread_releases_lock_after_worker_finishes(bridge_env):
    window = _DummyWindow(bridge_env)
    window.status = _CaptureStatus()
    stops = []
    window._auto_sync_timer = SimpleNamespace(stop=lambda: stops.append(True))
    completed = []

    assert window._start_bridge_sync_thread(lambda: completed.append(True)) is True
    deadline = time.time() + 1.0
    while time.time() < deadline and window._bridge_thread_lock.locked():
        time.sleep(0.01)

    assert completed == [True]
    assert stops == [True]
    assert window._bridge_thread_lock.locked() is False
    assert window._sync_run_active is False
    assert window._sync_cooldown_until > 0


def test_start_bridge_sync_thread_sets_post_sync_db_watch_cooldown(
    bridge_env,
):
    window = _DummyWindow(bridge_env)
    window.status = _CaptureStatus()
    window._auto_sync_timer = SimpleNamespace(stop=lambda: None)
    before = time.monotonic()

    assert window._start_bridge_sync_thread(lambda: None) is True
    deadline = time.time() + 1.0
    while time.time() < deadline and window._bridge_thread_lock.locked():
        time.sleep(0.01)

    assert window._sync_cooldown_until >= (
        before + tray_sync._POST_SYNC_DB_WATCH_COOLDOWN_SECONDS - 1.0
    )


def test_start_bridge_sync_thread_marks_sync_active_while_worker_runs(bridge_env):
    window = _DummyWindow(bridge_env)
    window.status = _CaptureStatus()
    window._auto_sync_timer = SimpleNamespace(stop=lambda: None)
    started = threading.Event()
    release = threading.Event()

    def _worker():
        started.set()
        release.wait(1.0)

    assert window._start_bridge_sync_thread(_worker) is True
    assert started.wait(1.0) is True
    assert window._sync_run_active is True
    assert window._initial_auto_sync_pending is False

    release.set()
    deadline = time.time() + 1.0
    while time.time() < deadline and window._bridge_thread_lock.locked():
        time.sleep(0.01)

    assert window._sync_run_active is False


def test_db_change_during_sync_does_not_rearm_auto_sync_timer(bridge_env):
    starts = []
    refreshes = []
    dummy = SimpleNamespace(
        _db_refresh_debounce=SimpleNamespace(start=lambda: refreshes.append("refresh")),
        _refresh_db_watch_paths=lambda: None,
        _sync_run_active=True,
        _sync_cooldown_until=0.0,
        _auto_sync_timer=SimpleNamespace(start=lambda: starts.append("auto-sync")),
    )

    task_tray.FullWindow._on_db_changed(dummy, "ignored")
    task_tray.FullWindow._on_db_dir_changed(dummy, "ignored")

    assert refreshes == ["refresh", "refresh"]
    assert starts == []


def test_db_change_does_not_arm_auto_sync_when_disabled():
    starts = []
    refreshes = []
    dummy = SimpleNamespace(
        _db_refresh_debounce=SimpleNamespace(start=lambda: refreshes.append("refresh")),
        _refresh_db_watch_paths=lambda: None,
        _auto_sync_enabled=False,
        _sync_run_active=False,
        _sync_cooldown_until=0.0,
        _auto_sync_timer=SimpleNamespace(start=lambda: starts.append("auto-sync")),
    )

    task_tray.FullWindow._on_db_changed(dummy, "ignored")

    assert refreshes == ["refresh"]
    assert starts == []


def test_db_dir_change_without_watch_path_delta_does_not_rearm_auto_sync_timer():
    starts = []
    refreshes = []
    dummy = SimpleNamespace(
        _db_refresh_debounce=SimpleNamespace(start=lambda: refreshes.append("refresh")),
        _refresh_db_watch_paths=lambda: False,
        _sync_run_active=False,
        _sync_cooldown_until=0.0,
        _auto_sync_timer=SimpleNamespace(start=lambda: starts.append("auto-sync")),
    )

    task_tray.FullWindow._on_db_dir_changed(dummy, "ignored")

    assert refreshes == ["refresh"]
    assert starts == []


def test_db_dir_change_with_watch_path_delta_rearms_auto_sync_timer():
    starts = []
    refreshes = []
    dummy = SimpleNamespace(
        _db_refresh_debounce=SimpleNamespace(start=lambda: refreshes.append("refresh")),
        _refresh_db_watch_paths=lambda: True,
        _sync_run_active=False,
        _sync_cooldown_until=0.0,
        _auto_sync_timer=SimpleNamespace(start=lambda: starts.append("auto-sync")),
    )

    task_tray.FullWindow._on_db_dir_changed(dummy, "ignored")

    assert refreshes == ["refresh"]
    assert starts == ["auto-sync"]


def test_initial_auto_sync_only_arms_while_pending():
    starts = []
    dummy = SimpleNamespace(
        _initial_auto_sync_pending=True,
        _sync_run_active=False,
        _sync_cooldown_until=0.0,
        _auto_sync_timer=SimpleNamespace(start=lambda: starts.append("auto-sync")),
    )

    task_tray.FullWindow._maybe_schedule_initial_auto_sync(dummy)

    assert starts == ["auto-sync"]


def test_initial_auto_sync_does_not_arm_when_disabled():
    starts = []
    dummy = SimpleNamespace(
        _auto_sync_enabled=False,
        _initial_auto_sync_pending=True,
        _sync_run_active=False,
        _sync_cooldown_until=0.0,
        _auto_sync_timer=SimpleNamespace(start=lambda: starts.append("auto-sync")),
    )

    task_tray.FullWindow._maybe_schedule_initial_auto_sync(dummy)

    assert starts == []


def test_initial_auto_sync_does_not_rearm_after_pending_consumed():
    starts = []
    dummy = SimpleNamespace(
        _initial_auto_sync_pending=False,
        _sync_run_active=False,
        _sync_cooldown_until=0.0,
        _auto_sync_timer=SimpleNamespace(start=lambda: starts.append("auto-sync")),
    )

    task_tray.FullWindow._maybe_schedule_initial_auto_sync(dummy)

    assert starts == []


def test_auto_sync_triggered_uses_pending_initiator():
    captured = []
    dummy = SimpleNamespace(
        _pending_auto_sync_initiator="db_file_change",
        _sync_bridge=lambda initiator="manual": captured.append(initiator),
    )

    BridgeSyncMixin._auto_sync_triggered(dummy)

    assert captured == ["db_file_change"]
    assert dummy._pending_auto_sync_initiator is None


def test_bootstrap_auto_sync_uses_pull_only_mode():
    captured = []
    dummy = SimpleNamespace(
        _pending_auto_sync_initiator="bootstrap",
        _periodic_pull=lambda initiator="periodic_pull": captured.append(
            ("pull", initiator)
        ),
        _sync_bridge=lambda initiator="manual": captured.append(("sync", initiator)),
    )

    BridgeSyncMixin._auto_sync_triggered(dummy)

    assert captured == [("pull", "bootstrap")]
    assert dummy._pending_auto_sync_initiator is None


def test_refresh_and_sync_delegates_to_sync_host(bridge_env):
    captured = []
    dummy = SimpleNamespace(
        refresh=lambda: captured.append("refresh"),
        _sync_host=SimpleNamespace(request_manual_sync=lambda: captured.append("sync")),
    )

    task_tray.FullWindow._refresh_and_sync(dummy)

    assert captured == ["refresh", "sync"]
