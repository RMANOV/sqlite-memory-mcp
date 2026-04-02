import json
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import task_tray
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
    assert profile["geometry_b64"]


def test_sync_bridge_skips_when_thread_already_running(bridge_env):
    window = _DummyWindow(bridge_env)
    window.status = _CaptureStatus()
    window._bridge_thread_lock.acquire()

    try:
        window._sync_bridge()
    finally:
        window._bridge_thread_lock.release()

    assert window.status.messages[-1][0] == "Sync already running"
