"""Bridge/sync mixin for FullWindow.

All methods here operate on the memory bridge (pull, push, shared.json profiles).
This is a mixin — it requires the following from the host class (FullWindow):

Qt signals (defined on FullWindow):
    _bridge_progress: pyqtSignal(int, str)
    _bridge_done:     pyqtSignal(str)

Qt widgets (set up in FullWindow.__init__):
    self._sync_bar:   QProgressBar
    self._sync_label: QLabel
    self.status:      QStatusBar

Methods called on self (must exist on FullWindow):
    self.refresh()
    self._process_recurring()

Module-level globals from task_tray (accessed via import):
    _theme_name, _font_size, _bold, _THEMES, _update_theme_colors

FullWindow instance attributes read by _restore_profile_from_bridge:
    self._tab_views, self._tab_keys, self._SORT_MODES,
    self._sort_mode, self._active_filters, self._excluded_filters,
    self._saved_active_tab, self._settings, self._db_refresh_debounce
"""

import base64
import json
import logging
import os
import subprocess
import threading
from datetime import date, datetime
from pathlib import Path

logger = logging.getLogger("task_tray")


class BridgeSyncMixin:
    """Bridge sync methods for FullWindow. Mixin — requires Qt signals and _BRIDGE_DIR."""

    # Class-level constants — belong here since only sync code uses them
    _BRIDGE_DIR = os.path.expanduser("~/.claude/memory/bridge")
    _SP_FLAGS = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

    # ── Sync entry points ───────────────────────────────────────────────

    def _auto_sync_triggered(self):
        """Debounce elapsed — run bridge sync."""
        self._sync_bridge()

    def _periodic_pull(self):
        """Periodic pull from remote — catches changes from other machines."""
        if not os.path.isdir(self._BRIDGE_DIR):
            return

        def _run():
            try:
                import bridge_sync_worker

                stats = bridge_sync_worker.main(
                    progress_callback=lambda pct, label: self._bridge_progress.emit(
                        pct, label
                    )
                )
                imported = stats.get("imported_new", 0) + stats.get(
                    "imported_updated", 0
                )
                if imported:
                    self._bridge_done.emit(f"Pulled {imported} updates from remote")
                    # Refresh UI after importing remote changes
                    self._db_refresh_debounce.start()
            except Exception as exc:
                logger.warning("Periodic pull failed: %s", exc)

        threading.Thread(target=_run, daemon=True).start()

    def _sync_bridge(self):
        """Sync memory bridge (pull + push + shared.json)."""
        if not os.path.isdir(self._BRIDGE_DIR):
            self.status.showMessage("Bridge dir not found", 3000)
            return

        def _run():
            try:
                import bridge_sync_worker

                stats = bridge_sync_worker.main(
                    progress_callback=lambda pct, label: self._bridge_progress.emit(
                        pct, label
                    )
                )

                # Patch UI profile into shared.json (tray-specific, no extra commit)
                self._patch_ui_profile()

                if stats.get("blocked_by_repo_state"):
                    self._bridge_done.emit(
                        f"Sync blocked: {stats.get('message', 'bridge repo needs attention')}"
                    )
                elif stats.get("blocked_by_safety"):
                    safety = stats.get("safety", {})
                    self._bridge_done.emit(
                        "Sync blocked by safety valve: "
                        f"{safety.get('descriptions_removed', 0)} descriptions removed, "
                        f"{safety.get('notes_removed', 0)} notes removed, "
                        f"{safety.get('descriptions_shrunk', 0)} descriptions shrunk, "
                        f"{safety.get('notes_shrunk', 0)} notes shrunk"
                    )
                elif stats.get("skipped"):
                    self._bridge_done.emit("Already in sync — no changes to push")
                elif not stats.get("pushed", False):
                    self._bridge_done.emit(
                        "Sync incomplete — pull/import finished but push did not"
                    )
                else:
                    n_ent = stats.get("entities", 0)
                    n_tasks = stats.get("tasks", 0)
                    self._bridge_done.emit(f"Synced: {n_ent} entities, {n_tasks} tasks")
            except Exception as exc:
                self._bridge_done.emit(f"Sync error: {exc}")

        threading.Thread(target=_run, daemon=True).start()

    # ── Signal handlers (must run on Qt main thread via signal/slot) ────

    def _on_sync_progress(self, pct, label):
        self._sync_bar.setValue(pct)
        self._sync_bar.setFormat(f"{pct}%  {label}")
        self._sync_label.hide()
        self._sync_bar.show()

    def _on_sync_done(self, msg):
        from PyQt6.QtCore import QTimer

        is_error = msg.startswith("Sync error")
        self._sync_bar.setValue(100)
        self._sync_bar.setFormat(msg[:50])
        hide_ms = 15000 if is_error else 4000
        QTimer.singleShot(hide_ms, self._show_last_sync_time)
        self.status.showMessage(msg, hide_ms)
        if not is_error:
            self._last_sync_at = datetime.now()
            self._process_recurring()
            self.refresh()

    def _show_last_sync_time(self):
        self._sync_bar.hide()
        if hasattr(self, "_last_sync_at") and self._last_sync_at:
            if self._last_sync_at.date() == date.today():
                ts = self._last_sync_at.strftime("%H:%M:%S")
            else:
                ts = self._last_sync_at.strftime("%a %d.%m, %H:%M:%S")
            self._sync_label.setText(f"Synced: {ts}")
        self._sync_label.show()

    # ── UI profile persistence (shared.json) ────────────────────────────

    def _restore_profile_from_bridge(self):
        """First-run recovery: load UI state from bridge shared.json profile."""
        import copy
        import socket as _socket

        # Import module-level globals from task_tray
        import task_tray as _tt

        shared_path = Path(self._BRIDGE_DIR) / "shared.json"
        if not shared_path.exists():
            return
        try:
            data = json.loads(shared_path.read_text(encoding="utf-8"))
            profiles = data.get("ui_profiles", {})
            profile = profiles.get(_socket.gethostname())
            if not profile:
                return
            if profile.get("theme") in _tt._THEMES:
                _tt._theme_name = profile["theme"]
            if (
                isinstance(profile.get("font_size"), int)
                and 10 <= profile["font_size"] <= 20
            ):
                _tt._font_size = profile["font_size"]
            _tt._bold = bool(profile.get("bold", False))
            if isinstance(profile.get("active_tab"), int):
                self._saved_active_tab = profile["active_tab"]

            # Load per-tab views from bridge profile (new format)
            bridge_tab_views = profile.get("tab_views")
            if isinstance(bridge_tab_views, dict):
                for key, view in bridge_tab_views.items():
                    if key in self._tab_views:
                        if view.get("sort") in self._SORT_MODES:
                            self._tab_views[key]["sort"] = view["sort"]
                        self._tab_views[key]["active"] = {
                            k: set(view.get("active", {}).get(k, []))
                            for k in ("priority", "due", "project")
                        }
                        self._tab_views[key]["excluded"] = {
                            k: set(view.get("excluded", {}).get(k, []))
                            for k in ("priority", "due", "project")
                        }
                # Sync working state from current tab
                cur_key = self._tab_keys[
                    min(getattr(self, "_saved_active_tab", 0), len(self._tab_keys) - 1)
                ]
                if cur_key in self._tab_views:
                    v = self._tab_views[cur_key]
                    self._sort_mode = v["sort"]
                    self._active_filters = v["active"]
                    self._excluded_filters = v["excluded"]
            else:
                # Backward compat: old format had flat sort_mode/active_filters
                if profile.get("sort_mode") in self._SORT_MODES:
                    for v in self._tab_views.values():
                        v["sort"] = profile["sort_mode"]
                    self._sort_mode = profile["sort_mode"]
                if isinstance(profile.get("active_filters"), dict):
                    af = {
                        k: set(profile["active_filters"].get(k, []))
                        for k in ("priority", "due", "project")
                    }
                    for v in self._tab_views.values():
                        v["active"] = copy.deepcopy(af)
                    self._active_filters = af
                if isinstance(profile.get("excluded_filters"), dict):
                    ef = {
                        k: set(profile["excluded_filters"].get(k, []))
                        for k in ("priority", "due", "project")
                    }
                    for v in self._tab_views.values():
                        v["excluded"] = copy.deepcopy(ef)
                    self._excluded_filters = ef

            geo_b64 = profile.get("geometry_b64")
            if geo_b64:
                from PyQt6.QtCore import QByteArray

                self.restoreGeometry(QByteArray(base64.b64decode(geo_b64)))
            _tt._update_theme_colors()
            self._save_ui_state()
        except (json.JSONDecodeError, OSError, KeyError, ValueError):
            pass

    def _patch_ui_profile(self):
        """Write own UI profile into shared.json (persisted on next sync cycle)."""
        import socket as _socket

        from db_utils import now_iso

        # Import module-level globals from task_tray
        import task_tray as _tt

        shared_path = Path(self._BRIDGE_DIR) / "shared.json"
        if not shared_path.exists():
            return
        try:
            data = json.loads(shared_path.read_text(encoding="utf-8"))
            profiles = data.get("ui_profiles", {})

            # Serialize per-tab views for bridge profile
            serializable_views = {}
            for key, view in self._tab_views.items():
                serializable_views[key] = {
                    "sort": view["sort"],
                    "active": {k: list(v) for k, v in view["active"].items()},
                    "excluded": {k: list(v) for k, v in view["excluded"].items()},
                }

            profiles[_socket.gethostname()] = {
                "theme": _tt._theme_name,
                "font_size": _tt._font_size,
                "bold": _tt._bold,
                "active_tab": int(self._settings.value("active_tab", 0)),
                "tab_views": serializable_views,
                "updated_at": now_iso(),
            }
            geo = self._settings.value("geometry")
            if geo:
                profiles[_socket.gethostname()]["geometry_b64"] = base64.b64encode(
                    bytes(geo)
                ).decode("ascii")
            data["ui_profiles"] = profiles
            shared_path.write_text(
                json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        except (json.JSONDecodeError, OSError):
            pass  # non-critical — UI profiles sync on next cycle
