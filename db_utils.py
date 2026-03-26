"""Shared DB connection, constants, and query helpers for sqlite-memory-mcp.

Single source of truth for task constants, DB connection setup, and common
utilities used by server.py, task_tray.py, and utility scripts.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import logging
import os
import re
import socket
import sqlite3
import subprocess
import sys
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# Suppress console windows on Windows when spawning git/gh from GUI
_NOWIN: dict = (
    {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
)

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
        "reminder_at",
        "type",
        "assignee",
        "shared_by",
        "updated_at",
        "visibility",
        "publish_requested_at",
    }
)

# ── Recurring task validation ─────────────────────────────────────────
RECURRING_EVERY = ("day", "week", "month", "year")
RECURRING_WEEKDAYS = frozenset(
    ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
)


def validate_recurring(raw: str) -> str | None:
    """Validate recurring JSON config. Returns error message or None if valid."""
    try:
        config = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return f"Invalid JSON: {raw!r}"
    if not isinstance(config, dict):
        return "Recurring config must be a JSON object"
    every = config.get("every", "").lower()
    if every not in RECURRING_EVERY:
        return f"Invalid 'every': {every}. Use: {RECURRING_EVERY}"
    # Optional interval (default 1)
    interval = config.get("interval")
    if interval is not None:
        try:
            iv = int(interval)
            if iv < 1:
                return f"'interval' must be >= 1. Got: {iv}"
        except (ValueError, TypeError):
            return f"'interval' must be an integer. Got: {interval!r}"
    if every == "week":
        day = config.get("day", "").lower()
        if day not in RECURRING_WEEKDAYS:
            return f"Weekly recurrence requires 'day' (weekday name). Got: {day!r}"
    if every == "month":
        day = config.get("day")
        if day is None:
            return "Monthly recurrence requires 'day' (1-31)"
        try:
            d = int(day)
            if not 1 <= d <= 31:
                return f"Monthly 'day' must be 1-31. Got: {d}"
        except (ValueError, TypeError):
            return f"Monthly 'day' must be an integer. Got: {day!r}"
    if every == "year":
        month = config.get("month")
        if month is not None:
            try:
                m = int(month)
                if not 1 <= m <= 12:
                    return f"Yearly 'month' must be 1-12. Got: {m}"
            except (ValueError, TypeError):
                return f"Yearly 'month' must be an integer. Got: {month!r}"
        day = config.get("day")
        if day is not None:
            try:
                d = int(day)
                if not 1 <= d <= 31:
                    return f"Yearly 'day' must be 1-31. Got: {d}"
            except (ValueError, TypeError):
                return f"Yearly 'day' must be an integer. Got: {day!r}"
    return None


def validate_task_fields(**kwargs: str | None) -> str | None:
    """Validate task enum/date/recurring fields. Returns error message or None.

    Pass only the fields that need validation. None values are skipped.
    Works for both create_task (pass all fields) and update_task (pass only changed fields).
    """
    section = kwargs.get("section")
    if section is not None and section not in TASK_SECTIONS:
        return f"Invalid section: {section}. Use: {TASK_SECTIONS}"
    priority = kwargs.get("priority")
    if priority is not None and priority not in TASK_PRIORITIES:
        return f"Invalid priority: {priority}. Use: {TASK_PRIORITIES}"
    status = kwargs.get("status")
    if status is not None and status not in TASK_STATUSES:
        return f"Invalid status: {status}. Use: {TASK_STATUSES}"
    type_ = kwargs.get("type")
    if type_ is not None and type_ not in TASK_TYPES:
        return f"Invalid type: {type_}. Use: {TASK_TYPES}"
    due_date = kwargs.get("due_date")
    if due_date:
        try:
            datetime.strptime(due_date, "%Y-%m-%d")
        except ValueError:
            return f"Invalid due_date: {due_date}. Use YYYY-MM-DD format"
    recurring = kwargs.get("recurring")
    if recurring:
        err = validate_recurring(recurring)
        if err:
            return f"Invalid recurring config: {err}"
    reminder_at = kwargs.get("reminder_at")
    if reminder_at:
        try:
            datetime.fromisoformat(reminder_at)
        except ValueError:
            return f"Invalid reminder_at: {reminder_at}. Use ISO datetime format"
    return None


# ── Bridge Sync v2: Per-field LWW conflict resolver ─────────────────────

MACHINE_ID = socket.gethostname()
_write_counter = itertools.count(1)  # atomic in CPython; no lock needed


def _next_machine_id() -> str:
    """Return machine_id with monotonic counter suffix for deterministic ordering.

    Two writes in the same microsecond on the same machine get different IDs:
    DESKTOP:0001, DESKTOP:0002 → lexicographic comparison picks the later write.
    """
    return f"{MACHINE_ID}:{next(_write_counter):06d}"


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
    "reminder_at",
    "type",
    "assignee",
    "shared_by",
    "visibility",
    "publish_requested_at",
    "created_at",
    "updated_at",
)

CONTENT_FIELDS = ("description", "notes")

CONTENT_SHRINK_GUARD_MIN_CHARS = 1000
CONTENT_SHRINK_GUARD_RATIO = 0.5

# Fields that enrichment pipelines must NEVER modify — only user/bridge can write these.
# Invariant: enrichment = add new records (facts, claims, chunks), never modify existing content.
ENRICHMENT_PROTECTED_FIELDS = frozenset({"title", "description", "notes"})


def content_length(value: Any) -> int:
    """Return trimmed length for text-like content, else 0."""
    if value is None:
        return 0
    return len(str(value).strip())


def has_meaningful_content(value: Any) -> bool:
    """True when a content field has non-whitespace text."""
    return content_length(value) > 0


def is_suspicious_content_shrink(
    old_value: Any,
    new_value: Any,
    *,
    min_chars: int = CONTENT_SHRINK_GUARD_MIN_CHARS,
    ratio: float = CONTENT_SHRINK_GUARD_RATIO,
) -> bool:
    """Flag likely data loss when substantial content collapses to a much shorter value."""
    old_len = content_length(old_value)
    if old_len < min_chars:
        return False
    return content_length(new_value) < (old_len * ratio)


def assert_enrichment_safe(fields: dict[str, Any] | set[str] | list[str]) -> None:
    """Raise ValueError if any enrichment-protected field is in the update set.

    Call this from any enrichment code path that touches tasks.
    """
    keys = set(fields) if isinstance(fields, dict) else set(fields)
    violation = keys & ENRICHMENT_PROTECTED_FIELDS
    if violation:
        raise ValueError(
            f"Enrichment invariant violation: cannot modify protected fields {sorted(violation)}. "
            "Enrichment must be additive-only (new facts/claims/chunks)."
        )


# Fields eligible for per-field LWW merge (excludes id, created_at, updated_at)
MERGEABLE_FIELDS = (
    "title",
    "status",
    "priority",
    "section",
    "due_date",
    "project",
    "parent_id",
    "recurring",
    "reminder_at",
    "type",
    "assignee",
    "shared_by",
    "visibility",
    "publish_requested_at",
    "description",
    "notes",
)

_TOMBSTONE_DAYS = 30

# Path traversal defense: task IDs must be UUID-safe (alphanumeric + hyphens)
_SAFE_TASK_ID = re.compile(r"^[a-zA-Z0-9\-]+$")
_SAFE_ENTITY_ID = re.compile(r"^[1-9][0-9]*$")


def setup_logger(name: str, log_file: str = "server.log") -> logging.Logger:
    """Configure file logger. Idempotent — safe to call multiple times."""
    log_path = Path.home() / ".claude" / "memory" / log_file
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    if not logger.handlers:
        fh = logging.FileHandler(log_path, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(fh)
    return logger


_log = logging.getLogger("sqlite-kb")

# ── DB connection ────────────────────────────────────────────────────────

_PRAGMAS = (
    "PRAGMA journal_mode=WAL;",
    "PRAGMA foreign_keys=ON;",
    "PRAGMA busy_timeout=10000;",
    "PRAGMA wal_autocheckpoint=100;",
)


_BUSY_RETRIES = 3
_BUSY_BASE_DELAY = 0.5  # seconds, doubles each retry


@contextmanager
def get_conn(db_path: str | None = None):
    """Yield a SQLite connection with PRAGMAs set, auto-commit/rollback.

    Uses explicit BEGIN/COMMIT to ensure each context-manager block is atomic.
    Retries BEGIN up to 3× on SQLITE_BUSY (exponential backoff on top of busy_timeout).
    """
    import time as _time

    # Retry connection + BEGIN on SQLITE_BUSY (lock contention with tray/bridge)
    conn = None
    for attempt in range(_BUSY_RETRIES):
        conn = sqlite3.connect(db_path or DB_PATH, isolation_level=None, timeout=10)
        conn.row_factory = sqlite3.Row
        for pragma in _PRAGMAS:
            conn.execute(pragma)
        try:
            conn.execute("BEGIN;")
            break  # transaction started successfully
        except sqlite3.OperationalError as e:
            conn.close()
            conn = None
            if "locked" in str(e).lower() and attempt < _BUSY_RETRIES - 1:
                _time.sleep(_BUSY_BASE_DELAY * (2**attempt))
                continue
            raise

    # Yield exactly once, outside the retry loop
    try:
        yield conn
        conn.execute("COMMIT;")
    except Exception:
        try:
            conn.execute("ROLLBACK;")
        except Exception:
            pass  # ROLLBACK failed — original exception is more important
        raise
    finally:
        if conn is not None:
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
        try:
            conn.execute("ROLLBACK;")
        except Exception:
            pass
        raise
    finally:
        conn.close()


# ── Git helpers ──────────────────────────────────────────────────────────

log = logging.getLogger(__name__)


def git_run(
    repo_dir: str, *args: str, timeout: int = 30
) -> subprocess.CompletedProcess:
    """Run git command in specified repo directory."""
    return subprocess.run(
        ["git", *args],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        timeout=timeout,
        **_NOWIN,
    )


def git_retry(
    repo_dir: str, *args: str, max_retries: int = 3, timeout: int = 30
) -> subprocess.CompletedProcess:
    """Git command with exponential backoff retry."""
    import time

    delays = [2, 4, 8]
    last_result = None
    for attempt in range(max_retries):
        last_result = git_run(repo_dir, *args, timeout=timeout)
        if last_result.returncode == 0:
            return last_result
        if attempt < max_retries - 1:
            log.warning(
                "git %s attempt %d/%d failed: %s — retrying in %ds",
                " ".join(args),
                attempt + 1,
                max_retries,
                last_result.stderr.strip(),
                delays[attempt],
            )
            time.sleep(delays[attempt])
    return last_result


def source_hash(name: str, entity_type: str, observations: list) -> str:
    """SHA256 hash for entity deduplication."""
    raw = json.dumps({"n": name, "t": entity_type, "o": observations}, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()


# ── Timestamp helpers ────────────────────────────────────────────────────


def now_iso() -> str:
    """ISO 8601 UTC timestamp."""
    return datetime.now(timezone.utc).isoformat()


def parse_iso_datetime_for_compare(value: str | None) -> datetime:
    """Parse ISO datetime defensively for ordering comparisons.

    Accepts `Z`, offset-aware, and naive timestamps. Invalid or missing values sort
    as the minimum UTC datetime.
    """
    raw = (value or "").strip()
    if not raw:
        return datetime.min.replace(tzinfo=timezone.utc)
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


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


# ── Entity helpers ────────────────────────────────────────────────────────


def get_entity_id(conn: sqlite3.Connection, name: str) -> int | None:
    """Look up entity ID by name. Returns None if not found."""
    row = conn.execute("SELECT id FROM entities WHERE name = ?", (name,)).fetchone()
    return row["id"] if row else None


def serialize_entity(
    conn: sqlite3.Connection, entity_row, *, include_timestamps: bool = False
) -> dict:
    """Serialize an entity row with its observations for export.

    Args:
        conn: DB connection.
        entity_row: Row from entities table (needs id, name, entity_type; optionally project, created_at, updated_at).
        include_timestamps: If True, observations include createdAt and entity includes createdAt/updatedAt.
    """
    entity_row = dict(entity_row)
    eid = entity_row["id"]
    if include_timestamps:
        obs = conn.execute(
            "SELECT content, created_at FROM observations WHERE entity_id = ? ORDER BY id",
            (eid,),
        ).fetchall()
        entity: dict = {
            "name": entity_row["name"],
            "entityType": entity_row["entity_type"],
            "observations": [
                {"content": o["content"], "createdAt": o["created_at"]} for o in obs
            ],
        }
        if entity_row.get("created_at"):
            entity["createdAt"] = entity_row["created_at"]
        if entity_row.get("updated_at"):
            entity["updatedAt"] = entity_row["updated_at"]
    else:
        obs = conn.execute(
            "SELECT content FROM observations WHERE entity_id = ? ORDER BY id",
            (eid,),
        ).fetchall()
        entity = {
            "name": entity_row["name"],
            "entityType": entity_row["entity_type"],
            "observations": [o["content"] for o in obs],
        }
    if entity_row.get("project"):
        entity["project"] = entity_row["project"]
    return entity


def export_relations(
    conn: sqlite3.Connection,
    entity_ids: set | list | None = None,
    *,
    include_timestamps: bool = False,
) -> list[dict]:
    """Export relations as list of dicts, optionally filtered by entity ID set."""
    base = (
        "SELECT ef.name AS from_name, et.name AS to_name, "
        "r.relation_type, r.created_at FROM relations r "
        "JOIN entities ef ON r.from_id = ef.id "
        "JOIN entities et ON r.to_id = et.id"
    )
    if entity_ids is not None:
        if not entity_ids:
            return []
        ids = list(entity_ids)
        ph = ",".join("?" * len(ids))
        rows = conn.execute(
            f"{base} WHERE r.from_id IN ({ph}) AND r.to_id IN ({ph})",
            ids + ids,
        ).fetchall()
    else:
        rows = conn.execute(f"{base} ORDER BY ef.name, et.name").fetchall()
    out = []
    for r in rows:
        d: dict = {
            "from": r["from_name"],
            "to": r["to_name"],
            "relationType": r["relation_type"],
        }
        if include_timestamps:
            d["createdAt"] = r["created_at"]
        out.append(d)
    return out


# ── Text helpers ──────────────────────────────────────────────────────────

STOPWORDS = frozenset(
    "the a an is are was were be been being have has had do does did "
    "will would shall should may might can could and or but if then "
    "else for of in on at to from by with".split()
)


def tokenize_for_similarity(text: str) -> set[str]:
    """Extract meaningful tokens from text for Jaccard similarity."""
    if not text:
        return set()
    words = re.findall(r"\w+", text.lower())
    return {w for w in words if len(w) >= 3 and w not in STOPWORDS}


def fts_query(raw: str) -> str:
    """Sanitize a user query for FTS5 MATCH.

    Wraps each token in double quotes to avoid FTS5 syntax errors
    from special characters, then joins with OR for broad matching.
    """
    tokens = raw.split()
    if not tokens:
        return '""'
    escaped = ['"' + t.replace('"', '""') + '"' for t in tokens]
    return " OR ".join(escaped)


# ── Task DAO ──────────────────────────────────────────────────────────────


class TaskDAO:
    """Data Access Object for tasks table. All raw SQL lives here.

    All methods are static and take a sqlite3.Connection as first argument,
    so they work with both get_conn() (server.py) and persistent connections
    (task_tray.py).
    """

    # ── All task columns for full SELECT ──
    ALL_COLS = (
        "id, title, description, status, priority, section, due_date, "
        "project, parent_id, notes, recurring, reminder_at, type, assignee, "
        "shared_by, visibility, publish_requested_at, created_at, updated_at"
    )

    @staticmethod
    def get_by_id(
        conn: sqlite3.Connection, task_id: str, columns: str | None = None
    ) -> dict | None:
        """Fetch a single task by ID. Returns dict or None."""
        cols = columns or TaskDAO.ALL_COLS
        row = conn.execute(
            f"SELECT {cols} FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        return dict(row) if row else None

    @staticmethod
    def exists(conn: sqlite3.Connection, task_id: str) -> bool:
        """Check if a task exists by ID."""
        return (
            conn.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone()
            is not None
        )

    @staticmethod
    def create(
        conn: sqlite3.Connection,
        task_id: str,
        title: str,
        now: str,
        *,
        description: str | None = None,
        status: str = "not_started",
        priority: str = "medium",
        section: str = "inbox",
        due_date: str | None = None,
        project: str | None = None,
        parent_id: str | None = None,
        notes: str | None = None,
        recurring: str | None = None,
        reminder_at: str | None = None,
        type: str = "task",
        assignee: str | None = None,
        shared_by: str | None = None,
        visibility: str = "private",
        publish_requested_at: str | None = None,
        created_at: str | None = None,
    ) -> None:
        """Insert a new task. Caller must also call upsert_field_versions."""
        conn.execute(
            "INSERT INTO tasks "
            "(id, title, description, status, priority, section, due_date, "
            "project, parent_id, notes, recurring, reminder_at, type, assignee, "
            "shared_by, visibility, publish_requested_at, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                task_id,
                title,
                description,
                status,
                priority,
                section,
                due_date,
                project,
                parent_id,
                notes,
                recurring,
                reminder_at,
                type,
                assignee,
                shared_by,
                visibility,
                publish_requested_at,
                created_at or now,
                now,
            ),
        )

    ALLOWED_UPDATE_COLUMNS: frozenset = TASK_ALLOWED_UPDATE_FIELDS

    @staticmethod
    def update(conn: sqlite3.Connection, task_id: str, fields: dict[str, Any]) -> int:
        """Update arbitrary fields on a task. Returns rowcount.

        Caller must set updated_at in fields and call upsert_field_versions.
        """
        if not fields:
            return 0
        unknown = set(fields) - TaskDAO.ALLOWED_UPDATE_COLUMNS
        if unknown:
            raise ValueError(f"Unknown task columns: {sorted(unknown)}")
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [task_id]
        cur = conn.execute(f"UPDATE tasks SET {set_clause} WHERE id = ?", values)
        return cur.rowcount

    @staticmethod
    def delete(conn: sqlite3.Connection, task_id: str) -> int:
        """Hard-delete a task. Returns rowcount. Prefer soft-delete (status=cancelled)."""
        return conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,)).rowcount

    @staticmethod
    def get_active(
        conn: sqlite3.Connection,
        columns: str | None = None,
        order_by: str = "created_at",
    ) -> list[dict]:
        """Return all active tasks (excludes done, archived, cancelled)."""
        cols = columns or TaskDAO.ALL_COLS
        exclusions = ",".join("?" * len(TASK_ACTIVE_EXCLUSIONS))
        rows = conn.execute(
            f"SELECT {cols} FROM tasks WHERE status NOT IN ({exclusions}) "
            f"ORDER BY {order_by}",
            list(TASK_ACTIVE_EXCLUSIONS),
        ).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def search(conn: sqlite3.Connection, query: str, limit: int = 50) -> list[dict]:
        """FTS5 search across tasks. Returns matching tasks ranked by relevance."""
        if not query or not query.strip():
            return []
        rows = conn.execute(
            "SELECT t.id, t.title, t.description, t.notes, t.status, t.priority, "
            "t.section, t.due_date, t.project, t.parent_id, t.type, t.updated_at, "
            "rank "
            "FROM tasks_fts JOIN tasks t ON tasks_fts.rowid = t.rowid "
            "WHERE tasks_fts MATCH ? "
            "ORDER BY rank LIMIT ?",
            (query, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def count_active(conn: sqlite3.Connection) -> int:
        """Count active (non-archived, non-cancelled) tasks."""
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM tasks "
            "WHERE status NOT IN ('archived', 'cancelled')"
        ).fetchone()
        return row["cnt"] if row else 0

    @staticmethod
    def count_by_visibility(conn: sqlite3.Connection, visibility: str) -> int:
        """Count tasks by visibility level."""
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM tasks WHERE visibility = ?",
            (visibility,),
        ).fetchone()
        return row["cnt"] if row else 0

    @staticmethod
    def archive_done(conn: sqlite3.Connection, older_than_days: int) -> list[str]:
        """Archive done tasks older than N days. Returns archived task IDs."""
        rows = conn.execute(
            "SELECT id FROM tasks WHERE status = 'done' AND type = 'task' "
            "AND updated_at < datetime('now', ?)",
            (f"-{older_than_days} days",),
        ).fetchall()
        ids = [r["id"] for r in rows]
        if ids:
            now = now_iso()
            conn.execute(
                "UPDATE tasks SET status = 'archived', updated_at = ? "
                "WHERE status = 'done' AND type = 'task' "
                "AND updated_at < datetime('now', ?)",
                (now, f"-{older_than_days} days"),
            )
        return ids

    @staticmethod
    def promote_pending_public(conn: sqlite3.Connection, cutoff_ts: str) -> int:
        """Promote pending_public tasks to public. Returns count promoted."""
        cur = conn.execute(
            "UPDATE tasks SET visibility = 'public' "
            "WHERE visibility = 'pending_public' AND publish_requested_at <= ?",
            (cutoff_ts,),
        )
        return cur.rowcount

    # ── Task-Entity Link operations ──

    @staticmethod
    def link_entity(
        conn: sqlite3.Connection,
        task_id: str,
        entity_id: int,
        link_type: str = "manual",
        score: float | None = None,
        created_at: str | None = None,
    ) -> None:
        """Create or update a task↔entity link."""
        ts = created_at or now_iso()
        conn.execute(
            "INSERT INTO task_entity_links "
            "(task_id, entity_id, link_type, score, created_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(task_id, entity_id) DO UPDATE SET "
            "link_type = excluded.link_type, score = excluded.score, "
            "created_at = excluded.created_at",
            (task_id, entity_id, link_type, score, ts),
        )

    @staticmethod
    def unlink_entity(conn: sqlite3.Connection, task_id: str, entity_id: int) -> int:
        """Remove a task↔entity link. Returns rowcount."""
        return conn.execute(
            "DELETE FROM task_entity_links WHERE task_id = ? AND entity_id = ?",
            (task_id, entity_id),
        ).rowcount

    @staticmethod
    def get_task_links(conn: sqlite3.Connection, task_id: str) -> list[dict]:
        """Get all entities linked to a task."""
        rows = conn.execute(
            "SELECT e.id AS entity_id, e.name AS entity_name, e.entity_type, "
            "tel.link_type, tel.score, tel.created_at "
            "FROM task_entity_links tel "
            "JOIN entities e ON e.id = tel.entity_id "
            "WHERE tel.task_id = ?",
            (task_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def get_entity_tasks(conn: sqlite3.Connection, entity_id: int) -> list[dict]:
        """Get all tasks linked to an entity."""
        rows = conn.execute(
            "SELECT t.id, t.title, t.status, t.priority, t.section, "
            "tel.link_type, tel.score, tel.created_at AS linked_at "
            "FROM task_entity_links tel "
            "JOIN tasks t ON t.id = tel.task_id "
            "WHERE tel.entity_id = ?",
            (entity_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def get_linked_entity_ids(conn: sqlite3.Connection, task_id: str) -> set[int]:
        """Get set of entity IDs already linked to a task."""
        rows = conn.execute(
            "SELECT entity_id FROM task_entity_links WHERE task_id = ?",
            (task_id,),
        ).fetchall()
        return {r["entity_id"] for r in rows}

    # ── UI-oriented queries (centralized from TaskDB) ──

    @staticmethod
    def get_done(conn: sqlite3.Connection, columns: str | None = None) -> list[dict]:
        """Return completed tasks, newest first."""
        cols = columns or TaskDAO.ALL_COLS
        rows = conn.execute(
            f"SELECT {cols} FROM tasks WHERE status = 'done' ORDER BY updated_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def purge_done(conn: sqlite3.Connection, cutoff_iso: str) -> int:
        """Delete done tasks older than cutoff. Returns count deleted."""
        cur = conn.execute(
            "DELETE FROM tasks WHERE status = 'done' AND type = 'task' "
            "AND updated_at < ?",
            (cutoff_iso,),
        )
        return cur.rowcount

    @staticmethod
    def get_suggested(conn: sqlite3.Connection, limit: int = 20) -> list[dict]:
        """Return prioritized mix: overdue + high/critical + nearest due."""
        pri_sql = build_priority_order_sql()
        exclusions = ",".join("?" * len(TASK_ACTIVE_EXCLUSIONS))
        rows = conn.execute(
            f"SELECT {TaskDAO.ALL_COLS} FROM tasks "
            f"WHERE status NOT IN ({exclusions}) "
            "ORDER BY "
            "CASE WHEN due_date IS NOT NULL AND due_date < date('now') THEN 0 ELSE 1 END, "
            f"{pri_sql}, "
            "CASE WHEN due_date IS NULL THEN 1 ELSE 0 END, due_date, "
            "created_at DESC "
            "LIMIT ?",
            list(TASK_ACTIVE_EXCLUSIONS) + [limit],
        ).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def get_notes(conn: sqlite3.Connection) -> list[dict]:
        """All notes (type='note'), excluding archived/cancelled."""
        pri_sql = build_priority_order_sql()
        rows = conn.execute(
            f"SELECT {TaskDAO.ALL_COLS} FROM tasks WHERE type = 'note' "
            "AND status NOT IN ('archived', 'cancelled') "
            f"ORDER BY {pri_sql}, updated_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def get_project_names(conn: sqlite3.Connection) -> list[str]:
        """Return project names sorted by active task count (most first)."""
        rows = conn.execute(
            "SELECT project, COUNT(*) as cnt FROM tasks "
            "WHERE project IS NOT NULL AND status NOT IN ('archived','cancelled') "
            "GROUP BY project ORDER BY cnt DESC"
        ).fetchall()
        return [r["project"] for r in rows]

    @staticmethod
    def promote_due_today(conn: sqlite3.Connection) -> int:
        """Auto-move tasks with due_date <= today from inbox/next to today."""
        cur = conn.execute(
            "UPDATE tasks SET section = 'today' "
            "WHERE due_date <= date('now') AND section IN ('inbox', 'next') "
            "AND status <> 'done' AND type = 'task'"
        )
        return cur.rowcount


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
    old_values: dict[str, Any] | None = None,
    new_values: dict[str, Any] | None = None,
) -> None:
    """Upsert field versions for the given fields.

    Args:
        old_values: {field_name: previous_value} — truncated to 500 chars.
        new_values: {field_name: new_value} — truncated to 500 chars.
    """
    ts = timestamp or now_iso()
    mid = machine_id or _next_machine_id()
    _old = old_values or {}
    _new = new_values or {}
    for field in fields:
        ov = _old.get(field)
        nv = _new.get(field)
        # Truncate to 500 chars to avoid bloat
        ov_str = str(ov)[:500] if ov is not None else None
        nv_str = str(nv)[:500] if nv is not None else None
        conn.execute(
            "INSERT OR REPLACE INTO task_field_versions "
            "(task_id, field_name, updated_at, updated_by, old_value, new_value) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (task_id, field, ts, mid, ov_str, nv_str),
        )


# ── Bridge Sync v2: Per-task file export ─────────────────────────────────

TASK_EXPORT_COLS = (
    "id, title, description, status, priority, section, due_date, "
    "project, parent_id, notes, recurring, reminder_at, type, assignee, shared_by, "
    "visibility, publish_requested_at, created_at, updated_at"
)

ENTITY_EXPORT_COLS = (
    "id, name, entity_type, project, visibility, created_at, updated_at"
)


def export_task_files(
    conn: sqlite3.Connection,
    bridge_dir: str,
    changed_since: str | None = None,
) -> list[str]:
    """Export tasks to per-task JSON files in tasks/. Returns exported IDs."""
    tasks_dir = Path(bridge_dir) / "tasks"
    tasks_dir.mkdir(exist_ok=True)

    status_filter = "AND status NOT IN ('archived', 'cancelled')"
    if changed_since:
        rows = conn.execute(
            f"SELECT {TASK_EXPORT_COLS} FROM tasks WHERE updated_at >= ? {status_filter}",
            (changed_since,),
        ).fetchall()
    else:
        rows = conn.execute(
            f"SELECT {TASK_EXPORT_COLS} FROM tasks WHERE 1=1 {status_filter}"
        ).fetchall()

    exported: list[str] = []
    # Build task map from already-fetched rows (no second query needed)
    task_ids = []
    task_map: dict[str, dict] = {}
    for row in rows:
        tid = row["id"]
        if _SAFE_TASK_ID.match(tid):
            task_ids.append(tid)
            task_map[tid] = dict(row)
    if not task_ids:
        return exported

    # Batch fetch all field versions in one query
    ph = ",".join("?" * len(task_ids))
    fv_rows = conn.execute(
        "SELECT task_id, field_name, updated_at, updated_by "
        "FROM task_field_versions WHERE task_id IN ({})".format(ph),
        task_ids,
    ).fetchall()
    fv_map: dict[str, dict] = {}
    for fvr in fv_rows:
        fv_map.setdefault(fvr["task_id"], {})[fvr["field_name"]] = [
            fvr["updated_at"],
            fvr["updated_by"],
        ]

    # Batch fetch task-entity links
    link_rows = conn.execute(
        "SELECT tel.task_id, tel.entity_id, e.name, tel.link_type, "
        "tel.score, tel.created_at "
        "FROM task_entity_links tel JOIN entities e ON e.id = tel.entity_id "
        "WHERE tel.task_id IN ({})".format(ph),
        task_ids,
    ).fetchall()
    link_map: dict[str, list[dict]] = {}
    for lr in link_rows:
        link_map.setdefault(lr["task_id"], []).append(
            {
                "name": lr["name"],
                "link_type": lr["link_type"],
                "score": lr["score"],
                "created_at": lr["created_at"],
            }
        )

    for tid in task_ids:
        task = task_map[tid]
        task["_field_ts"] = fv_map.get(tid, {})
        task["_links"] = link_map.get(tid, [])
        task_path = tasks_dir / f"{tid}.json"

        # Content-aware export: preserve bridge descriptions/notes if local is NULL
        if task_path.exists():
            try:
                existing = json_loads(task_path.read_text(encoding="utf-8"))
                for content_field in CONTENT_FIELDS:
                    if not task.get(content_field) and existing.get(content_field):
                        task[content_field] = existing[content_field]
            except (ValueError, OSError):
                pass

        task_path.write_text(json_dumps(task), encoding="utf-8")
        exported.append(tid)

    # Clean stale files only during full export (changed_since=None).
    # During incremental export, task_ids is partial — cleanup would delete valid files.
    if not changed_since:
        active_ids = set(task_ids)
        for stale in tasks_dir.iterdir():
            if stale.suffix == ".json" and stale.stem not in active_ids:
                stale.unlink()

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

    # Tombstones: recently archived/cancelled (30 days)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=_TOMBSTONE_DAYS)).isoformat()
    tombstone_rows = conn.execute(
        f"SELECT {meta_cols} FROM tasks "
        "WHERE status IN ('archived', 'cancelled') AND updated_at > ? "
        "ORDER BY updated_at",
        (cutoff,),
    ).fetchall()

    # Batch-fetch field versions for all tasks + tombstones (avoid N+1)
    all_ids = [r["id"] for r in rows] + [r["id"] for r in tombstone_rows]
    fv_map: dict[str, dict[str, list]] = {}
    if all_ids:
        ph = ",".join("?" * len(all_ids))
        fv_rows = conn.execute(
            f"SELECT task_id, field_name, updated_at, updated_by "
            f"FROM task_field_versions WHERE task_id IN ({ph})",
            all_ids,
        ).fetchall()
        for fvr in fv_rows:
            fv_map.setdefault(fvr["task_id"], {})[fvr["field_name"]] = [
                fvr["updated_at"],
                fvr["updated_by"],
            ]

    tasks: list[dict] = []
    for r in rows:
        entry = dict(r)
        entry["_field_ts"] = fv_map.get(r["id"], {})
        tasks.append(entry)
    for r in tombstone_rows:
        entry = dict(r)
        entry["_tombstone"] = True
        entry["_field_ts"] = fv_map.get(r["id"], {})
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


# ── Bridge Sync v2: Per-field LWW merge ──────────────────────────────────


def _parse_field_ts(remote_fts: dict, field: str, fallback_ts: str) -> tuple[str, str]:
    """Extract (timestamp, machine_id) from _field_ts entry with fallback."""
    entry = remote_fts.get(field)
    if isinstance(entry, (list, tuple)) and len(entry) >= 2:
        return str(entry[0]), str(entry[1])
    # Backward compat: no _field_ts → task-level updated_at.
    # Use MACHINE_ID (not "") so old peers don't systematically lose ties.
    return fallback_ts, MACHINE_ID


def merge_import_tasks(
    conn: sqlite3.Connection,
    remote_tasks: list[dict],
    import_content: bool = False,
) -> tuple[int, int]:
    """Per-field LWW (Last-Write-Wins) merge. Returns (new_count, updated_field_count).

    NOT a true CRDT — uses timestamp-based conflict resolution without vector clocks.
    Works well for single-user multi-machine sync. May lose updates on clock skew >1s.

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
    _clock_skew_warned = False  # only log once per merge batch

    # Sort parents before children to avoid FK violations
    tasks_sorted = sorted(
        remote_tasks,
        key=lambda t: (t.get("parent_id") is not None, t.get("created_at", "")),
    )

    for remote in tasks_sorted:
        tid = remote.get("id")
        if not tid or not _SAFE_TASK_ID.match(tid):
            continue

        sanitize_task_enums(remote)
        remote_fts = remote.get("_field_ts", {})
        fallback_ts = remote.get("updated_at", "")

        # Clock skew detection: warn if remote timestamp is >5s ahead of local
        if not _clock_skew_warned and fallback_ts > now:
            try:
                delta = (
                    datetime.fromisoformat(fallback_ts) - datetime.fromisoformat(now)
                ).total_seconds()
                if delta > 5:
                    _log.warning(
                        "Clock skew detected: remote is %.1fs ahead (task %s). "
                        "LWW merge may produce unexpected results.",
                        delta,
                        tid,
                    )
                    _clock_skew_warned = True
            except (ValueError, TypeError):
                pass

        # Handle tombstones — only merge status field
        if remote.get("_tombstone"):
            existing = conn.execute(
                "SELECT id, updated_at FROM tasks WHERE id = ?", (tid,)
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

                tombstone_wins = (remote_ts, remote_by) > (local_ts, local_by)

                # Fallback: if field timestamps are equal, tombstone wins
                # when its updated_at is newer (archival may not update field_versions)
                if not tombstone_wins and remote_ts == local_ts:
                    remote_updated = remote.get("updated_at", "")
                    local_updated = existing["updated_at"] or ""
                    if remote_updated > local_updated:
                        tombstone_wins = True
                        _log.info(
                            "Tombstone fallback: %s wins via updated_at (%s > %s)",
                            tid[:12],
                            remote_updated[:19],
                            local_updated[:19],
                        )

                if tombstone_wins:
                    conn.execute(
                        "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                        (remote["status"], now, tid),
                    )
                    conn.execute(
                        "INSERT OR REPLACE INTO task_field_versions "
                        "(task_id, field_name, updated_at, updated_by) "
                        "VALUES (?, ?, ?, ?)",
                        (
                            tid,
                            "status",
                            remote_ts or fallback_ts,
                            remote_by or _MACHINE_ID,
                        ),
                    )
                    updated_fields += 1
            continue

        # Match by UUID only — authoritative in LWW model
        existing = conn.execute(
            "SELECT id, updated_at FROM tasks WHERE id = ?", (tid,)
        ).fetchone()

        if existing:
            local_id = existing["id"]
            local_fvs = get_field_versions(conn, local_id)

            # Per-field LWW merge
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
                    # Content protection: never nullify or drastically shrink local content
                    if field in CONTENT_FIELDS:
                        local_content = conn.execute(
                            f"SELECT {field} FROM tasks WHERE id = ?", (local_id,)
                        ).fetchone()
                        local_val = local_content[0] if local_content else None
                        if has_meaningful_content(
                            local_val
                        ) and not has_meaningful_content(remote_val):
                            _log.warning(
                                "LWW content protection: keeping local %s for task %s "
                                "(remote is empty but local has %d chars)",
                                field,
                                local_id,
                                content_length(local_val),
                            )
                            continue
                        if is_suspicious_content_shrink(local_val, remote_val):
                            _log.warning(
                                "LWW shrink guard: keeping local %s for task %s "
                                "(remote would shrink %d -> %d chars)",
                                field,
                                local_id,
                                content_length(local_val),
                                content_length(remote_val),
                            )
                            continue
                    fields_to_update[field] = remote_val
                    conn.execute(
                        "INSERT OR REPLACE INTO task_field_versions "
                        "(task_id, field_name, updated_at, updated_by) "
                        "VALUES (?, ?, ?, ?)",
                        (local_id, field, remote_ts, remote_by),
                    )
                    updated_fields += 1

            # NULL-fill: adopt remote content fields when local is NULL
            # (non-LWW — only fills gaps, never overwrites existing content)
            # Always applied regardless of import_content: LWW may skip content
            # when local has newer timestamp but NULL value (e.g. freshly created task).
            for content_field in CONTENT_FIELDS:
                if content_field in fields_to_update:
                    continue  # already handled by LWW above
                remote_val = remote.get(content_field)
                if not remote_val:
                    continue
                local_val = conn.execute(
                    f"SELECT {content_field} FROM tasks WHERE id = ?",
                    (local_id,),
                ).fetchone()
                if local_val and local_val[0] is None:
                    fields_to_update[content_field] = remote_val
                    updated_fields += 1

            if fields_to_update:
                # Validate field names against allowlist (defense-in-depth)
                safe_fields = {
                    k: v for k, v in fields_to_update.items() if k in MERGEABLE_FIELDS
                }
                if safe_fields:
                    safe_fields["updated_at"] = (
                        now  # ensure incremental export picks up merged tasks
                    )
                    set_clause = ", ".join(f"{k} = ?" for k in safe_fields)
                    values = list(safe_fields.values()) + [local_id]
                    conn.execute(f"UPDATE tasks SET {set_clause} WHERE id = ?", values)
        else:
            # New task — insert (content only if import_content)
            # Note: cancelled tasks still exist as rows (soft-delete), so they're
            # handled by the `if existing:` branch above — LWW field versioning
            # prevents remote from overwriting the newer local cancelled status.

            # Dedup guard: skip if same title already exists (any status)
            remote_title = remote.get("title")
            if remote_title:
                dedup = conn.execute(
                    "SELECT id, status FROM tasks WHERE title = ? LIMIT 1",
                    (remote_title,),
                ).fetchone()
                if dedup:
                    _log.info(
                        "Dedup guard: skipping new task '%s' — same title "
                        "exists locally (task %s, status=%s)",
                        remote_title[:50],
                        dedup["id"][:12],
                        dedup["status"],
                    )
                    continue

            desc = remote.get("description") if import_content else None
            notes = remote.get("notes") if import_content else None
            conn.execute(
                "INSERT OR IGNORE INTO tasks "
                "(id, title, description, status, priority, section, due_date, "
                "project, parent_id, notes, recurring, reminder_at, type, assignee, shared_by, "
                "visibility, publish_requested_at, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                    remote.get("reminder_at"),
                    remote.get("type", "task"),
                    remote.get("assignee"),
                    remote.get("shared_by"),
                    remote.get("visibility", "private"),
                    remote.get("publish_requested_at"),
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

    # Import task-entity links from remote tasks (reuse `now` from above)
    for rt in remote_tasks:
        remote_links = rt.get("_links")
        if not remote_links:
            continue
        tid = rt.get("id", "")
        if not _SAFE_TASK_ID.match(tid):
            continue
        local_task = conn.execute(
            "SELECT id FROM tasks WHERE id = ?", (tid,)
        ).fetchone()
        if not local_task:
            continue
        for link in remote_links:
            ename = link.get("name")
            if not ename:
                continue
            entity = conn.execute(
                "SELECT id FROM entities WHERE name = ?", (ename,)
            ).fetchone()
            if not entity:
                continue
            conn.execute(
                "INSERT OR IGNORE INTO task_entity_links "
                "(task_id, entity_id, link_type, score, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    tid,
                    entity["id"],
                    link.get("link_type", "auto"),
                    link.get("score"),
                    link.get("created_at", now),
                ),
            )

    return new_count, updated_fields


# ── Bridge Sync v2: Lazy content loading ─────────────────────────────────


def load_task_content(task_id: str, bridge_dir: str) -> dict | None:
    """Lazy-load full task (notes/description) from bridge per-task file."""
    if not _SAFE_TASK_ID.match(task_id):
        return None
    task_file = Path(bridge_dir) / "tasks" / f"{task_id}.json"
    if not task_file.exists():
        return None
    try:
        return json_loads(task_file.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None


# ── FTS sync helper ──────────────────────────────────────────────────────


def fts_sync_entity(conn: sqlite3.Connection, entity_id: int) -> None:
    """Rebuild the FTS entry for a given entity.

    Gathers all observations, concatenates them, and upserts into memory_fts.
    Used by bridge_sync_worker to keep FTS in sync after entity imports.
    """
    row = conn.execute(
        "SELECT id, name, entity_type FROM entities WHERE id = ?",
        (entity_id,),
    ).fetchone()
    if row is None:
        conn.execute("DELETE FROM memory_fts WHERE rowid = ?", (entity_id,))
        return

    obs_rows = conn.execute(
        "SELECT content FROM observations WHERE entity_id = ? ORDER BY id",
        (entity_id,),
    ).fetchall()
    obs_text = "\n".join(r["content"] for r in obs_rows)

    conn.execute("DELETE FROM memory_fts WHERE rowid = ?", (entity_id,))
    conn.execute(
        "INSERT INTO memory_fts(rowid, name, entity_type, observations_text) VALUES (?, ?, ?, ?)",
        (row["id"], row["name"], row["entity_type"], obs_text),
    )


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

    # Write to temp dir first, then rename for atomicity
    import shutil
    import tempfile

    tmp_dir = Path(tempfile.mkdtemp(dir=bridge_dir, prefix=".tasks_migrate_"))
    try:
        count = 0
        for task in tasks:
            tid = task.get("id")
            if not tid or not _SAFE_TASK_ID.match(tid):
                continue
            if "_field_ts" not in task:
                task["_field_ts"] = {}
            (tmp_dir / f"{tid}.json").write_text(json_dumps(task), encoding="utf-8")
            count += 1

        # Atomic rename
        if tasks_dir.exists():
            shutil.rmtree(tasks_dir)
        tmp_dir.rename(tasks_dir)
    except Exception:
        # Cleanup temp dir on failure — don't leave corrupt state
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise

    _log.info("Migrated %d tasks from shared.json to per-task files", count)
    return True


# ── Per-Entity File Export/Import ─────────────────────────────────────


def export_entity_files(conn: sqlite3.Connection, bridge_dir: str) -> list[int]:
    """Export shared entities to per-entity JSON files in entities/ directory.

    Each file: entities/{id}.json with full observations.
    Returns list of exported entity IDs.
    """
    entities_dir = Path(bridge_dir) / "entities"
    entities_dir.mkdir(exist_ok=True)

    rows = conn.execute(
        f"SELECT {ENTITY_EXPORT_COLS} FROM entities "
        "WHERE project LIKE 'shared%' ORDER BY name"
    ).fetchall()

    if not rows:
        return []

    # Batch-fetch all observations for shared entities (avoid N+1)
    eids = [r["id"] for r in rows]
    ph = ",".join("?" * len(eids))
    obs_rows = conn.execute(
        f"SELECT entity_id, content, created_at FROM observations "
        f"WHERE entity_id IN ({ph}) ORDER BY entity_id, id",
        eids,
    ).fetchall()

    # Group observations by entity_id
    obs_by_eid: dict[int, list] = {}
    for o in obs_rows:
        obs_by_eid.setdefault(o["entity_id"], []).append(
            {"content": o["content"], "createdAt": o["created_at"]}
        )

    exported = []
    for r in rows:
        eid = r["id"]
        entity = {
            "id": eid,
            "name": r["name"],
            "entityType": r["entity_type"],
            "project": r["project"],
            "observations": obs_by_eid.get(eid, []),
            "createdAt": r["created_at"],
            "updatedAt": r["updated_at"],
        }
        fpath = entities_dir / f"{eid}.json"
        fpath.write_text(json_dumps(entity), encoding="utf-8")
        exported.append(eid)

    # Clean stale files (entities no longer in DB)
    active_ids = {str(eid) for eid in exported}
    for f in entities_dir.glob("*.json"):
        if f.stem not in active_ids:
            f.unlink()

    return exported


def export_entities_index(conn: sqlite3.Connection, bridge_dir: str) -> int:
    """Write entities_index.json — metadata only, no observations."""
    rows = conn.execute(
        f"SELECT {ENTITY_EXPORT_COLS} FROM entities "
        "WHERE project LIKE 'shared%' ORDER BY name"
    ).fetchall()

    # Batch-fetch observation counts
    eids = [r["id"] for r in rows]
    obs_counts: dict[int, int] = {}
    if eids:
        ph = ",".join("?" * len(eids))
        for row in conn.execute(
            f"SELECT entity_id, COUNT(*) as cnt FROM observations "
            f"WHERE entity_id IN ({ph}) GROUP BY entity_id",
            eids,
        ):
            obs_counts[row["entity_id"]] = row["cnt"]

    entries = []
    for r in rows:
        entries.append(
            {
                "id": r["id"],
                "name": r["name"],
                "entityType": r["entity_type"],
                "project": r["project"],
                "visibility": r["visibility"],
                "createdAt": r["created_at"],
                "updatedAt": r["updated_at"],
                "observation_count": obs_counts.get(r["id"], 0),
            }
        )

    index = {
        "version": 1,
        "format": "entity_bridge_v1",
        "pushed_at": now_iso(),
        "machine_id": socket.gethostname(),
        "entities": entries,
    }
    index_path = Path(bridge_dir) / "entities_index.json"
    index_path.write_text(json_dumps(index), encoding="utf-8")
    return len(entries)


def load_entity_content(entity_id: int | str, bridge_dir: str) -> dict | None:
    """Load full entity (with observations) from per-entity file."""
    eid_str = str(entity_id)
    if not _SAFE_ENTITY_ID.match(eid_str):
        return None
    fpath = Path(bridge_dir) / "entities" / f"{eid_str}.json"
    if not fpath.exists():
        return None
    try:
        return json_loads(fpath.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None


def migrate_entities_to_per_files(bridge_dir: str) -> bool:
    """Signal that entity migration to per-file format is needed.

    Unlike tasks (which have UUID filenames in shared.json), entities
    use integer DB IDs not present in shared.json. Actual file creation
    happens during the first export_entity_files() call.
    Returns True if migration marker was created.
    """
    entities_dir = Path(bridge_dir) / "entities"
    index_path = Path(bridge_dir) / "entities_index.json"
    if index_path.exists() or (
        entities_dir.exists() and any(entities_dir.glob("*.json"))
    ):
        return False
    entities_dir.mkdir(exist_ok=True)
    return True
