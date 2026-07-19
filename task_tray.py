"""Task Tray — SQLite Task Manager.

System tray widget with dual mode: compact popup + full window.
Reads/writes directly to ~/.claude/memory/memory.db.
"""

# ruff: noqa: E402

import atexit
import copy
import faulthandler
import json
import logging
import logging.handlers
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
    _handler = logging.handlers.RotatingFileHandler(
        os.path.join(_log_dir, "task_tray.log"),
        maxBytes=2 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    _handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    _root_logger = logging.getLogger()
    _root_logger.addHandler(_handler)
    _root_logger.setLevel(logging.WARNING)
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
    TaskDAO,
    add_task_attachment,
    apply_task_mutation,
    create_task_with_ledger,
    ensure_dashboard_schema,
    get_conn,
    get_daily_dashboard,
    is_overdue,
    normalize_project_filter_values,
    now_iso,
    priority_sort_key,
    purge_old_dashboard_days,
    remove_task_attachment,
    resolve_task_attachment_path,
)
from schema import init_db
from smart_retrieval import suggested_ready
from task_status_cas import StatusToken, transition_status


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, value)


# Page size cap for "All" and "Done" tabs to keep QListWidget responsive
_TAB_PAGE_SIZE = 200
_TRAY_READY_REVIEW_LIMIT = 50
_TRAY_SUGGESTED_LIMIT = _env_int("SQLITE_MEMORY_TRAY_SUGGESTED_LIMIT", 50, 1)
_TRAY_SEARCH_INDEX_LIMIT = _env_int("SQLITE_MEMORY_TRAY_SEARCH_INDEX_LIMIT", 300, 1)
_TRAY_INDEX_TEXT_CHARS = _env_int("SQLITE_MEMORY_TRAY_INDEX_TEXT_CHARS", 800, 80)
_TRAY_RSS_LOG_MB = _env_int("SQLITE_MEMORY_TRAY_RSS_LOG_MB", 512, 0)
_TRAY_RSS_EXIT_MB = _env_int("SQLITE_MEMORY_TRAY_RSS_EXIT_MB", 3072, 0)
_TRAY_RSS_CHECK_INTERVAL_MS = _env_int(
    "SQLITE_MEMORY_TRAY_RSS_CHECK_INTERVAL_MS", 60_000, 10_000
)


def _bounded_tray_limit(limit: int | None, hard_cap: int) -> int:
    if limit is None:
        return hard_cap
    try:
        value = int(limit)
    except (TypeError, ValueError):
        return hard_cap
    return min(max(1, value), hard_cap)


def _current_rss_mb() -> float:
    """Return current process RSS in MiB from procfs."""
    try:
        with open("/proc/self/statm", encoding="utf-8") as fh:
            rss_pages = int(fh.read().split()[1])
        page_size = os.sysconf("SC_PAGE_SIZE")
        return rss_pages * page_size / (1024 * 1024)
    except (OSError, IndexError, ValueError):
        return 0.0


def _trim_index_text(value):
    if isinstance(value, str) and len(value) > _TRAY_INDEX_TEXT_CHARS:
        return value[:_TRAY_INDEX_TEXT_CHARS]
    return value


def _tray_search_index_rows(*groups: list[dict]) -> list[dict]:
    """Build a bounded, lightweight search-index projection for the tray."""
    rows = []
    seen = set()
    fields = (
        "id",
        "title",
        "description",
        "notes",
        "project",
        "section",
        "status",
        "priority",
        "due_date",
        "type",
        "updated_at",
    )
    for group in groups:
        for task in group:
            task_id = task.get("id")
            if not task_id or task_id in seen:
                continue
            seen.add(task_id)
            rows.append({field: _trim_index_text(task.get(field)) for field in fields})
            if len(rows) >= _TRAY_SEARCH_INDEX_LIMIT:
                return rows
    return rows


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
        ensure_dashboard_schema(self._conn)
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

    def get_ready_review_tasks(self, limit=50):
        """Return closed rows explicitly marked for ready-context review."""
        return TaskDAO.get_ready_review_candidates(
            self._conn,
            columns=_UI_COLS,
            limit=limit,
        )

    def purge_old_done(self, days=30):
        """Retire done tasks older than `days` days. Returns count retired.

        Bridge-visible: tier-1 archives (tombstones) old done tasks so automated
        incremental sync sees the change and cleans stale bridge files; tier-2
        hard-purges tombstones that have aged past the export window.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        return TaskDAO.purge_done(self._conn, cutoff)

    def get_suggested_tasks(self, limit=20):
        """Return Suggested popup rows through ready-context policy."""
        tasks = self.get_all_active() + self.get_ready_review_tasks(
            limit=_TRAY_READY_REVIEW_LIMIT
        )
        return suggested_ready(
            tasks,
            include_readings=False,
            limit=_bounded_tray_limit(limit, _TRAY_SUGGESTED_LIMIT),
        )

    def get_all_notes(self):
        """Visible open notes. Excludes done/archived/cancelled."""
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

    def get_dashboard(self):
        """Return today's local dashboard projection."""
        return get_daily_dashboard(self._conn)

    def purge_old_dashboard(self):
        """Drop dashboard rows older than today's local dashboard day."""
        return purge_old_dashboard_days(self._conn)

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
        notes=None,
        type="task",
        reminder_at=None,
        recurring=None,
        attachments=None,
    ):
        """Insert new task, return its ID."""
        task_id = str(uuid.uuid4())
        now = now_iso()
        with self._transact(self._conn):
            create_task_with_ledger(
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
                notes=notes,
                type=type,
                reminder_at=reminder_at,
                recurring=recurring,
                tool_name="task_tray.add_task",
            )
            for file_path in attachments or []:
                add_task_attachment(
                    self._conn,
                    task_id,
                    file_path,
                    tool_name="task_tray.add_task_attachment",
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
            result = apply_task_mutation(
                self._conn,
                task_id,
                {"status": "done"},
                timestamp=now,
                tool_name="task_tray.mark_done",
            )
            if result.get("updated", 0) == 0:
                return False
        if self.on_change:
            self.on_change()
        return True

    def update_task(self, task_id, **fields):
        """Update arbitrary fields on a task."""
        if not fields:
            return False
        now = now_iso()
        with self._transact(self._conn):
            result = apply_task_mutation(
                self._conn,
                task_id,
                fields,
                timestamp=now,
                tool_name="task_tray.update_task",
            )
            if result.get("updated", 0) == 0:
                return False
        if self.on_change:
            self.on_change()
        return True

    def get_task_attachments(self, task_id, include_removed=False):
        """Return attachment metadata for a task."""
        return TaskDAO.get_attachments(
            self._conn,
            task_id,
            include_removed=include_removed,
        )

    def resolve_attachment_path(self, attachment):
        """Resolve best local path for an attachment."""
        return resolve_task_attachment_path(attachment)

    def apply_attachment_changes(self, task_id, add_paths=None, remove_ids=None):
        """Apply attachment additions/removals atomically for a task."""
        add_paths = [p for p in (add_paths or []) if p]
        remove_ids = [aid for aid in (remove_ids or []) if aid]
        if not add_paths and not remove_ids:
            return False
        changed = False
        with self._transact(self._conn):
            for file_path in add_paths:
                add_task_attachment(
                    self._conn,
                    task_id,
                    file_path,
                    tool_name="task_tray.add_task_attachment",
                )
                changed = True
            for attachment_id in remove_ids:
                changed = (
                    remove_task_attachment(
                        self._conn,
                        attachment_id,
                        tool_name="task_tray.remove_task_attachment",
                    )
                    or changed
                )
        if changed and self.on_change:
            self.on_change()
        return changed

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
            # Cancel the target task
            result = apply_task_mutation(
                self._conn,
                task_id,
                {"status": "cancelled"},
                timestamp=now,
                tool_name="task_tray.delete_task",
            )
            if result.get("updated", 0) == 0:
                return False
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
                    for sid in sibling_ids:
                        apply_task_mutation(
                            self._conn,
                            sid,
                            {"status": "cancelled"},
                            timestamp=now,
                            tool_name="task_tray.delete_task",
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
    QAbstractItemView,
)
from PyQt6.QtGui import (
    QIcon,
    QAction,
    QActionGroup,
    QColor,
    QKeySequence,
    QShortcut,
)
from PyQt6.QtCore import (
    QFileSystemWatcher,
    QObject,
    QSettings,
    Qt,
    QTimer,
    pyqtSignal,
)
from pathlib import Path

from tray_filters import FilterMixin
from premium_task_tray import maybe_load_task_tray_extension
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
    _build_debate_surface_style,
    _build_list_style,
    _build_debate_reader_style,
    _REFRESH_INTERVAL_MS,
    # Constants
    _UI_COLS,
    # Dialog classes + TaskListWidget
    TrayPopup,
    CustomDesignDialog,
    EditTaskDialog,
    ReminderPopupDialog,
    TaskListWidget,
    create_tray_icon_pixmap,
)
from debate_read_dao import DebateReadDAO
from debate_list_widget import (
    DebateReaderDialog,
    DebateTabWidget,
    apply_debate_controls,
    default_debate_control_params,
    normalize_debate_control_params,
)

_PURGE_INTERVAL_MS = 3_600_000  # 1 hour
_PERIODIC_PULL_INTERVAL_MS = 15 * 60_000
_BACKGROUND_AUDIT_INTERVAL_MS = 60 * 60_000
_BACKGROUND_AUDIT_STARTUP_DELAY_MS = 10 * 60_000
_REMINDER_MAX_DELIVERIES = 3
_REMINDER_REPEAT_DELAYS_SECONDS = (5 * 60, 15 * 60)


def _reminder_delivery_key(task_id: str, reminder_at: str | None) -> tuple[str, str]:
    return (task_id, reminder_at or "")


def _should_deliver_reminder(
    delivery_state: dict[tuple[str, str], dict[str, float | int]],
    task_id: str,
    reminder_at: str | None,
    now_monotonic: float,
) -> bool:
    """Return True when a due reminder may be shown under the backoff policy."""
    key = _reminder_delivery_key(task_id, reminder_at)
    state = delivery_state.get(key)
    if state is None:
        delivery_state[key] = {
            "count": 1,
            "last_at": now_monotonic,
            "next_at": now_monotonic + _REMINDER_REPEAT_DELAYS_SECONDS[0],
        }
        return True

    count = int(state.get("count", 0))
    if count >= _REMINDER_MAX_DELIVERIES:
        return False

    next_at = float(state.get("next_at", now_monotonic))
    if now_monotonic < next_at:
        return False

    count += 1
    state["count"] = count
    state["last_at"] = now_monotonic
    if count >= _REMINDER_MAX_DELIVERIES:
        state["next_at"] = float("inf")
    else:
        state["next_at"] = now_monotonic + _REMINDER_REPEAT_DELAYS_SECONDS[count - 1]
    return True


def _clear_reminder_delivery_state(
    delivery_state: dict[tuple[str, str], dict[str, float | int]],
    task_id: str,
) -> None:
    for key in [key for key in delivery_state if key[0] == task_id]:
        delivery_state.pop(key, None)


def _run_recurring_maintenance(db_path):
    """Process recurring tasks silently (idempotent)."""
    try:
        from recurring_tasks import process_recurring

        with get_conn(db_path) as conn:
            return process_recurring(conn, dry_run=False)
    except _OPTIONAL_PIPELINE_ERRORS as exc:
        logging.getLogger("task_tray").warning("recurring: %s", exc)
        return []


class _BridgeSignalBus(QObject):
    progress = pyqtSignal(int, str)
    done = pyqtSignal(str)
    refresh_requested = pyqtSignal()


class _TrayStatusProxy:
    """Status sink for app-level sync ownership without a permanent status bar."""

    def __init__(self, app):
        self._app = app

    def showMessage(self, message, timeout):
        logger.info("tray_status message=%r timeout_ms=%s", message, timeout)
        full_window = getattr(self._app, "full_window", None)
        if full_window and full_window.isVisible():
            full_window.status.showMessage(message, timeout)
            return
        if message.startswith(("Sync error", "Sync blocked", "Sync incomplete")):
            self._app.tray.showMessage(
                "SQLite Memory Tray",
                message,
                QSystemTrayIcon.MessageIcon.Warning,
                timeout,
            )


# Per-tab sort/filter constants
_FIXED_VIEW_TABS = frozenset({"dashboard", "projects"})
_DEFAULT_TAB_VIEW = {
    "sort": "ready",
    "active": {"priority": set(), "due": set(), "project": set()},
    "excluded": {"priority": set(), "due": set(), "project": set()},
    "params": {},
}
_DASHBOARD_KIND_ORDER = {
    "decision": 0,
    "difficulty": 1,
    "misunderstanding": 2,
    "advice": 3,
    "option": 4,
    "result": 5,
}
_DASHBOARD_KIND_TAG = {
    "decision": "D",
    "difficulty": "!",
    "misunderstanding": "?",
    "advice": "A",
    "option": "O",
    "result": "R",
}
_DASHBOARD_KIND_COLOR = {
    "decision": "#ffd166",
    "difficulty": "#ff8a80",
    "misunderstanding": "#80cbc4",
    "advice": "#a5d6a7",
    "option": "#90caf9",
    "result": "#c5cae9",
}


def _normalize_filter_payload(filter_payload):
    """Normalize persisted include/exclude filter payloads."""
    payload = filter_payload or {}
    return {
        "priority": set(payload.get("priority", [])),
        "due": set(payload.get("due", [])),
        "project": normalize_project_filter_values(payload.get("project", [])),
    }


class FullWindow(QMainWindow, BridgeSyncMixin, FilterMixin):
    """Full task manager window with tabs, search, sort, and suggested view."""

    _bridge_done = pyqtSignal(str)
    _bridge_progress = pyqtSignal(int, str)  # (percent, step_label)
    _enrich_done = pyqtSignal(str)
    _enrich_running = pyqtSignal(str)
    _entity_search_done = pyqtSignal(list, int)  # (entity_results, seq_id)

    # Sort modes cycle: priority → due → created → priority ...
    _SORT_MODES = ("ready", "priority", "due", "created", "project")
    _SORT_LABELS = {
        "ready": "Sort: Ready",
        "priority": "Sort: Priority",
        "due": "Sort: Due Date",
        "created": "Sort: Created",
        "project": "Sort: Project",
    }

    def __init__(self, db, sync_host=None, parent=None):
        super().__init__(parent)
        self.db = db
        self._sync_host = sync_host
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
        self._raw_cache: dict[str, list] = {}
        self._tab_total_counts: dict[str, int] = {}
        self._search_engine = db.search_engine
        self._premium_tray_extension = maybe_load_task_tray_extension(
            server_name="sqlite-task-tray"
        )
        self._design_button_visible = False
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

        self._SORT_MODES = tuple(self._SORT_MODES)
        self._SORT_LABELS = dict(self._SORT_LABELS)
        if self._premium_tray_extension:
            for mode, label in self._premium_tray_extension.extra_sort_modes.items():
                if mode not in self._SORT_LABELS:
                    self._SORT_MODES = (*self._SORT_MODES, mode)
                    self._SORT_LABELS[mode] = label

        # Tab order: Dashboard, Suggested, Today, Inbox, Next, Notes, All, Done
        self._tab_keys = [
            "dashboard",
            "suggested",
            "today",
            "inbox",
            "next",
            "projects",
            "notes",
            "all",
            "done",
            # Read-only debate tabs (BUILD STEP 1, spec §2/§3). Appended so the
            # existing task tab indices are unchanged. ADOPTION FIX 2: `waiting`
            # ("Waiting on Me") leads — it is North-Star pain request #1.
            "waiting",
            "recent",
            "topics",
        ]
        # Debate tabs render through DebateListWidget (read-only), never
        # TaskListWidget — no itemChanged/mutation wiring (spec §2.0, B3).
        self._DEBATE_TABS = ("waiting", "recent", "topics")
        if self._premium_tray_extension:
            self._tab_keys.insert(1, self._premium_tray_extension.tab_key)
        self._tab_labels = {
            "dashboard": "Dashboard",
            "suggested": "Suggested",
            "today": "Today",
            "inbox": "Inbox",
            "next": "Next",
            "projects": "Projects",
            "notes": "Notes",
            "all": "All",
            "done": "Done",
            "recent": "Recent Decisions",
            "waiting": "Waiting on Me",
            "topics": "Debate by Topic",
        }
        if self._premium_tray_extension:
            self._tab_labels[self._premium_tray_extension.tab_key] = (
                self._premium_tray_extension.tab_label
            )
        # Read-only debate DAO (own mode=ro + query_only connection to the same
        # DB file; NO write path). Fail-open: if unavailable the debate tabs
        # render empty and the rest of the tray is unaffected.
        try:
            self._debate_dao = DebateReadDAO(self.db.db_path)
        except Exception:
            logging.getLogger("task_tray").warning(
                "DebateReadDAO unavailable; debate tabs will be empty", exc_info=True
            )
            self._debate_dao = None
        self._topics_open_topic = None  # None → digest; else show that thread
        self._debate_source_cache = {}

        self.tab_lists = {}
        self._debate_pages = {}
        self._debate_controls = {}
        self._waiting_task_controls = None
        self._waiting_task_list = None
        self._debate_task_inflight = set()
        for key in self._tab_keys:
            if key in self._DEBATE_TABS:
                page = DebateTabWidget(key)
                lw = page.list_widget
                lw.navigate_requested.connect(self._on_debate_navigate)
                lw.reader_requested.connect(self._open_debate_reader)
                lw.task_completion_requested.connect(
                    self._on_debate_task_completion_requested
                )
                page.controls.changed.connect(
                    lambda params, k=key: self._on_debate_controls_changed(k, params)
                )
                page.controls.back_requested.connect(self._on_topics_back)
                self._debate_pages[key] = page
                self._debate_controls[key] = page.controls
                if key == "waiting" and page.secondary_controls is not None:
                    self._waiting_task_controls = page.secondary_controls
                    self._waiting_task_list = page.secondary_list
                    page.secondary_controls.changed.connect(
                        lambda params: self._on_debate_controls_changed(
                            "waiting", params, section_b=True
                        )
                    )
                    page.secondary_list.reader_requested.connect(
                        self._open_debate_reader
                    )
                    page.secondary_list.task_completion_requested.connect(
                        self._on_debate_task_completion_requested
                    )
            else:
                lw = TaskListWidget(self.db)
                lw._search_engine = self._search_engine
                lw.itemChanged.connect(lambda item, k=key: self._on_item_changed(item))
            self.tab_lists[key] = lw
            self.tabs.addTab(
                self._debate_pages[key] if key in self._DEBATE_TABS else lw,
                self._tab_labels[key],
            )

        # TaskListWidget styles itself in its constructor. Debate lists and
        # controls are separate read-only widgets, so bind them to the same
        # restored user appearance before the first frame is painted.
        self._apply_debate_appearance()

        # B1a: dashboard rows are a read-only projection. Allow multi-row
        # mouse/keyboard selection and a Ctrl+C that copies the selection (or
        # the whole tab if nothing is selected) with readable line breaks. The
        # shortcut only reads item text → clipboard; it performs no DB write,
        # navigation, snooze, archive, or done toggle.
        dash_lw = self.tab_lists.get("dashboard")
        if dash_lw is not None:
            dash_lw.setSelectionMode(
                QAbstractItemView.SelectionMode.ExtendedSelection
            )
            dash_copy = QShortcut(
                QKeySequence(QKeySequence.StandardKey.Copy), dash_lw
            )
            dash_copy.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            dash_copy.activated.connect(
                lambda lw=dash_lw: self._copy_dashboard(lw)
            )

        # B1: Per-tab view state dict (sort + filters per tab)
        self._tab_views = {
            key: copy.deepcopy(_DEFAULT_TAB_VIEW) for key in self._tab_keys
        }
        for key in self._DEBATE_TABS:
            self._tab_views[key]["params"] = default_debate_control_params(key)
        if self._premium_tray_extension:
            premium_key = self._premium_tray_extension.tab_key
            if premium_key in self._tab_views:
                self._tab_views[premium_key]["params"] = self._normalize_tab_params(
                    premium_key,
                    self._premium_tray_extension.default_params,
                )
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
                    self._tab_views[key]["active"] = _normalize_filter_payload(
                        view.get("active", {})
                    )
                    self._tab_views[key]["excluded"] = _normalize_filter_payload(
                        view.get("excluded", {})
                    )
                    self._tab_views[key]["params"] = self._normalize_tab_params(
                        key, view.get("params", {})
                    )
        except (json.JSONDecodeError, TypeError, ValueError, AttributeError):
            pass

        # Materialize the restored JSON state into the visible native controls.
        # set_state() blocks its own signals, so this cannot trigger a DB read.
        for key in self._DEBATE_TABS:
            params = self._normalize_tab_params(
                key, self._tab_views[key].get("params", {})
            )
            self._tab_views[key]["params"] = params
            self._debate_controls[key].set_state(params)
        if self._waiting_task_controls is not None:
            self._waiting_task_controls.set_state(
                self._tab_views["waiting"]["params"].get(
                    "section_b_controls", {}
                )
            )

        # Persist the active tab by key for future compatibility. Do not force
        # Dashboard on startup: it is a curated projection and may be empty.
        legacy_idx = int(self._settings.value("active_tab", 0))
        saved_key = self._settings.value("active_tab_key", "")
        if not isinstance(saved_key, str) or saved_key not in self._tab_keys:
            saved_key = self._tab_keys[min(legacy_idx, len(self._tab_keys) - 1)]
        # The daily working surface is Today. Dashboard is curated and may be
        # empty, while the previously saved tab can be stale after restarts.
        initial_key = "today" if "today" in self._tab_keys else saved_key
        self._saved_active_tab = self._tab_keys.index(initial_key)
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
        self._rebuild_sort_menu()
        self._sort_toolbar_action = toolbar.addWidget(self._sort_btn)

        self._design_btn = QToolButton()
        self._design_btn.setText("Design...")
        self._design_btn.clicked.connect(self._edit_custom_design)
        self._design_btn.setVisible(False)
        toolbar.addWidget(self._design_btn)

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
        self._sort_toolbar_action.setVisible(not _is_fixed_initial)
        self._update_design_button_visibility()

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

        if self._sync_host is not None:
            self._sync_host._bridge_progress.connect(self._on_sync_progress)
            self._sync_host._bridge_done.connect(self._on_sync_done)
            if getattr(self._sync_host, "_last_sync_at", None):
                self._last_sync_at = self._sync_host._last_sync_at
                self._show_last_sync_time()

        # Intelligence v2 enrich signals
        self._enrich_in_progress = False
        self._enrich_running.connect(lambda msg: self.status.showMessage(msg, 30000))
        self._enrich_done.connect(self._on_enrich_done)
        self._entity_search_done.connect(self._on_entity_results)

        # Auto-refresh every 30s
        self._refresh_timer = QTimer(self)
        self._refresh_timer.timeout.connect(self.refresh)

        self.refresh()

    def _on_db_changed(self, path):
        """DB file changed — start/restart debounce timers."""
        self._db_refresh_debounce.start()  # 500ms UI refresh debounce
        self._refresh_db_watch_paths()
        if not getattr(self, "_auto_sync_enabled", True):
            return
        if self._sync_run_active or time.monotonic() < self._sync_cooldown_until:
            return  # suppress sync cascade from own sync operations
        self._auto_sync_timer.start()  # 60s bridge sync debounce

    def _on_db_dir_changed(self, path):
        """Directory changed — catch WAL create/rotate events."""
        self._db_refresh_debounce.start()
        watch_paths_changed = self._refresh_db_watch_paths()
        if not getattr(self, "_auto_sync_enabled", True):
            return
        if self._sync_run_active or time.monotonic() < self._sync_cooldown_until:
            return
        if watch_paths_changed:
            self._auto_sync_timer.start()

    def _refresh_db_watch_paths(self):
        """Ensure DB watcher tracks the DB, WAL, and parent directory."""
        wanted_dirs = {self._db_watch_dir}
        wanted_files = set()
        db_path = Path(self.db.db_path)
        for candidate in (db_path, Path(f"{self.db.db_path}-wal")):
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

    def _run_purge(self):
        self._last_purged = self.db.purge_old_done(days=30)
        self.db.purge_old_dashboard()

    def _process_recurring(self):
        created = _run_recurring_maintenance(self.db.db_path)
        if created:
            self.refresh()

    # ── Appearance ─────────────────────────────────────────────────────

    def _apply_debate_appearance(self):
        """Apply the current user theme/font/bold state to every debate view."""
        list_style = _build_list_style()
        surface_style = _build_debate_surface_style()
        for key in getattr(self, "_DEBATE_TABS", ()):
            page = self._debate_pages.get(key)
            if page is not None:
                page.setStyleSheet(surface_style)
            lw = self.tab_lists.get(key)
            if lw is not None:
                lw.setStyleSheet(list_style)
                lw.viewport().update()
        if self._waiting_task_list is not None:
            self._waiting_task_list.setStyleSheet(list_style)
            self._waiting_task_list.viewport().update()

    def _apply_appearance(self):
        """Rebuild all stylesheets from current theme/font/bold state."""
        _update_theme_colors()
        self.setStyleSheet(_build_main_style())
        self._filter_bar.setStyleSheet(_build_filter_style())
        for key, lw in self.tab_lists.items():
            if key in self._DEBATE_TABS:
                continue
            lw.setStyleSheet(_build_list_style())
            lw.viewport().update()
        self._apply_debate_appearance()
        self._settings.setValue("theme", _td._theme_name)
        self._settings.setValue("font_size", _td._font_size)
        self._settings.setValue("bold", "true" if _td._bold else "false")
        self._build_filter_chips()
        self._save_ui_state()
        self.refresh()

    def _normalize_tab_params(self, key, params):
        if key in getattr(self, "_DEBATE_TABS", ()):
            return normalize_debate_control_params(key, params)
        if self._premium_tray_extension and key == self._premium_tray_extension.tab_key:
            return self._premium_tray_extension.normalize_params(params)
        return dict(params or {})

    def _rebuild_sort_menu(self):
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

    def _update_design_button_visibility(self):
        if not hasattr(self, "_design_btn"):
            return
        current_key = ""
        idx = self.tabs.currentIndex()
        if 0 <= idx < len(self._tab_keys):
            current_key = self._tab_keys[idx]
        visible = bool(
            self._premium_tray_extension
            and current_key == self._premium_tray_extension.tab_key
        )
        if visible and self._premium_tray_extension:
            params = self._tab_views.get(current_key, {}).get("params", {})
            label_builder = getattr(
                self._premium_tray_extension,
                "design_button_label",
                None,
            )
            if callable(label_builder):
                self._design_btn.setText(str(label_builder(params) or "Design..."))
            else:
                self._design_btn.setText("Design...")
        self._design_btn.setVisible(visible)

    def _edit_custom_design(self):
        if not self._premium_tray_extension:
            return
        key = self._premium_tray_extension.tab_key
        params = self._tab_views.get(key, {}).get("params", {})
        dlg = CustomDesignDialog(params, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            raw_params = dlg.get_params()
            try:
                apply_dialog = getattr(
                    self._premium_tray_extension,
                    "apply_dialog_params",
                    None,
                )
                if callable(apply_dialog):
                    new_params = apply_dialog(raw_params)
                else:
                    new_params = raw_params
            except ValueError as exc:
                self.status.showMessage(str(exc), 8000)
                return
            self._tab_views[key]["params"] = self._normalize_tab_params(key, new_params)
            self._update_design_button_visibility()
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
                    "active": _normalize_filter_payload(self._active_filters),
                    "excluded": _normalize_filter_payload(self._excluded_filters),
                    "params": self._normalize_tab_params(
                        key,
                        self._tab_views[key].get("params", {}),
                    ),
                }

        # Serialize per-tab views
        serializable = {}
        for key, view in self._tab_views.items():
            serializable[key] = {
                "sort": view["sort"],
                "active": {
                    "priority": list(view["active"]["priority"]),
                    "due": list(view["active"]["due"]),
                    "project": sorted(
                        normalize_project_filter_values(view["active"]["project"])
                    ),
                },
                "excluded": {
                    "priority": list(view["excluded"]["priority"]),
                    "due": list(view["excluded"]["due"]),
                    "project": sorted(
                        normalize_project_filter_values(view["excluded"]["project"])
                    ),
                },
                "params": self._normalize_tab_params(key, view.get("params", {})),
            }
        self._settings.setValue("tab_views", json.dumps(serializable))
        self._settings.setValue("active_tab", self.tabs.currentIndex())
        idx = self.tabs.currentIndex()
        if 0 <= idx < len(self._tab_keys):
            self._settings.setValue("active_tab_key", self._tab_keys[idx])

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
        if self._sync_host is not None:
            self._sync_host.request_manual_sync()
        else:
            self._sync_bridge(initiator="manual")

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
        if mode == "ready":
            state_rank = {
                "ready_now": 0,
                "suggested_ready": 1,
                "blocked": 2,
                "waiting": 3,
                "cleanup_candidate": 4,
            }
            urgency_rank = {"critical": 0, "high": 1, "medium": 2, "low": 3}
            return sorted(
                tasks,
                key=lambda t: (
                    state_rank.get(t.get("_ready_state"), 9),
                    urgency_rank.get(t.get("_ready_urgency"), 9),
                    priority_sort_key(t),
                    t.get("due_date") or "9999-12-31",
                    t.get("created_at") or "",
                ),
            )
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
        if mode == "updated":
            return sorted(tasks, key=lambda t: t.get("updated_at") or "", reverse=True)
        if mode == "mailbox":
            return sorted(
                tasks,
                key=lambda t: (
                    t.get("mailbox_key") or "zzz_none",
                    t.get("updated_at") or "",
                ),
                reverse=False,
            )
        if mode == "client":
            return sorted(
                tasks,
                key=lambda t: (
                    t.get("client_ref") or t.get("project") or "zzz_none",
                    t.get("updated_at") or "",
                ),
            )
        if mode == "risk":
            risk_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
            return sorted(
                tasks,
                key=lambda t: (
                    risk_order.get(str(t.get("risk_level") or "").lower(), 4),
                    priority_sort_key(t),
                ),
            )
        if mode == "kind":
            return sorted(
                tasks,
                key=lambda t: (
                    t.get("_premium_kind") or "zzz_none",
                    t.get("updated_at") or "",
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
        dashboard_rows = self.db.get_dashboard()
        premium_rows = []
        premium_key = (
            self._premium_tray_extension.tab_key if self._premium_tray_extension else ""
        )
        if self._premium_tray_extension:
            try:
                premium_rows = self._premium_tray_extension.build_rows(
                    params=self._tab_views.get(premium_key, {}).get("params", {}),
                    search_text="",
                ).get("rows", [])
            except Exception as exc:
                logger.warning("premium tray rows unavailable: %s", exc, exc_info=True)
                premium_rows = []

        # Rebuild SmartKey search index with a bounded projection. The tray must
        # not retain every closed note/task body just to keep fuzzy search warm.
        self._search_engine.rebuild_index(
            _tray_search_index_rows(all_active, premium_rows, done)
        )

        ready_review = self.db.get_ready_review_tasks(limit=_TRAY_READY_REVIEW_LIMIT)
        suggested = suggested_ready(
            all_active + ready_review,
            include_readings=False,
            limit=_TRAY_SUGGESTED_LIMIT,
        )
        notes = [t for t in all_active if t.get("type") == "note"] + [
            t for t in done if t.get("type") == "note"
        ]

        raw = {
            "dashboard": dashboard_rows,
            "suggested": suggested,
            "today": [t for t in all_active if t.get("section") == "today"],
            "inbox": [t for t in all_active if t.get("section") == "inbox"],
            "next": [t for t in all_active if t.get("section") == "next"],
            "notes": notes,
            "projects": all_active,
            "all": all_active + done,
            "done": done,
        }
        if premium_key:
            raw[premium_key] = premium_rows

        # Keep only raw buckets. Filtering/sorting/rendering is lazy per active tab.
        self._raw_cache = raw
        self._filtered_cache = {}
        self._tab_total_counts = {}
        if self._search_text:
            # Async entity search — tasks render immediately, entities arrive via signal
            self._entity_results = []
            self._request_entity_search(self._search_text)
        else:
            self._cancel_entity_searches()
            self._entity_results = []

        # Update tab visibility. Dashboard is visible only when curated rows
        # exist; otherwise Today/Suggested remain the open daily TODO surface.
        # Debate tabs load from the read-only DAO, not from `raw`; they must stay
        # visible through the initial + every periodic refresh (their `raw` bucket
        # is always empty, so they would otherwise be hidden by the count check).
        always_visible = (
            "suggested", "today", "notes", "projects",
            *getattr(self, "_DEBATE_TABS", ()),
        )
        if premium_key:
            always_visible = (*always_visible, premium_key)
        for i, key in enumerate(self._tab_keys):
            count = len(raw.get(key, []))
            self.tabs.setTabVisible(i, count > 0 or key in always_visible)

        # Actual current index AFTER all tab visibility and search changes
        current_idx = self.tabs.currentIndex()
        if not (0 <= current_idx < len(self._tab_keys)) or not self.tabs.isTabVisible(
            current_idx
        ):
            for preferred in ("today", "suggested", "projects", "notes"):
                idx = self._tab_keys.index(preferred)
                if self.tabs.isTabVisible(idx):
                    self.tabs.setCurrentIndex(idx)
                    current_idx = idx
                    break

        # Lazy rendering: only load the currently active tab
        if 0 <= current_idx < len(self._tab_keys):
            self._load_tab(self._tab_keys[current_idx])

        # Status bar — derive summary from already-fetched data
        s = self.db.get_summary(all_active)
        task_count = sum(1 for t in all_active if t.get("type", "task") == "task")
        note_count = len(notes)
        done_count = len(done)
        msg = f"Tasks: {task_count} | Notes: {note_count} | Done: {done_count} | Overdue: {s['overdue']}"
        if premium_rows:
            msg += f" | Premium: {len(premium_rows)}"
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
                    "params": self._normalize_tab_params(
                        old_key,
                        self._tab_views[old_key].get("params", {}),
                    ),
                }

        # Load incoming tab's state
        self._current_tab_idx = idx
        if idx < len(self._tab_keys):
            new_key = self._tab_keys[idx]
            if new_key in self._tab_views:
                v = self._tab_views[new_key]
                self._sort_mode = v["sort"]
                self._active_filters = _normalize_filter_payload(v["active"])
                self._excluded_filters = _normalize_filter_payload(v["excluded"])
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
            if new_key not in self._DEBATE_TABS:
                self._build_filter_chips()

            # Hide sort/filter UI for fixed tabs
            is_fixed = new_key in _FIXED_VIEW_TABS
            is_debate = new_key in self._DEBATE_TABS
            show_task_sort = not is_fixed and not is_debate
            self._sort_btn.setVisible(show_task_sort)
            self._sort_toolbar_action.setVisible(show_task_sort)
            self._filter_bar.setVisible(not is_debate)
            self._search_input.setPlaceholderText(
                "Search everywhere: debates, tasks, knowledge…"
                if is_debate else "Search tasks..."
            )
            if is_debate:
                self._debate_controls[new_key].set_state(
                    self._tab_views[new_key].get("params", {})
                )
                if new_key == "waiting" and self._waiting_task_controls is not None:
                    self._waiting_task_controls.set_state(
                        self._tab_views[new_key].get("params", {}).get(
                            "section_b_controls", {}
                        )
                    )
                self._debate_controls[new_key].set_thread_mode(
                    new_key == "topics" and bool(self._topics_open_topic)
                )
            self._update_design_button_visibility()

        self._save_ui_state()
        if idx < len(self._tab_keys):
            self._load_tab(self._tab_keys[idx])

    # ---- Read-only debate tabs (BUILD STEP 1, spec §2/§3) ------------------
    def _debate_recent_params(self):
        params = normalize_debate_control_params(
            "recent", self._tab_views.get("recent", {}).get("params", {})
        )
        hours = params.get("hours", 24)
        kinds = params.get("kinds") or ["DECISION", "STATE", "STATUS"]
        # Role is a client-side chip in the browser board. Querying all roles
        # keeps its dynamic option set intact and avoids a second DB round-trip.
        return hours, kinds, None

    def _on_debate_controls_changed(self, key, params, *, section_b=False):
        """Persist and apply native client controls without re-reading SQLite."""
        if key not in self._DEBATE_TABS or key not in self._tab_views:
            return
        old = self._normalize_tab_params(
            key, self._tab_views[key].get("params", {})
        )
        if key == "waiting" and section_b:
            new = copy.deepcopy(old)
            new["section_b_controls"] = normalize_debate_control_params(
                "waiting_tasks", params
            )
        elif key == "waiting":
            merged = dict(params or {})
            merged["section_b_controls"] = old.get("section_b_controls", {})
            new = self._normalize_tab_params(key, merged)
        else:
            new = self._normalize_tab_params(key, params)
        self._tab_views[key]["params"] = new
        server_changed = key == "recent" and any(
            old.get(field) != new.get(field) for field in ("hours", "kinds")
        )
        if server_changed:
            self._debate_source_cache.pop(key, None)
        self._filtered_cache.pop(key, None)
        self._save_ui_state()

        idx = self.tabs.currentIndex()
        if not (0 <= idx < len(self._tab_keys)) or self._tab_keys[idx] != key:
            return
        if not server_changed and key in self._debate_source_cache:
            rows = self._apply_debate_controls(key, self._debate_source_cache[key])
            self._filtered_cache[key] = rows
            self._load_debate_tab(key, rows)
        else:
            self._load_tab(key)

    def _apply_debate_controls(self, key, rows):
        """Return a filtered/sorted copy and update the visible result count."""
        if not rows or "search" in rows:
            return rows
        params = self._tab_views.get(key, {}).get("params", {})
        out = dict(rows)
        items = []
        target = ""
        if key == "recent":
            target = "items"
            items = list(rows.get(target, []))
        elif key == "waiting":
            section_a = list(rows.get("section_a", []))
            visible_a = apply_debate_controls(key, section_a, params)
            section_b = list(rows.get("section_b", []))
            section_b_params = normalize_debate_control_params(
                "waiting_tasks", params.get("section_b_controls", {})
            )
            visible_b = apply_debate_controls(
                "waiting_tasks", section_b, section_b_params
            )
            out["section_a"] = visible_a
            out["section_b"] = visible_b
            out["_control_count"] = (len(visible_a), len(section_a))
            out["_section_b_control_count"] = (len(visible_b), len(section_b))
            self._debate_controls[key].set_available(section_a)
            if self._waiting_task_controls is not None:
                self._waiting_task_controls.set_available(section_b)
            return out
        elif key == "topics" and "thread" in rows:
            thread = dict(rows.get("thread", {}))
            items = list(thread.get("messages", []))
            visible = apply_debate_controls(key, items, params)
            thread["messages"] = visible
            out["thread"] = thread
            out["_control_count"] = (len(visible), len(items))
            self._debate_controls[key].set_available(items)
            return out
        elif key == "topics":
            digest = dict(rows.get("digest", {}))
            items = list(digest.get("topics", []))
            visible = apply_debate_controls(key, items, params)
            digest["topics"] = visible
            out["digest"] = digest
            out["_control_count"] = (len(visible), len(items))
            self._debate_controls[key].set_available(items)
            return out
        if target:
            visible = apply_debate_controls(key, items, params)
            out[target] = visible
            out["_control_count"] = (len(visible), len(items))
            self._debate_controls[key].set_available(items)
        return out

    def _build_debate_rows(self, key):
        """Return the raw read-only structure for a debate tab (never the task path)."""
        dao = self._debate_dao
        if dao is None:
            return {}
        try:
            # Toolbar search spans debate + tasks + knowledge via the dedicated
            # per-source BM25 path (spec §2d/§4, M4 verbatim board order). This
            # bypasses TaskSearchEngine/SmartKey entirely and never re-sorts.
            if self._search_text:
                return {"search": dao.board_search(self._search_text)}
            if key == "recent":
                hours, kinds, role = self._debate_recent_params()
                source = dao.recent(hours, role, kinds)
            elif key == "waiting":
                items, cand = dao.waiting_section_a()
                task_items, task_before = dao.waiting_section_b()
                source = {
                    "section_a": items,
                    "section_a_before": cand,
                    "section_b": task_items,
                    "section_b_before": task_before,
                }
            elif key == "topics":
                if self._topics_open_topic:
                    source = {"thread": dao.topic_thread(self._topics_open_topic)}
                else:
                    source = {"digest": dao.topics()}
            else:
                source = {}
            self._debate_source_cache[key] = source
            return self._apply_debate_controls(key, source)
        except sqlite3.Error:
            logging.getLogger("task_tray").warning(
                "debate read failed for tab %s", key, exc_info=True
            )
        return {}

    def _load_debate_tab(self, key, rows):
        lw = self.tab_lists[key]
        lw.clear_rows()
        if key == "waiting" and self._waiting_task_list is not None:
            self._waiting_task_list.clear_rows()
            b_visible, b_total = (
                rows.get("_section_b_control_count", (0, 0)) if rows else (0, 0)
            )
            self._waiting_task_controls.set_count(b_visible, b_total)
        visible, total = rows.get("_control_count", (0, 0)) if rows else (0, 0)
        self._debate_controls[key].set_count(visible, total)
        self._debate_controls[key].set_thread_mode(
            key == "topics" and "thread" in (rows or {})
        )
        if not rows:
            lw.add_header("—")
            return
        if "search" in rows:
            self._debate_controls[key].setEnabled(False)
            if key == "waiting" and self._waiting_task_controls is not None:
                self._waiting_task_controls.setEnabled(False)
                self._waiting_task_list.add_header(
                    "Global results are shown in the upper list"
                )
            self._load_debate_search(lw, rows["search"])
            return
        self._debate_controls[key].setEnabled(True)
        if key == "waiting" and self._waiting_task_controls is not None:
            self._waiting_task_controls.setEnabled(True)
        if key == "recent":
            items = rows.get("items", [])
            lw.add_header(f"Recent decisions ({len(items)})")
            for it in items:
                mid = it["msg_id"]
                text = (f"[{it.get('kind','')}] {it.get('role','')} · "
                        f"{it.get('age','')} · {mid[:8]} — {it.get('line','')}")
                copy_block = (f"{it.get('role','')} · {it.get('kind','')} · "
                              f"{it.get('age','')} · {mid} · {it.get('body','')}")
                lw.add_debate_row(mid, text, topic_id=it.get("topic_id"),
                                  copy_payload=copy_block,
                                  reader_payload=self._debate_reader_payload(it))
        elif key == "waiting":
            items = rows.get("section_a", [])
            # The old denominator counted every recent Q/DECISION candidate,
            # not operator asks. "1 of 99" was therefore misleading noise on
            # the operator surface; only the actionable result count belongs
            # in this header.
            lw.add_header(f"Waiting on me ({len(items)})")
            for it in items:
                mid = it["msg_id"]
                stale = " ⏳" if it.get("stale") else ""
                text = (f"[{it.get('kind','')}] {it.get('role','')} · "
                        f"{it.get('age','')}{stale} · {mid[:8]} — {it.get('line','')}")
                copy_block = (f"{it.get('role','')} · {it.get('kind','')} · "
                              f"{it.get('age','')} · {mid} · {it.get('body','')}")
                lw.add_debate_row(
                    mid, text, topic_id=None, copy_payload=copy_block,
                    reader_payload=self._debate_reader_payload(it),
                )
            task_items = rows.get("section_b", [])
            task_lw = self._waiting_task_list
            task_lw.add_header(
                f"Your tasks — now ({len(task_items)} of "
                f"{rows.get('section_b_before', 0)})"
            )
            for task in task_items:
                task_id = str(task.get("id") or "")
                due = task.get("due_date") or "no due date"
                text = (
                    f"[{task.get('section','')}] {task.get('priority','')} · "
                    f"{due} · {task.get('project','')} — {task.get('title','')}"
                )
                record = "\n".join(
                    f"{name}: {value}" for name, value in (
                        ("id", task_id),
                        ("title", task.get("title") or ""),
                        ("section", task.get("section") or ""),
                        ("priority", task.get("priority") or ""),
                        ("status", task.get("status") or ""),
                        ("due_date", task.get("due_date") or ""),
                        ("project", task.get("project") or ""),
                        ("updated_at", task.get("updated_at") or ""),
                    ) if value
                )
                task_lw.add_task_row(
                    task_id, text,
                    copy_payload=record,
                    reader_payload={
                        "title": str(task.get("title") or task_id),
                        "body": str(task.get("title") or ""),
                        "record": record,
                    },
                    completion_payload=self._task_completion_payload(task),
                )
        elif key == "topics":
            if "thread" in rows:
                th = rows["thread"]
                lw.add_header(
                    f"◀ {th.get('title','')} [{th.get('state','')}] ({th.get('count',0)})"
                )
                for m in th.get("messages", []):
                    mid = m["msg_id"]
                    indent = "    " if m.get("reply_to") else ""
                    text = (f"{indent}[{m.get('kind','')}] {m.get('role','')} · "
                            f"{m.get('age','')} · {mid[:8]} — {m.get('line','')}")
                    copy_block = (f"{m.get('role','')} · {m.get('kind','')} · "
                                  f"{m.get('age','')} · {mid} · {m.get('body','')}")
                    lw.add_debate_row(mid, text, topic_id=th.get("topic_id"),
                                      copy_payload=copy_block,
                                      reader_payload=self._debate_reader_payload(
                                          m, topic_id=th.get("topic_id")
                                      ))
            else:
                topics = rows.get("digest", {}).get("topics", [])
                lw.add_header(f"Topics ({len(topics)}) — double-click to open thread")
                for t in topics:
                    tid = t["topic_id"]
                    text = (f"[{t.get('state','')}] {t.get('title','')} · "
                            f"{t.get('count',0)} messages · {t.get('age','')}")
                    lw.add_debate_row(tid, text, topic_id=tid, copy_payload=tid)

    @staticmethod
    def _debate_reader_payload(item, *, topic_id=None):
        """Build both human-readable text and a complete copyable record."""
        msg_id = str(item.get("msg_id") or item.get("id") or "")
        topic = str(topic_id or item.get("topic_id") or "")
        body = str(item.get("body") or item.get("title") or "")
        fields = (
            ("msg_id", msg_id),
            ("topic_id", topic),
            ("role", item.get("role") or ""),
            ("kind", item.get("kind") or item.get("type") or ""),
            ("priority", item.get("priority") or ""),
            ("timestamp", item.get("ts") or item.get("updated_at") or ""),
        )
        record = "\n".join(f"{name}: {value}" for name, value in fields if value)
        if body:
            record = f"{record}\n\n{body}" if record else body
        title_bits = [
            f"[{item.get('kind') or item.get('type') or ''}]",
            str(item.get("role") or item.get("title") or ""),
            msg_id,
        ]
        return {
            "title": " · ".join(bit for bit in title_bits if bit),
            "body": body,
            "record": record,
        }

    def _load_debate_search(self, lw, result):
        """Render grouped per-source BM25 search results.

        Verbatim board order per source (no cross-source merge, no recency
        band — M4). Debate and knowledge records remain inert; active task/note
        records expose the same narrow CAS completion control in all three tabs.
        """
        debate = result.get("debate", [])
        tasks = result.get("tasks", [])
        knowledge = result.get("knowledge", [])
        lw.add_header(f"Debates ({len(debate)})")
        for r in debate:
            mid = r["msg_id"]
            text = (f"[{r.get('kind','')}] {r.get('role','')} · {mid[:8]} — "
                    f"{r.get('snippet','') or r.get('body','')}")
            lw.add_debate_row(mid, text, topic_id=r.get("topic_id"),
                              copy_payload=f"{mid} · {r.get('body','')}",
                              reader_payload=self._debate_reader_payload(r))
        lw.add_header(f"Tasks/notes ({len(tasks)})")
        for r in tasks:
            tid = r.get("id", "")
            text = f"[{r.get('type','')}/{r.get('status','')}] {r.get('title','')}"
            record = "\n".join(
                f"{name}: {value}" for name, value in (
                    ("id", tid),
                    ("type", r.get("type") or ""),
                    ("status", r.get("status") or ""),
                    ("section", r.get("section") or ""),
                    ("project", r.get("project") or ""),
                    ("title", r.get("title") or ""),
                ) if value
            )
            lw.add_task_row(
                tid,
                text,
                copy_payload=record,
                reader_payload={
                    "title": str(r.get("title") or tid),
                    "body": str(r.get("title") or ""),
                    "record": record,
                },
                completion_payload=self._task_completion_payload(r),
            )
        lw.add_header(f"Knowledge ({len(knowledge)})")
        for r in knowledge:
            eid = r.get("id", "")
            text = f"[{r.get('type','')}] {r.get('name','')}"
            lw.add_debate_row(f"entity-{eid}", text, topic_id=None,
                              copy_payload=str(r.get("name", "")),
                              reader_payload={
                                  "title": str(r.get("name") or eid),
                                  "body": str(r.get("name") or ""),
                                  "record": "\n".join(
                                      f"{name}: {value}" for name, value in (
                                          ("id", eid),
                                          ("type", r.get("type") or ""),
                                          ("name", r.get("name") or ""),
                                      ) if value
                                  ),
                              })

    @staticmethod
    def _task_completion_payload(task):
        """Return the exact rendered status token, or None for an inert row."""
        task_id = str(task.get("id") or "").strip()
        task_type = str(task.get("type") or "task").strip()
        status = str(task.get("status") or "").strip()
        event_id = str(task.get("status_event_id") or "").strip()
        try:
            order = int(task.get("status_order") or 0)
        except (TypeError, ValueError):
            order = 0
        if (
            not task_id
            or task_type not in {"task", "note"}
            or status not in {"not_started", "in_progress"}
            or order <= 0
            or not event_id
        ):
            return None
        return {
            "id": task_id,
            "type": task_type,
            "title": str(task.get("title") or task_id),
            "expected_status": status,
            "expected_order": order,
            "expected_event_id": event_id,
        }

    def _on_debate_task_completion_requested(self, payload, checked):
        """Defer a task/note completion outside QListWidget signal dispatch."""
        if not isinstance(payload, dict):
            return
        task_id = str(payload.get("id") or "")
        if not task_id:
            return
        if not checked:
            self._debate_task_inflight.discard(task_id)
            return
        if task_id in self._debate_task_inflight:
            return
        self._debate_task_inflight.add(task_id)
        QTimer.singleShot(
            10,
            lambda p=dict(payload): self._run_debate_task_completion(p),
        )

    def _run_debate_task_completion(self, payload):
        """Ignore a deferred callback when the operator unchecked meanwhile."""
        task_id = str(payload.get("id") or "")
        if task_id not in self._debate_task_inflight:
            return
        self._apply_debate_task_completion(payload)

    def _invalidate_debate_task_views(self):
        self._debate_source_cache.clear()
        for key in self._DEBATE_TABS:
            self._filtered_cache.pop(key, None)

    def _set_debate_task_checked(self, task_id, checked):
        for page in self._debate_pages.values():
            page.list_widget.set_task_checked(task_id, checked)
            if page.secondary_list is not None:
                page.secondary_list.set_task_checked(task_id, checked)

    def _apply_debate_task_completion(self, payload):
        """Apply one CAS-safe active -> done transition on the live task DB."""
        task_id = str(payload.get("id") or "")
        try:
            token = StatusToken(
                task_id=task_id,
                status=str(payload.get("expected_status") or ""),
                updated_order=int(payload.get("expected_order") or 0),
                source_event_id=str(payload.get("expected_event_id") or ""),
            )
            if payload.get("type") not in {"task", "note"}:
                raise ValueError("unsupported record type")
            result = transition_status(
                self.db.db_path,
                token,
                "done",
                actor_id="operator",
                forbid_path=None,
            )
            if result.get("outcome") != "applied":
                reason = result.get("reason") or result.get("outcome") or "conflict"
                self._set_debate_task_checked(task_id, False)
                self.status.showMessage(
                    f"Task was not changed ({reason}); the list was refreshed.",
                    8000,
                )
                self._invalidate_debate_task_views()
                self.refresh()
                return

            self._invalidate_debate_task_views()
            self.status.showMessage(
                f"Done: {payload.get('title') or task_id}", 5000
            )
            callback = getattr(self.db, "on_change", None)
            if callable(callback):
                callback()
            else:
                self.refresh()
        except (
            PermissionError,
            RuntimeError,
            sqlite3.Error,
            TypeError,
            ValueError,
        ) as exc:
            logging.getLogger("task_tray").error(
                "Error completing task/note %s: %s", task_id, exc, exc_info=True
            )
            self._set_debate_task_checked(task_id, False)
            self.status.showMessage(f"DB error — item not saved. {exc}", 8000)
        finally:
            self._debate_task_inflight.discard(task_id)

    def _open_debate_reader(self, payload):
        """Open a local read-only reader; selection and Ctrl+C stay native."""
        dialog = DebateReaderDialog(payload, self)
        dialog.setStyleSheet(_build_debate_reader_style())
        dialog.exec()

    def _on_topics_back(self):
        """Return from a read-only thread to the topic digest."""
        if not self._topics_open_topic:
            return
        self._topics_open_topic = None
        self._debate_source_cache.pop("topics", None)
        self._filtered_cache.pop("topics", None)
        self._debate_controls["topics"].set_thread_mode(False)
        self._load_tab("topics")

    def _on_debate_navigate(self, target):
        """In-app navigation from a debate row: open the topics tab on a topic.

        Read-only: switches tab + loads a read-only thread. No DB write.
        """
        if not target:
            return
        self._topics_open_topic = str(target)
        self._debate_source_cache.pop("topics", None)
        self._filtered_cache.pop("topics", None)
        if "topics" in self._tab_keys:
            self.tabs.setCurrentIndex(self._tab_keys.index("topics"))
            self._load_tab("topics")

    def _build_tab_rows(self, key):
        """Filter/sort a single tab on demand and cache only that result."""
        if key in getattr(self, "_DEBATE_TABS", ()):
            rows = self._build_debate_rows(key)
            self._filtered_cache[key] = rows
            return rows
        source = list(self._raw_cache.get(key, []))
        if key == "dashboard":
            self._tab_total_counts[key] = len(source)
            self._filtered_cache[key] = source
            return source

        if self._search_text:
            search_results = self._search_engine.search(
                self._search_text,
                source,
                conn=self.db._conn,
                use_vector=False,
            )
            result_ids = {task["id"] for task in search_results}
            source = [task for task in source if task.get("id") in result_ids]
            if key in self._tab_views:
                view = self._tab_views[key]
                source = self._filter_chips_only(
                    source,
                    view["active"],
                    view["excluded"],
                )
                sort_mode = view["sort"]
            else:
                sort_mode = None
        elif key in self._tab_views:
            view = self._tab_views[key]
            source = self._filter(source, view["active"], view["excluded"])
            sort_mode = view["sort"]
        else:
            source = self._filter(source)
            sort_mode = None

        rows = self._sort_tasks(source, sort_mode)
        self._tab_total_counts[key] = len(rows)
        if key == "suggested":
            rows = rows[:12]
        elif key in ("all", "done"):
            rows = rows[:_TAB_PAGE_SIZE]
        self._filtered_cache[key] = rows
        return rows

    @staticmethod
    def _dashboard_sort_key(row):
        return (
            _DASHBOARD_KIND_ORDER.get(row.get("kind"), 99),
            str(row.get("updated_at") or ""),
            str(row.get("slot") or ""),
        )

    def _add_dashboard_header(self, lw, text):
        header = QListWidgetItem(text)
        # B1a: selectable (for mouse/keyboard select + Ctrl+C) but NOT
        # checkable/editable — so selecting/copying cannot mutate task state.
        header.setFlags(
            Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        )
        header.setBackground(QColor("#1d2b36"))
        header.setForeground(QColor("#d9e2ec"))
        font = header.font()
        font.setBold(True)
        header.setFont(font)
        lw.addItem(header)

    def _add_dashboard_row(self, lw, row):
        kind = str(row.get("kind") or "")
        tag = _DASHBOARD_KIND_TAG.get(kind, kind[:1].upper() or "-")
        text = f"  [{tag}] {row.get('body') or ''}"
        if row.get("priority") == "H":
            text = f"{text}  (H)"
        item = QListWidgetItem(text)
        item.setData(
            Qt.ItemDataRole.UserRole,
            f"dashboard:{row.get('day')}:{row.get('task_id')}:{kind}:{row.get('slot')}",
        )
        # B1a: selectable (for mouse/keyboard select + Ctrl+C copy) but NOT
        # checkable/editable. Double-click/context-menu are guarded against the
        # `dashboard:` UserRole prefix in TaskListWidget, so selecting/copying a
        # row cannot navigate, snooze, archive, delete, or mutate any task.
        item.setFlags(
            Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        )
        item.setForeground(QColor(_DASHBOARD_KIND_COLOR.get(kind, "#cbd5e1")))
        tooltip_bits = [
            f"task_id={row.get('task_id')}",
            f"kind={kind}",
            f"slot={row.get('slot')}",
        ]
        if row.get("src_msg_id"):
            tooltip_bits.append(f"src_msg_id={row.get('src_msg_id')}")
        item.setToolTip(" | ".join(tooltip_bits))
        lw.addItem(item)

    def _load_dashboard_tab(self, lw, rows):
        fp = tuple(
            (
                r.get("day"),
                r.get("task_id"),
                r.get("kind"),
                r.get("slot"),
                r.get("body"),
                r.get("priority"),
                r.get("updated_at"),
                r.get("task_title"),
                r.get("task_section"),
                r.get("task_status"),
            )
            for r in rows
        )
        if fp == lw._last_fp:
            return
        lw._last_fp = fp
        lw._tasks = []
        lw.blockSignals(True)
        try:
            lw.clear()
            today_groups: dict[str, list[dict]] = {}
            today_titles: dict[str, str] = {}
            other_rows: list[dict] = []
            for row in rows:
                if row.get("task_section") == "today":
                    task_id = str(row.get("task_id") or "")
                    today_groups.setdefault(task_id, []).append(row)
                    title = row.get("task_title") or task_id[:8]
                    today_titles[task_id] = str(title)
                else:
                    other_rows.append(row)

            if not rows:
                footer = QListWidgetItem("No dashboard items for today.")
                footer.setFlags(Qt.ItemFlag.NoItemFlags)
                footer.setForeground(QColor("#888"))
                lw.addItem(footer)
                return

            for task_id, task_rows in today_groups.items():
                title = today_titles.get(task_id) or task_id[:8]
                status = next(
                    (
                        str(row.get("task_status") or "").strip()
                        for row in task_rows
                        if row.get("task_status")
                    ),
                    "",
                )
                status_suffix = (
                    f" [{status.replace('_', ' ').upper()}]" if status else ""
                )
                self._add_dashboard_header(
                    lw, f"-- {title}{status_suffix} ({len(task_rows)}) --"
                )
                for row in sorted(task_rows, key=self._dashboard_sort_key):
                    self._add_dashboard_row(lw, row)

            if other_rows:
                self._add_dashboard_header(
                    lw, f"-- Other / debate work-items ({len(other_rows)}) --"
                )
                for row in sorted(
                    other_rows,
                    key=lambda r: (
                        str(r.get("task_title") or r.get("task_id") or ""),
                        *self._dashboard_sort_key(r),
                    ),
                ):
                    if not row.get("task_title"):
                        label = str(row.get("task_id") or "")[:8]
                        row = dict(row)
                        row["body"] = f"{label}: {row.get('body') or ''}"
                    self._add_dashboard_row(lw, row)
        finally:
            lw.blockSignals(False)

    def _copy_dashboard(self, lw):
        """Copy dashboard text to the clipboard. Read-only: reads QListWidget
        item text (headings/counts + [tag] body/status/priority lines) and
        writes it to the clipboard. Copies the current selection, or the whole
        tab when nothing is selected. Performs NO DB write, navigation, snooze,
        archive, or done toggle — it cannot change any task state."""
        selected = lw.selectedItems()
        if selected:
            # Preserve visual (top-to-bottom) order regardless of click order.
            # QListWidgetItem is unhashable in PyQt6, so deriving a dict keyed
            # by the item crashes exactly when a multi-row selection is copied.
            items = sorted(selected, key=lw.row)
        else:
            items = [lw.item(i) for i in range(lw.count())]
        lines = [(it.text() or "").rstrip() for it in items if it is not None]
        text = "\n".join(line for line in lines if line)
        if text:
            _td._clipboard_write(text)
            self.status.showMessage("Dashboard copied to clipboard.", 3000)

    def _load_tab(self, key):
        """Render a single tab from cached data. Caps All/Done at 200 items."""
        if key in getattr(self, "_DEBATE_TABS", ()):
            rows = self._filtered_cache.get(key)
            if rows is None:
                rows = self._build_tab_rows(key)
            self._load_debate_tab(key, rows)
            return
        tasks = self._filtered_cache.get(key)
        if tasks is None:
            tasks = self._build_tab_rows(key)
        cap_msg = ""
        total = self._tab_total_counts.get(key, len(tasks))
        if key in ("all", "done") and total > len(tasks):
            cap_msg = f"── {total - len(tasks)} more items... ──"

        # Entity results only shown in "suggested" tab during search
        entities = (
            self._entity_results if (self._search_text and key == "suggested") else []
        )

        lw = self.tab_lists[key]
        if key == "dashboard":
            self._load_dashboard_tab(lw, tasks)
        elif key == "suggested":
            lw.load_smart_grouped(tasks, entities=entities)
        elif (
            self._premium_tray_extension and key == self._premium_tray_extension.tab_key
        ):
            params = self._tab_views.get(key, {}).get("params", {})
            group_by = str(params.get("group_by") or "smart").lower()
            if group_by == "project":
                proj_sorted = sorted(
                    tasks, key=lambda t: t.get("project") or "zzz_none"
                )
                lw.load_grouped_by_project(proj_sorted)
            elif group_by == "client":
                lw.load_grouped_by_field(tasks, "client_ref", empty_label="Unscoped")
            elif group_by == "mailbox":
                lw.load_grouped_by_field(tasks, "mailbox_key", empty_label="No mailbox")
            elif group_by == "risk":
                lw.load_grouped_by_field(tasks, "risk_level", empty_label="No risk")
            elif group_by == "kind":
                lw.load_grouped_by_field(tasks, "_premium_kind", empty_label="Other")
            else:
                lw.load_smart_grouped(tasks)
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
        # Skip entity items (no checkbox behavior). B1a: dashboard projection
        # rows are selectable/copyable only — never checkable — so a select/copy
        # can never reach mark_done/update_task here.
        if isinstance(task_id, str) and (
            task_id.startswith("entity:")
            or task_id.startswith("premium:")
            or task_id.startswith("dashboard:")
            or task_id.startswith("debate:")  # B3 defense-in-depth: read-only debate rows
        ):
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
            attachment_changes = dlg.get_attachment_changes()
            title = vals.pop("title", "")
            if title:
                self.db.add_task(
                    title,
                    attachments=attachment_changes["add_paths"],
                    **vals,
                )
                self.refresh()

    def showEvent(self, event):
        super().showEvent(event)
        self._refresh_timer.start(_REFRESH_INTERVAL_MS)
        if getattr(self, "_last_sync_at", None):
            self._show_last_sync_time()
        self.refresh()

    def closeEvent(self, event):
        self._settings.setValue("geometry", self.saveGeometry())
        self._save_ui_state()
        self._search_engine.save()
        self._refresh_timer.stop()
        self._search_timer.stop()
        event.ignore()
        self.hide()


# ── App Controller ──────────────────────────────────────────────────


class TaskTrayApp(BridgeSyncMixin):
    """Main application controller."""

    def __init__(self, instance_socket=None):
        self.app = QApplication(sys.argv)
        self.app.setQuitOnLastWindowClosed(False)
        self.db = TaskDB()
        self.db.on_change = self._refresh_all
        self.app.aboutToQuit.connect(self._on_quit)
        self.full_window = None
        self._last_sync_at = None

        self._bridge_signal_bus = _BridgeSignalBus(self.app)
        self._bridge_progress = self._bridge_signal_bus.progress
        self._bridge_done = self._bridge_signal_bus.done
        self._bridge_refresh_requested = self._bridge_signal_bus.refresh_requested
        self._bridge_done.connect(self._on_app_sync_done)
        self._background_db_write_lock = threading.Lock()
        self._enrich_cache_thread_running = False
        self._enrich_cache_thread_lock = threading.Lock()
        self._auto_sync_enabled = (
            os.environ.get("SQLITE_MEMORY_TRAY_AUTO_SYNC", "1") != "0"
        )

        # Periodic entity enrichment cache refresh (60s safety net for external writes)
        self._enrich_timer = QTimer(self.app)
        self._enrich_timer.timeout.connect(self._schedule_enrich_cache_refresh)
        self._enrich_timer.start(60_000)

        self._audit_timer = None
        if os.environ.get("SQLITE_MEMORY_TRAY_BACKGROUND_AUDIT") == "1":
            self._audit_timer = QTimer(self.app)
            self._audit_timer.timeout.connect(self._start_background_memory_audit)
            self._audit_timer.start(_BACKGROUND_AUDIT_INTERVAL_MS)
            QTimer.singleShot(
                _BACKGROUND_AUDIT_STARTUP_DELAY_MS,
                self._start_background_memory_audit,
            )

        # Tray icon
        self.tray = QSystemTrayIcon()
        self._update_icon()
        self.tray.setToolTip(self._tooltip())
        self.tray.activated.connect(self._on_tray_activated)
        self.status = _TrayStatusProxy(self)

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

        # Reminder timer — check every 60s for due reminders
        self._reminder_timer = QTimer(self.app)
        self._reminder_timer.timeout.connect(self._check_reminders)
        self._reminder_timer.start(60_000)
        self._reminder_delivery_state: dict[
            tuple[str, str], dict[str, float | int]
        ] = {}
        self._active_reminder_keys: set[tuple[str, str]] = set()
        self._active_reminder_dlgs: list = []  # keep dialogs alive until closed
        QTimer.singleShot(5000, self._check_reminders)  # initial check after startup

        # Single-instance socket polling
        self._instance_socket = instance_socket
        if instance_socket:
            self._instance_timer = QTimer(self.app)
            self._instance_timer.timeout.connect(self._poll_instance_socket)
            self._instance_timer.start(2000)

        self._last_purged = self.db.purge_old_done(days=30)
        self._purge_timer = QTimer(self.app)
        self._purge_timer.timeout.connect(self._run_purge)
        self._purge_timer.start(_PURGE_INTERVAL_MS)

        self._db_watch_dir = str(Path(self.db.db_path).parent)
        self._db_path = self.db.db_path
        self._db_watcher = QFileSystemWatcher(self.app)
        self._db_watcher.fileChanged.connect(self._on_db_changed)
        self._db_watcher.directoryChanged.connect(self._on_db_dir_changed)
        self._refresh_db_watch_paths()
        self._auto_sync_timer = QTimer(self.app)
        self._auto_sync_timer.setSingleShot(True)
        self._auto_sync_timer.setInterval(60_000)
        self._auto_sync_timer.timeout.connect(self._auto_sync_triggered)
        self._periodic_pull_timer = QTimer(self.app)
        self._periodic_pull_timer.setInterval(_PERIODIC_PULL_INTERVAL_MS)
        self._periodic_pull_timer.timeout.connect(self._periodic_pull)
        if self._auto_sync_enabled:
            self._periodic_pull_timer.start()
        self._db_refresh_debounce = QTimer(self.app)
        self._db_refresh_debounce.setSingleShot(True)
        self._db_refresh_debounce.setInterval(500)
        self._db_refresh_debounce.timeout.connect(self._refresh_all)
        self._bridge_refresh_requested.connect(self._db_refresh_debounce.start)
        self._rss_restart_requested = False
        self._rss_next_log_at = 0.0
        self._rss_timer = QTimer(self.app)
        self._rss_timer.timeout.connect(self._check_memory_budget)
        self._rss_timer.start(_TRAY_RSS_CHECK_INTERVAL_MS)
        self._sync_run_active = False
        self._sync_cooldown_until = 0.0
        self._initial_auto_sync_pending = self._auto_sync_enabled
        self._pending_auto_sync_initiator = None

        self._process_recurring()
        self._maybe_schedule_initial_auto_sync()

    def _update_icon(self, summary=None):
        if summary is None:
            summary = self.db.get_summary()
        pm = create_tray_icon_pixmap(summary["overdue"])
        self.tray.setIcon(QIcon(pm))

    def _start_background_memory_audit(self):
        threading.Thread(target=self._run_background_memory_audit, daemon=True).start()

    def _run_background_memory_audit(self):
        audit_lock = getattr(self, "_background_db_write_lock", None)
        audit_lock_acquired = False
        if audit_lock is not None:
            audit_lock_acquired = audit_lock.acquire(blocking=False)
            if not audit_lock_acquired:
                logger.info("background memory audit skipped: DB writer active")
                return
        try:
            from memory_audit import maybe_run_memory_audit

            with get_conn(self.db.db_path) as conn:
                result = maybe_run_memory_audit(
                    conn,
                    runner_name="tray_background",
                    cadence_minutes=60,
                    repair=True,
                    stale_sync_minutes=120,
                    emit_event=True,
                )
            if result.get("status") not in {"skipped_due", "disabled"}:
                logger.info(
                    "background memory audit: open=%s resolved=%s next=%s",
                    result.get("open_issue_count", 0),
                    result.get("resolved_issue_count", 0),
                    result.get("scheduled_next_run_after"),
                )
        except Exception as exc:
            logger.warning("background memory audit failed: %s", exc)
        finally:
            if audit_lock is not None and audit_lock_acquired:
                audit_lock.release()

    def _run_purge(self):
        self._last_purged = self.db.purge_old_done(days=30)
        self.db.purge_old_dashboard()

    def _process_recurring(self):
        created = _run_recurring_maintenance(self.db.db_path)
        if created:
            self._refresh_all()

    def _build_ui_profile(self):
        if self.full_window is None:
            return None
        return self.full_window._build_ui_profile()

    def _on_app_sync_done(self, msg):
        is_success = msg.startswith(("Synced:", "Pulled ", "Already in sync"))
        if is_success:
            self._last_sync_at = datetime.now()
            if self.full_window is not None:
                self.full_window._last_sync_at = self._last_sync_at
            self._process_recurring()
        self._refresh_all()

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
            attachment_changes = dlg.get_attachment_changes()
            title = vals.pop("title", "")
            if title:
                self.db.add_task(
                    title,
                    attachments=attachment_changes["add_paths"],
                    **vals,
                )

    def _schedule_enrich_cache_refresh(self):
        """Run at most one enrich-cache refresh worker at a time."""
        with self._enrich_cache_thread_lock:
            if self._enrich_cache_thread_running:
                logger.debug("enrich cache refresh skipped: worker already running")
                return
            self._enrich_cache_thread_running = True

        def _work():
            try:
                self.db._refresh_enrich_cache()
            finally:
                with self._enrich_cache_thread_lock:
                    self._enrich_cache_thread_running = False

        threading.Thread(target=_work, daemon=True).start()

    def _open_full(self):
        logger.info("open_full requested")
        try:
            if self.popup:
                self.popup.hide()
            if not self.full_window:
                logger.info("open_full creating FullWindow")
                self.full_window = FullWindow(self.db, sync_host=self)
                logger.info("open_full FullWindow created")
            self._force_full_window_visible()
            QTimer.singleShot(250, self._force_full_window_visible)
        except Exception:
            logger.exception("open_full failed")
            self.tray.showMessage(
                "Task Tray",
                "Full window failed to open; see task_tray.log",
                QSystemTrayIcon.MessageIcon.Critical,
                10_000,
            )

    def _force_full_window_visible(self):
        if not self.full_window:
            return
        self.full_window.setWindowState(
            (self.full_window.windowState() & ~Qt.WindowState.WindowMinimized)
            | Qt.WindowState.WindowActive
        )
        self.full_window.showNormal()
        self.full_window.show()
        self.full_window.raise_()
        self.full_window.activateWindow()
        hwnd = int(self.full_window.winId())
        if sys.platform.startswith("win"):
            try:
                import ctypes

                user32 = ctypes.windll.user32
                user32.ShowWindow(hwnd, 9)  # SW_RESTORE
                user32.BringWindowToTop(hwnd)
                user32.SetForegroundWindow(hwnd)
            except Exception:
                logger.exception("win32 force-visible failed")
        logger.info(
            "open_full force-visible qt_visible=%s qt_state=%s win_id=%s",
            self.full_window.isVisible(),
            str(self.full_window.windowState()),
            hwnd,
        )

    def _poll_instance_socket(self):
        """Check if another instance sent a SHOW command."""
        if not self._instance_socket:
            return
        try:
            conn, _ = self._instance_socket.accept()
            data = conn.recv(64)
            conn.close()
            logger.info("instance socket received %r", data)
            if data == b"SHOW":
                self._open_full()
        except BlockingIOError:
            pass
        except OSError:
            pass
        except Exception:
            logger.exception("instance socket poll failed")

    def _refresh_all(self):
        """Update tray icon badge + tooltip after any change."""
        self._schedule_enrich_cache_refresh()
        summary = self.db.get_summary()
        self._update_icon(summary)
        self.tray.setToolTip(self._tooltip(summary))
        if self.popup and self.popup.isVisible():
            self.popup.refresh()
        if self.full_window and self.full_window.isVisible():
            self.full_window.refresh()

    def _check_reminders(self):
        """Check for tasks with due reminders and show notifications."""
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
            reminder_key = _reminder_delivery_key(tid, row["reminder_at"])
            if reminder_key in self._active_reminder_keys:
                continue
            if not _should_deliver_reminder(
                self._reminder_delivery_state,
                tid,
                row["reminder_at"],
                time.monotonic(),
            ):
                continue

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
                self._active_reminder_keys.add(reminder_key)
                self._active_reminder_dlgs.append(dlg)
                dlg.finished.connect(
                    lambda _, d=dlg: (
                        self._active_reminder_dlgs.remove(d)
                        if hasattr(self, "_active_reminder_dlgs")
                        and d in self._active_reminder_dlgs
                        else None
                    )
                )
                dlg.finished.connect(
                    lambda _, k=reminder_key: self._active_reminder_keys.discard(k)
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
        _clear_reminder_delivery_state(self._reminder_delivery_state, task_id)

    def _dismiss_reminder(self, task_id: str):
        """Clear the reminder."""
        self.db.update_task(task_id, reminder_at=None)
        _clear_reminder_delivery_state(self._reminder_delivery_state, task_id)

    def _check_memory_budget(self):
        """Log tray RSS growth and self-restart before the kernel OOM killer does."""
        rss_mb = _current_rss_mb()
        if rss_mb <= 0:
            return
        now = time.monotonic()
        if (
            _TRAY_RSS_LOG_MB
            and rss_mb >= _TRAY_RSS_LOG_MB
            and now >= getattr(self, "_rss_next_log_at", 0.0)
        ):
            logger.warning(
                "tray_rss_watchdog rss_mb=%.1f log_threshold_mb=%s exit_threshold_mb=%s",
                rss_mb,
                _TRAY_RSS_LOG_MB,
                _TRAY_RSS_EXIT_MB,
            )
            self._rss_next_log_at = now + 300.0
        if _TRAY_RSS_EXIT_MB and rss_mb >= _TRAY_RSS_EXIT_MB:
            self._restart_due_to_memory(rss_mb)

    def _restart_due_to_memory(self, rss_mb: float):
        if getattr(self, "_rss_restart_requested", False):
            return
        self._rss_restart_requested = True
        logger.error(
            "tray_rss_watchdog_restart rss_mb=%.1f exit_threshold_mb=%s",
            rss_mb,
            _TRAY_RSS_EXIT_MB,
        )
        try:
            self._enrich_timer.stop()
            self._rss_timer.stop()
            if self._audit_timer is not None:
                self._audit_timer.stop()
            self._reminder_timer.stop()
            self._purge_timer.stop()
            self._periodic_pull_timer.stop()
            self._auto_sync_timer.stop()
            self._db_refresh_debounce.stop()
            if self._instance_socket:
                self._instance_socket.close()
            self.db.search_engine.save()
            self.db.close()
        except Exception:
            logger.exception("tray memory restart cleanup failed")
        executable = sys.executable or "python3"
        script = os.path.abspath(sys.argv[0])
        argv = [executable, script, *sys.argv[1:]]
        try:
            os.execv(executable, argv)
        except OSError:
            logger.exception("tray memory restart exec failed")
            self.app.quit()

    def _on_quit(self):
        self._enrich_timer.stop()
        self._rss_timer.stop()
        if self._audit_timer is not None:
            self._audit_timer.stop()
        self._reminder_timer.stop()
        self._purge_timer.stop()
        self._periodic_pull_timer.stop()
        self._auto_sync_timer.stop()
        self._db_refresh_debounce.stop()
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
