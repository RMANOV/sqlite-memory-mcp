"""Bridge/sync helpers shared by the tray app and full window.

All methods here operate on the memory bridge (pull, push, shared.json profiles).
This is a mixin — long-lived sync ownership should live on the tray app host,
while the full window can reuse the UI/profile helpers.

Expected host capabilities for active sync ownership:
    self._bridge_progress.emit(int, str)
    self._bridge_done.emit(str)
    self._bridge_refresh_requested.emit()
    self.status.showMessage(str, int)
    self._db_watcher / self._auto_sync_timer / self._periodic_pull_timer
    self._db_refresh_debounce / self._db_watch_dir / self._sync_run_active
    self._sync_cooldown_until / self._initial_auto_sync_pending
    optional self._background_db_write_lock for serializing background DB writers
    self._build_ui_profile()

Expected host capabilities for window UI integration:
    self._sync_bar:   QProgressBar
    self._sync_label: QLabel
    self.status:      QStatusBar

Module-level globals from task_tray (accessed via import):
    _theme_name, _font_size, _bold, _THEMES, _update_theme_colors

Window instance attributes read by _restore_profile_from_bridge:
    self._tab_views, self._tab_keys, self._SORT_MODES,
    self._sort_mode, self._active_filters, self._excluded_filters,
    self._saved_active_tab, self._settings
"""

import base64
import json
import logging
import os
import subprocess
import threading
import time
from datetime import date, datetime
from pathlib import Path

logger = logging.getLogger("task_tray")
_GIT_PULL_TIMEOUT = 120
_POST_SYNC_DB_WATCH_COOLDOWN_SECONDS = 90.0


def _bridge_head(repo_dir: str) -> str | None:
    from db_utils import git_run

    result = git_run(repo_dir, "rev-parse", "HEAD", timeout=10)
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _bridge_git_pull(repo_dir: str):
    from db_utils import git_retry

    fetch = git_retry(
        repo_dir,
        "fetch",
        "origin",
        "main",
        timeout=_GIT_PULL_TIMEOUT,
    )
    if fetch.returncode != 0:
        return fetch

    local = git_retry(repo_dir, "rev-parse", "HEAD", timeout=10)
    remote = git_retry(repo_dir, "rev-parse", "origin/main", timeout=10)
    base = git_retry(repo_dir, "merge-base", "HEAD", "origin/main", timeout=10)
    if local.returncode != 0 or remote.returncode != 0 or base.returncode != 0:
        detail = " ".join(
            (result.stderr or result.stdout).strip()
            for result in (local, remote, base)
            if result.returncode != 0
        ).strip()
        return subprocess.CompletedProcess(
            ["git", "graph-inspect"],
            1,
            "",
            detail or "bridge git graph inspection failed",
        )

    local_sha = local.stdout.strip()
    remote_sha = remote.stdout.strip()
    base_sha = base.stdout.strip()
    if local_sha == remote_sha or base_sha == remote_sha:
        return fetch
    if base_sha == local_sha:
        return git_retry(
            repo_dir,
            "merge",
            "--ff-only",
            "origin/main",
            timeout=_GIT_PULL_TIMEOUT,
        )

    return subprocess.CompletedProcess(
        ["git", "merge", "--ff-only", "origin/main"],
        1,
        "",
        "bridge repo local and origin/main diverged; explicit recovery required",
    )


def _normalize_filter_payload(filter_payload):
    """Normalize persisted include/exclude filter payloads."""
    from db_utils import normalize_project_filter_values

    payload = filter_payload or {}
    return {
        "priority": set(payload.get("priority", [])),
        "due": set(payload.get("due", [])),
        "project": normalize_project_filter_values(payload.get("project", [])),
    }


class BridgeSyncMixin:
    """Bridge sync methods shared by long-lived tray hosts and UI clients."""

    # Class-level constants — belong here since only sync code uses them
    _BRIDGE_DIR = os.path.expanduser("~/.claude/memory/bridge")
    _SP_FLAGS = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    _bridge_thread_lock = threading.Lock()

    # ── Sync entry points ───────────────────────────────────────────────

    def _auto_sync_triggered(self):
        """Debounce elapsed — run the appropriate bridge sync mode."""
        initiator = getattr(self, "_pending_auto_sync_initiator", "auto")
        self._pending_auto_sync_initiator = None
        if initiator == "bootstrap":
            self._periodic_pull(initiator=initiator)
            return
        self._sync_bridge(initiator=initiator)

    def _arm_auto_sync(self, initiator: str):
        """Restart the debounce timer and remember the most recent initiator."""
        self._pending_auto_sync_initiator = initiator
        self._auto_sync_timer.start()

    def _log_sync_event(self, phase: str, *, initiator: str, mode: str, **fields):
        extras = " ".join(f"{key}={value!r}" for key, value in sorted(fields.items()))
        if extras:
            logger.info(
                "tray_sync phase=%s initiator=%s mode=%s %s",
                phase,
                initiator,
                mode,
                extras,
            )
        else:
            logger.info(
                "tray_sync phase=%s initiator=%s mode=%s",
                phase,
                initiator,
                mode,
            )

    def _request_db_refresh_from_worker(self):
        """Request a debounced UI refresh from a worker-safe path."""
        signal = getattr(self, "_bridge_refresh_requested", None)
        if signal is not None:
            signal.emit()
            return
        self._db_refresh_debounce.start()

    def _on_db_changed(self, path):
        """DB file changed — start/restart debounce timers."""
        self._db_refresh_debounce.start()
        self._refresh_db_watch_paths()
        if not getattr(self, "_auto_sync_enabled", True):
            return
        if self._sync_run_active or time.monotonic() < self._sync_cooldown_until:
            return
        self._arm_auto_sync("db_file_change")

    def _on_db_dir_changed(self, path):
        """Directory changed — catch WAL create/rotate events."""
        self._db_refresh_debounce.start()
        watch_paths_changed = self._refresh_db_watch_paths()
        if not getattr(self, "_auto_sync_enabled", True):
            return
        if self._sync_run_active or time.monotonic() < self._sync_cooldown_until:
            return
        if watch_paths_changed:
            self._arm_auto_sync("db_dir_watch_update")

    def _refresh_db_watch_paths(self):
        """Ensure DB watcher tracks the DB, WAL, and parent directory."""
        wanted_dirs = {self._db_watch_dir}
        wanted_files = set()
        db_path = Path(self.db.db_path if hasattr(self, "db") else self._db_path)
        for candidate in (db_path, Path(f"{db_path}-wal")):
            if candidate.exists():
                wanted_files.add(str(candidate))
        current_files = set(self._db_watcher.files())
        current_dirs = set(self._db_watcher.directories())
        stale = sorted((current_files - wanted_files) | (current_dirs - wanted_dirs))
        if stale:
            self._db_watcher.removePaths(stale)
        missing = sorted((wanted_files - current_files) | (wanted_dirs - current_dirs))
        if missing:
            self._db_watcher.addPaths(missing)
        return bool(stale or missing)

    def _maybe_schedule_initial_auto_sync(self):
        """Arm one startup sync after the tray app has initialized."""
        if not getattr(self, "_auto_sync_enabled", True):
            return
        if not getattr(self, "_initial_auto_sync_pending", False):
            return
        if self._sync_run_active or time.monotonic() < self._sync_cooldown_until:
            return
        arm = getattr(self, "_arm_auto_sync", None)
        if callable(arm):
            arm("bootstrap")
        else:
            self._auto_sync_timer.start()

    def _start_bridge_sync_thread(
        self,
        target,
        busy_message=None,
        *,
        initiator: str = "manual",
        mode: str = "sync",
    ):
        """Run at most one bridge sync thread at a time."""
        if not self._bridge_thread_lock.acquire(blocking=False):
            if busy_message:
                self.status.showMessage(busy_message, 3000)
            self._log_sync_event(
                "busy",
                initiator=initiator,
                mode=mode,
                message=busy_message or "busy",
            )
            return False

        if hasattr(self, "_initial_auto_sync_pending"):
            self._initial_auto_sync_pending = False
        self._sync_run_active = True
        auto_sync_timer = getattr(self, "_auto_sync_timer", None)
        if auto_sync_timer is not None:
            auto_sync_timer.stop()
        self._log_sync_event("started", initiator=initiator, mode=mode)

        def _wrapped():
            try:
                background_lock = getattr(self, "_background_db_write_lock", None)
                if background_lock is None:
                    target()
                else:
                    with background_lock:
                        target()
            finally:
                self._sync_cooldown_until = (
                    time.monotonic() + _POST_SYNC_DB_WATCH_COOLDOWN_SECONDS
                )
                self._sync_run_active = False
                self._log_sync_event("finished", initiator=initiator, mode=mode)
                self._bridge_thread_lock.release()

        threading.Thread(target=_wrapped, daemon=True).start()
        return True

    def _periodic_pull(self, initiator: str = "periodic_pull"):
        """Periodic pull from remote — catches changes from other machines."""
        if not getattr(self, "_auto_sync_enabled", True):
            return
        if not os.path.isdir(self._BRIDGE_DIR):
            return

        def _run():
            try:
                before_head = _bridge_head(self._BRIDGE_DIR)
                pull_result = _bridge_git_pull(self._BRIDGE_DIR)
                if pull_result.returncode != 0:
                    detail = (pull_result.stderr or pull_result.stdout).strip()
                    self._log_sync_event(
                        "result",
                        initiator=initiator,
                        mode="pull_only",
                        pulled=False,
                        changed=False,
                        error=detail or "git pull failed",
                    )
                    return

                after_head = _bridge_head(self._BRIDGE_DIR)
                # Bootstrap follows the same import path as periodic pull: when
                # HEAD advanced during pull, fall through to bridge_sync_worker
                # so the local SQLite absorbs remote changes (tombstones, edits).
                # Returning early here would leave local state stale after a
                # HEAD change — only git-pulled, never imported.
                if before_head and after_head and before_head == after_head:
                    self._log_sync_event(
                        "result",
                        initiator=initiator,
                        mode="pull_only",
                        pulled=True,
                        changed=False,
                        imported=0,
                    )
                    return

                import bridge_sync_worker

                stats = bridge_sync_worker.main(
                    pull_only=True,
                    progress_callback=lambda pct, label: self._bridge_progress.emit(
                        pct, label
                    ),
                )
                imported = stats.get("imported_new", 0) + stats.get(
                    "imported_updated", 0
                )
                self._log_sync_event(
                    "result",
                    initiator=initiator,
                    mode="pull_only",
                    imported=imported,
                    pushed=stats.get("pushed", False),
                    already_running=stats.get("already_running", False),
                )
                if imported:
                    self._bridge_done.emit(f"Pulled {imported} updates from remote")
                    self._request_db_refresh_from_worker()
            except Exception as exc:
                self._log_sync_event(
                    "error",
                    initiator=initiator,
                    mode="pull_only",
                    error=str(exc),
                )
                logger.warning("Periodic pull failed: %s", exc)

        self._start_bridge_sync_thread(
            _run,
            initiator=initiator,
            mode="pull_only",
        )

    def request_manual_sync(self):
        """Explicit user-triggered sync request."""
        self._sync_bridge(initiator="manual")

    def _sync_bridge(self, initiator: str = "manual"):
        """Sync memory bridge (pull + push + shared.json)."""
        if not os.path.isdir(self._BRIDGE_DIR):
            self.status.showMessage("Bridge dir not found", 3000)
            self._log_sync_event(
                "blocked",
                initiator=initiator,
                mode="sync",
                reason="bridge_dir_missing",
            )
            return
        if self._bridge_thread_lock.locked():
            self.status.showMessage("Sync already running", 3000)
            self._log_sync_event(
                "busy",
                initiator=initiator,
                mode="sync",
                reason="thread_lock",
            )
            return

        ui_profile = self._build_ui_profile()

        def _run():
            try:
                import bridge_sync_worker

                stats = bridge_sync_worker.main(
                    ui_profile=ui_profile,
                    progress_callback=lambda pct, label: self._bridge_progress.emit(
                        pct, label
                    ),
                )
                self._log_sync_event(
                    "result",
                    initiator=initiator,
                    mode="sync",
                    pushed=stats.get("pushed", False),
                    skipped=stats.get("skipped", False),
                    entities=stats.get("entities", 0),
                    tasks=stats.get("tasks", 0),
                    imported_new=stats.get("imported_new", 0),
                    imported_updated=stats.get("imported_updated", 0),
                    blocked_by_repo_state=stats.get("blocked_by_repo_state", False),
                    blocked_by_safety=stats.get("blocked_by_safety", False),
                )

                if stats.get("blocked_by_repo_state"):
                    self._bridge_done.emit(
                        f"Sync blocked: {stats.get('message', 'bridge repo needs attention')}"
                    )
                elif stats.get("already_running"):
                    self._bridge_done.emit("Sync already running")
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
                    detail = stats.get("message")
                    if detail:
                        self._bridge_done.emit(f"Sync incomplete — {detail}")
                    else:
                        self._bridge_done.emit(
                            "Sync incomplete — pull/import finished but push did not"
                        )
                else:
                    n_ent = stats.get("entities", 0)
                    n_tasks = stats.get("tasks", 0)
                    self._bridge_done.emit(f"Synced: {n_ent} entities, {n_tasks} tasks")
            except Exception as exc:
                self._log_sync_event(
                    "error",
                    initiator=initiator,
                    mode="sync",
                    error=str(exc),
                )
                self._bridge_done.emit(f"Sync error: {exc}")

        self._start_bridge_sync_thread(
            _run,
            busy_message="Sync already running",
            initiator=initiator,
            mode="sync",
        )

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

            bridge_tab_views = profile.get("tab_views")
            if isinstance(bridge_tab_views, dict):
                for key, view in bridge_tab_views.items():
                    if key in self._tab_views:
                        if view.get("sort") in self._SORT_MODES:
                            self._tab_views[key]["sort"] = view["sort"]
                        self._tab_views[key]["active"] = _normalize_filter_payload(
                            view.get("active", {})
                        )
                        self._tab_views[key]["excluded"] = _normalize_filter_payload(
                            view.get("excluded", {})
                        )
                        normalizer = getattr(self, "_normalize_tab_params", None)
                        if callable(normalizer):
                            self._tab_views[key]["params"] = normalizer(
                                key, view.get("params", {})
                            )
                        else:
                            self._tab_views[key]["params"] = dict(
                                view.get("params", {})
                            )
                # Sync working state from current tab
                cur_key = self._tab_keys[
                    min(getattr(self, "_saved_active_tab", 0), len(self._tab_keys) - 1)
                ]
                if cur_key in self._tab_views:
                    v = self._tab_views[cur_key]
                    self._sort_mode = v["sort"]
                    self._active_filters = _normalize_filter_payload(v["active"])
                    self._excluded_filters = _normalize_filter_payload(v["excluded"])

            geo_b64 = profile.get("geometry_b64")
            if geo_b64:
                from PyQt6.QtCore import QByteArray

                self.restoreGeometry(QByteArray(base64.b64decode(geo_b64)))
            _tt._update_theme_colors()
            self._save_ui_state()
        except (json.JSONDecodeError, OSError, KeyError, ValueError):
            pass

    def _build_ui_profile(self):
        """Serialize the current UI profile for bridge export."""
        from db_utils import now_iso

        # Import module-level globals from task_tray
        import task_tray as _tt

        serializable_views = {}
        for key, view in self._tab_views.items():
            serializable_views[key] = {
                "sort": view["sort"],
                "active": {
                    "priority": list(view["active"]["priority"]),
                    "due": list(view["active"]["due"]),
                    "project": sorted(
                        _normalize_filter_payload(view["active"])["project"]
                    ),
                },
                "excluded": {
                    "priority": list(view["excluded"]["priority"]),
                    "due": list(view["excluded"]["due"]),
                    "project": sorted(
                        _normalize_filter_payload(view["excluded"])["project"]
                    ),
                },
                "params": dict(view.get("params", {})),
            }

        profile = {
            "theme": _tt._theme_name,
            "font_size": _tt._font_size,
            "bold": _tt._bold,
            "active_tab": int(self._settings.value("active_tab", 0)),
            "tab_views": serializable_views,
            "updated_at": now_iso(),
        }
        geo = self._settings.value("geometry")
        if geo:
            profile["geometry_b64"] = base64.b64encode(bytes(geo)).decode("ascii")
        return profile
