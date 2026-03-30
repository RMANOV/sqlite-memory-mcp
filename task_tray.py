"""Task Tray — SQLite Task Manager.

System tray widget with dual mode: compact popup + full window.
Reads/writes directly to ~/.claude/memory/memory.db.
"""

import atexit
import copy
import faulthandler
import json
import logging
import os
import socket as _socket
import sqlite3
import sys
import tempfile
import threading
import uuid
import time
from datetime import datetime, timedelta, timezone


def _resolve_log_dir() -> str:
    for path in (
        os.path.expanduser("~/.claude/mcp_servers/sqlite_kb"),
        os.path.join(tempfile.gettempdir(), "sqlite-memory-mcp"),
    ):
        try:
            os.makedirs(path, exist_ok=True)
            return path
        except OSError:
            continue
    return tempfile.gettempdir()


def _open_log_file(path: str):
    try:
        return open(path, "a", encoding="utf-8", errors="replace")
    except OSError:
        return open(os.devnull, "a")


_log_dir = _resolve_log_dir()
_crash_log = _open_log_file(os.path.join(_log_dir, "crash.log"))
atexit.register(_crash_log.close)
try:
    faulthandler.enable(file=_crash_log)
except (OSError, RuntimeError, ValueError):
    pass
try:
    logging.basicConfig(
        filename=os.path.join(_log_dir, "task_tray.log"),
        level=logging.WARNING,
        format="%(asctime)s %(levelname)s %(message)s",
    )
except OSError:
    logging.basicConfig(
        filename=os.devnull,
        level=logging.WARNING,
        format="%(asctime)s %(levelname)s %(message)s",
    )
logger = logging.getLogger("task_tray")

_OPTIONAL_VECTOR_ERRORS = (
    ImportError,
    sqlite3.Error,
    OSError,
    RuntimeError,
    ValueError,
)
_OPTIONAL_PIPELINE_ERRORS = (
    ImportError,
    sqlite3.Error,
    OSError,
    RuntimeError,
    ValueError,
)

from task_search import TaskSearchEngine

from db_utils import (
    DB_PATH,
    MERGEABLE_FIELDS,
    TaskDAO,
    get_conn,
    is_overdue,
    now_iso,
    priority_sort_key,
    upsert_field_versions,
)
from schema import init_db

# Page size cap for "All" and "Done" tabs to keep QListWidget responsive
_TAB_PAGE_SIZE = 200


class TaskDB:
    """Direct sqlite3 wrapper for tasks table."""

    def __init__(self, db_path=None):
        self.db_path = db_path or DB_PATH
        self.on_change = None
        # Run shared schema migrations before opening the long-lived GUI connection.
        init_db(self.db_path)
        self._conn = sqlite3.connect(self.db_path, isolation_level=None, timeout=10)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=10000")
        self._repair_fts_if_needed()

        self._last_promote_time: float = 0.0
        self.search_engine = TaskSearchEngine()

        # Entity enrichment cache — pre-loaded obs preview + task count
        self._enrich_cache_obs: dict[int, str] = {}
        self._enrich_cache_tc: dict[int, int] = {}
        self._enrich_cache_lock = threading.Lock()
        self._enrich_refresh_lock = threading.Lock()
        threading.Thread(target=self._refresh_enrich_cache, daemon=True).start()

        self._wal_timer = QTimer(QApplication.instance())
        self._wal_timer.timeout.connect(self._wal_checkpoint)
        self._wal_timer.start(300_000)  # 5 minutes

    class _transact:
        """Explicit transaction block for multi-statement atomic writes.

        In autocommit mode (isolation_level=None), each execute() auto-commits.
        This context manager groups multiple statements into a single transaction.
        """

        def __init__(self, conn):
            self._conn = conn

        def __enter__(self):
            self._conn.execute("BEGIN")
            return self._conn

        def __exit__(self, exc_type, exc_val, exc_tb):
            if exc_type is None:
                self._conn.execute("COMMIT")
            else:
                try:
                    self._conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            return False

    def _wal_checkpoint(self):
        try:
            self._conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
        except sqlite3.Error:
            pass

    def _repair_fts_if_needed(self):
        """Check FTS5 indexes and rebuild if corrupted."""
        for fts_table in ("tasks_fts", "memory_fts"):
            try:
                self._conn.execute(
                    f"INSERT INTO {fts_table}({fts_table}, rank) VALUES('integrity-check', 1)"
                )
            except sqlite3.Error:
                try:
                    self._conn.execute(
                        f"INSERT INTO {fts_table}({fts_table}) VALUES('rebuild')"
                    )
                    self._conn.commit()
                    logging.getLogger("task_tray").warning(
                        "Repaired corrupted FTS index: %s", fts_table
                    )
                except sqlite3.Error as e:
                    logger.warning("FTS rebuild failed: %s", e)

    def close(self):
        self._wal_timer.stop()
        self._conn.close()

    def promote_due_today(self):
        """Auto-move tasks with due_date <= today (throttled to 60s)."""
        now = time.monotonic()
        if now - self._last_promote_time < 60:
            return 0
        self._last_promote_time = now
        return TaskDAO.promote_due_today(self._conn)

    def get_all_active(self):
        """Return all active tasks (excludes done, archived, cancelled)."""
        return TaskDAO.get_active(self._conn, columns=_UI_COLS)

    def get_done_tasks(self):
        """Return completed tasks, newest first."""
        return TaskDAO.get_done(self._conn, columns=_UI_COLS)

    def purge_old_done(self, days=30):
        """Delete done tasks older than `days` days. Returns count deleted."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        return TaskDAO.purge_done(self._conn, cutoff)

    def get_suggested_tasks(self, limit=20):
        """Return prioritized mix: overdue + high/critical + nearest due."""
        return TaskDAO.get_suggested(self._conn, limit)

    def get_all_notes(self):
        """All notes (never-deleted). Excludes archived/cancelled."""
        return TaskDAO.get_notes(self._conn)

    def get_project_names(self):
        """Return project names sorted by active task count (most first)."""
        return TaskDAO.get_project_names(self._conn)

    def get_summary(self, tasks=None):
        """Return dict with total, overdue counts. Accepts pre-fetched tasks."""
        if tasks is None:
            tasks = self.get_all_active()
        overdue = sum(1 for t in tasks if is_overdue(t["due_date"]))
        return {"total": len(tasks), "overdue": overdue}

    def get_tasks(self, section=None):
        """Return tasks excluding archived/cancelled, optionally filtered by section."""
        if section:
            rows = self._conn.execute(
                f"SELECT {_UI_COLS} FROM tasks "
                "WHERE status NOT IN ('archived', 'cancelled') "
                "AND section = ? "
                "ORDER BY created_at",
                (section,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                f"SELECT {_UI_COLS} FROM tasks "
                "WHERE status NOT IN ('archived', 'cancelled') "
                "ORDER BY created_at"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_overdue(self):
        """Return active tasks with past due_date."""
        rows = self._conn.execute(
            f"SELECT {_UI_COLS} FROM tasks "
            "WHERE due_date < date('now') "
            "AND due_date IS NOT NULL "
            "AND status NOT IN ('done', 'archived', 'cancelled') "
            "ORDER BY due_date"
        ).fetchall()
        return [dict(r) for r in rows]

    def add_task(
        self,
        title,
        section="inbox",
        priority="medium",
        due_date=None,
        project=None,
        status="not_started",
        description=None,
        type="task",
    ):
        """Insert new task, return its ID."""
        task_id = str(uuid.uuid4())
        now = now_iso()
        with self._transact(self._conn):
            TaskDAO.create(
                self._conn,
                task_id,
                title,
                now,
                description=description,
                status=status,
                section=section,
                priority=priority,
                due_date=due_date,
                project=project,
                type=type,
            )
            upsert_field_versions(
                self._conn,
                task_id,
                MERGEABLE_FIELDS,
                now,
                new_values={
                    "title": title,
                    "description": description,
                    "status": status,
                    "section": section,
                    "priority": priority,
                    "due_date": due_date,
                    "project": project,
                    "type": type,
                },
            )
        if self.on_change:
            self.on_change()
        return task_id

    @staticmethod
    def _recurring_series_key(raw: str | None) -> str | None:
        """Normalize recurring config for sibling matching."""
        if not raw:
            return None
        try:
            config = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return raw
        if not isinstance(config, dict):
            return raw
        config = dict(config)
        config.pop("last_spawned", None)
        return json.dumps(config, sort_keys=True, separators=(",", ":"))

    def mark_done(self, task_id):
        """Set status=done."""
        now = now_iso()
        with self._transact(self._conn):
            old_row = self._conn.execute(
                "SELECT status FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            old_status = old_row["status"] if old_row else None
            updated = TaskDAO.update(
                self._conn, task_id, {"status": "done", "updated_at": now}
            )
            if updated == 0:
                return False
            upsert_field_versions(
                self._conn,
                task_id,
                ("status",),
                now,
                old_values={"status": old_status},
                new_values={"status": "done"},
            )
        if self.on_change:
            self.on_change()
        return True

    def update_task(self, task_id, **fields):
        """Update arbitrary fields on a task."""
        if not fields:
            return False
        now = now_iso()
        changed = tuple(k for k in fields if k in MERGEABLE_FIELDS)
        fields["updated_at"] = now
        with self._transact(self._conn):
            if changed:
                old_row = self._conn.execute(
                    f"SELECT {', '.join(changed)} FROM tasks WHERE id = ?",
                    (task_id,),
                ).fetchone()
                old_values = {k: old_row[k] for k in changed} if old_row else {}
            else:
                old_values = {}
            updated = TaskDAO.update(self._conn, task_id, fields)
            if updated == 0:
                return False
            if changed:
                upsert_field_versions(
                    self._conn,
                    task_id,
                    changed,
                    now,
                    old_values=old_values,
                    new_values={k: fields[k] for k in changed},
                )
        if self.on_change:
            self.on_change()
        return True

    def delete_task(self, task_id):
        """Soft-delete: cancel task (creates tombstone for bridge sync).
        For recurring tasks, also cancel done siblings to stop respawn cycle."""
        now = now_iso()
        with self._transact(self._conn):
            # Read task metadata before cancelling
            row = TaskDAO.get_by_id(
                self._conn,
                task_id,
                "title, recurring, project, parent_id, type, status",
            )
            old_status = row["status"] if row else None
            # Cancel the target task
            updated = TaskDAO.update(
                self._conn, task_id, {"status": "cancelled", "updated_at": now}
            )
            if updated == 0:
                return False
            upsert_field_versions(
                self._conn,
                task_id,
                ("status",),
                now,
                old_values={"status": old_status},
                new_values={"status": "cancelled"},
            )
            # For recurring tasks: cancel all done siblings to break spawn cycle
            if row and row["recurring"]:
                series_key = self._recurring_series_key(row["recurring"])
                sibling_ids = [
                    s["id"]
                    for s in self._conn.execute(
                        "SELECT id, recurring FROM tasks WHERE title=? AND status='done' "
                        "AND recurring IS NOT NULL AND id!=? AND project IS ? "
                        "AND parent_id IS ? AND type=?",
                        (
                            row["title"],
                            task_id,
                            row["project"],
                            row["parent_id"],
                            row["type"],
                        ),
                    ).fetchall()
                    if self._recurring_series_key(s["recurring"]) == series_key
                ]
                if sibling_ids:
                    ph = ",".join("?" * len(sibling_ids))
                    self._conn.execute(
                        f"UPDATE tasks SET status='cancelled', updated_at=? "
                        f"WHERE id IN ({ph})",
                        [now, *sibling_ids],
                    )
                    for sid in sibling_ids:
                        upsert_field_versions(
                            self._conn,
                            sid,
                            ("status",),
                            now,
                            old_values={"status": "done"},
                            new_values={"status": "cancelled"},
                        )
        if self.on_change:
            self.on_change()
        return True

    # ── Entity Link helpers (v2.2.0) ─────────────────────────────────

    def search_entities(self, query: str, limit: int = 10) -> list[dict]:
        """FTS5 search for entities (for autocomplete in link dialog)."""
        if not query or len(query.strip()) < 2:
            return []
        words = query.strip().split()
        fts_q = " OR ".join('"' + w.replace('"', '""') + '"' for w in words if w)
        if not fts_q:
            return []
        rows = self._conn.execute(
            "SELECT rowid, name, entity_type, "
            "(SELECT COUNT(*) FROM observations WHERE entity_id = memory_fts.rowid) AS obs_count "
            "FROM memory_fts WHERE memory_fts MATCH ? LIMIT ?",
            (fts_q, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def search_entities_hybrid(
        self, query: str, limit: int = 10, use_vector: bool = True
    ) -> list[dict]:
        """Hybrid entity search: FTS5 + optional vector, enriched with obs preview + task count."""
        fts_results = self.search_entities(query, limit)
        if not fts_results:
            return []

        # Optional vector search via vec_search module
        if use_vector:
            try:
                from vec_search import vector_search, rrf_merge

                vec_results = vector_search(self._conn, query, limit)
                if vec_results:
                    # Normalize FTS results to match rrf_merge expected format (eid key)
                    fts_for_rrf = [
                        {
                            "eid": r["rowid"],
                            "name": r["name"],
                            "entity_type": r.get("entity_type", ""),
                            "project": None,
                        }
                        for r in fts_results
                    ]
                    merged = rrf_merge(fts_for_rrf, vec_results, k=60)
                    # Rebuild result list from merged ranking
                    by_id = {r["rowid"]: r for r in fts_results}
                    for vr in vec_results:
                        if vr["eid"] not in by_id:
                            by_id[vr["eid"]] = {
                                "rowid": vr["eid"],
                                "name": vr["name"],
                                "entity_type": vr.get("entity_type", ""),
                                "obs_count": 0,
                            }
                    fts_results = [
                        by_id[m["eid"]] for m in merged if m["eid"] in by_id
                    ][:limit]
            except _OPTIONAL_VECTOR_ERRORS as exc:
                logger.debug("Entity vector search unavailable: %s", exc)

        # Batch enrich: obs preview + task count
        eids = [r["rowid"] for r in fts_results]
        if not eids:
            return []
        placeholders = ",".join("?" * len(eids))

        obs_map = {}
        for row in self._conn.execute(
            f"SELECT entity_id, content FROM observations WHERE entity_id IN ({placeholders}) "
            "GROUP BY entity_id",
            eids,
        ).fetchall():
            obs_map[row["entity_id"]] = row["content"][:80]

        tc_map = {}
        try:
            for row in self._conn.execute(
                f"SELECT entity_id, COUNT(*) as cnt FROM task_entity_links "
                f"WHERE entity_id IN ({placeholders}) GROUP BY entity_id",
                eids,
            ).fetchall():
                tc_map[row["entity_id"]] = row["cnt"]
        except sqlite3.OperationalError as exc:
            logger.debug("Entity task link counts unavailable: %s", exc)

        return [
            {
                "entity_id": r["rowid"],
                "name": r["name"],
                "entity_type": r.get("entity_type", ""),
                "obs_preview": obs_map.get(r["rowid"], ""),
                "obs_count": r.get("obs_count", 0),
                "task_count": tc_map.get(r["rowid"], 0),
                "_is_entity": True,
            }
            for r in fts_results
        ]

    def _refresh_enrich_cache(self):
        """Bulk-load obs preview + task count for all entities. Thread-safe."""
        if not self._enrich_refresh_lock.acquire(blocking=False):
            return
        try:
            with get_conn(self.db_path) as conn:
                obs = {}
                for row in conn.execute(
                    "SELECT entity_id, content FROM observations GROUP BY entity_id"
                ).fetchall():
                    obs[row["entity_id"]] = row["content"][:80]
                tc = {}
                try:
                    for row in conn.execute(
                        "SELECT entity_id, COUNT(*) as cnt FROM task_entity_links GROUP BY entity_id"
                    ).fetchall():
                        tc[row["entity_id"]] = row["cnt"]
                except sqlite3.OperationalError as exc:
                    logger.debug("Entity enrich task counts unavailable: %s", exc)
                with self._enrich_cache_lock:
                    self._enrich_cache_obs = obs
                    self._enrich_cache_tc = tc
        except sqlite3.Error as e:
            logger.warning("Enrich cache refresh failed: %s", e)
        finally:
            self._enrich_refresh_lock.release()

    def _get_enrich(self, entity_id: int) -> tuple[str, int]:
        """Get cached (obs_preview, task_count). Returns ("", 0) on miss."""
        with self._enrich_cache_lock:
            return (
                self._enrich_cache_obs.get(entity_id, ""),
                self._enrich_cache_tc.get(entity_id, 0),
            )

    def search_entities_fast(self, query: str, limit: int = 10) -> list[dict]:
        """FTS5 + vector search with cached enrichment. Thread-safe (own connection)."""
        if not query or len(query.strip()) < 2:
            return []
        words = query.strip().split()
        fts_q = " OR ".join('"' + w.replace('"', '""') + '"' for w in words if w)
        if not fts_q:
            return []
        with get_conn(self.db_path) as conn:
            try:
                rows = conn.execute(
                    "SELECT rowid, name, entity_type, "
                    "(SELECT COUNT(*) FROM observations WHERE entity_id = memory_fts.rowid) AS obs_count "
                    "FROM memory_fts WHERE memory_fts MATCH ? LIMIT ?",
                    (fts_q, limit),
                ).fetchall()
            except sqlite3.Error as exc:
                logger.warning("Entity FTS search failed: %s", exc)
                return []
            fts_results = [dict(r) for r in rows]

            # Vector search (ALWAYS enabled) — graceful degradation if deps missing
            try:
                from vec_search import vector_search, rrf_merge, load_vec

                if load_vec(conn):
                    vec_results = vector_search(conn, query, limit)
                    if vec_results and fts_results:
                        fts_for_rrf = [
                            {
                                "eid": r["rowid"],
                                "name": r["name"],
                                "entity_type": r.get("entity_type", ""),
                                "project": None,
                            }
                            for r in fts_results
                        ]
                        merged = rrf_merge(fts_for_rrf, vec_results, k=60)
                        by_id = {r["rowid"]: r for r in fts_results}
                        for vr in vec_results:
                            if vr["eid"] not in by_id:
                                by_id[vr["eid"]] = {
                                    "rowid": vr["eid"],
                                    "name": vr["name"],
                                    "entity_type": vr.get("entity_type", ""),
                                    "obs_count": 0,
                                }
                        fts_results = [
                            by_id[m["eid"]] for m in merged if m["eid"] in by_id
                        ][:limit]
                    elif vec_results and not fts_results:
                        fts_results = [
                            {
                                "rowid": vr["eid"],
                                "name": vr["name"],
                                "entity_type": vr.get("entity_type", ""),
                                "obs_count": 0,
                            }
                            for vr in vec_results[:limit]
                        ]
            except _OPTIONAL_VECTOR_ERRORS as exc:
                logger.debug("Fast entity vector search unavailable: %s", exc)

        # Apply cached enrichment (zero SQL queries)
        results = []
        for r in fts_results:
            eid = r["rowid"]
            obs_preview, task_count = self._get_enrich(eid)
            results.append(
                {
                    "entity_id": eid,
                    "name": r["name"],
                    "entity_type": r.get("entity_type", ""),
                    "obs_preview": obs_preview,
                    "obs_count": r.get("obs_count", 0),
                    "task_count": task_count,
                    "_is_entity": True,
                }
            )
        return results

    def link_task_entity(
        self, task_id: str, entity_id: int, link_type: str = "manual"
    ) -> bool:
        """Create a manual link between a task and an entity."""
        now = now_iso()
        try:
            with self._transact(self._conn) as conn:
                TaskDAO.link_entity(conn, task_id, entity_id, link_type, created_at=now)
            return True
        except (sqlite3.OperationalError, sqlite3.IntegrityError):
            return False

    def get_task_links(self, task_id: str) -> list[dict]:
        """Get all entities linked to a task."""
        try:
            return TaskDAO.get_task_links(self._conn, task_id)
        except sqlite3.OperationalError:
            return []

    def unlink_task_entity(self, task_id: str, entity_id: int) -> bool:
        """Remove a link between a task and an entity."""
        with self._transact(self._conn) as conn:
            removed = TaskDAO.unlink_entity(conn, task_id, entity_id)
        return removed > 0


# ── UI Layer ────────────────────────────────────────────────────────

from PyQt6.QtWidgets import (
    QApplication,
    QSystemTrayIcon,
    QMenu,
    QLabel,
    QLineEdit,
    QMainWindow,
    QTabWidget,
    QListWidgetItem,
    QToolBar,
    QToolButton,
    QStatusBar,
    QDialog,
    QProgressBar,
)
from PyQt6.QtGui import QIcon, QAction, QActionGroup, QColor
from PyQt6.QtCore import (
    QFileSystemWatcher,
    QSettings,
    Qt,
    QTimer,
    pyqtSignal,
)
from pathlib import Path

from tray_filters import FilterMixin
from tray_sync import BridgeSyncMixin
import tray_dialogs as _td
from tray_dialogs import (
    # Theme system
    _THEMES,
    _theme_name,
    _font_size,
    _bold,
    _T,
    _update_theme_colors,
    _build_main_style,
    _build_filter_style,
    _build_list_style,
    _REFRESH_INTERVAL_MS,
    # Constants
    _UI_COLS,
    # Dialog classes + TaskListWidget
    TrayPopup,
    EditTaskDialog,
    ReminderPopupDialog,
    TaskListWidget,
    create_tray_icon_pixmap,
    _suggested_sort_key,
)

_PURGE_INTERVAL_MS = 3_600_000  # 1 hour

# Per-tab sort/filter constants
_FIXED_VIEW_TABS = frozenset({"suggested", "projects"})
_DEFAULT_TAB_VIEW = {
    "sort": "priority",
    "active": {"priority": set(), "due": set(), "project": set()},
    "excluded": {"priority": set(), "due": set(), "project": set()},
}


class FullWindow(QMainWindow, BridgeSyncMixin, FilterMixin):
    """Full task manager window with tabs, search, sort, and suggested view."""

    _bridge_done = pyqtSignal(str)
    _bridge_progress = pyqtSignal(int, str)  # (percent, step_label)
    _enrich_done = pyqtSignal(str)
    _enrich_running = pyqtSignal(str)
    _entity_search_done = pyqtSignal(list, int)  # (entity_results, seq_id)

    # Sort modes cycle: priority → due → created → priority ...
    _SORT_MODES = ("priority", "due", "created", "project")
    _SORT_LABELS = {
        "priority": "Sort: Priority",
        "due": "Sort: Due Date",
        "created": "Sort: Created",
        "project": "Sort: Project",
    }

    def __init__(self, db, parent=None):
        super().__init__(parent)
        self.db = db
        self._sort_mode = "priority"
        self._search_text = ""
        self._entity_results: list[dict] = []
        self._entity_seq_id = 0
        self._entity_search_lock = threading.Lock()
        self._entity_search_running = False
        self._pending_entity_search: tuple[int, str] | None = None
        self._pre_search_tab: int | None = None  # tab to restore after search clears
        self._active_filters = {"priority": set(), "due": set(), "project": set()}
        self._excluded_filters = {"priority": set(), "due": set(), "project": set()}
        self._minus_mode = False
        self._filter_chips = {}
        self._last_projects = None
        self._project_cache_time: float = 0.0  # monotonic time of last project query
        self._filtered_cache: dict[str, list] = {}  # lazy tab rendering cache
        self._search_engine = db.search_engine
        self.setWindowTitle("Task Manager \u2014 SQLite Memory")
        self.resize(800, 600)

        primary = QApplication.primaryScreen()
        if primary:
            screen = primary.availableGeometry()
            self.move(screen.center() - self.rect().center())

        self._settings = QSettings("TaskTray", "FullWindow")
        geometry = self._settings.value("geometry")
        if geometry:
            self.restoreGeometry(geometry)

        # Restore appearance settings (mutate tray_dialogs module globals)
        _td._theme_name = self._settings.value("theme", "blue")
        if _td._theme_name not in _THEMES:
            _td._theme_name = "blue"
        _td._font_size = int(self._settings.value("font_size", 13))
        _td._bold = self._settings.value("bold", "false") == "true"
        _update_theme_colors()

        self.setStyleSheet(_build_main_style())

        # Central widget with tabs
        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)

        # Tab order: Suggested, Today, Inbox, Next, Notes, All, Done
        self._tab_keys = [
            "suggested",
            "today",
            "inbox",
            "next",
            "projects",
            "notes",
            "all",
            "done",
        ]
        self._tab_labels = {
            "suggested": "Suggested",
            "today": "Today",
            "inbox": "Inbox",
            "next": "Next",
            "projects": "Projects",
            "notes": "Notes",
            "all": "All",
            "done": "Done",
        }
        self.tab_lists = {}
        for key in self._tab_keys:
            lw = TaskListWidget(self.db)
            lw._search_engine = self._search_engine
            lw.itemChanged.connect(lambda item, k=key: self._on_item_changed(item))
            self.tab_lists[key] = lw
            self.tabs.addTab(lw, self._tab_labels[key])

        # B1: Per-tab view state dict (sort + filters per tab)
        self._tab_views = {
            key: copy.deepcopy(_DEFAULT_TAB_VIEW) for key in self._tab_keys
        }
        self._current_tab_idx = 0  # track for state swapping on tab change

        # B2: Restore per-tab state from QSettings
        parsed = {}
        try:
            raw_views = self._settings.value("tab_views", "{}")
            parsed = json.loads(raw_views) if isinstance(raw_views, str) else {}
            for key, view in parsed.items():
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
        except (json.JSONDecodeError, TypeError, ValueError, AttributeError):
            pass

        # Set working state from the initial tab
        self._saved_active_tab = int(self._settings.value("active_tab", 0))
        initial_key = self._tab_keys[
            min(self._saved_active_tab, len(self._tab_keys) - 1)
        ]
        if initial_key in self._tab_views:
            v = self._tab_views[initial_key]
            self._sort_mode = v["sort"]
            self._active_filters = v["active"]
            self._excluded_filters = v["excluded"]

        # First-run recovery: if QSettings has no tab_views, try bridge profile
        if self._settings.value("tab_views") is None:
            self._restore_profile_from_bridge()

        # Restore saved active tab
        self.tabs.setCurrentIndex(min(self._saved_active_tab, len(self._tab_keys) - 1))
        self._current_tab_idx = self.tabs.currentIndex()
        self.tabs.currentChanged.connect(self._on_tab_changed)

        # Toolbar: actions + search + sort
        toolbar = QToolBar()
        toolbar.setMovable(False)
        add_action = QAction("+ Add Task", self)
        add_action.triggered.connect(self._add_task)
        toolbar.addAction(add_action)
        refresh_action = QAction("Refresh + Sync", self)
        refresh_action.triggered.connect(self._refresh_and_sync)
        toolbar.addAction(refresh_action)
        toolbar.addSeparator()

        # ── Intelligence v2 enrich buttons (amber→orange→red gradient) ──
        for obj_name, label, depth in [
            ("enrich_quick", "\u26a1 Quick", "quick"),
            ("enrich_standard", "\U0001f52c Std", "standard"),
            ("enrich_deep", "\U0001f9e0 Deep", "deep"),
        ]:
            btn = QToolButton()
            btn.setText(label)
            btn.setObjectName(obj_name)
            btn.setToolTip(f"Enrich context: {depth}")
            btn.clicked.connect(lambda checked, d=depth: self._run_enrich(d))
            toolbar.addWidget(btn)
        toolbar.addSeparator()

        # Instant search bar (debounced 300ms)
        self._search_input = QLineEdit()
        self._search_input.setObjectName("search")
        self._search_input.setPlaceholderText("Search tasks...")
        self._search_input.setClearButtonEnabled(True)
        self._search_input.textChanged.connect(self._on_search)
        toolbar.addWidget(self._search_input)
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(300)
        self._search_timer.timeout.connect(self.refresh)

        toolbar.addSeparator()

        # Sort ▾ mega-button
        self._sort_btn = QToolButton()
        self._sort_btn.setText(f"{self._SORT_LABELS[self._sort_mode]} \u25be")
        self._sort_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        sort_menu = QMenu(self)
        self._sort_action_group = QActionGroup(self)
        self._sort_action_group.setExclusive(True)
        self._sort_actions = {}
        for mode in self._SORT_MODES:
            act = QAction(self._SORT_LABELS[mode].replace("Sort: ", ""), self)
            act.setCheckable(True)
            act.setChecked(mode == self._sort_mode)
            act.triggered.connect(lambda checked, m=mode: self._set_sort(m))
            self._sort_action_group.addAction(act)
            sort_menu.addAction(act)
            self._sort_actions[mode] = act
        sort_menu.addSeparator()
        reset_sort_act = QAction("Reset Sort && Filters", self)
        reset_sort_act.triggered.connect(self._reset_sort_filters)
        sort_menu.addAction(reset_sort_act)
        self._sort_btn.setMenu(sort_menu)
        toolbar.addWidget(self._sort_btn)

        toolbar.addSeparator()

        # View ▾ mega-button
        self._view_btn = QToolButton()
        self._view_btn.setText("View \u25be")
        self._view_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        view_menu = QMenu(self)

        # Theme sub-section
        self._theme_action_group = QActionGroup(self)
        self._theme_action_group.setExclusive(True)
        self._theme_actions = {}
        theme_items = [
            ("blue", "\u25c6 Blue (default)"),
            ("black", "\u25fc True Black"),
            ("light", "\u25fb Light"),
        ]
        for name, label in theme_items:
            act = QAction(label, self)
            act.setCheckable(True)
            act.setChecked(name == _theme_name)
            act.triggered.connect(lambda checked, n=name: self._set_theme(n))
            self._theme_action_group.addAction(act)
            view_menu.addAction(act)
            self._theme_actions[name] = act
        view_menu.addSeparator()

        font_down_act = QAction("A\u2212  Smaller Font", self)
        font_down_act.triggered.connect(self._font_down)
        view_menu.addAction(font_down_act)
        font_up_act = QAction("A+  Larger Font", self)
        font_up_act.triggered.connect(self._font_up)
        view_menu.addAction(font_up_act)
        self._bold_action = QAction("Bold", self)
        self._bold_action.setCheckable(True)
        self._bold_action.setChecked(_bold)
        self._bold_action.triggered.connect(self._toggle_bold)
        view_menu.addAction(self._bold_action)
        view_menu.addSeparator()

        # ── Auto-Enrich depth (Intelligence v2) ──
        enrich_header = QAction("Auto-Enrich before Sync:", self)
        enrich_header.setEnabled(False)
        view_menu.addAction(enrich_header)
        self._enrich_depth_group = QActionGroup(self)
        self._enrich_depth_group.setExclusive(True)
        saved_depth = self._settings.value("auto_enrich_depth", "off")
        for depth_val, depth_label in [
            ("off", "Off"),
            ("quick", "\u26a1 Quick"),
            ("standard", "\U0001f52c Standard"),
            ("deep", "\U0001f9e0 Deep"),
        ]:
            act = QAction(depth_label, self)
            act.setCheckable(True)
            act.setChecked(depth_val == saved_depth)
            act.triggered.connect(
                lambda checked, d=depth_val: self._set_enrich_depth(d)
            )
            self._enrich_depth_group.addAction(act)
            view_menu.addAction(act)
        view_menu.addSeparator()

        reset_view_act = QAction("Reset View", self)
        reset_view_act.triggered.connect(self._reset_view)
        view_menu.addAction(reset_view_act)
        self._view_btn.setMenu(view_menu)
        toolbar.addWidget(self._view_btn)

        self.addToolBar(toolbar)

        # Filter chip bar
        self._filter_bar = QToolBar("Filters")
        self._filter_bar.setMovable(False)
        self._filter_bar.setStyleSheet(_build_filter_style())
        self._build_filter_chips()
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, self._filter_bar)

        # Hide sort/filter UI for fixed tabs on initial load
        _initial_key = self._tab_keys[
            min(self._saved_active_tab, len(self._tab_keys) - 1)
        ]
        _is_fixed_initial = _initial_key in _FIXED_VIEW_TABS
        self._sort_btn.setVisible(not _is_fixed_initial)

        # Status bar
        self.status = QStatusBar()
        self.setStatusBar(self.status)

        # Last sync timestamp label (permanent, right corner)
        self._sync_label = QLabel("")
        self._sync_label.setStyleSheet(
            f"color: {_T()['text2']}; font-size: {_font_size - 2}px; padding-right: 8px;"
        )
        self.status.addPermanentWidget(self._sync_label)

        # Bridge sync progress bar (hidden by default, replaces label visually)
        self._sync_bar = QProgressBar()
        self._sync_bar.setFixedWidth(280)
        self._sync_bar.setTextVisible(True)
        self._sync_bar.setRange(0, 100)
        self._sync_bar.hide()
        self.status.addPermanentWidget(self._sync_bar)

        # Bridge sync signals (thread-safe → main thread)
        self._bridge_progress.connect(self._on_sync_progress)
        self._bridge_done.connect(self._on_sync_done)

        # Intelligence v2 enrich signals
        self._enrich_in_progress = False
        self._enrich_running.connect(lambda msg: self.status.showMessage(msg, 30000))
        self._enrich_done.connect(self._on_enrich_done)
        self._entity_search_done.connect(self._on_entity_results)

        # Auto-refresh every 30s
        self._refresh_timer = QTimer(self)
        self._refresh_timer.timeout.connect(self.refresh)

        # Purge done tasks once at startup, then hourly
        self._last_purged = self.db.purge_old_done(days=30)
        self._purge_timer = QTimer(self)
        self._purge_timer.timeout.connect(self._run_purge)
        self._purge_timer.start(_PURGE_INTERVAL_MS)

        # Auto-sync: watch DB, WAL, and parent directory so WAL-only writes are seen.
        self._db_watch_dir = str(Path(self.db.db_path).parent)
        self._db_watcher = QFileSystemWatcher(self)
        self._refresh_db_watch_paths()
        self._db_watcher.fileChanged.connect(self._on_db_changed)
        self._db_watcher.directoryChanged.connect(self._on_db_dir_changed)
        self._auto_sync_timer = QTimer(self)
        self._auto_sync_timer.setSingleShot(True)
        self._auto_sync_timer.setInterval(60_000)  # 60s debounce
        self._auto_sync_timer.timeout.connect(self._auto_sync_triggered)

        # Periodic pull: import remote changes even without local edits
        self._periodic_pull_timer = QTimer(self)
        self._periodic_pull_timer.setInterval(5 * 60_000)  # 5 minutes
        self._periodic_pull_timer.timeout.connect(self._periodic_pull)
        self._periodic_pull_timer.start()
        self._db_refresh_debounce = QTimer(self)
        self._db_refresh_debounce.setSingleShot(True)
        self._db_refresh_debounce.setInterval(500)  # 500ms UI debounce
        self._db_refresh_debounce.timeout.connect(self.refresh)

        # Process recurring tasks at startup
        self._process_recurring()

        self.refresh()

    def _on_db_changed(self, path):
        """DB file changed — start/restart debounce timers."""
        self._auto_sync_timer.start()  # 60s bridge sync debounce
        self._db_refresh_debounce.start()  # 500ms UI refresh debounce
        self._refresh_db_watch_paths()

    def _on_db_dir_changed(self, path):
        """Directory changed — catch WAL create/rotate events."""
        self._auto_sync_timer.start()
        self._db_refresh_debounce.start()
        self._refresh_db_watch_paths()

    def _refresh_db_watch_paths(self):
        """Ensure DB watcher tracks the DB, WAL, and parent directory."""
        wanted = {self._db_watch_dir}
        db_path = Path(self.db.db_path)
        for candidate in (db_path, Path(f"{self.db.db_path}-wal")):
            if candidate.exists():
                wanted.add(str(candidate))
        current = set(self._db_watcher.files()) | set(self._db_watcher.directories())
        missing = sorted(wanted - current)
        if missing:
            self._db_watcher.addPaths(missing)

    def _run_purge(self):
        self._last_purged = self.db.purge_old_done(days=30)

    def _process_recurring(self):
        """Process recurring tasks silently (idempotent)."""
        try:
            from recurring_tasks import process_recurring

            with get_conn() as conn:
                created = process_recurring(conn, dry_run=False)
            if created:
                self.refresh()
        except _OPTIONAL_PIPELINE_ERRORS as exc:
            logging.getLogger("task_tray").warning("recurring: %s", exc)

    # ── Appearance ─────────────────────────────────────────────────────

    def _apply_appearance(self):
        """Rebuild all stylesheets from current theme/font/bold state."""
        _update_theme_colors()
        self.setStyleSheet(_build_main_style())
        self._filter_bar.setStyleSheet(_build_filter_style())
        for lw in self.tab_lists.values():
            lw.setStyleSheet(_build_list_style())
            lw.viewport().update()
        self._settings.setValue("theme", _td._theme_name)
        self._settings.setValue("font_size", _td._font_size)
        self._settings.setValue("bold", "true" if _td._bold else "false")
        self._build_filter_chips()
        self._save_ui_state()
        self.refresh()

    def _save_ui_state(self):
        """Persist all UI state to QSettings (per-tab views)."""
        # Sync working state back to current tab before serializing
        idx = getattr(self, "_current_tab_idx", 0)
        if idx < len(self._tab_keys):
            key = self._tab_keys[idx]
            if key in self._tab_views:
                self._tab_views[key] = {
                    "sort": self._sort_mode,
                    "active": copy.deepcopy(self._active_filters),
                    "excluded": copy.deepcopy(self._excluded_filters),
                }

        # Serialize per-tab views
        serializable = {}
        for key, view in self._tab_views.items():
            serializable[key] = {
                "sort": view["sort"],
                "active": {k: list(v) for k, v in view["active"].items()},
                "excluded": {k: list(v) for k, v in view["excluded"].items()},
            }
        self._settings.setValue("tab_views", json.dumps(serializable))
        self._settings.setValue("active_tab", self.tabs.currentIndex())

    def _font_down(self):
        if _td._font_size > 10:
            _td._font_size -= 1
            self._apply_appearance()

    def _font_up(self):
        if _td._font_size < 20:
            _td._font_size += 1
            self._apply_appearance()

    def _toggle_bold(self, checked):
        _td._bold = checked
        if hasattr(self, "_bold_action"):
            self._bold_action.setChecked(_td._bold)
        self._apply_appearance()

    def _set_theme(self, name):
        _td._theme_name = name
        if hasattr(self, "_theme_actions") and name in self._theme_actions:
            self._theme_actions[name].setChecked(True)
        self._apply_appearance()

    def _set_enrich_depth(self, depth):
        """Set auto-enrich depth for pre-sync pipeline."""
        self._settings.setValue("auto_enrich_depth", depth)

    def _set_sort(self, mode):
        """Set sort mode from mega-button menu."""
        self._sort_mode = mode
        self._sort_btn.setText(f"{self._SORT_LABELS[mode]} \u25be")
        self._save_ui_state()
        self.refresh()

    def _reset_sort_filters(self):
        """Reset sort to priority and clear all filter chips."""
        self._sort_mode = "priority"
        self._sort_btn.setText(f"{self._SORT_LABELS['priority']} \u25be")
        if hasattr(self, "_sort_actions") and "priority" in self._sort_actions:
            self._sort_actions["priority"].setChecked(True)
        for s in self._active_filters.values():
            s.clear()
        for s in self._excluded_filters.values():
            s.clear()
        if hasattr(self, "_minus_btn"):
            self._minus_btn.setChecked(False)
        self._minus_mode = False
        for btn in self._filter_chips.values():
            btn.setChecked(False)
        self._update_clear_btn()
        self._save_ui_state()
        self.refresh()

    def _reset_view(self):
        """Reset theme=blue, font=13, bold=off."""
        _td._theme_name = "blue"
        _td._font_size = 13
        _td._bold = False
        if hasattr(self, "_theme_actions") and "blue" in self._theme_actions:
            self._theme_actions["blue"].setChecked(True)
        if hasattr(self, "_bold_action"):
            self._bold_action.setChecked(False)
        self._apply_appearance()

    # ── Bridge sync (see tray_sync.BridgeSyncMixin) ─────────────────

    def _refresh_and_sync(self):
        """Refresh task list then sync memory bridge to GitHub."""
        self.refresh()
        self._sync_bridge()

    def _run_enrich(self, depth: str = "quick"):
        """Run Intelligence v2 enrich pipeline in background thread."""
        if getattr(self, "_enrich_in_progress", False):
            self.status.showMessage("Enrich already running...", 2000)
            return
        self._enrich_in_progress = True
        self._enrich_running.emit(f"AI Enqueue ({depth})...")

        def _work():
            try:
                from intelligence_v2 import assess_context as _assess
                from claim_graph import extract_candidate_claims as _extract
                from context_packer import (
                    build_context_pack as _pack,
                    warm_recent_task_packs as _warm_task_packs,
                )
                from impact_graph import explain_impact as _impact
                from db_utils import get_conn
                import time

                assessed = 0
                claims = 0
                promoted = 0
                impacts = 0
                task_packs = 0

                # Fetch pending chunks (brief read-only connection)
                with get_conn(self.db.db_path) as conn:
                    pending_rows = conn.execute(
                        "SELECT chunk_id FROM context_chunks "
                        "WHERE state = 'no_enrich' LIMIT 30"
                    ).fetchall()
                    pending = [r["chunk_id"] for r in pending_rows]

                # Process unlocking sequentially
                for i, chunk_id in enumerate(pending):
                    self._enrich_running.emit(
                        f"AI: Unlocking context ({i + 1}/{len(pending)})..."
                    )
                    with get_conn(self.db.db_path) as conn:
                        _assess(conn, chunk_id)
                    time.sleep(0.01)

                # Fetch active enrichable chunks
                with get_conn(self.db.db_path) as conn:
                    enrich_rows = conn.execute(
                        "SELECT chunk_id FROM context_chunks "
                        "WHERE state = 'enrichable' LIMIT 20"
                    ).fetchall()
                    enrichable = [r["chunk_id"] for r in enrich_rows]

                for i, chunk_id in enumerate(enrichable):
                    self._enrich_running.emit(
                        f"AI: Assessing chunk ({i + 1}/{len(enrichable)})..."
                    )
                    with get_conn(self.db.db_path) as conn:
                        _assess(conn, chunk_id)
                        assessed += 1

                        if depth in ("standard", "deep"):
                            self._enrich_running.emit(
                                f"AI: Extracting claims ({i + 1}/{len(enrichable)})..."
                            )
                            cr = _extract(conn, chunk_id)
                            extracted = cr.get("claims", [])
                            claims += cr.get("claims_extracted", 0)

                            if extracted:
                                from claim_graph import auto_promote_layer1

                                promoted_results = auto_promote_layer1(conn, extracted)
                                promoted += len(promoted_results)
                    time.sleep(0.01)

                self._enrich_running.emit("AI: Compiling knowledge context pack...")
                with get_conn(self.db.db_path) as conn:
                    _pack(conn, "executor")
                    self._enrich_running.emit("AI: Warming task-specific packs...")
                    task_pack_stats = _warm_task_packs(
                        conn, pack_type="executor", limit=8
                    )
                    task_packs = task_pack_stats.get("task_packs_with_context", 0)

                    if depth == "deep":
                        from lazy_enrichment import run_health_sweep

                        self._enrich_running.emit(
                            "AI: Executing health sweep cross-checks..."
                        )
                        report = run_health_sweep(conn)
                        promoted += len(report.get("promoted", []))

                        recent = conn.execute(
                            "SELECT fact_id FROM canonical_facts "
                            "WHERE updated_at >= datetime('now', '-7 days') LIMIT 10"
                        ).fetchall()
                        for f in recent:
                            _impact(conn, "fact", f["fact_id"])
                            impacts += 1

                self._enrich_done.emit(
                    f"Intelligence Pass Complete: {assessed} chunks, {claims} candidates, "
                    f"{promoted} facts promoted, {task_packs} task packs warmed."
                )
            except _OPTIONAL_PIPELINE_ERRORS as exc:
                logger.exception("Enrich pipeline failed")
                self._enrich_done.emit(f"Enrich error: {exc}")

        threading.Thread(target=_work, daemon=True).start()

    def _on_enrich_done(self, msg):
        self._enrich_in_progress = False  # reset on GUI thread via signal
        is_error = msg.startswith("Enrich error")
        self.status.showMessage(msg, 10000 if is_error else 5000)

    def _on_entity_results(self, entities: list, seq_id: int):
        """Handle async entity search results. Discard if stale."""
        if seq_id != self._entity_seq_id:
            return  # stale — user typed new query
        self._entity_results = entities
        # Re-render only the current tab if it shows entities
        idx = self.tabs.currentIndex()
        if 0 <= idx < len(self._tab_keys):
            key = self._tab_keys[idx]
            if key == "suggested" and self._search_text:
                self._load_tab(key)

    def _cancel_entity_searches(self):
        """Invalidate in-flight entity searches and clear queued work."""
        self._entity_seq_id += 1
        with self._entity_search_lock:
            self._pending_entity_search = None

    def _request_entity_search(self, query: str):
        """Coalesce entity searches into a single background worker."""
        self._entity_seq_id += 1
        seq_id = self._entity_seq_id
        with self._entity_search_lock:
            self._pending_entity_search = (seq_id, query)
            if self._entity_search_running:
                return
            self._entity_search_running = True

        def _entity_worker():
            while True:
                with self._entity_search_lock:
                    pending = self._pending_entity_search
                    self._pending_entity_search = None
                if pending is None:
                    with self._entity_search_lock:
                        if self._pending_entity_search is None:
                            self._entity_search_running = False
                            return
                    continue
                worker_seq_id, worker_query = pending
                results = self.db.search_entities_fast(worker_query, limit=10)
                self._entity_search_done.emit(results, worker_seq_id)

        threading.Thread(target=_entity_worker, daemon=True).start()

    def _import_remote_entities(self, remote_entities, conn=None):
        """Import entities from remote shared.json that don't exist locally."""
        if conn is not None:
            self._import_remote_entities_inner(conn, remote_entities)
        else:
            with get_conn(self.db.db_path) as conn:
                self._import_remote_entities_inner(conn, remote_entities)

    @staticmethod
    def _import_remote_entities_inner(conn, remote_entities):
        for e in remote_entities:
            existing = conn.execute(
                "SELECT id FROM entities WHERE name = ?", (e["name"],)
            ).fetchone()
            if existing:
                continue
            now = now_iso()
            eid = conn.execute(
                "INSERT INTO entities (name, entity_type, project, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    e["name"],
                    e["entityType"],
                    e.get("project") or "shared:bridge",
                    now,
                    now,
                ),
            ).lastrowid
            for o in e.get("observations", []):
                conn.execute(
                    "INSERT INTO observations (entity_id, content, created_at) "
                    "VALUES (?, ?, ?)",
                    (eid, o["content"], o.get("createdAt", now)),
                )

    def _sort_tasks(self, tasks, sort_mode=None):
        """Sort tasks by given sort mode (or current working sort mode)."""
        mode = sort_mode or self._sort_mode
        if mode == "priority":
            return sorted(tasks, key=priority_sort_key)
        if mode == "due":
            return sorted(
                tasks,
                key=lambda t: (
                    0 if t.get("due_date") else 1,
                    t.get("due_date") or "9999-12-31",
                    priority_sort_key(t),
                ),
            )
        if mode == "project":
            return sorted(
                tasks,
                key=lambda t: (
                    t.get("project") or "zzz_none",
                    priority_sort_key(t),
                ),
            )
        # mode == "created"
        return sorted(tasks, key=lambda t: t.get("created_at") or "", reverse=True)

    def _cycle_sort(self):
        """Cycle to next sort mode and refresh."""
        idx = self._SORT_MODES.index(self._sort_mode)
        self._sort_mode = self._SORT_MODES[(idx + 1) % len(self._SORT_MODES)]
        self._sort_btn.setText(f"{self._SORT_LABELS[self._sort_mode]} \u25be")
        if hasattr(self, "_sort_actions") and self._sort_mode in self._sort_actions:
            self._sort_actions[self._sort_mode].setChecked(True)
        self._save_ui_state()
        self.refresh()

    def refresh(self):
        if getattr(self, "_refreshing", False):
            return
        self._refreshing = True
        try:
            self._do_refresh()
        finally:
            self._refreshing = False

    def _do_refresh(self):
        # Auto-promote tasks whose due date has arrived
        self.db.promote_due_today()

        # Rebuild project chips (cached 60s)
        now_mono = time.monotonic()
        if now_mono - self._project_cache_time >= 60:
            self._project_cache_time = now_mono
            projects = self.db.get_project_names()
            if self._last_projects != projects:
                self._last_projects = projects
                self._build_filter_chips()

        # 2 DB queries instead of 4: derive suggested & notes in Python
        all_active = self.db.get_all_active()
        done = self.db.get_done_tasks()

        # Rebuild SmartKey search index (skips if fingerprint unchanged)
        self._search_engine.rebuild_index(all_active + done)

        suggested = sorted(all_active, key=_suggested_sort_key)[:20]
        notes = [t for t in all_active if t.get("type") == "note"] + [
            t for t in done if t.get("type") == "note"
        ]

        raw = {
            "suggested": suggested,
            "today": [t for t in all_active if t.get("section") == "today"],
            "inbox": [t for t in all_active if t.get("section") == "inbox"],
            "next": [t for t in all_active if t.get("section") == "next"],
            "notes": notes,
            "projects": all_active,
            "all": all_active + done,
            "done": done,
        }

        # Pre-compute filtered+sorted data for all tabs (cheap Python ops)
        self._filtered_cache = {}
        if self._search_text:
            # Global search: search ALL tasks, then distribute into tabs
            all_tasks = all_active + done
            global_results = self._search_engine.search(
                self._search_text, all_tasks, conn=self.db._conn, use_vector=False
            )
            # Async entity search — tasks render immediately, entities arrive via signal
            self._entity_results = []
            self._request_entity_search(self._search_text)
            global_ids = {t["id"] for t in global_results}
            for key in self._tab_keys:
                if key == "suggested":
                    # Use full search results, not the pre-limited top-20 list
                    source = global_results
                else:
                    source = [t for t in raw[key] if t["id"] in global_ids]
                if key in self._tab_views:
                    v = self._tab_views[key]
                    self._filtered_cache[key] = self._sort_tasks(source, v["sort"])
                else:
                    self._filtered_cache[key] = self._sort_tasks(source)
        else:
            self._cancel_entity_searches()
            self._entity_results = []
            for key in self._tab_keys:
                if key in self._tab_views:
                    v = self._tab_views[key]
                    filtered = self._filter(raw[key], v["active"], v["excluded"])
                    self._filtered_cache[key] = self._sort_tasks(filtered, v["sort"])
                else:
                    self._filtered_cache[key] = self._sort_tasks(self._filter(raw[key]))

        # Update tab visibility (suggested, notes, projects always visible)
        always_visible = ("suggested", "today", "notes", "projects")
        for i, key in enumerate(self._tab_keys):
            count = len(self._filtered_cache[key])
            self.tabs.setTabVisible(i, count > 0 or key in always_visible)

        # Auto-switch to first tab with results when searching
        if self._search_text:
            cur = self.tabs.currentIndex()
            current_key = self._tab_keys[cur] if cur < len(self._tab_keys) else ""
            if not self._filtered_cache.get(current_key):
                for i, key in enumerate(self._tab_keys):
                    if self._filtered_cache.get(key):
                        self.tabs.setCurrentIndex(i)
                        break

        # Actual current index AFTER all tab visibility and search changes
        current_idx = self.tabs.currentIndex()

        # Lazy rendering: only load the currently active tab
        if 0 <= current_idx < len(self._tab_keys):
            self._load_tab(self._tab_keys[current_idx])

        # Status bar — derive summary from already-fetched data
        s = self.db.get_summary(all_active)
        task_count = sum(1 for t in all_active if t.get("type", "task") == "task")
        note_count = len(notes)
        done_count = len(done)
        msg = f"Tasks: {task_count} | Notes: {note_count} | Done: {done_count} | Overdue: {s['overdue']}"
        if self._search_text:
            msg += f" | Filter: '{self._search_text}'"
        inc_count = sum(len(v) for v in self._active_filters.values())
        exc_count = sum(len(v) for v in self._excluded_filters.values())
        if inc_count or exc_count:
            parts = []
            if inc_count:
                parts.append(f"{inc_count} include")
            if exc_count:
                parts.append(f"{exc_count} exclude")
            msg += f" | Filters: {', '.join(parts)}"
        self.status.showMessage(msg)

    def _on_tab_changed(self, idx):
        """Handle tab switch: save current tab's view, load new tab's view."""
        # Save outgoing tab's state
        old_idx = getattr(self, "_current_tab_idx", 0)
        if old_idx < len(self._tab_keys):
            old_key = self._tab_keys[old_idx]
            if old_key in self._tab_views:
                self._tab_views[old_key] = {
                    "sort": self._sort_mode,
                    "active": copy.deepcopy(self._active_filters),
                    "excluded": copy.deepcopy(self._excluded_filters),
                }

        # Load incoming tab's state
        self._current_tab_idx = idx
        if idx < len(self._tab_keys):
            new_key = self._tab_keys[idx]
            if new_key in self._tab_views:
                v = self._tab_views[new_key]
                self._sort_mode = v["sort"]
                self._active_filters = copy.deepcopy(v["active"])
                self._excluded_filters = copy.deepcopy(v["excluded"])
            else:
                self._sort_mode = "priority"
                self._active_filters = {
                    "priority": set(),
                    "due": set(),
                    "project": set(),
                }
                self._excluded_filters = {
                    "priority": set(),
                    "due": set(),
                    "project": set(),
                }

            # Update sort button
            self._sort_btn.setText(f"{self._SORT_LABELS[self._sort_mode]} \u25be")
            if hasattr(self, "_sort_actions") and self._sort_mode in self._sort_actions:
                self._sort_actions[self._sort_mode].setChecked(True)

            # Rebuild filter chips for new tab
            self._build_filter_chips()

            # Hide sort/filter UI for fixed tabs
            is_fixed = new_key in _FIXED_VIEW_TABS
            self._sort_btn.setVisible(not is_fixed)

        self._save_ui_state()
        if idx < len(self._tab_keys):
            self._load_tab(self._tab_keys[idx])

    def _load_tab(self, key):
        """Render a single tab from cached data. Caps All/Done at 200 items."""
        tasks = self._filtered_cache.get(key)
        if tasks is None:
            return
        cap_msg = ""
        if key in ("all", "done") and len(tasks) > _TAB_PAGE_SIZE:
            cap_msg = f"── {len(tasks) - _TAB_PAGE_SIZE} more items... ──"
            tasks = tasks[:_TAB_PAGE_SIZE]

        # Entity results only shown in "suggested" tab during search
        entities = (
            self._entity_results if (self._search_text and key == "suggested") else []
        )

        lw = self.tab_lists[key]
        if key == "suggested":
            lw.load_smart_grouped(tasks, entities=entities)
        elif key == "projects":
            proj_sorted = sorted(tasks, key=lambda t: t.get("project") or "zzz_none")
            lw.load_grouped_by_project(proj_sorted)
        else:
            lw.load_tasks(tasks)

        if cap_msg:
            sentinel = QListWidgetItem(cap_msg)
            sentinel.setFlags(Qt.ItemFlag.NoItemFlags)
            sentinel.setForeground(QColor("#888"))
            lw.addItem(sentinel)

    def _on_item_changed(self, item):
        task_id = item.data(Qt.ItemDataRole.UserRole)
        if not task_id:
            return
        # Skip entity items (no checkbox behavior)
        if isinstance(task_id, str) and task_id.startswith("entity:"):
            return
        checked = item.checkState() == Qt.CheckState.Checked
        # Defer DB write out of signal handler — immediate clear() during
        # itemChanged dispatch deletes the C++ QListWidgetItem, causing segfault.
        QTimer.singleShot(10, lambda: self._apply_check_change(task_id, checked))

    def _apply_check_change(self, task_id, checked):
        try:
            if checked:
                self.db.mark_done(task_id)
            else:
                self.db.update_task(task_id, status="not_started")
            # on_change() triggers _refresh_all → full_window.refresh(),
            # but refresh explicitly as safety net (re-entrancy guard makes it cheap).
            self.refresh()
        except sqlite3.Error as exc:
            logging.getLogger("task_tray").error(
                "Error toggling task %s: %s",
                task_id,
                exc,
                exc_info=True,
            )
            self._revert_checkbox(task_id, checked)
            self.status.showMessage(f"DB error — task not saved. {exc}", 8000)

    def _revert_checkbox(self, task_id, was_checked):
        """Revert checkbox to opposite state after a failed DB write."""
        revert_to = Qt.CheckState.Unchecked if was_checked else Qt.CheckState.Checked
        for lw in self.tab_lists.values():
            lw.blockSignals(True)
            for i in range(lw.count()):
                item = lw.item(i)
                if item and item.data(Qt.ItemDataRole.UserRole) == task_id:
                    item.setCheckState(revert_to)
            lw.blockSignals(False)

    def _add_task(self):
        task = {"title": "", "section": "inbox", "priority": "medium"}
        dlg = EditTaskDialog(task, self, db=self.db)
        dlg.setWindowTitle("Add Task")
        if dlg.exec() == QDialog.DialogCode.Accepted:
            vals = dlg.get_values()
            title = vals.pop("title", "")
            if title:
                self.db.add_task(title, **vals)
                self.refresh()

    def showEvent(self, event):
        super().showEvent(event)
        self._refresh_timer.start(_REFRESH_INTERVAL_MS)
        self._purge_timer.start(_PURGE_INTERVAL_MS)
        self._auto_sync_timer.start()
        self.refresh()

    def closeEvent(self, event):
        self._settings.setValue("geometry", self.saveGeometry())
        self._save_ui_state()
        self._search_engine.save()
        self._refresh_timer.stop()
        self._purge_timer.stop()
        self._auto_sync_timer.stop()
        self._db_refresh_debounce.stop()
        self._search_timer.stop()
        event.ignore()
        self.hide()


# ── App Controller ──────────────────────────────────────────────────


class TaskTrayApp:
    """Main application controller."""

    def __init__(self, instance_socket=None):
        self.app = QApplication(sys.argv)
        self.app.setQuitOnLastWindowClosed(False)
        self.db = TaskDB()
        self.db.on_change = self._refresh_all
        self.app.aboutToQuit.connect(self._on_quit)

        # Periodic entity enrichment cache refresh (60s safety net for external writes)
        self._enrich_timer = QTimer(self.app)
        self._enrich_timer.timeout.connect(
            lambda: threading.Thread(
                target=self.db._refresh_enrich_cache, daemon=True
            ).start()
        )
        self._enrich_timer.start(60_000)

        # Tray icon
        self.tray = QSystemTrayIcon()
        self._update_icon()
        self.tray.setToolTip(self._tooltip())
        self.tray.activated.connect(self._on_tray_activated)

        # Context menu
        menu = QMenu()
        open_action = QAction("Open Full Window", menu)
        open_action.triggered.connect(self._open_full)
        menu.addAction(open_action)
        add_task_action = QAction("Add Task", menu)
        add_task_action.triggered.connect(self._quick_add_from_tray)
        menu.addAction(add_task_action)
        menu.addSeparator()
        quit_action = QAction("Quit", menu)
        quit_action.triggered.connect(self.app.quit)
        menu.addAction(quit_action)
        self.tray.setContextMenu(menu)

        self.tray.show()
        self.popup = None
        self.full_window = None

        # Reminder timer — check every 60s for due reminders
        self._reminder_timer = QTimer(self.app)
        self._reminder_timer.timeout.connect(self._check_reminders)
        self._reminder_timer.start(60_000)
        self._shown_reminder_ids: dict[str, float] = {}  # task_id → monotonic ts
        self._active_reminder_dlgs: list = []  # keep dialogs alive until closed
        QTimer.singleShot(5000, self._check_reminders)  # initial check after startup

        # Single-instance socket polling
        self._instance_socket = instance_socket
        if instance_socket:
            self._instance_timer = QTimer(self.app)
            self._instance_timer.timeout.connect(self._poll_instance_socket)
            self._instance_timer.start(500)

    def _update_icon(self, summary=None):
        if summary is None:
            summary = self.db.get_summary()
        pm = create_tray_icon_pixmap(summary["overdue"])
        self.tray.setIcon(QIcon(pm))

    def _tooltip(self, summary=None):
        if summary is None:
            summary = self.db.get_summary()
        return f"Tasks: {summary['total']} | Overdue: {summary['overdue']}"

    def _on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self._toggle_popup()

    def _toggle_popup(self):
        if self.popup and self.popup.isVisible():
            self.popup.hide()
            return
        if not self.popup:
            self.popup = TrayPopup(self.db, self._open_full)
        geo = self.tray.geometry()
        self.popup.show_near_tray(geo)

    def _quick_add_from_tray(self):
        task = {"title": "", "section": "today", "priority": "medium"}
        dlg = EditTaskDialog(task, db=self.db)
        dlg.setWindowTitle("Add Task")
        if dlg.exec() == QDialog.DialogCode.Accepted:
            vals = dlg.get_values()
            title = vals.pop("title", "")
            if title:
                self.db.add_task(title, **vals)

    def _open_full(self):
        if self.popup:
            self.popup.hide()
        if not self.full_window:
            self.full_window = FullWindow(self.db)
        self.full_window.show()
        self.full_window.raise_()
        self.full_window.activateWindow()

    def _poll_instance_socket(self):
        """Check if another instance sent a SHOW command."""
        if not self._instance_socket:
            return
        try:
            conn, _ = self._instance_socket.accept()
            data = conn.recv(64)
            conn.close()
            if data == b"SHOW":
                self._open_full()
        except BlockingIOError:
            pass
        except OSError:
            pass

    def _refresh_all(self):
        """Update tray icon badge + tooltip after any change."""
        threading.Thread(target=self.db._refresh_enrich_cache, daemon=True).start()
        summary = self.db.get_summary()
        self._update_icon(summary)
        self.tray.setToolTip(self._tooltip(summary))
        if self.popup and self.popup.isVisible():
            self.popup.refresh()
        if self.full_window and self.full_window.isVisible():
            self.full_window.refresh()

    def _check_reminders(self):
        """Check for tasks with due reminders and show notifications."""
        # Auto-cleanup: evict entries older than 5 min so reminders can re-fire
        cutoff = time.monotonic() - 300
        self._shown_reminder_ids = {
            k: v for k, v in self._shown_reminder_ids.items() if v > cutoff
        }

        try:
            now_str = now_iso()
            with get_conn(self.db.db_path) as conn:
                rows = conn.execute(
                    "SELECT id, title, description, priority, reminder_at FROM tasks "
                    "WHERE reminder_at IS NOT NULL AND reminder_at <= ? "
                    "AND status NOT IN ('done', 'archived', 'cancelled')",
                    (now_str,),
                ).fetchall()
        except sqlite3.Error as exc:
            logging.getLogger("task_tray").warning("reminder check: %s", exc)
            return

        for row in rows:
            tid = row["id"]
            if tid in self._shown_reminder_ids:
                continue
            self._shown_reminder_ids[tid] = time.monotonic()

            # Critical priority or >1h overdue → popup dialog
            is_critical = row["priority"] == "critical"
            try:
                reminder_dt = datetime.fromisoformat(row["reminder_at"])
                overdue_minutes = (
                    datetime.now(timezone.utc) - reminder_dt
                ).total_seconds() / 60
                is_very_overdue = overdue_minutes > 60
            except (ValueError, TypeError):
                is_very_overdue = False

            if is_critical or is_very_overdue:
                dlg = ReminderPopupDialog(
                    tid,
                    row["title"],
                    row["priority"],
                    row["description"],
                )
                dlg.snoozed.connect(self._snooze_reminder)
                dlg.dismissed.connect(self._dismiss_reminder)
                self._active_reminder_dlgs.append(dlg)
                dlg.finished.connect(
                    lambda _, d=dlg: (
                        self._active_reminder_dlgs.remove(d)
                        if hasattr(self, "_active_reminder_dlgs")
                        and d in self._active_reminder_dlgs
                        else None
                    )
                )
                dlg.show()
            else:
                self.tray.showMessage(
                    "Task Reminder",
                    row["title"],
                    QSystemTrayIcon.MessageIcon.Information,
                    10_000,
                )

    def _snooze_reminder(self, task_id: str, minutes: int):
        """Reschedule reminder to NOW + minutes."""
        new_time = datetime.now(timezone.utc) + timedelta(minutes=minutes)
        self.db.update_task(task_id, reminder_at=new_time.isoformat())
        self._shown_reminder_ids.pop(task_id, None)

    def _dismiss_reminder(self, task_id: str):
        """Clear the reminder."""
        self.db.update_task(task_id, reminder_at=None)
        self._shown_reminder_ids.pop(task_id, None)

    def _on_quit(self):
        self._enrich_timer.stop()
        self._reminder_timer.stop()
        if self._instance_socket:
            self._instance_socket.close()
        self.db.close()

    def run(self):
        return self.app.exec()


_INSTANCE_TCP_PORT = 47831  # localhost-only, for Windows (no AF_UNIX)
_USE_UNIX = hasattr(_socket, "AF_UNIX")


def _try_single_instance():
    """If another instance is running, send SHOW signal and return None.
    Otherwise, bind the socket and return the server socket."""
    if _USE_UNIX:
        return _try_single_instance_unix()
    return _try_single_instance_tcp()


def _try_single_instance_unix():
    """AF_UNIX variant (Linux): abstract namespace, zero cleanup."""
    addr = "\0TaskTray_SingleInstance"
    try:
        client = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        client.connect(addr)
        client.send(b"SHOW")
        client.close()
        return None
    except (ConnectionRefusedError, FileNotFoundError, OSError):
        pass
    srv = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    try:
        srv.bind(addr)
        srv.listen(1)
        srv.setblocking(False)
        return srv
    except OSError:
        srv.close()
        raise


def _try_single_instance_tcp():
    """TCP localhost variant (Windows): no AF_UNIX available."""
    addr = ("127.0.0.1", _INSTANCE_TCP_PORT)
    try:
        client = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        client.connect(addr)
        client.send(b"SHOW")
        client.close()
        return None
    except (ConnectionRefusedError, OSError):
        pass
    srv = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    srv.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
    try:
        srv.bind(addr)
        srv.listen(1)
        srv.setblocking(False)
        return srv
    except OSError:
        srv.close()
        raise


def main():
    srv = _try_single_instance()
    if srv is None:
        sys.exit(0)
    app = TaskTrayApp(instance_socket=srv)
    if "--show" in sys.argv:
        app._open_full()
    sys.exit(app.run())


if __name__ == "__main__":
    main()
