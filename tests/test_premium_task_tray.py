import os
import sqlite3
import sys
from contextlib import contextmanager

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import premium_task_tray
from schema import init_db


@contextmanager
def _conn_ctx(db_path: str):
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("BEGIN")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def test_maybe_load_task_tray_extension_returns_none_when_denied(tmp_path, monkeypatch):
    db_path = str(tmp_path / "memory.db")
    init_db(db_path)
    monkeypatch.setenv("SQLITE_MEMORY_PREMIUM_ENTRYPOINT", str(tmp_path / "missing.py"))
    monkeypatch.setattr(premium_task_tray, "_get_conn", lambda: _conn_ctx(db_path))
    monkeypatch.setattr(
        premium_task_tray,
        "evaluate_feature_gate",
        lambda conn, **kwargs: {
            "allowed": False,
            "decision": "denied",
            "reason": "feature_not_entitled",
            "feature_id": "custom_design_tab",
        },
    )

    extension = premium_task_tray.maybe_load_task_tray_extension(
        server_name="sqlite-task-tray"
    )

    assert extension is None


def test_maybe_load_task_tray_extension_loads_builder(tmp_path, monkeypatch):
    db_path = str(tmp_path / "memory.db")
    init_db(db_path)
    premium_file = tmp_path / "premium_tray_plugin.py"
    premium_file.write_text(
        "class DemoExtension:\n"
        "    tab_key = 'custom_design'\n"
        "    tab_label = 'Custom Design'\n"
        "    default_params = {'focus': 'mixed', 'group_by': 'smart', 'limit': 25}\n"
        "    extra_sort_modes = {'client': 'Sort: Client'}\n"
        "    def normalize_params(self, params):\n"
        "        return dict(params or {})\n"
        "    def build_rows(self, *, params=None, search_text=''):\n"
        "        return {'rows': []}\n"
        "\n"
        "def build_task_tray_extension(*, server_name=None, mount_context=None):\n"
        "    ext = DemoExtension()\n"
        "    ext.server_name = server_name\n"
        "    ext.feature_id = mount_context.feature_id\n"
        "    ext.selection_mode = mount_context.config.get('_premium_selection', {}).get('selection_mode')\n"
        "    ext.selected_packs = mount_context.config.get('_premium_selection', {}).get('selected_packs', [])\n"
        "    return ext\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SQLITE_MEMORY_PREMIUM_ENTRYPOINT", str(premium_file))
    monkeypatch.setattr(premium_task_tray, "_get_conn", lambda: _conn_ctx(db_path))
    monkeypatch.setattr(
        premium_task_tray,
        "evaluate_feature_gate",
        lambda conn, **kwargs: {
            "allowed": True,
            "decision": "allowed",
            "reason": "entitlement_valid",
            "feature_id": "custom_design_tab",
            "entitlement_id": "ent-ui",
            "customer_id": "cust-ui",
            "selection_mode": "packs_and_features",
            "selected_packs": ["custom_design_surface"],
            "effective_features": ["custom_design_tab", "advanced_ranking"],
        },
    )

    extension = premium_task_tray.maybe_load_task_tray_extension(
        server_name="sqlite-task-tray"
    )

    assert extension is not None
    assert extension.tab_key == "custom_design"
    assert extension.server_name == "sqlite-task-tray"
    assert extension.feature_id == "custom_design_tab"
    assert extension.selection_mode == "packs_and_features"
    assert extension.selected_packs == ["custom_design_surface"]
