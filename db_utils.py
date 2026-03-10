"""Shared DB connection, constants, and query helpers for sqlite-memory-mcp.

Single source of truth for task constants, DB connection setup, and common
utilities used by server.py, task_tray.py, and utility scripts.
"""

from __future__ import annotations

import logging
import os
import socket
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:
    import orjson

    def json_dumps(obj: Any, **kw) -> str:
        """Fast JSON serialize via orjson (returns str for compatibility)."""
        return orjson.dumps(obj).decode("utf-8")

    def json_loads(s: str | bytes) -> Any:
        return orjson.loads(s)

except ImportError:
    import json

    def json_dumps(obj: Any, **kw) -> str:  # type: ignore[misc]
        return json.dumps(obj, ensure_ascii=False, **kw)

    def json_loads(s: str | bytes) -> Any:  # type: ignore[misc]
        return json.loads(s)


# ── Paths ────────────────────────────────────────────────────────────────

DB_PATH = os.environ.get(
    "SQLITE_MEMORY_DB",
    os.path.expanduser("~/.claude/memory/memory.db"),
)

BRIDGE_REPO = os.environ.get(
    "BRIDGE_REPO",
    os.path.expanduser("~/.claude/memory/bridge"),
)

# ── Task constants (canonical ordering) ──────────────────────────────────

TASK_SECTIONS = ("inbox", "today", "next", "someday", "waiting")
TASK_PRIORITIES = ("low", "medium", "high", "critical")  # ascending rank
TASK_STATUSES = ("not_started", "in_progress", "done", "archived", "cancelled")
TASK_TYPES = ("task", "note")
TASK_HIDDEN_STATUSES = ("archived", "cancelled")
TASK_ACTIVE_EXCLUSIONS = ("done", "archived", "cancelled")

# v0.7.0: Public knowledge visibility
VISIBILITY_LEVELS = ("private", "pending_public", "public")
PUBLISH_STANDBY_MINUTES = 15

# Collaboration constants
TRUST_LEVELS = ("read_only", "read_write")
SHARE_TYPES = ("entity", "relation", "all")
ENTITY_ORIGINS = ("local",)  # "shared:{username}" added dynamically

# v0.9.0: Quality rating constants (HARDCODED — not configurable to prevent gaming)
VERIFICATION_OUTCOMES = ("confirmed", "contradicted", "inconclusive")
VERIFICATION_WEIGHTS = {"confirmed": 1.0, "inconclusive": 0.5, "contradicted": 0.0}

# Composite score weights (sealed)
IQ_WEIGHTS = {
    "specificity": 0.35,
    "falsifiability": 0.25,
    "internal_consistency": 0.25,
    "novelty": 0.15,
}
TIER_WEIGHTS = {"iq": 0.40, "verification": 0.35, "cross_validation": 0.25}

# Anomaly detection
RATING_BURST_THRESHOLD = 5
RATING_BURST_WINDOW_HOURS = 24

PRIORITY_RANK = {p: i for i, p in enumerate(TASK_PRIORITIES)}

PRIORITY_COLORS = {
    "critical": "#e53e3e",
    "high": "#dd6b20",
    "medium": "#2b6cb0",
    "low": "#718096",
}

TASK_ALLOWED_UPDATE_FIELDS = frozenset(
    {
        "title",
        "description",
        "status",
        "section",
        "priority",
        "due_date",
        "project",
        "parent_id",
        "notes",
        "recurring",
        "type",
        "assignee",
        "shared_by",
        "updated_at",
        "visibility",
        "publish_requested_at",
    }
)

# ── Bridge Sync v2: Per-field LWW CRDT ──────────────────────────────────

MACHINE_ID = socket.gethostname()

METADATA_FIELDS = (
    "id",
    "title",
    "status",
    "priority",
    "section",
    "due_date",
    "project",
    "parent_id",
    "recurring",
    "type",
    "assignee",
    "shared_by",
    "created_at",
    "updated_at",
)

CONTENT_FIELDS = ("description", "notes")

# Fields eligible for per-field CRDT merge (excludes id, created_at, updated_at)
MERGEABLE_FIELDS = (
    "title",
    "status",
    "priority",
    "section",
    "due_date",
    "project",
    "parent_id",
    "recurring",
    "type",
    "assignee",
    "shared_by",
    "description",
    "notes",
)

_TOMBSTONE_DAYS = 30

_log = logging.getLogger("sqlite-kb")

# ── DB connection ────────────────────────────────────────────────────────

_PRAGMAS = (
    "PRAGMA journal_mode=WAL;",
    "PRAGMA foreign_keys=ON;",
    "PRAGMA busy_timeout=10000;",
)


@contextmanager
def get_conn(db_path: str | None = None):
    """Yield a SQLite connection with PRAGMAs set, auto-commit/rollback."""
    conn = sqlite3.connect(db_path or DB_PATH, isolation_level=None, timeout=10)
    conn.row_factory = sqlite3.Row
    for pragma in _PRAGMAS:
        conn.execute(pragma)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def bulk_conn(db_path: str | None = None):
    """Connection optimized for bulk inserts: single transaction, relaxed sync.

    Use for batch imports where throughput matters more than per-row durability.
    WAL checkpoint runs automatically after the transaction commits.
    """
    conn = sqlite3.connect(db_path or DB_PATH, isolation_level=None, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=30000;")
    conn.execute("PRAGMA synchronous=NORMAL;")  # faster writes, safe with WAL
    conn.execute("PRAGMA cache_size=-64000;")  # 64MB cache for bulk ops
    conn.execute("BEGIN;")
    try:
        yield conn
        conn.execute("COMMIT;")
        conn.execute("PRAGMA wal_checkpoint(PASSIVE);")
    except Exception:
        conn.execute("ROLLBACK;")
        raise
    finally:
        conn.close()


# ── Timestamp helpers ────────────────────────────────────────────────────


def now_iso() -> str:
    """ISO 8601 UTC timestamp."""
    return datetime.now(timezone.utc).isoformat()


def parse_iso_date(s: str | None) -> date | None:
    """Parse YYYY-MM-DD to date, or None on invalid/missing input."""
    if not s:
        return None
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


def is_overdue(due_date_str: str | None) -> bool:
    """Return True if due_date_str is a valid date in the past."""
    d = parse_iso_date(due_date_str)
    return d is not None and d < date.today()


# ── SQL helpers ──────────────────────────────────────────────────────────


def build_priority_order_sql(prefix: str = "") -> str:
    """Return a CASE clause for SQL ORDER BY priority (critical first).

    Args:
        prefix: Table alias prefix, e.g. "t." for qualified column refs.
    """
    col = f"{prefix}priority"
    return (
        f"CASE {col} WHEN 'critical' THEN 0 WHEN 'high' THEN 1 "
        f"WHEN 'medium' THEN 2 WHEN 'low' THEN 3 END"
    )


def sanitize_task_enums(task: dict) -> None:
    """Clamp task enum fields to valid values in-place."""
    if task.get("status") not in TASK_STATUSES:
        task["status"] = "not_started"
    if task.get("priority") not in TASK_PRIORITIES:
        task["priority"] = "medium"
    if task.get("section") not in TASK_SECTIONS:
        task["section"] = "inbox"
    if task.get("type") not in TASK_TYPES:
        task["type"] = "task"


def priority_sort_key(task: dict[str, Any]) -> tuple:
    """Python sort key: (priority_rank ascending, due_date ascending)."""
    rank = PRIORITY_RANK.get(task.get("priority", "low"), 0)
    # Invert so critical (3) sorts first
    inv_rank = len(TASK_PRIORITIES) - 1 - rank
    parsed = parse_iso_date(task.get("due_date"))
    due = parsed.isoformat() if parsed else "9999-12-31"
    return (inv_rank, due)


# ── Bridge Sync v2: Field version tracking ───────────────────────────────


def get_field_versions(
    conn: sqlite3.Connection, task_id: str
) -> dict[str, tuple[str, str]]:
    """Get all field versions for a task as {field: (timestamp, machine_id)}."""
    rows = conn.execute(
        "SELECT field_name, updated_at, updated_by "
        "FROM task_field_versions WHERE task_id = ?",
        (task_id,),
    ).fetchall()
    return {r["field_name"]: (r["updated_at"], r["updated_by"]) for r in rows}


def upsert_field_versions(
    conn: sqlite3.Connection,
    task_id: str,
    fields: tuple | list,
    timestamp: str | None = None,
    machine_id: str | None = None,
) -> None:
    """Upsert field versions for the given fields."""
    ts = timestamp or now_iso()
    mid = machine_id or MACHINE_ID
    for field in fields:
        conn.execute(
            "INSERT OR REPLACE INTO task_field_versions "
            "(task_id, field_name, updated_at, updated_by) VALUES (?, ?, ?, ?)",
            (task_id, field, ts, mid),
        )


# ── Bridge Sync v2: Per-task file export ─────────────────────────────────

_TASK_EXPORT_COLS = (
    "id, title, description, status, priority, section, due_date, "
    "project, parent_id, notes, recurring, type, assignee, shared_by, "
    "created_at, updated_at"
)


def export_task_files(
    conn: sqlite3.Connection,
    bridge_dir: str,
    changed_since: str | None = None,
) -> list[str]:
    """Export tasks to per-task JSON files in tasks/. Returns exported IDs."""
    tasks_dir = Path(bridge_dir) / "tasks"
    tasks_dir.mkdir(exist_ok=True)

    if changed_since:
        rows = conn.execute(
            "SELECT id FROM tasks WHERE updated_at > ?", (changed_since,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT id FROM tasks").fetchall()

    exported: list[str] = []
    for row in rows:
        tid = row["id"]
        task_row = conn.execute(
            f"SELECT {_TASK_EXPORT_COLS} FROM tasks WHERE id = ?", (tid,)
        ).fetchone()
        if not task_row:
            continue
        task = dict(task_row)
        fvs = get_field_versions(conn, tid)
        task["_field_ts"] = {f: list(v) for f, v in fvs.items()}
        task_path = tasks_dir / f"{tid}.json"
        task_path.write_text(json_dumps(task), encoding="utf-8")
        exported.append(tid)
    return exported


def export_index_json(conn: sqlite3.Connection, bridge_dir: str) -> int:
    """Build index.json: metadata + field versions for all active tasks + tombstones.

    Returns count of tasks in index.
    """
    # Active tasks (no description/notes — metadata only)
    meta_cols = ", ".join(METADATA_FIELDS)
    rows = conn.execute(
        f"SELECT {meta_cols} FROM tasks "
        "WHERE status NOT IN ('archived', 'cancelled') ORDER BY created_at"
    ).fetchall()

    tasks: list[dict] = []
    for r in rows:
        entry = dict(r)
        fvs = get_field_versions(conn, r["id"])
        entry["_field_ts"] = {f: list(v) for f, v in fvs.items()}
        tasks.append(entry)

    # Tombstones: recently archived/cancelled (30 days)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=_TOMBSTONE_DAYS)).isoformat()
    tombstone_rows = conn.execute(
        f"SELECT {meta_cols} FROM tasks "
        "WHERE status IN ('archived', 'cancelled') AND updated_at > ? "
        "ORDER BY updated_at",
        (cutoff,),
    ).fetchall()
    for r in tombstone_rows:
        entry = dict(r)
        entry["_tombstone"] = True
        fvs = get_field_versions(conn, r["id"])
        entry["_field_ts"] = {f: list(v) for f, v in fvs.items()}
        tasks.append(entry)

    index = {
        "version": 4,
        "format": "bridge_v2",
        "pushed_at": now_iso(),
        "machine_id": MACHINE_ID,
        "tasks": tasks,
    }

    index_path = Path(bridge_dir) / "index.json"
    index_path.write_text(json_dumps(index), encoding="utf-8")
    return len(tasks)


# ── Bridge Sync v2: Per-field LWW CRDT merge ────────────────────────────


def _parse_field_ts(remote_fts: dict, field: str, fallback_ts: str) -> tuple[str, str]:
    """Extract (timestamp, machine_id) from _field_ts entry with fallback."""
    entry = remote_fts.get(field)
    if isinstance(entry, (list, tuple)) and len(entry) >= 2:
        return str(entry[0]), str(entry[1])
    # Backward compat: no _field_ts → task-level updated_at
    return fallback_ts, ""


def merge_import_tasks(
    conn: sqlite3.Connection,
    remote_tasks: list[dict],
    import_content: bool = False,
) -> tuple[int, int]:
    """Per-field LWW CRDT merge. Returns (new_count, updated_field_count).

    Merge rule: for each field, (timestamp, machine_id) lexicographic comparison.
    Remote wins if (remote_ts, remote_by) > (local_ts, local_by).

    import_content=False: only merge metadata fields (for index.json pull).
    import_content=True: also merge description/notes (for on-demand load).
    """
    fields_to_merge = (
        list(MERGEABLE_FIELDS)
        if import_content
        else [f for f in MERGEABLE_FIELDS if f not in CONTENT_FIELDS]
    )

    new_count = 0
    updated_fields = 0
    now = now_iso()

    # Sort parents before children to avoid FK violations
    tasks_sorted = sorted(
        remote_tasks,
        key=lambda t: (t.get("parent_id") is not None, t.get("created_at", "")),
    )

    for remote in tasks_sorted:
        tid = remote.get("id")
        if not tid:
            continue

        sanitize_task_enums(remote)
        remote_fts = remote.get("_field_ts", {})
        fallback_ts = remote.get("updated_at", "")

        # Handle tombstones — only merge status field
        if remote.get("_tombstone"):
            existing = conn.execute(
                "SELECT id FROM tasks WHERE id = ?", (tid,)
            ).fetchone()
            if existing:
                remote_ts, remote_by = _parse_field_ts(
                    remote_fts, "status", fallback_ts
                )
                local_fv = conn.execute(
                    "SELECT updated_at, updated_by FROM task_field_versions "
                    "WHERE task_id = ? AND field_name = 'status'",
                    (tid,),
                ).fetchone()
                local_ts = local_fv["updated_at"] if local_fv else ""
                local_by = local_fv["updated_by"] if local_fv else ""
                if (remote_ts, remote_by) > (local_ts, local_by):
                    conn.execute(
                        "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                        (remote["status"], remote.get("updated_at", now), tid),
                    )
                    conn.execute(
                        "INSERT OR REPLACE INTO task_field_versions "
                        "(task_id, field_name, updated_at, updated_by) "
                        "VALUES (?, ?, ?, ?)",
                        (tid, "status", remote_ts, remote_by),
                    )
                    updated_fields += 1
            continue

        # Match by UUID only — authoritative in CRDT model
        existing = conn.execute(
            "SELECT id, updated_at FROM tasks WHERE id = ?", (tid,)
        ).fetchone()

        if existing:
            local_id = existing["id"]
            local_fvs = get_field_versions(conn, local_id)

            # Per-field CRDT merge
            fields_to_update: dict[str, Any] = {}
            for field in fields_to_merge:
                if field not in remote:
                    continue
                remote_val = remote.get(field)
                remote_ts, remote_by = _parse_field_ts(remote_fts, field, fallback_ts)

                local_fv = local_fvs.get(field)
                local_ts = local_fv[0] if local_fv else ""
                local_by = local_fv[1] if local_fv else ""

                # Lexicographic: (timestamp, machine_id) — higher wins
                if (remote_ts, remote_by) > (local_ts, local_by):
                    fields_to_update[field] = remote_val
                    conn.execute(
                        "INSERT OR REPLACE INTO task_field_versions "
                        "(task_id, field_name, updated_at, updated_by) "
                        "VALUES (?, ?, ?, ?)",
                        (local_id, field, remote_ts, remote_by),
                    )
                    updated_fields += 1

            if fields_to_update:
                # Validate field names against allowlist (defense-in-depth)
                safe_fields = {
                    k: v for k, v in fields_to_update.items() if k in MERGEABLE_FIELDS
                }
                if safe_fields:
                    set_clause = ", ".join(f"{k} = ?" for k in safe_fields)
                    values = list(safe_fields.values()) + [local_id]
                    conn.execute(f"UPDATE tasks SET {set_clause} WHERE id = ?", values)
        else:
            # New task — insert (content only if import_content)
            desc = remote.get("description") if import_content else None
            notes = remote.get("notes") if import_content else None
            conn.execute(
                "INSERT OR IGNORE INTO tasks "
                "(id, title, description, status, priority, section, due_date, "
                "project, parent_id, notes, recurring, type, assignee, shared_by, "
                "created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    tid,
                    remote.get("title", ""),
                    desc,
                    remote.get("status", "not_started"),
                    remote.get("priority", "medium"),
                    remote.get("section", "inbox"),
                    remote.get("due_date"),
                    remote.get("project"),
                    remote.get("parent_id"),
                    notes,
                    remote.get("recurring"),
                    remote.get("type", "task"),
                    remote.get("assignee"),
                    remote.get("shared_by"),
                    remote.get("created_at", now),
                    remote.get("updated_at", now),
                ),
            )
            # Seed field versions from remote _field_ts
            for field in MERGEABLE_FIELDS:
                fts, fby = _parse_field_ts(remote_fts, field, fallback_ts)
                conn.execute(
                    "INSERT OR IGNORE INTO task_field_versions "
                    "(task_id, field_name, updated_at, updated_by) "
                    "VALUES (?, ?, ?, ?)",
                    (tid, field, fts, fby),
                )
            new_count += 1

    return new_count, updated_fields


# ── Bridge Sync v2: Lazy content loading ─────────────────────────────────


def load_task_content(task_id: str, bridge_dir: str) -> dict | None:
    """Lazy-load full task (notes/description) from bridge per-task file."""
    task_file = Path(bridge_dir) / "tasks" / f"{task_id}.json"
    if not task_file.exists():
        return None
    try:
        return json_loads(task_file.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None


# ── Bridge Sync v2: Migration helper ─────────────────────────────────────


def migrate_to_per_task_files(bridge_dir: str) -> bool:
    """One-time migration: split shared.json into per-task files."""
    tasks_dir = Path(bridge_dir) / "tasks"
    if tasks_dir.exists() and any(tasks_dir.glob("*.json")):
        return False  # Already migrated

    shared_path = Path(bridge_dir) / "shared.json"
    if not shared_path.exists():
        return False

    try:
        data = json_loads(shared_path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return False

    tasks = data.get("tasks", [])
    if not tasks:
        return False

    tasks_dir.mkdir(exist_ok=True)
    count = 0
    for task in tasks:
        tid = task.get("id")
        if not tid:
            continue
        if "_field_ts" not in task:
            task["_field_ts"] = {}
        (tasks_dir / f"{tid}.json").write_text(json_dumps(task), encoding="utf-8")
        count += 1

    _log.info("Migrated %d tasks from shared.json to per-task files", count)
    return True
