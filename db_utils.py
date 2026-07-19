"""Shared DB connection, constants, and query helpers for sqlite-memory-mcp.

Single source of truth for task constants, DB connection setup, and common
utilities used by server.py, task_tray.py, and utility scripts.
"""

from __future__ import annotations

import base64
import hashlib
import inspect
import json
import logging
import mimetypes
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import tempfile
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# Suppress console windows on Windows when spawning git/gh from GUI
_NOWIN: dict = (
    {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
)


def _false_askpass_path() -> str | None:
    if os.name == "nt":
        return None
    for candidate in ("/bin/false", "/usr/bin/false"):
        if os.path.exists(candidate):
            return candidate
    return None


def _git_ssh_command(value: str | None = None) -> str:
    command = value or "ssh"
    if "BatchMode" in command:
        return command
    command_name = os.path.basename(command.strip().split(maxsplit=1)[0].strip("\"'"))
    if value and os.name == "nt" and "ssh" not in command_name.lower():
        return command
    return f"{command} -o BatchMode=yes"


def noninteractive_git_env() -> dict[str, str]:
    """Return a git environment that cannot open terminal or GUI prompts."""
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_SSH_COMMAND"] = _git_ssh_command(env.get("GIT_SSH_COMMAND"))
    env["SSH_ASKPASS_REQUIRE"] = "never"

    askpass = _false_askpass_path()
    if askpass is None:
        env.pop("GIT_ASKPASS", None)
        env.pop("SSH_ASKPASS", None)
    else:
        env["GIT_ASKPASS"] = askpass
        env["SSH_ASKPASS"] = askpass
    return env


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

TASK_ATTACHMENT_ROOT = os.environ.get(
    "TASK_ATTACHMENT_ROOT",
    os.path.expanduser("~/.claude/memory/task_attachments"),
)

# ── Task constants (canonical ordering) ──────────────────────────────────

TASK_SECTIONS = ("inbox", "today", "next", "someday", "waiting", "done")
TASK_PRIORITIES = ("low", "medium", "high", "critical")  # ascending rank
TASK_STATUSES = ("not_started", "in_progress", "done", "archived", "cancelled")
TASK_TYPES = ("task", "note")
TASK_HIDDEN_STATUSES = ("archived", "cancelled")
TASK_ACTIVE_EXCLUSIONS = ("done", "archived", "cancelled")

DASHBOARD_KINDS = (
    "result",
    "option",
    "decision",
    "difficulty",
    "misunderstanding",
    "advice",
)
DASHBOARD_PRIORITIES = ("H", "M", "L")
DASHBOARD_KIND_CAP = 8
DASHBOARD_TASK_CAP = 40

_DASHBOARD_KIND_ALIASES = {
    "r": "result",
    "result": "result",
    "o": "option",
    "option": "option",
    "d": "decision",
    "decision": "decision",
    "!": "difficulty",
    "difficulty": "difficulty",
    "?": "misunderstanding",
    "misunderstanding": "misunderstanding",
    "a": "advice",
    "advice": "advice",
}
_DASHBOARD_PRIORITY_ALIASES = {
    "h": "H",
    "high": "H",
    "critical": "H",
    "m": "M",
    "medium": "M",
    "l": "L",
    "low": "L",
}
_DASHBOARD_TOPIC_RE = re.compile(r"^DAILY_(\d{8})$")
_DASHBOARD_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# v5: Extended memory keys — written to separate files during bridge export
EXTENDED_MEMORY_KEYS = (
    "context_chunks",
    "context_annotations",
    "context_questions",
    "candidate_claims",
    "claim_evidence",
    "canonical_facts",
    "provenance_links",
    "knowledge_links",
    "memory_events",
    "memory_audit_issues",
    "memory_artifacts",
    "memory_conflicts",
    "memory_audit_state",
)

# v0.7.0: Public knowledge visibility
VISIBILITY_LEVELS = ("private", "pending_public", "public")
PUBLISH_STANDBY_MINUTES = 15
BRIDGE_SYNC_DELAY = 60  # seconds; shared between bridge_server and task_tray

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

_PROJECT_ALIAS_CANONICAL = {
    "mappingstudio": "mapping-studio",
    "smartkey": "SmartKey",
}


def _project_alias_key(project: str) -> str:
    """Build a punctuation-insensitive key for project alias matching."""
    return re.sub(r"[-_\s]+", "", project.strip().casefold())


def normalize_project_name(project: str | None) -> str | None:
    """Normalize known project aliases while preserving unrelated names."""
    if project is None:
        return None
    cleaned = re.sub(r"\s+", " ", str(project)).strip()
    if not cleaned:
        return None
    return _PROJECT_ALIAS_CANONICAL.get(_project_alias_key(cleaned), cleaned)


def normalize_project_filter_values(values: Any) -> set[str]:
    """Normalize a project filter collection into canonical project names."""
    result: set[str] = set()
    if not values:
        return result
    for value in values:
        normalized = normalize_project_name(value)
        if normalized:
            result.add(normalized)
    return result


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

GITHUB_USER_RE = re.compile(r"^[a-zA-Z0-9](?:[a-zA-Z0-9\-]{0,37}[a-zA-Z0-9])?$")

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

MACHINE_ID = os.environ.get("MACHINE_ID", socket.gethostname())
_HLC_COUNTER_BITS = 16
_HLC_COUNTER_MASK = (1 << _HLC_COUNTER_BITS) - 1
_HLC_PACKED_MIN = 1 << 32


def _iso_to_epoch_ms(value: str | None) -> int:
    raw = (value or "").strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw) if raw else datetime.now(timezone.utc)
    except ValueError:
        dt = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.astimezone(timezone.utc).timestamp() * 1000)


def _decode_logical_clock(clock: int) -> tuple[int, int]:
    value = int(clock or 0)
    if value <= 0:
        return 0, 0
    if value < _HLC_PACKED_MIN:
        return value, 0
    return value >> _HLC_COUNTER_BITS, value & _HLC_COUNTER_MASK


def _pack_logical_clock(physical_ms: int, counter: int) -> int:
    return (max(0, int(physical_ms)) << _HLC_COUNTER_BITS) | (
        int(counter) & _HLC_COUNTER_MASK
    )


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
TASK_FILE_HYDRATION_FIELDS = ("_attachments", "_field_ts", "_links", "_tombstone")

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


def is_archived_duplicate_redirect_task(task: Any) -> bool:
    """True when an archived task intentionally redirects to a canonical item."""

    def _value(field: str) -> Any:
        if isinstance(task, dict):
            return task.get(field)
        try:
            return task[field]
        except (IndexError, KeyError, TypeError):
            return None

    if _value("status") != "archived":
        return False

    text = "\n".join(
        str(_value(field) or "") for field in ("title", "description", "notes")
    ).upper()
    has_duplicate_marker = "ARCHIVED DUPLICATE" in text
    has_redirect_marker = "DO NOT USE" in text or "SUPERSEDED" in text
    return has_duplicate_marker and has_redirect_marker


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

FIELD_TS_VALUE_FIELDS = ("status", "section")
_FIELD_VALUE_MISSING = object()

_TOMBSTONE_DAYS = 30

# Path traversal defense for direct filename usage: raw task IDs may only use
# alphanumerics and hyphens. Legacy IDs are still supported via encoded stems.
_SAFE_TASK_ID = re.compile(r"^[a-zA-Z0-9\-]+$")
_SAFE_ENTITY_ID = re.compile(r"^[1-9][0-9]*$")


def _task_storage_stem(task_id: str) -> str:
    """Return a filesystem-safe stem for per-task bridge files."""
    if _SAFE_TASK_ID.match(task_id):
        return task_id
    encoded = base64.urlsafe_b64encode(task_id.encode("utf-8")).decode("ascii")
    return f"_id_{encoded.rstrip('=')}"


def _task_storage_path(task_id: str, bridge_dir: str) -> Path:
    """Return the canonical per-task bridge file path for a task id."""
    return Path(bridge_dir) / "tasks" / f"{_task_storage_stem(task_id)}.json"


_ATTACHMENT_NAME_SANITIZE_RE = re.compile(r'[<>:"/\\|?*\x00-\x1F]+')


def _safe_attachment_name(file_name: str) -> str:
    """Return a filesystem-safe attachment file name."""
    base = os.path.basename((file_name or "").strip()) or "attachment"
    cleaned = _ATTACHMENT_NAME_SANITIZE_RE.sub("_", base).strip(" .")
    return cleaned or "attachment"


def _task_attachment_relpath(task_id: str, attachment_id: str, file_name: str) -> str:
    """Return attachment relpath shared by local store and bridge export."""
    safe_name = _safe_attachment_name(file_name)
    return f"{_task_storage_stem(task_id)}/{attachment_id}__{safe_name}"


def _resolve_attachment_path(root_dir: str, stored_relpath: str) -> Path:
    """Resolve a stored attachment path under a root dir with traversal defense."""
    root = Path(root_dir).resolve()
    target = (root / stored_relpath).resolve()
    if target != root and root not in target.parents:
        raise ValueError(f"Attachment path escapes root: {stored_relpath!r}")
    return target


def _local_attachment_path(stored_relpath: str, root_dir: str | None = None) -> Path:
    return _resolve_attachment_path(root_dir or TASK_ATTACHMENT_ROOT, stored_relpath)


def _bridge_attachment_path(stored_relpath: str, bridge_dir: str) -> Path:
    return _resolve_attachment_path(
        os.path.join(bridge_dir, "attachments"), stored_relpath
    )


def _copy_attachment_file(src: Path, dst: Path) -> None:
    """Copy attachment bytes atomically, creating parent dirs as needed."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = dst.with_suffix(dst.suffix + ".tmp")
    shutil.copy2(src, tmp_path)
    os.replace(tmp_path, dst)


def setup_logger(name: str, log_file: str = "server.log") -> logging.Logger:
    """Configure file logger. Idempotent — safe to call multiple times."""
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    if not logger.handlers:
        candidates = [
            Path.home() / ".claude" / "memory" / log_file,
            Path(tempfile.gettempdir()) / "sqlite-memory-mcp" / log_file,
        ]
        for log_path in candidates:
            try:
                log_path.parent.mkdir(parents=True, exist_ok=True)
                fh = logging.FileHandler(log_path, encoding="utf-8")
            except OSError:
                continue
            fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            logger.addHandler(fh)
            break
        if not logger.handlers:
            logger.addHandler(logging.NullHandler())
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

_DB_INIT_DONE: set[str] = set()
_DB_INIT_ACTIVE: set[str] = set()
_DB_INIT_COND = threading.Condition()
_DB_INIT_LOCAL = threading.local()


def ensure_db_initialized(db_path: str | None = None) -> str:
    """Initialize the target DB lazily, once per process and path."""
    target = db_path or DB_PATH
    local_paths = getattr(_DB_INIT_LOCAL, "paths", set())
    if target in local_paths:
        return target

    with _DB_INIT_COND:
        while target in _DB_INIT_ACTIVE:
            _DB_INIT_COND.wait()
        if target in _DB_INIT_DONE:
            return target
        _DB_INIT_ACTIVE.add(target)

    _DB_INIT_LOCAL.paths = set(local_paths) | {target}
    try:
        from schema import init_db

        init_db(target)
    except Exception:
        with _DB_INIT_COND:
            _DB_INIT_ACTIVE.discard(target)
            _DB_INIT_COND.notify_all()
        raise
    finally:
        local_paths = set(getattr(_DB_INIT_LOCAL, "paths", set()))
        local_paths.discard(target)
        _DB_INIT_LOCAL.paths = local_paths

    with _DB_INIT_COND:
        _DB_INIT_ACTIVE.discard(target)
        _DB_INIT_DONE.add(target)
        _DB_INIT_COND.notify_all()
    return target


@contextmanager
def get_conn(db_path: str | None = None):
    """Yield a SQLite connection with PRAGMAs set, auto-commit/rollback.

    Uses explicit BEGIN/COMMIT to ensure each context-manager block is atomic.
    Retries BEGIN up to 3× on SQLITE_BUSY (exponential backoff on top of busy_timeout).
    """
    import time as _time

    target = db_path or DB_PATH
    if db_path is None:
        ensure_db_initialized(target)

    # Retry connection + BEGIN on SQLITE_BUSY (lock contention with tray/bridge)
    conn = None
    for attempt in range(_BUSY_RETRIES):
        conn = sqlite3.connect(target, isolation_level=None, timeout=10)
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
def get_conn_immediate(db_path: str | None = None):
    """Like ``get_conn()`` but starts the transaction in IMMEDIATE mode.

    Plain ``BEGIN`` is DEFERRED — SQLite waits until the first write to
    take a RESERVED lock. Two writers that each open a connection,
    issue some reads, and then try to upgrade to RESERVED can collide
    on the SHARED→RESERVED race instead of serializing cleanly. For
    DAO functions whose race-safety contract requires reads + writes
    to be linearised against other writers (debate_signal_advance and
    debate_post_with_recipients in v3.9.3), open the txn with
    ``BEGIN IMMEDIATE`` so the RESERVED lock is held from the very
    first statement.

    Per ADVOCATE turn-2 amendment 1A (msg:34adcb3e): wrapper-scoped
    contract — direct DAO callers using regular ``get_conn()`` retain
    the old race risk; the production MCP path always uses this
    wrapper for write tools.
    """
    import time as _time

    target = db_path or DB_PATH
    if db_path is None:
        ensure_db_initialized(target)

    conn = None
    for attempt in range(_BUSY_RETRIES):
        conn = sqlite3.connect(target, isolation_level=None, timeout=10)
        conn.row_factory = sqlite3.Row
        for pragma in _PRAGMAS:
            conn.execute(pragma)
        try:
            conn.execute("BEGIN IMMEDIATE;")
            break
        except sqlite3.OperationalError as e:
            conn.close()
            conn = None
            if "locked" in str(e).lower() and attempt < _BUSY_RETRIES - 1:
                _time.sleep(_BUSY_BASE_DELAY * (2**attempt))
                continue
            raise

    try:
        yield conn
        conn.execute("COMMIT;")
    except Exception:
        try:
            conn.execute("ROLLBACK;")
        except Exception:
            pass
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
    target = db_path or DB_PATH
    if db_path is None:
        ensure_db_initialized(target)
    conn = sqlite3.connect(target, isolation_level=None, timeout=30)
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

BRIDGE_GENERATED_FILES = frozenset(
    {
        "shared.json",
        "shared.js",
        "index.json",
        "entities_index.json",
        "kanban_payload.json",  # render-only derived mirror (v3.12.4); regenerated each export
        ".bridge_sync.lock",  # legacy in-repo lock path; safe to discard
    }
)
BRIDGE_GENERATED_TEMP_FILES = frozenset(
    {
        "shared.tmp",  # legacy temp path used by older bridge writers
        "shared.json.tmp",
        "shared.js.tmp",
        "index.json.tmp",
        "entities_index.json.tmp",
    }
)
BRIDGE_GENERATED_DIRS = frozenset(
    {"tasks", "entities", "attachments", "public_knowledge", "extended_memory"}
)


def validate_github_username(username: str) -> None:
    """Raise ValueError when a collaborator/assignee name is not GitHub-safe."""
    if not GITHUB_USER_RE.match(username):
        raise ValueError(f"Invalid GitHub username: {username!r}")


_BRIDGE_CONFLICT_STATES = frozenset({"AA", "AU", "DD", "DU", "UA", "UD", "UU"})


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
        env=noninteractive_git_env(),
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
        try:
            last_result = git_run(repo_dir, *args, timeout=timeout)
        except subprocess.TimeoutExpired:
            last_result = subprocess.CompletedProcess(
                ["git", *args],
                124,
                "",
                f"git {' '.join(args)} timed out after {timeout}s",
            )
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


def ensure_bridge_git_identity(
    repo_dir: str,
    *,
    source_repo_dir: str | None = None,
) -> dict[str, Any]:
    """Copy the source repo git identity into the bridge repo as local config."""
    source_dir = source_repo_dir or str(Path(__file__).resolve().parent)

    def _cfg(repo: str, *cfg_args: str) -> str | None:
        result = git_run(repo, *cfg_args, timeout=10)
        if result.returncode != 0:
            return None
        value = result.stdout.strip()
        return value or None

    bridge_name = _cfg(repo_dir, "config", "--get", "user.name")
    bridge_email = _cfg(repo_dir, "config", "--get", "user.email")
    target_name = (
        _cfg(source_dir, "config", "--get", "user.name")
        or _cfg(source_dir, "config", "--global", "user.name")
        or bridge_name
    )
    target_email = (
        _cfg(source_dir, "config", "--get", "user.email")
        or _cfg(source_dir, "config", "--global", "user.email")
        or bridge_email
    )

    changed = False
    if target_name and bridge_name != target_name:
        result = git_run(repo_dir, "config", "user.name", target_name, timeout=10)
        if result.returncode == 0:
            bridge_name = target_name
            changed = True
    if target_email and bridge_email != target_email:
        result = git_run(repo_dir, "config", "user.email", target_email, timeout=10)
        if result.returncode == 0:
            bridge_email = target_email
            changed = True

    return {
        "changed": changed,
        "user_name": bridge_name,
        "user_email": bridge_email,
    }


def _bridge_status_path(line: str) -> str:
    """Extract the repo-relative path from a git status --porcelain line."""
    path = line[3:].strip()
    if " -> " in path:
        path = path.split(" -> ", 1)[1]
    return path.replace("\\", "/").strip("/")


def is_generated_bridge_path(path: str) -> bool:
    """Return True for files/directories regenerated by bridge sync."""
    rel = path.replace("\\", "/").strip("/")
    if rel in BRIDGE_GENERATED_FILES or rel in BRIDGE_GENERATED_TEMP_FILES:
        return True
    if rel.endswith(".tmp"):
        return any(rel.startswith(f"{dirname}/") for dirname in BRIDGE_GENERATED_DIRS)
    return any(rel == d or rel.startswith(f"{d}/") for d in BRIDGE_GENERATED_DIRS)


def _bridge_generated_symlink_issues(repo_dir: str, limit: int = 3) -> list[str]:
    """Return generated bridge paths that are symlinks or escape the repo root."""
    repo_root = Path(repo_dir).resolve()
    flagged: list[str] = []
    seen: set[str] = set()

    def _record(path: Path) -> None:
        rel = path.relative_to(repo_dir).as_posix()
        if rel not in seen:
            seen.add(rel)
            flagged.append(rel)

    for rel_name in sorted(BRIDGE_GENERATED_FILES | BRIDGE_GENERATED_DIRS):
        path = Path(repo_dir) / rel_name
        if not path.exists() and not path.is_symlink():
            continue
        if path.is_symlink():
            _record(path)
            if len(flagged) >= limit:
                return flagged
            continue
        try:
            resolved = path.resolve(strict=True)
        except OSError:
            _record(path)
            if len(flagged) >= limit:
                return flagged
            continue
        if resolved != repo_root and repo_root not in resolved.parents:
            _record(path)
            if len(flagged) >= limit:
                return flagged
            continue
        if not path.is_dir():
            continue
        try:
            for child in path.rglob("*"):
                if not child.is_symlink():
                    continue
                _record(child)
                if len(flagged) >= limit:
                    return flagged
        except OSError:
            _record(path)
            if len(flagged) >= limit:
                return flagged
    return flagged


def _bridge_git_path(repo_dir: str, path_name: str) -> Path:
    """Return a git metadata path without mutating the repo."""
    return Path(repo_dir) / ".git" / path_name


def _bridge_git_operation_blocker(repo_dir: str) -> str | None:
    """Return a fail-closed blocker for active git sequencing state."""
    sequence_markers = (
        "rebase-merge",
        "rebase-apply",
        "MERGE_HEAD",
        "CHERRY_PICK_HEAD",
        "REVERT_HEAD",
    )
    active = [
        marker
        for marker in sequence_markers
        if _bridge_git_path(repo_dir, marker).exists()
    ]
    if active:
        return (
            "bridge repo has active git operation "
            f"({', '.join(active)}); manual recovery required before sync"
        )
    return None


# E1: sequence-marker auto-recovery. rebase-merge / rebase-apply / MERGE_HEAD are
# left-behind half-operations that bridge_pull/push never create (ff-only by design);
# they can be safely auto-aborted IFF the working tree carries no user-managed
# changes. CHERRY_PICK_HEAD / REVERT_HEAD stay manual.
_BRIDGE_AUTO_ABORTABLE_MARKERS = ("rebase-merge", "rebase-apply", "MERGE_HEAD")
_BRIDGE_MANUAL_MARKERS = ("CHERRY_PICK_HEAD", "REVERT_HEAD")

# Last auto-abort attempt record (Option A: contract-preserving — ensure_bridge_repo_ready
# keeps its (bool, str|None) return; bridge_doctor reads this via the accessor below).
_last_bridge_auto_abort: dict | None = None


def get_last_bridge_auto_abort() -> dict | None:
    """Read-only accessor for the last E1 auto-abort attempt (bridge_doctor surface)."""
    return _last_bridge_auto_abort


def _bridge_working_tree_safe_for_abort(repo_dir: str) -> tuple[bool, str | None]:
    """True only if every porcelain entry is absent or an exclusively generated
    bridge artifact. Any non-generated conflicted/dirty/staged/untracked path makes
    auto-abort unsafe (we must not discard user-managed work)."""
    status = git_run(repo_dir, "status", "--porcelain")
    if status.returncode != 0:
        detail = (status.stderr or status.stdout).strip()
        return False, f"cannot inspect bridge status: {detail or 'unknown git error'}"
    lines = [ln for ln in status.stdout.splitlines() if ln.strip()]
    paths = [_bridge_status_path(ln) for ln in lines]
    non_generated = [p for p in paths if not is_generated_bridge_path(p)]
    if non_generated:
        shown = ", ".join(non_generated[:3])
        return False, f"non-generated working-tree changes present: {shown}"
    return True, None


def _bridge_auto_abort_recover(repo_dir: str, markers: list[str]) -> dict:
    """Bounded auto-abort of left-behind sequence state. Each abort is git_run(timeout=5).
    Returns a structured record with per-command success/failure (no exceptions escape)."""
    cmds: list[tuple[str, str]] = []
    if any(m in ("rebase-merge", "rebase-apply") for m in markers):
        cmds.append(("rebase", "--abort"))
    if "MERGE_HEAD" in markers:
        cmds.append(("merge", "--abort"))
    attempts: list[dict] = []
    for cmd in cmds:
        try:
            r = git_run(repo_dir, *cmd, timeout=5)
            attempts.append(
                {
                    "cmd": " ".join(cmd),
                    "ok": r.returncode == 0,
                    "detail": ((r.stderr or r.stdout).strip()[:200]),
                }
            )
        except subprocess.TimeoutExpired:
            attempts.append(
                {"cmd": " ".join(cmd), "ok": False, "detail": "timeout(5s)"}
            )
    return {"markers_detected": list(markers), "aborts": attempts}


def inspect_bridge_repo_blocker(repo_dir: str) -> str | None:
    """Return a fail-closed blocker message for unsafe bridge repo states.

    This helper is intentionally read-only. It detects active git sequencing and
    unresolved conflicts before higher-level sync code can run recovery logic.
    """
    blocker = _bridge_git_operation_blocker(repo_dir)
    if blocker:
        return blocker

    status = git_run(repo_dir, "status", "--porcelain")
    if status.returncode != 0:
        detail = (status.stderr or status.stdout).strip()
        return f"cannot inspect bridge status: {detail or 'unknown git error'}"

    lines = [ln for ln in status.stdout.splitlines() if ln.strip()]
    conflict_lines = [ln for ln in lines if ln[:2] in _BRIDGE_CONFLICT_STATES]
    if not conflict_lines:
        return None

    conflict_paths = [_bridge_status_path(ln) for ln in conflict_lines]
    unsafe = [p for p in conflict_paths if not is_generated_bridge_path(p)]
    shown = ", ".join((unsafe or conflict_paths)[:3])
    if unsafe:
        return f"bridge repo has unresolved user-managed conflicts: {shown}"
    return f"bridge repo has unresolved generated conflicts: {shown}"


def ensure_bridge_repo_ready(repo_dir: str) -> tuple[bool, str | None]:
    """Prepare the bridge repo for sync without discarding user-managed files.

    Safe behavior:
    - detached HEAD -> try to return to main
    - active git operation/conflicts -> block sync, do not mutate recovery state
    - edits in generated artifacts -> discard/rebuild
    - edits in user-managed files -> block sync and surface the paths
    """
    blocker = _bridge_git_operation_blocker(repo_dir)
    if blocker:
        global _last_bridge_auto_abort
        abortable = [
            m
            for m in _BRIDGE_AUTO_ABORTABLE_MARKERS
            if _bridge_git_path(repo_dir, m).exists()
        ]
        manual = [
            m for m in _BRIDGE_MANUAL_MARKERS if _bridge_git_path(repo_dir, m).exists()
        ]
        # Auto-recover only when the blocker is EXCLUSIVELY from auto-abortable markers.
        if not abortable or manual:
            return False, blocker
        # Pre-abort gate (conflict-gate per spec): never abort over user-managed work.
        safe, unsafe_reason = _bridge_working_tree_safe_for_abort(repo_dir)
        if not safe:
            _last_bridge_auto_abort = {
                "markers_detected": abortable,
                "aborts": [],
                "skipped": unsafe_reason,
            }
            return (
                False,
                f"{blocker}; auto-recovery skipped ({unsafe_reason}); "
                "blocked_by_repo_state preserved",
            )
        record = _bridge_auto_abort_recover(repo_dir, abortable)
        _last_bridge_auto_abort = record
        recheck = _bridge_git_operation_blocker(repo_dir)
        if recheck is not None:
            summary = "; ".join(
                f"{a['cmd']}={'ok' if a['ok'] else 'fail'}" for a in record["aborts"]
            )
            return (
                False,
                f"{blocker}; auto-recovery failed [{summary}]; "
                "blocked_by_repo_state preserved",
            )
        # Sequence state cleared -> fall through to the normal readiness flow.

    branch = git_run(repo_dir, "rev-parse", "--abbrev-ref", "HEAD")
    if branch.returncode != 0:
        detail = (branch.stderr or branch.stdout).strip()
        return False, f"cannot inspect bridge branch: {detail or 'unknown git error'}"

    if branch.stdout.strip() == "HEAD":
        checkout = git_run(repo_dir, "checkout", "main")
        if checkout.returncode != 0:
            git_run(repo_dir, "fetch", "origin")
            checkout = git_run(repo_dir, "checkout", "-B", "main", "origin/main")
        if checkout.returncode != 0:
            detail = (checkout.stderr or checkout.stdout).strip()
            return (
                False,
                f"bridge repo is detached and could not checkout main: {detail}",
            )

    # Install the tombstone-safe merge driver + .gitattributes (idempotent).
    # This wires the second line of defense so any FUTURE external git pull/merge
    # of a tombstone-bearing bridge file reconciles instead of resurrecting. It
    # is best-effort: the DB-layer merge (merge_import_tasks) remains the
    # authoritative tombstone-union, so a config write failure must not block
    # sync. Skipped when the bridge repo isn't initialized (no .git dir).
    if (Path(repo_dir) / ".git").exists():
        try:
            from bridge_merge_driver import ensure_bridge_merge_protection

            protection = ensure_bridge_merge_protection(repo_dir)
            if not protection.get("ok"):
                log.warning(
                    "bridge merge protection install incomplete: %s", protection
                )
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("bridge merge protection install failed: %s", exc)

    symlink_issues = _bridge_generated_symlink_issues(repo_dir)
    if symlink_issues:
        shown = ", ".join(symlink_issues[:3])
        return (
            False,
            f"bridge repo contains unsafe generated symlinks/escaped paths: {shown}",
        )

    status = git_run(repo_dir, "status", "--porcelain")
    if status.returncode != 0:
        detail = (status.stderr or status.stdout).strip()
        return False, f"cannot inspect bridge status: {detail or 'unknown git error'}"

    lines = [ln for ln in status.stdout.splitlines() if ln.strip()]
    if not lines:
        return True, None

    conflict_lines = [ln for ln in lines if ln[:2] in _BRIDGE_CONFLICT_STATES]
    if conflict_lines:
        conflict_paths = [_bridge_status_path(ln) for ln in conflict_lines]
        unsafe = [p for p in conflict_paths if not is_generated_bridge_path(p)]
        if unsafe:
            shown = ", ".join(unsafe[:3])
            return (
                False,
                f"resolve bridge conflicts in user-managed files first: {shown}",
            )
        # Generated-only unmerged state with no active sequence operation
        # (the "stuck UU without MERGE_HEAD" class): safe to auto-heal — these
        # files are rebuilt from the DB on the next export. We resolve the
        # conflict (prefer theirs so an incoming tombstone survives until the
        # DB-layer merge rewrites it) rather than blocking sync indefinitely.
        try:
            from bridge_merge_driver import auto_heal_unmerged_generated

            heal = auto_heal_unmerged_generated(repo_dir)
        except Exception as exc:  # pragma: no cover - defensive
            heal = {"healed": [], "skipped": f"auto-heal error: {exc}"}
        if heal.get("healed"):
            log.info(
                "Auto-healed stuck unmerged generated bridge artifacts: %s",
                ", ".join(heal["healed"][:5]),
            )
            status = git_run(repo_dir, "status", "--porcelain")
            if status.returncode != 0:
                detail = (status.stderr or status.stdout).strip()
                return (
                    False,
                    f"cannot inspect bridge status: {detail or 'unknown git error'}",
                )
            lines = [ln for ln in status.stdout.splitlines() if ln.strip()]
            still_conflicted = [ln for ln in lines if ln[:2] in _BRIDGE_CONFLICT_STATES]
            if still_conflicted:
                shown = ", ".join(
                    _bridge_status_path(ln) for ln in still_conflicted[:3]
                )
                return (
                    False,
                    f"resolve bridge conflicts in generated files first: {shown}",
                )
            if not lines:
                return True, None
        else:
            shown = ", ".join(conflict_paths[:3])
            return False, f"resolve bridge conflicts in generated files first: {shown}"

    # A dirty path is allowed through readiness when it is a regenerable
    # generated artifact OR the merge-driver's own managed .gitattributes seed.
    # The latter is content-verified (must carry our managed block) and was just
    # staged by ensure_bridge_merge_protection above; it rides the worker's next
    # commit. Without this, first-time runtime seeding of .gitattributes (not a
    # generated path) would block sync with "commit or stash bridge repo edits".
    def _path_allowed_dirty(path: str) -> bool:
        if is_generated_bridge_path(path):
            return True
        try:
            from bridge_merge_driver import is_managed_gitattributes

            return is_managed_gitattributes(repo_dir, path)
        except Exception:  # pragma: no cover - defensive
            return False

    dirty_paths = [_bridge_status_path(ln) for ln in lines]
    unsafe = [p for p in dirty_paths if not _path_allowed_dirty(p)]
    if unsafe:
        shown = ", ".join(unsafe[:3])
        return False, f"commit or stash bridge repo edits before sync: {shown}"

    # Generated artifacts are safe to rebuild from DB state.
    generated_paths = [
        "shared.json",
        "shared.js",
        "index.json",
        "entities_index.json",
        "kanban_payload.json",
        ".bridge_sync.lock",
        "shared.tmp",
        "shared.json.tmp",
        "shared.js.tmp",
        "index.json.tmp",
        "entities_index.json.tmp",
        "tasks",
        "entities",
        "attachments",
        "public_knowledge",
        "extended_memory",
    ]
    restore = git_run(repo_dir, "checkout", "--", *generated_paths)
    clean = git_run(repo_dir, "clean", "-fd", "--", *generated_paths)
    if restore.returncode != 0 and clean.returncode != 0:
        detail = ((restore.stderr or "") + " " + (clean.stderr or "")).strip()
        return False, f"failed to discard generated bridge artifacts: {detail}"

    status = git_run(repo_dir, "status", "--porcelain")
    if status.returncode != 0:
        detail = (status.stderr or status.stdout).strip()
        return False, f"cannot verify bridge cleanup: {detail or 'unknown git error'}"

    remaining = [
        _bridge_status_path(ln) for ln in status.stdout.splitlines() if ln.strip()
    ]
    unsafe = [p for p in remaining if not _path_allowed_dirty(p)]
    if unsafe:
        shown = ", ".join(unsafe[:3])
        return False, f"bridge repo still has user-managed edits after cleanup: {shown}"

    return True, None


def source_hash(name: str, entity_type: str, observations: list) -> str:
    """SHA256 hash for entity deduplication."""
    raw = json.dumps({"n": name, "t": entity_type, "o": observations}, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()


# ── Timestamp helpers ────────────────────────────────────────────────────


def now_iso() -> str:
    """ISO 8601 UTC timestamp."""
    return datetime.now(timezone.utc).isoformat()


def _sqlite_table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def _sqlite_has_column(
    conn: sqlite3.Connection, table_name: str, column_name: str
) -> bool:
    try:
        rows = conn.execute(f"PRAGMA table_info('{table_name}')").fetchall()
    except sqlite3.OperationalError:
        return False
    # PRAGMA table_info columns: (cid, name, type, notnull, dflt_value, pk).
    # Use positional access so this works whether the caller's connection has
    # row_factory=sqlite3.Row set or returns plain tuples (bin/task opens a
    # raw connection without row_factory).
    return any(r[1] == column_name for r in rows)


def normalize_dashboard_kind(kind: str) -> str:
    """Normalize dashboard kind aliases to the canonical six-kind enum."""
    normalized = _DASHBOARD_KIND_ALIASES.get(str(kind or "").strip().lower())
    if normalized is None:
        raise ValueError("dashboard kind must be one of: " + ", ".join(DASHBOARD_KINDS))
    return normalized


def normalize_dashboard_priority(priority: str | None) -> str:
    """Normalize H/M/L dashboard priority aliases."""
    raw = str(priority or "M").strip()
    normalized = _DASHBOARD_PRIORITY_ALIASES.get(raw.lower())
    if normalized is None and raw.upper() in DASHBOARD_PRIORITIES:
        normalized = raw.upper()
    if normalized is None:
        raise ValueError("dashboard priority must be H, M, or L")
    return normalized


def dash_topic_id(day: str | None = None) -> str:
    """Return the DAILY_YYYYMMDD topic id for a dashboard day."""
    day = day or dash_today()
    if not _DASHBOARD_DAY_RE.fullmatch(day):
        raise ValueError(f"dashboard day must be YYYY-MM-DD, got {day!r}")
    return "DAILY_" + day.replace("-", "")


def dash_today(topic_id: str | None = None) -> str:
    """Return local dashboard day and validate it against DAILY_YYYYMMDD.

    If ``topic_id`` or ``SQLITE_MEMORY_DASH_TOPIC_ID`` is supplied, it must match
    today's local date. This keeps UTC/local rollover drift visible instead of
    silently writing under the wrong daily topic.
    """
    day = date.today().isoformat()
    candidate = topic_id or os.environ.get("SQLITE_MEMORY_DASH_TOPIC_ID") or ""
    if candidate:
        m = _DASHBOARD_TOPIC_RE.fullmatch(candidate)
        if not m:
            raise ValueError(
                f"dashboard topic must match DAILY_YYYYMMDD: {candidate!r}"
            )
        topic_day = f"{m.group(1)[:4]}-{m.group(1)[4:6]}-{m.group(1)[6:8]}"
        if topic_day != day:
            raise ValueError(
                f"dashboard topic/day mismatch: topic={candidate} local_day={day}"
            )
    return day


def ensure_dashboard_schema(conn: sqlite3.Connection) -> None:
    """Create the machine-local daily dashboard projection table if needed."""
    kind_check = ", ".join(f"'{k}'" for k in DASHBOARD_KINDS)
    priority_check = ", ".join(f"'{p}'" for p in DASHBOARD_PRIORITIES)
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS daily_dashboard (
            day        TEXT NOT NULL,
            task_id    TEXT NOT NULL,
            kind       TEXT NOT NULL CHECK(kind IN ({kind_check})),
            slot       TEXT NOT NULL CHECK(length(trim(slot)) > 0),
            body       TEXT NOT NULL CHECK(length(body) <= 240),
            priority   TEXT NOT NULL DEFAULT 'M' CHECK(priority IN ({priority_check})),
            src_msg_id TEXT DEFAULT NULL,
            author     TEXT NOT NULL DEFAULT 'conductor',
            updated_at TEXT NOT NULL,
            PRIMARY KEY(day, task_id, kind, slot)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_daily_dashboard_day_task "
        "ON daily_dashboard(day, task_id, updated_at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_daily_dashboard_day_kind "
        "ON daily_dashboard(day, kind, updated_at DESC)"
    )


def _dashboard_test_override(allow_test_override: bool = False) -> bool:
    return allow_test_override or os.environ.get(
        "SQLITE_MEMORY_DASH_TEST_OVERRIDE"
    ) in {
        "1",
        "true",
        "yes",
    }


def assert_dashboard_conductor_writer(
    conn: sqlite3.Connection,
    *,
    writer_session: str | None,
    day: str | None = None,
    allow_test_override: bool = False,
) -> None:
    """Fail closed unless writer_session owns the active CONDUCTOR binding.

    NOTE (ADVOCATE 2026-06-07): this is a COOPERATIVE session-binding guard,
    NOT an identity proof or security boundary. ``writer_session`` is a
    caller-supplied string (via ``--writer-session`` / env), so anything that
    knows the active CONDUCTOR session_id can pass it. The real protection is
    deployment shape — executors have no CLI/MCP path to this writer — plus the
    author='conductor'/updated_at audit stamp on every row. Do not rely on this
    as an authentication mechanism.
    """
    day = day or dash_today()
    if _dashboard_test_override(allow_test_override):
        return
    topic_id = dash_topic_id(day)
    writer = str(writer_session or "").strip()
    if not writer:
        raise PermissionError(
            "dashboard write denied: writer_session required for hard CONDUCTOR guard"
        )
    row = conn.execute(
        "SELECT session_id FROM debate_role_bindings "
        "WHERE topic_id = ? AND role = 'CONDUCTOR' AND state = 'active' "
        "ORDER BY generation DESC LIMIT 1",
        (topic_id,),
    ).fetchone()
    if row is None:
        raise PermissionError(
            f"dashboard write denied: no active CONDUCTOR binding for {topic_id}"
        )
    active = row["session_id"] if isinstance(row, sqlite3.Row) else row[0]
    if active != writer:
        raise PermissionError(
            "dashboard write denied: writer_session is not active CONDUCTOR "
            f"for {topic_id}"
        )


def _priority_rank_sql() -> str:
    return "CASE priority WHEN 'L' THEN 0 WHEN 'M' THEN 1 WHEN 'H' THEN 2 ELSE 0 END"


def _prune_dashboard_rows(
    conn: sqlite3.Connection,
    *,
    day: str,
    task_id: str,
    kind: str | None = None,
    cap: int,
) -> int:
    where = "day = ? AND task_id = ?"
    params: list[Any] = [day, task_id]
    if kind is not None:
        where += " AND kind = ?"
        params.append(kind)
    rows = conn.execute(
        "SELECT day, task_id, kind, slot FROM daily_dashboard "
        f"WHERE {where} "
        "ORDER BY updated_at DESC, "
        f"{_priority_rank_sql()} DESC, slot ASC",
        params,
    ).fetchall()
    stale = rows[cap:]
    for row in stale:
        conn.execute(
            "DELETE FROM daily_dashboard "
            "WHERE day = ? AND task_id = ? AND kind = ? AND slot = ?",
            (row["day"], row["task_id"], row["kind"], row["slot"]),
        )
    return len(stale)


def _enforce_dashboard_caps(
    conn: sqlite3.Connection, *, day: str, task_id: str, kind: str
) -> int:
    removed = _prune_dashboard_rows(
        conn,
        day=day,
        task_id=task_id,
        kind=kind,
        cap=DASHBOARD_KIND_CAP,
    )
    removed += _prune_dashboard_rows(
        conn,
        day=day,
        task_id=task_id,
        cap=DASHBOARD_TASK_CAP,
    )
    return removed


def dash_upsert(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    kind: str,
    slot: str,
    body: str,
    priority: str | None = "M",
    src_msg_id: str | None = None,
    writer_session: str | None = None,
    day: str | None = None,
    updated_at: str | None = None,
    allow_test_override: bool = False,
) -> dict[str, Any]:
    """Upsert one daily dashboard item after the hard CONDUCTOR write guard."""
    ensure_dashboard_schema(conn)
    day = day or dash_today()
    if not _DASHBOARD_DAY_RE.fullmatch(day):
        raise ValueError(f"dashboard day must be YYYY-MM-DD, got {day!r}")
    assert_dashboard_conductor_writer(
        conn,
        writer_session=writer_session,
        day=day,
        allow_test_override=allow_test_override,
    )
    task_id = str(task_id or "").strip()
    slot = str(slot or "").strip()
    body = str(body or "").strip()
    kind = normalize_dashboard_kind(kind)
    priority = normalize_dashboard_priority(priority)
    if not task_id:
        raise ValueError("dashboard task_id required")
    if not slot:
        raise ValueError("dashboard slot required")
    if not body:
        raise ValueError("dashboard body required")
    if len(body) > 240:
        raise ValueError("dashboard body must be <= 240 characters")
    ts = updated_at or now_iso()
    conn.execute(
        "INSERT INTO daily_dashboard "
        "(day, task_id, kind, slot, body, priority, src_msg_id, author, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'conductor', ?) "
        "ON CONFLICT(day, task_id, kind, slot) DO UPDATE SET "
        "body = excluded.body, priority = excluded.priority, "
        "src_msg_id = excluded.src_msg_id, author = 'conductor', "
        "updated_at = excluded.updated_at",
        (day, task_id, kind, slot, body, priority, src_msg_id, ts),
    )
    evicted = _enforce_dashboard_caps(conn, day=day, task_id=task_id, kind=kind)
    return {
        "day": day,
        "task_id": task_id,
        "kind": kind,
        "slot": slot,
        "priority": priority,
        "updated_at": ts,
        "evicted": evicted,
    }


def dash_retract(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    kind: str,
    slot: str,
    writer_session: str | None = None,
    day: str | None = None,
    allow_test_override: bool = False,
) -> int:
    """Remove one daily dashboard item after the hard CONDUCTOR write guard."""
    ensure_dashboard_schema(conn)
    day = day or dash_today()
    if not _DASHBOARD_DAY_RE.fullmatch(day):
        raise ValueError(f"dashboard day must be YYYY-MM-DD, got {day!r}")
    assert_dashboard_conductor_writer(
        conn,
        writer_session=writer_session,
        day=day,
        allow_test_override=allow_test_override,
    )
    kind = normalize_dashboard_kind(kind)
    task_id = str(task_id or "").strip()
    slot = str(slot or "").strip()
    if not task_id or not slot:
        raise ValueError("dashboard task_id and slot required")
    cur = conn.execute(
        "DELETE FROM daily_dashboard "
        "WHERE day = ? AND task_id = ? AND kind = ? AND slot = ?",
        (day, task_id, kind, slot),
    )
    return cur.rowcount or 0


def get_daily_dashboard(
    conn: sqlite3.Connection,
    *,
    day: str | None = None,
) -> list[dict[str, Any]]:
    """Return one local-day dashboard projection; does not create schema."""
    day = day or dash_today()
    if not _DASHBOARD_DAY_RE.fullmatch(day):
        raise ValueError(f"dashboard day must be YYYY-MM-DD, got {day!r}")
    if not _sqlite_table_exists(conn, "daily_dashboard"):
        return []
    rows = conn.execute(
        "SELECT d.day, d.task_id, d.kind, d.slot, d.body, d.priority, "
        "d.src_msg_id, d.author, d.updated_at, "
        "t.title AS task_title, t.section AS task_section, "
        "t.status AS task_status, t.project AS task_project "
        "FROM daily_dashboard d "
        "LEFT JOIN tasks t ON t.id = d.task_id "
        "WHERE d.day = ? "
        "ORDER BY CASE WHEN t.section = 'today' THEN 0 ELSE 1 END, "
        "LOWER(COALESCE(t.title, d.task_id)), "
        "CASE d.kind WHEN 'decision' THEN 0 WHEN 'difficulty' THEN 1 "
        "WHEN 'misunderstanding' THEN 2 WHEN 'advice' THEN 3 "
        "WHEN 'option' THEN 4 WHEN 'result' THEN 5 ELSE 9 END, "
        "d.updated_at DESC, d.slot ASC",
        (day,),
    ).fetchall()
    return [dict(row) for row in rows]


def purge_old_dashboard_days(
    conn: sqlite3.Connection,
    *,
    today: str | None = None,
) -> int:
    """Delete dashboard rows older than today's local day."""
    if not _sqlite_table_exists(conn, "daily_dashboard"):
        return 0
    today = today or dash_today()
    cur = conn.execute("DELETE FROM daily_dashboard WHERE day < ?", (today,))
    return cur.rowcount or 0


def _new_event_id() -> str:
    return uuid.uuid4().hex


def _json_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    try:
        return json_dumps(value)
    except Exception:
        return str(value)


def _infer_event_origin() -> tuple[str, str]:
    for frame_info in inspect.stack()[2:]:
        module = inspect.getmodule(frame_info.frame)
        mod_name = module.__name__ if module else ""
        if mod_name.endswith("db_utils"):
            continue
        func_name = frame_info.function or "unknown"
        if mod_name:
            return f"{mod_name}.{func_name}", mod_name
        return func_name, ""
    return "system.unknown", "system"


def _next_logical_clock(
    conn: sqlite3.Connection,
    machine_id: str | None = None,
    *,
    floor: int | None = None,
    updated_at: str | None = None,
) -> int:
    """Return next HLC-style packed logical clock for the given device.

    The packed integer is globally comparable across machines because its high bits
    encode physical UTC milliseconds, while low bits encode an intra-millisecond
    counter. This keeps wall-time ordering for unrelated writes and still preserves
    causality when remote clocks have been observed locally.
    """
    mid = machine_id or MACHINE_ID
    now = updated_at or now_iso()
    now_ms = _iso_to_epoch_ms(now)
    floor_value = int(floor or 0)
    if not _sqlite_table_exists(conn, "memory_cursors"):
        physical_ms = max(now_ms, _decode_logical_clock(floor_value)[0])
        counter = 0
        if physical_ms == _decode_logical_clock(floor_value)[0]:
            counter = _decode_logical_clock(floor_value)[1] + 1
        return _pack_logical_clock(physical_ms, counter)
    row = conn.execute(
        "SELECT last_clock FROM memory_cursors WHERE machine_id = ?",
        (mid,),
    ).fetchone()
    # Positional access works whether the caller's connection has
    # row_factory=sqlite3.Row set or returns plain tuples.
    current = int(row[0]) if row else 0
    current_ms, current_counter = _decode_logical_clock(current)
    floor_ms, floor_counter = _decode_logical_clock(floor_value)
    physical_ms = max(now_ms, current_ms, floor_ms)
    if physical_ms == current_ms == floor_ms:
        counter = max(current_counter, floor_counter) + 1
    elif physical_ms == current_ms:
        counter = current_counter + 1
    elif physical_ms == floor_ms:
        counter = floor_counter + 1
    else:
        counter = 0
    next_clock = _pack_logical_clock(physical_ms, counter)
    conn.execute(
        "INSERT INTO memory_cursors (machine_id, last_clock, updated_at) "
        "VALUES (?, ?, ?) "
        "ON CONFLICT(machine_id) DO UPDATE SET "
        "last_clock = excluded.last_clock, updated_at = excluded.updated_at",
        (mid, next_clock, now),
    )
    return next_clock


def _observe_logical_clock(
    conn: sqlite3.Connection,
    floor: int,
    *,
    machine_id: str | None = None,
    updated_at: str | None = None,
) -> int:
    """Advance the local device cursor to at least the observed remote clock."""
    mid = machine_id or MACHINE_ID
    now = updated_at or now_iso()
    observed = int(floor or 0)
    if not _sqlite_table_exists(conn, "memory_cursors"):
        return observed
    row = conn.execute(
        "SELECT last_clock FROM memory_cursors WHERE machine_id = ?",
        (mid,),
    ).fetchone()
    # Positional access works whether the caller's connection has
    # row_factory=sqlite3.Row set or returns plain tuples.
    current = int(row[0]) if row else 0
    next_clock = max(current, observed)
    conn.execute(
        "INSERT INTO memory_cursors (machine_id, last_clock, updated_at) "
        "VALUES (?, ?, ?) "
        "ON CONFLICT(machine_id) DO UPDATE SET "
        "last_clock = excluded.last_clock, updated_at = excluded.updated_at",
        (mid, next_clock, now),
    )
    return next_clock


def _event_sort_key(
    event_ts: str | None,
    machine_id: str | None,
    logical_clock: int = 0,
) -> tuple[int, Any, str]:
    if int(logical_clock or 0) >= _HLC_PACKED_MIN:
        return (1, int(logical_clock), str(machine_id or ""))
    return (0, parse_iso_datetime_for_compare(event_ts), str(machine_id or ""))


def record_memory_event(
    conn: sqlite3.Connection,
    *,
    event_type: str,
    aggregate_kind: str,
    aggregate_id: str,
    field_name: str | None = None,
    actor_type: str = "system",
    actor_id: str | None = None,
    machine_id: str | None = None,
    tool_name: str | None = None,
    event_ts: str | None = None,
    old_value: Any = None,
    new_value: Any = None,
    payload: dict[str, Any] | list[Any] | None = None,
    parent_event_id: str | None = None,
    source_kind: str | None = None,
    source_ref: str | None = None,
    source_excerpt: str | None = None,
    source_start: int | None = None,
    source_end: int | None = None,
    logical_clock: int | None = None,
) -> dict[str, Any]:
    """Append immutable event to the local ledger and return its metadata."""
    if not _sqlite_table_exists(conn, "memory_events"):
        inferred_tool, inferred_actor = _infer_event_origin()
        return {
            "event_id": None,
            "logical_clock": int(logical_clock or 0),
            "machine_id": machine_id or MACHINE_ID,
            "tool_name": tool_name or inferred_tool,
            "actor_id": actor_id or inferred_actor,
        }

    inferred_tool, inferred_actor = _infer_event_origin()
    resolved_tool = tool_name or inferred_tool
    resolved_actor = actor_id or inferred_actor or MACHINE_ID
    mid = machine_id or MACHINE_ID
    ts = event_ts or now_iso()
    clock = int(logical_clock or _next_logical_clock(conn, mid, updated_at=ts))
    event_id = _new_event_id()

    conn.execute(
        "INSERT INTO memory_events ("
        "event_id, event_type, aggregate_kind, aggregate_id, field_name, "
        "actor_type, actor_id, machine_id, tool_name, logical_clock, event_ts, "
        "old_value, new_value, payload_json, parent_event_id, source_kind, "
        "source_ref, source_excerpt, source_start, source_end"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            event_id,
            event_type,
            aggregate_kind,
            aggregate_id,
            field_name,
            actor_type,
            resolved_actor,
            mid,
            resolved_tool,
            clock,
            ts,
            _json_text(old_value),
            _json_text(new_value),
            json_dumps(payload) if payload is not None else None,
            parent_event_id,
            source_kind,
            source_ref,
            source_excerpt,
            source_start,
            source_end,
        ),
    )
    return {
        "event_id": event_id,
        "logical_clock": clock,
        "machine_id": mid,
        "tool_name": resolved_tool,
        "actor_id": resolved_actor,
    }


def add_provenance_link(
    conn: sqlite3.Connection,
    *,
    subject_kind: str,
    subject_ref: str,
    source_kind: str,
    source_ref: str,
    span_start: int | None = None,
    span_end: int | None = None,
    excerpt: str | None = None,
    confidence: float = 1.0,
    created_at: str | None = None,
) -> str | None:
    if not _sqlite_table_exists(conn, "provenance_links"):
        return None
    row = conn.execute(
        "SELECT provenance_id FROM provenance_links "
        "WHERE subject_kind = ? AND subject_ref = ? AND source_kind = ? "
        "AND source_ref = ? AND COALESCE(span_start, -1) = COALESCE(?, -1) "
        "AND COALESCE(span_end, -1) = COALESCE(?, -1)",
        (subject_kind, subject_ref, source_kind, source_ref, span_start, span_end),
    ).fetchone()
    if row:
        return row["provenance_id"]
    prov_id = _new_event_id()
    conn.execute(
        "INSERT INTO provenance_links "
        "(provenance_id, subject_kind, subject_ref, source_kind, source_ref, "
        "span_start, span_end, excerpt, confidence, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            prov_id,
            subject_kind,
            subject_ref,
            source_kind,
            source_ref,
            span_start,
            span_end,
            excerpt,
            confidence,
            created_at or now_iso(),
        ),
    )
    return prov_id


def add_knowledge_link(
    conn: sqlite3.Connection,
    *,
    subject_kind: str,
    subject_ref: str,
    relation_type: str,
    object_kind: str,
    object_ref: str,
    rationale: str | None = None,
    active: bool = True,
    created_at: str | None = None,
) -> str | None:
    if not _sqlite_table_exists(conn, "knowledge_links"):
        return None
    row = conn.execute(
        "SELECT link_id FROM knowledge_links "
        "WHERE subject_kind = ? AND subject_ref = ? AND relation_type = ? "
        "AND object_kind = ? AND object_ref = ? AND active = ?",
        (
            subject_kind,
            subject_ref,
            relation_type,
            object_kind,
            object_ref,
            1 if active else 0,
        ),
    ).fetchone()
    if row:
        return row["link_id"]
    link_id = _new_event_id()
    conn.execute(
        "INSERT INTO knowledge_links "
        "(link_id, subject_kind, subject_ref, relation_type, object_kind, object_ref, "
        "rationale, created_at, active) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            link_id,
            subject_kind,
            subject_ref,
            relation_type,
            object_kind,
            object_ref,
            rationale,
            created_at or now_iso(),
            1 if active else 0,
        ),
    )
    return link_id


def effective_fact_confidence(
    confidence: float,
    *,
    updated_at: str | None = None,
    contradiction_count: int = 0,
    half_life_days: int = 180,
) -> float:
    """Decay stale facts slowly and penalize unresolved contradictions."""
    value = max(0.0, min(1.0, float(confidence)))
    if updated_at:
        try:
            age_days = max(
                0.0,
                (
                    datetime.now(timezone.utc) - datetime.fromisoformat(updated_at)
                ).total_seconds()
                / 86400.0,
            )
            value *= 0.5 ** (age_days / max(1, half_life_days))
        except (TypeError, ValueError):
            pass
    if contradiction_count > 0:
        value *= max(0.2, 1.0 - (0.2 * contradiction_count))
    return max(0.0, min(1.0, value))


def upsert_memory_artifact(
    conn: sqlite3.Connection,
    *,
    artifact_kind: str,
    scope_kind: str,
    scope_ref: str,
    body: str,
    artifact_key: str | None = None,
    title: str | None = None,
    confidence: float = 1.0,
    status: str = "active",
    valid_from: str | None = None,
    valid_to: str | None = None,
    source_event_id: str | None = None,
    created_at: str | None = None,
    updated_at: str | None = None,
    provenance: list[dict[str, Any]] | None = None,
    tool_name: str | None = None,
    emit_event: bool = True,
) -> dict[str, Any]:
    if not _sqlite_table_exists(conn, "memory_artifacts"):
        return {"artifact_id": None, "artifact_key": None, "changed": False}
    now = updated_at or now_iso()
    key = artifact_key or f"{artifact_kind}:{scope_kind}:{scope_ref}"
    row = conn.execute(
        "SELECT artifact_id, title, body, confidence, status, valid_from, valid_to, source_event_id "
        "FROM memory_artifacts WHERE artifact_key = ?",
        (key,),
    ).fetchone()
    if row is None:
        artifact_id = _new_event_id()
        conn.execute(
            "INSERT INTO memory_artifacts ("
            "artifact_id, artifact_key, artifact_kind, scope_kind, scope_ref, title, body, "
            "confidence, status, valid_from, valid_to, source_event_id, created_at, updated_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                artifact_id,
                key,
                artifact_kind,
                scope_kind,
                scope_ref,
                title,
                body,
                confidence,
                status,
                valid_from,
                valid_to,
                source_event_id,
                created_at or now,
                now,
            ),
        )
        changed = True
    else:
        artifact_id = row["artifact_id"]
        changed = any(
            (
                row["title"] != title,
                row["body"] != body,
                float(row["confidence"] or 0.0) != float(confidence),
                row["status"] != status,
                row["valid_from"] != valid_from,
                row["valid_to"] != valid_to,
                row["source_event_id"] != source_event_id,
            )
        )
        if changed:
            conn.execute(
                "UPDATE memory_artifacts SET title = ?, body = ?, confidence = ?, "
                "status = ?, valid_from = ?, valid_to = ?, source_event_id = ?, updated_at = ? "
                "WHERE artifact_id = ?",
                (
                    title,
                    body,
                    confidence,
                    status,
                    valid_from,
                    valid_to,
                    source_event_id,
                    now,
                    artifact_id,
                ),
            )

    for prov in provenance or []:
        add_provenance_link(
            conn,
            subject_kind="artifact",
            subject_ref=artifact_id,
            source_kind=str(prov.get("source_kind") or "artifact"),
            source_ref=str(prov.get("source_ref") or scope_ref),
            span_start=prov.get("span_start"),
            span_end=prov.get("span_end"),
            excerpt=prov.get("excerpt"),
            confidence=float(prov.get("confidence", 1.0) or 1.0),
            created_at=now,
        )

    if emit_event and changed:
        record_memory_event(
            conn,
            event_type="artifact_upserted",
            aggregate_kind="artifact",
            aggregate_id=artifact_id,
            tool_name=tool_name or "db_utils.upsert_memory_artifact",
            event_ts=now,
            old_value=dict(row) if row is not None else None,
            new_value={
                "artifact_kind": artifact_kind,
                "scope_kind": scope_kind,
                "scope_ref": scope_ref,
                "title": title,
                "status": status,
            },
            payload={
                "artifact_key": key,
                "artifact_kind": artifact_kind,
                "scope_kind": scope_kind,
                "scope_ref": scope_ref,
            },
            source_kind=scope_kind,
            source_ref=scope_ref,
            source_excerpt=title or body[:200],
            parent_event_id=source_event_id,
        )

    return {
        "artifact_id": artifact_id,
        "artifact_key": key,
        "changed": changed,
    }


def record_memory_conflict(
    conn: sqlite3.Connection,
    *,
    aggregate_kind: str,
    aggregate_id: str,
    winner: str,
    field_name: str | None = None,
    local_value: Any = None,
    remote_value: Any = None,
    local_updated_at: str | None = None,
    remote_updated_at: str | None = None,
    local_updated_order: int = 0,
    remote_updated_order: int = 0,
    local_source_event_id: str | None = None,
    remote_source_event_id: str | None = None,
    rationale: str | None = None,
    created_at: str | None = None,
) -> str | None:
    if not _sqlite_table_exists(conn, "memory_conflicts"):
        return None
    now = created_at or now_iso()
    # A winner means the conflict is auto-decided (terminal), not pending human
    # review — record it resolved so the ledger self-limits and the audit's
    # status='open' query surfaces only genuinely undecided conflicts.
    _decided = bool(winner and str(winner).strip())
    conflict_status = "resolved" if _decided else "open"
    conflict_resolved_at = now if _decided else None
    key_payload = {
        "aggregate_kind": aggregate_kind,
        "aggregate_id": aggregate_id,
        "field_name": field_name,
        "local_source_event_id": local_source_event_id,
        "remote_source_event_id": remote_source_event_id,
        "local_updated_at": local_updated_at,
        "remote_updated_at": remote_updated_at,
        "winner": winner,
        "local_value": _json_text(local_value),
        "remote_value": _json_text(remote_value),
    }
    conflict_key = hashlib.sha256(
        json_dumps(key_payload, sort_keys=True).encode("utf-8")
    ).hexdigest()
    row = conn.execute(
        "SELECT conflict_id FROM memory_conflicts WHERE conflict_key = ?",
        (conflict_key,),
    ).fetchone()
    local_text = _json_text(local_value)
    remote_text = _json_text(remote_value)
    if row is None:
        conflict_id = _new_event_id()
        conn.execute(
            "INSERT INTO memory_conflicts ("
            "conflict_id, conflict_key, aggregate_kind, aggregate_id, field_name, "
            "local_value, remote_value, local_updated_at, remote_updated_at, "
            "local_updated_order, remote_updated_order, local_source_event_id, "
            "remote_source_event_id, winner, status, rationale, created_at, updated_at, resolved_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                conflict_id,
                conflict_key,
                aggregate_kind,
                aggregate_id,
                field_name,
                local_text,
                remote_text,
                local_updated_at,
                remote_updated_at,
                int(local_updated_order or 0),
                int(remote_updated_order or 0),
                local_source_event_id,
                remote_source_event_id,
                winner,
                conflict_status,
                rationale,
                now,
                now,
                conflict_resolved_at,
            ),
        )
    else:
        conflict_id = row["conflict_id"]
        conn.execute(
            "UPDATE memory_conflicts SET local_value = ?, remote_value = ?, "
            "local_updated_at = ?, remote_updated_at = ?, local_updated_order = ?, "
            "remote_updated_order = ?, local_source_event_id = ?, remote_source_event_id = ?, "
            "winner = ?, status = ?, rationale = ?, updated_at = ?, resolved_at = ? "
            "WHERE conflict_id = ?",
            (
                local_text,
                remote_text,
                local_updated_at,
                remote_updated_at,
                int(local_updated_order or 0),
                int(remote_updated_order or 0),
                local_source_event_id,
                remote_source_event_id,
                winner,
                conflict_status,
                rationale,
                now,
                conflict_resolved_at,
                conflict_id,
            ),
        )
    return conflict_id


CONFLICT_LEDGER_RETENTION_DAYS = 30


def prune_memory_conflicts(
    conn: sqlite3.Connection, *, retention_days: int = CONFLICT_LEDGER_RETENTION_DAYS
) -> tuple[int, int]:
    """Keep the conflict ledger bounded so bridge sync never bloats.

    Backfill: a conflict with a winner is auto-decided (terminal), not pending
    review, so mark any lingering 'open' such rows resolved. Prune: drop
    resolved rows older than the retention window. Date-only comparison avoids
    the ISO 'T'/timezone-suffix mismatch that breaks a raw datetime() compare.
    Returns (backfilled, pruned). Caller owns the transaction/commit.
    """
    if not _sqlite_table_exists(conn, "memory_conflicts"):
        return (0, 0)
    backfilled = conn.execute(
        "UPDATE memory_conflicts SET status = 'resolved', "
        "resolved_at = COALESCE(resolved_at, updated_at) "
        "WHERE status = 'open' AND winner IS NOT NULL AND TRIM(winner) <> ''"
    ).rowcount
    pruned = conn.execute(
        "DELETE FROM memory_conflicts WHERE status = 'resolved' "
        "AND substr(updated_at, 1, 10) < date('now', ?)",
        (f"-{int(retention_days)} days",),
    ).rowcount
    return (backfilled, pruned)


def promote_pending_public_entities(
    conn: sqlite3.Connection,
    cutoff_ts: str,
    updated_at: str | None = None,
) -> int:
    """Promote pending_public entities to public. Returns count promoted."""
    ts = updated_at or now_iso()
    cur = conn.execute(
        "UPDATE entities SET visibility='public', updated_at = ? "
        "WHERE visibility='pending_public' AND publish_requested_at <= ?",
        (ts, cutoff_ts),
    )
    return cur.rowcount


def bridge_change_summary(
    conn: sqlite3.Connection,
    since_ts: str,
    publish_cutoff_ts: str | None = None,
) -> dict[str, int]:
    """Return bridge-relevant changes since a sync watermark.

    Includes relation/rating churn and pending_public items whose standby window
    has elapsed, so incremental push logic does not skip exportable changes.
    """
    cutoff = (
        publish_cutoff_ts
        or (
            datetime.now(timezone.utc) - timedelta(minutes=PUBLISH_STANDBY_MINUTES)
        ).isoformat()
    )

    values: dict[str, int] = {
        "changed_tasks": 0,
        "changed_entities": 0,
        "changed_relations": 0,
        "changed_ratings": 0,
        "changed_chunks": 0,
        "changed_annotations": 0,
        "changed_questions": 0,
        "changed_claims": 0,
        "changed_facts": 0,
        "changed_provenance": 0,
        "changed_truth_links": 0,
        "changed_events": 0,
        "changed_audit_issues": 0,
        "ready_public_entities": 0,
        "ready_public_tasks": 0,
    }

    if _sqlite_table_exists(conn, "tasks"):
        values["changed_tasks"] = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE updated_at > ?",
            (since_ts,),
        ).fetchone()[0]
        values["ready_public_tasks"] = conn.execute(
            "SELECT COUNT(*) FROM tasks "
            "WHERE visibility = 'pending_public' AND publish_requested_at <= ?",
            (cutoff,),
        ).fetchone()[0]

    if _sqlite_table_exists(conn, "entities"):
        values["changed_entities"] = conn.execute(
            "SELECT COUNT(*) FROM entities WHERE updated_at > ?",
            (since_ts,),
        ).fetchone()[0]
        values["ready_public_entities"] = conn.execute(
            "SELECT COUNT(*) FROM entities "
            "WHERE visibility = 'pending_public' AND publish_requested_at <= ?",
            (cutoff,),
        ).fetchone()[0]

    if _sqlite_table_exists(conn, "relations"):
        values["changed_relations"] = conn.execute(
            "SELECT COUNT(*) FROM relations WHERE created_at > ?",
            (since_ts,),
        ).fetchone()[0]

    if _sqlite_table_exists(conn, "knowledge_ratings"):
        values["changed_ratings"] = conn.execute(
            "SELECT COUNT(*) FROM knowledge_ratings WHERE rated_at > ?",
            (since_ts,),
        ).fetchone()[0]

    if _sqlite_table_exists(conn, "context_chunks"):
        values["changed_chunks"] = conn.execute(
            "SELECT COUNT(*) FROM context_chunks WHERE updated_at > ?",
            (since_ts,),
        ).fetchone()[0]

    if _sqlite_table_exists(conn, "context_annotations"):
        values["changed_annotations"] = conn.execute(
            "SELECT COUNT(*) FROM context_annotations WHERE created_at > ?",
            (since_ts,),
        ).fetchone()[0]

    if _sqlite_table_exists(conn, "context_questions"):
        values["changed_questions"] = conn.execute(
            "SELECT COUNT(*) FROM context_questions "
            "WHERE COALESCE(answered_at, created_at) > ?",
            (since_ts,),
        ).fetchone()[0]

    if _sqlite_table_exists(conn, "candidate_claims"):
        values["changed_claims"] = conn.execute(
            "SELECT COUNT(*) FROM candidate_claims WHERE updated_at > ?",
            (since_ts,),
        ).fetchone()[0]

    if _sqlite_table_exists(conn, "canonical_facts"):
        values["changed_facts"] = conn.execute(
            "SELECT COUNT(*) FROM canonical_facts WHERE updated_at > ?",
            (since_ts,),
        ).fetchone()[0]

    if _sqlite_table_exists(conn, "provenance_links"):
        values["changed_provenance"] = conn.execute(
            "SELECT COUNT(*) FROM provenance_links WHERE created_at > ?",
            (since_ts,),
        ).fetchone()[0]

    if _sqlite_table_exists(conn, "knowledge_links"):
        values["changed_truth_links"] = conn.execute(
            "SELECT COUNT(*) FROM knowledge_links WHERE created_at > ?",
            (since_ts,),
        ).fetchone()[0]

    if _sqlite_table_exists(conn, "memory_events"):
        values["changed_events"] = conn.execute(
            "SELECT COUNT(*) FROM memory_events WHERE event_ts > ?",
            (since_ts,),
        ).fetchone()[0]

    if _sqlite_table_exists(conn, "memory_audit_issues"):
        values["changed_audit_issues"] = conn.execute(
            "SELECT COUNT(*) FROM memory_audit_issues WHERE last_detected_at > ?",
            (since_ts,),
        ).fetchone()[0]

    return values


def bridge_has_changes(
    conn: sqlite3.Connection,
    since_ts: str,
    publish_cutoff_ts: str | None = None,
) -> bool:
    return any(bridge_change_summary(conn, since_ts, publish_cutoff_ts).values())


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


def _timestamp_is_newer(candidate_ts: str | None, baseline_ts: str | None) -> bool:
    return parse_iso_datetime_for_compare(
        candidate_ts
    ) > parse_iso_datetime_for_compare(baseline_ts)


def _max_iso_timestamp(*timestamps: str | None) -> str:
    """Return the newest non-empty timestamp string using normalized ISO ordering."""
    best = ""
    best_dt = datetime.min.replace(tzinfo=timezone.utc)
    for ts in timestamps:
        raw = (ts or "").strip()
        if not raw:
            continue
        dt = parse_iso_datetime_for_compare(raw)
        if dt > best_dt:
            best = raw
            best_dt = dt
    return best


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
        project = normalize_project_name(project)
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
        fields = dict(fields)
        if "project" in fields:
            fields["project"] = normalize_project_name(fields.get("project"))
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
    def get_ready_review_candidates(
        conn: sqlite3.Connection,
        columns: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """Return closed rows explicitly marked for ready-context review."""
        cols = columns or TaskDAO.ALL_COLS
        exclusions = ",".join("?" * len(TASK_ACTIVE_EXCLUSIONS))
        markers = (
            "cleanup_candidate",
            "done_but_recently_confused",
            "reopen_requested_by_user",
            "reopen",
            "superseded",
            "duplicate",
        )
        marker_sql = " OR ".join(["instr(task_text, ?) > 0"] * len(markers))
        rows = conn.execute(
            f"SELECT {cols} FROM ("
            f"SELECT {cols}, lower("
            "coalesce(title, '') || ' ' || "
            "coalesce(description, '') || ' ' || "
            "coalesce(notes, '')"
            ") AS task_text "
            "FROM tasks "
            f"WHERE status IN ({exclusions})"
            f") WHERE {marker_sql} "
            "ORDER BY updated_at DESC LIMIT ?",
            list(TASK_ACTIVE_EXCLUSIONS) + list(markers) + [max(1, int(limit))],
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
            for task_id in ids:
                apply_task_mutation(
                    conn,
                    task_id,
                    {"status": "archived"},
                    timestamp=now,
                    tool_name="db_utils.TaskDAO.archive_done",
                )
        return ids

    @staticmethod
    def promote_pending_public(
        conn: sqlite3.Connection,
        cutoff_ts: str,
        updated_at: str | None = None,
    ) -> int:
        """Promote pending_public tasks to public. Returns count promoted."""
        ts = updated_at or now_iso()
        rows = conn.execute(
            "SELECT id FROM tasks WHERE visibility = 'pending_public' "
            "AND publish_requested_at <= ?",
            (cutoff_ts,),
        ).fetchall()
        promoted = 0
        for row in rows:
            result = apply_task_mutation(
                conn,
                row["id"],
                {"visibility": "public"},
                timestamp=ts,
                tool_name="db_utils.TaskDAO.promote_pending_public",
            )
            promoted += int(result.get("updated", 0))
        return promoted

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

    @staticmethod
    def get_attachments(
        conn: sqlite3.Connection,
        task_id: str,
        *,
        include_removed: bool = False,
    ) -> list[dict]:
        """Return attachment metadata for a task."""
        if not _sqlite_table_exists(conn, "task_attachments"):
            return []
        sql = (
            "SELECT attachment_id, task_id, file_name, stored_relpath, media_type, "
            "file_size, status, created_at, updated_at "
            "FROM task_attachments WHERE task_id = ?"
        )
        params: list[Any] = [task_id]
        if not include_removed:
            sql += " AND status = 'active'"
        sql += " ORDER BY created_at, attachment_id"
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def get_attachment(conn: sqlite3.Connection, attachment_id: str) -> dict | None:
        """Return attachment metadata by id."""
        if not _sqlite_table_exists(conn, "task_attachments"):
            return None
        row = conn.execute(
            "SELECT attachment_id, task_id, file_name, stored_relpath, media_type, "
            "file_size, status, created_at, updated_at "
            "FROM task_attachments WHERE attachment_id = ?",
            (attachment_id,),
        ).fetchone()
        return dict(row) if row else None

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
    def purge_done(
        conn: sqlite3.Connection,
        cutoff_iso: str,
        hard_delete_before_iso: str | None = None,
    ) -> int:
        """Retire old done tasks in a bridge-visible, two-tier way.

        Tier 1 (tombstone): done tasks older than ``cutoff_iso`` are transitioned
        to ``archived`` via ``apply_task_mutation`` rather than hard-deleted. This
        bumps ``updated_at`` and writes a status field version, so the change is
        visible to ``bridge_change_summary`` (incremental sync no longer skips it)
        and the next full export emits a proper tombstone AND removes the stale
        per-task bridge file. A bare ``DELETE`` was invisible to both, leaving
        stale files behind on automated sync and resurrecting deletions on peers.

        Tier 2 (hard purge): push-aware. Only ``archived`` rows whose tombstone
        has been *successfully pushed* (``tombstone_pushed_at IS NOT NULL``) AND
        whose push predates ``hard_delete_before_iso`` (default: now −
        ``_TOMBSTONE_DAYS``) are hard-deleted. Retention is measured FROM the
        push, not from ``updated_at``: a tombstone created offline and pushed only
        weeks later still gets a full retention window after the push for every
        peer to pull it before the row disappears locally.

        Rows with ``tombstone_pushed_at IS NULL`` are NEVER hard-deleted — they
        have not provably reached the bridge, so deleting them could let a peer
        still holding the old ``done`` row resurrect the task (the 2026-05-08
        "12 tasks resurrected" incident class). The export window
        (``export_task_files`` / ``_export_index_json``) is the exact complement
        of this gate, so an un-pushed tombstone stays exportable until a
        successful push stamps it, and only then can it age out of either side.

        Only ``archived`` is swept (Tier 1 and ``archive_done`` only ever produce
        ``archived``); ``cancelled`` is the user soft-delete state and is
        intentionally left untouched to avoid silently destroying user data.

        Returns the count of done tasks retired (Tier 1) this cycle, preserving the
        historical "rows purged from the active view" meaning of the return value.
        """
        rows = conn.execute(
            "SELECT id FROM tasks WHERE status = 'done' AND type = 'task' "
            "AND updated_at < ?",
            (cutoff_iso,),
        ).fetchall()
        ids = [r["id"] for r in rows]
        retired = 0
        if ids:
            now = now_iso()
            for task_id in ids:
                result = apply_task_mutation(
                    conn,
                    task_id,
                    {"status": "archived"},
                    timestamp=now,
                    tool_name="db_utils.TaskDAO.purge_done",
                )
                retired += int(result.get("updated", 0))

        # Tier 2: hard-purge archived tombstones that have been pushed AND aged
        # past the retention window measured from the push. Un-pushed tombstones
        # (tombstone_pushed_at IS NULL) are retained indefinitely so a deletion is
        # never destroyed before it provably reaches the bridge. 'cancelled' (user
        # soft-delete) is deliberately excluded.
        hard_cutoff = (
            hard_delete_before_iso
            or (
                datetime.now(timezone.utc) - timedelta(days=_TOMBSTONE_DAYS)
            ).isoformat()
        )
        conn.execute(
            "DELETE FROM tasks WHERE type = 'task' "
            "AND status = 'archived' "
            "AND tombstone_pushed_at IS NOT NULL AND tombstone_pushed_at < ? "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM tasks child WHERE child.parent_id = tasks.id"
            ")",
            (hard_cutoff,),
        )
        return retired

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
        """Visible open notes (type='note'), excluding closed statuses."""
        pri_sql = build_priority_order_sql()
        exclusions = ",".join("?" * len(TASK_ACTIVE_EXCLUSIONS))
        rows = conn.execute(
            f"SELECT {TaskDAO.ALL_COLS} FROM tasks WHERE type = 'note' "
            f"AND status NOT IN ({exclusions}) "
            f"ORDER BY {pri_sql}, updated_at DESC",
            list(TASK_ACTIVE_EXCLUSIONS),
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
        aggregated: dict[str, int] = {}
        for row in rows:
            canonical = normalize_project_name(row["project"])
            if not canonical:
                continue
            aggregated[canonical] = aggregated.get(canonical, 0) + int(row["cnt"] or 0)
        return [
            name
            for name, _count in sorted(
                aggregated.items(), key=lambda item: (-item[1], item[0].casefold())
            )
        ]

    @staticmethod
    def promote_due_today(conn: sqlite3.Connection) -> int:
        """Auto-move tasks with due_date <= today from inbox/next to today."""
        rows = conn.execute(
            "SELECT id FROM tasks "
            "WHERE due_date <= date('now') AND section IN ('inbox', 'next') "
            "AND status NOT IN ('done', 'archived', 'cancelled') AND type = 'task'"
        ).fetchall()
        if not rows:
            return 0
        ts = now_iso()
        moved = 0
        for row in rows:
            result = apply_task_mutation(
                conn,
                row["id"],
                {"section": "today"},
                timestamp=ts,
                tool_name="db_utils.TaskDAO.promote_due_today",
            )
            moved += int(result.get("updated", 0))
        return moved


# ── Task attachments ─────────────────────────────────────────────────────


def _touch_task_updated_at(
    conn: sqlite3.Connection,
    task_id: str,
    timestamp: str,
) -> None:
    """Bump task.updated_at when related attachment metadata changes."""
    conn.execute("UPDATE tasks SET updated_at = ? WHERE id = ?", (timestamp, task_id))


def upsert_task_attachment_metadata(
    conn: sqlite3.Connection,
    *,
    attachment_id: str,
    task_id: str,
    file_name: str,
    stored_relpath: str,
    media_type: str | None,
    file_size: int,
    status: str = "active",
    created_at: str | None = None,
    updated_at: str | None = None,
) -> dict[str, Any]:
    """Insert or update a task attachment metadata row."""
    if not _sqlite_table_exists(conn, "task_attachments"):
        return {"attachment_id": attachment_id, "changed": False}
    now = updated_at or now_iso()
    row = conn.execute(
        "SELECT file_name, stored_relpath, media_type, file_size, status, created_at, updated_at "
        "FROM task_attachments WHERE attachment_id = ?",
        (attachment_id,),
    ).fetchone()
    file_size_int = int(file_size or 0)
    if row is None:
        conn.execute(
            "INSERT INTO task_attachments "
            "(attachment_id, task_id, file_name, stored_relpath, media_type, file_size, "
            "status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                attachment_id,
                task_id,
                file_name,
                stored_relpath,
                media_type,
                file_size_int,
                status,
                created_at or now,
                now,
            ),
        )
        return {"attachment_id": attachment_id, "changed": True, "created": True}

    changed = any(
        (
            row["file_name"] != file_name,
            row["stored_relpath"] != stored_relpath,
            row["media_type"] != media_type,
            int(row["file_size"] or 0) != file_size_int,
            row["status"] != status,
        )
    )
    if changed:
        conn.execute(
            "UPDATE task_attachments SET file_name = ?, stored_relpath = ?, media_type = ?, "
            "file_size = ?, status = ?, updated_at = ? WHERE attachment_id = ?",
            (
                file_name,
                stored_relpath,
                media_type,
                file_size_int,
                status,
                now,
                attachment_id,
            ),
        )
    return {"attachment_id": attachment_id, "changed": changed, "created": False}


def resolve_task_attachment_path(
    attachment: dict[str, Any] | sqlite3.Row,
    *,
    local_root: str | None = None,
    bridge_repo: str | None = None,
) -> str | None:
    """Resolve the best available on-disk path for an attachment."""
    stored_relpath = (dict(attachment).get("stored_relpath") or "").strip()
    if not stored_relpath:
        return None
    candidates = []
    try:
        candidates.append(_local_attachment_path(stored_relpath, local_root))
    except ValueError:
        return None
    try:
        candidates.append(
            _bridge_attachment_path(stored_relpath, bridge_repo or BRIDGE_REPO)
        )
    except ValueError:
        return None
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return str(candidates[0])


def add_task_attachment(
    conn: sqlite3.Connection,
    task_id: str,
    source_path: str,
    *,
    tool_name: str | None = None,
    local_root: str | None = None,
) -> dict[str, Any]:
    """Copy a file into managed attachment storage and attach it to a task."""
    row = conn.execute("SELECT id FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        raise ValueError(f"Task {task_id} not found")
    src = Path(source_path).expanduser().resolve()
    if not src.exists() or not src.is_file():
        raise FileNotFoundError(source_path)

    ts = now_iso()
    attachment_id = str(uuid.uuid4())
    file_name = src.name
    stored_relpath = _task_attachment_relpath(task_id, attachment_id, file_name)
    dst = _local_attachment_path(stored_relpath, local_root)
    _copy_attachment_file(src, dst)
    media_type = mimetypes.guess_type(file_name)[0]
    file_size = dst.stat().st_size
    upsert_task_attachment_metadata(
        conn,
        attachment_id=attachment_id,
        task_id=task_id,
        file_name=file_name,
        stored_relpath=stored_relpath,
        media_type=media_type,
        file_size=file_size,
        status="active",
        created_at=ts,
        updated_at=ts,
    )
    _touch_task_updated_at(conn, task_id, ts)
    record_memory_event(
        conn,
        event_type="attachment_add",
        aggregate_kind="task_attachment",
        aggregate_id=attachment_id,
        tool_name=tool_name or "db_utils.add_task_attachment",
        event_ts=ts,
        new_value={
            "task_id": task_id,
            "file_name": file_name,
            "stored_relpath": stored_relpath,
            "file_size": file_size,
        },
        payload={
            "task_id": task_id,
            "attachment_id": attachment_id,
            "file_name": file_name,
            "stored_relpath": stored_relpath,
            "file_size": file_size,
            "media_type": media_type,
        },
        source_kind="file",
        source_ref=str(src),
        source_excerpt=file_name,
    )
    return TaskDAO.get_attachment(conn, attachment_id) or {
        "attachment_id": attachment_id,
        "task_id": task_id,
        "file_name": file_name,
        "stored_relpath": stored_relpath,
        "media_type": media_type,
        "file_size": file_size,
        "status": "active",
        "created_at": ts,
        "updated_at": ts,
    }


def remove_task_attachment(
    conn: sqlite3.Connection,
    attachment_id: str,
    *,
    tool_name: str | None = None,
    local_root: str | None = None,
) -> bool:
    """Soft-remove an attachment metadata row and delete the local managed copy."""
    attachment = TaskDAO.get_attachment(conn, attachment_id)
    if not attachment:
        return False
    if attachment.get("status") == "removed":
        return False
    ts = now_iso()
    conn.execute(
        "UPDATE task_attachments SET status = 'removed', updated_at = ? WHERE attachment_id = ?",
        (ts, attachment_id),
    )
    try:
        local_path = _local_attachment_path(attachment["stored_relpath"], local_root)
    except ValueError:
        local_path = None
    if local_path and local_path.exists():
        try:
            local_path.unlink()
        except OSError:
            pass
    _touch_task_updated_at(conn, attachment["task_id"], ts)
    record_memory_event(
        conn,
        event_type="attachment_remove",
        aggregate_kind="task_attachment",
        aggregate_id=attachment_id,
        tool_name=tool_name or "db_utils.remove_task_attachment",
        event_ts=ts,
        old_value=attachment,
        new_value={"status": "removed"},
        payload={
            "task_id": attachment["task_id"],
            "attachment_id": attachment_id,
            "stored_relpath": attachment["stored_relpath"],
        },
        source_kind="task",
        source_ref=attachment["task_id"],
        source_excerpt=attachment["file_name"],
    )
    return True


def sync_task_attachments_from_remote(
    conn: sqlite3.Connection,
    remote_tasks: list[dict[str, Any]],
    bridge_dir: str,
    *,
    local_root: str | None = None,
) -> tuple[int, int]:
    """Merge attachment metadata/files from remote hydrated task payloads."""
    if not _sqlite_table_exists(conn, "task_attachments"):
        return (0, 0)
    imported = 0
    removed = 0
    local_root_dir = local_root or TASK_ATTACHMENT_ROOT
    for task in remote_tasks:
        task_id = task.get("id")
        if not task_id:
            continue
        exists = conn.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if exists is None:
            continue
        for attachment in task.get("_attachments", []) or []:
            attachment_id = attachment.get("attachment_id")
            stored_relpath = attachment.get("stored_relpath")
            file_name = attachment.get("file_name")
            if not attachment_id or not stored_relpath or not file_name:
                continue
            status = attachment.get("status") or "active"
            result = upsert_task_attachment_metadata(
                conn,
                attachment_id=attachment_id,
                task_id=task_id,
                file_name=file_name,
                stored_relpath=stored_relpath,
                media_type=attachment.get("media_type"),
                file_size=int(attachment.get("file_size") or 0),
                status=status,
                created_at=attachment.get("created_at"),
                updated_at=attachment.get("updated_at"),
            )
            if result.get("changed"):
                if status == "removed":
                    removed += 1
                else:
                    imported += 1
            if status == "active":
                try:
                    remote_path = _bridge_attachment_path(stored_relpath, bridge_dir)
                    local_path = _local_attachment_path(stored_relpath, local_root_dir)
                except ValueError:
                    continue
                if remote_path.exists():
                    if not local_path.exists() or local_path.stat().st_size != int(
                        attachment.get("file_size") or remote_path.stat().st_size
                    ):
                        _copy_attachment_file(remote_path, local_path)
            else:
                try:
                    local_path = _local_attachment_path(stored_relpath, local_root_dir)
                except ValueError:
                    continue
                if local_path.exists():
                    try:
                        local_path.unlink()
                    except OSError:
                        pass
    return (imported, removed)


def normalize_task_projects(
    conn: sqlite3.Connection,
    *,
    tool_name: str = "db_utils.normalize_task_projects",
    actor_type: str = "system",
    actor_id: str | None = None,
) -> list[dict[str, str]]:
    """Canonicalize existing task project values via the authoritative mutation path."""
    rows = conn.execute(
        "SELECT id, project FROM tasks WHERE project IS NOT NULL AND TRIM(project) <> '' "
        "ORDER BY created_at, id"
    ).fetchall()
    changed: list[dict[str, str]] = []
    for row in rows:
        old_project = row["project"]
        new_project = normalize_project_name(old_project)
        if new_project == old_project:
            continue
        result = apply_task_mutation(
            conn,
            row["id"],
            {"project": new_project},
            tool_name=tool_name,
            actor_type=actor_type,
            actor_id=actor_id,
            source_kind="task",
            source_ref=row["id"],
        )
        if result.get("updated"):
            changed.append(
                {
                    "task_id": row["id"],
                    "old_project": old_project,
                    "new_project": new_project or "",
                }
            )
    return changed


# ── Authoritative task mutation helpers ─────────────────────────────────


def _task_initial_values(
    *,
    title: str,
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
) -> dict[str, Any]:
    project = normalize_project_name(project)
    values = {field: None for field in MERGEABLE_FIELDS}
    values.update(
        {
            "title": title,
            "description": description,
            "status": status,
            "priority": priority,
            "section": section,
            "due_date": due_date,
            "project": project,
            "parent_id": parent_id,
            "notes": notes,
            "recurring": recurring,
            "reminder_at": reminder_at,
            "type": type,
            "assignee": assignee,
            "shared_by": shared_by,
            "visibility": visibility,
            "publish_requested_at": publish_requested_at,
        }
    )
    return values


def create_task_with_ledger(
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
    tool_name: str | None = None,
    actor_type: str = "system",
    actor_id: str | None = None,
    source_kind: str = "task",
    source_ref: str | None = None,
    provenance_map: dict[str, dict[str, Any]] | None = None,
) -> None:
    """Create a task row and seed authoritative field/event history in one call."""
    project = normalize_project_name(project)
    TaskDAO.create(
        conn,
        task_id,
        title,
        now,
        description=description,
        status=status,
        priority=priority,
        section=section,
        due_date=due_date,
        project=project,
        parent_id=parent_id,
        notes=notes,
        recurring=recurring,
        reminder_at=reminder_at,
        type=type,
        assignee=assignee,
        shared_by=shared_by,
        visibility=visibility,
        publish_requested_at=publish_requested_at,
        created_at=created_at,
    )
    upsert_field_versions(
        conn,
        task_id,
        MERGEABLE_FIELDS,
        now,
        old_values={},
        new_values=_task_initial_values(
            title=title,
            description=description,
            status=status,
            priority=priority,
            section=section,
            due_date=due_date,
            project=project,
            parent_id=parent_id,
            notes=notes,
            recurring=recurring,
            reminder_at=reminder_at,
            type=type,
            assignee=assignee,
            shared_by=shared_by,
            visibility=visibility,
            publish_requested_at=publish_requested_at,
        ),
        tool_name=tool_name,
        actor_type=actor_type,
        actor_id=actor_id,
        source_kind=source_kind,
        source_ref=source_ref or task_id,
        provenance_map=provenance_map,
    )


def mark_tasks_done_by_title(
    conn: sqlite3.Connection,
    title_query: str,
    *,
    timestamp: str | None = None,
    tool_name: str | None = None,
    actor_type: str = "system",
    actor_id: str | None = None,
) -> int:
    """Mark all non-done tasks matching a title substring via the mutation API."""
    rows = conn.execute(
        "SELECT id FROM tasks WHERE title LIKE ? AND status != 'done'",
        (f"%{title_query}%",),
    ).fetchall()
    if not rows:
        return 0
    ts = timestamp or now_iso()
    marked = 0
    for row in rows:
        result = apply_task_mutation(
            conn,
            row["id"],
            {"status": "done", "section": "done"},
            timestamp=ts,
            tool_name=tool_name or "db_utils.mark_tasks_done_by_title",
            actor_type=actor_type,
            actor_id=actor_id,
            source_kind="task",
            source_ref=row["id"],
        )
        marked += int(result.get("updated", 0))
    return marked


def apply_task_mutation(
    conn: sqlite3.Connection,
    task_id: str,
    changes: dict[str, Any],
    *,
    timestamp: str | None = None,
    machine_id: str | None = None,
    tool_name: str | None = None,
    actor_type: str = "system",
    actor_id: str | None = None,
    source_kind: str = "task",
    source_ref: str | None = None,
    provenance_map: dict[str, dict[str, Any]] | None = None,
    record_events: bool = True,
    touch_updated_at: bool = True,
    expected_status: str | None = None,
    expected_status_order: int | None = None,
    expected_status_event_id: str | None = None,
) -> dict[str, Any]:
    """Apply task changes and persist matching field/event history.

    This is the preferred write path for any task update outside low-level merge code.
    """
    raw_changes = {k: v for k, v in changes.items() if k != "updated_at"}
    status_cas = expected_status is not None
    if status_cas and set(raw_changes) != {"status"}:
        raise ValueError("status CAS accepts exactly one field: status")
    if status_cas and expected_status_order is None:
        raise ValueError("status CAS requires expected_status_order")
    if status_cas and (
        not isinstance(expected_status_event_id, str)
        or not expected_status_event_id.strip()
    ):
        raise ValueError("status CAS requires expected_status_event_id")
    if "project" in raw_changes:
        raw_changes["project"] = normalize_project_name(raw_changes.get("project"))
    if not raw_changes:
        return {
            "updated": 0,
            "changed_fields": (),
            "updated_at": timestamp or now_iso(),
        }
    unknown = set(raw_changes) - (TaskDAO.ALLOWED_UPDATE_COLUMNS - {"updated_at"})
    if unknown:
        raise ValueError(f"Unknown task columns: {sorted(unknown)}")

    changed_fields = tuple(k for k in raw_changes if k in MERGEABLE_FIELDS)
    select_cols = sorted(set(raw_changes) | {"updated_at", "type"})
    row = conn.execute(
        f"SELECT {', '.join(select_cols)} FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if row is None:
        return {
            "updated": 0,
            "changed_fields": (),
            "missing": True,
            "outcome": "conflict" if status_cas else "missing",
        }

    old_values = {field: row[field] for field in changed_fields}
    effective_changes = {
        field: value for field, value in raw_changes.items() if row[field] != value
    }
    effective_fields = tuple(
        field for field in changed_fields if field in effective_changes
    )
    if not effective_changes and not status_cas:
        return {
            "updated": 0,
            "changed_fields": (),
            "updated_at": row["updated_at"],
            "old_values": old_values,
            "new_values": {},
        }

    ts = timestamp or now_iso()

    # Optimistic status transitions branch before the ordinary no-op return.
    # A stale request whose target already equals the current value is still a
    # conflict: its field-version token no longer names the row the caller saw.
    if status_cas:
        if row["type"] != "task":
            return {
                "updated": 0,
                "changed_fields": (),
                "outcome": "conflict",
                "reason": "not_task",
            }
        version = get_status_version(conn, task_id)
        if version is None or version[0] <= 0 or version[1] is None:
            return {
                "updated": 0,
                "changed_fields": (),
                "outcome": "conflict",
                "reason": "missing_status_version",
            }
        target_status = raw_changes["status"]
        cur = conn.execute(
            "UPDATE tasks SET status=?, updated_at=?, tombstone_pushed_at=NULL "
            "WHERE id=? AND type='task' AND status=? "
            "AND EXISTS (SELECT 1 FROM task_field_versions "
            "WHERE task_id=? AND field_name='status' AND updated_order=? "
            "AND source_event_id IS ?)",
            (
                target_status,
                ts,
                task_id,
                expected_status,
                task_id,
                int(expected_status_order),
                expected_status_event_id,
            ),
        )
        if cur.rowcount == 0:
            return {
                "updated": 0,
                "changed_fields": (),
                "outcome": "conflict",
                "reason": "stale_status_version",
            }
        upsert_field_versions(
            conn,
            task_id,
            ("status",),
            ts,
            machine_id=machine_id,
            old_values={"status": expected_status},
            new_values={"status": target_status},
            tool_name=tool_name,
            actor_type=actor_type,
            actor_id=actor_id,
            source_kind=source_kind,
            source_ref=source_ref or task_id,
            provenance_map=provenance_map,
            record_events=record_events,
        )
        return {
            "updated": 1,
            "changed_fields": ("status",),
            "updated_at": ts,
            "old_values": {"status": expected_status},
            "new_values": {"status": target_status},
            "outcome": "applied",
        }
    if touch_updated_at:
        effective_changes["updated_at"] = ts

    # Push-aware tombstone retention: any status change invalidates a prior
    # tombstone push stamp. A reactivated task (archived -> in_progress) and a
    # later re-archive form a NEW tombstone that has not itself been pushed;
    # carrying the old stamp forward would let the new tombstone age out of
    # export / become Tier-2 deletable without propagating, resurrecting the row
    # on a peer. Clearing on every status change makes each new state require its
    # own push confirmation (re-tombstoning then starts NULL = protected).
    if "status" in effective_changes:
        effective_changes["tombstone_pushed_at"] = None

    set_clause = ", ".join(f"{field} = ?" for field in effective_changes)
    values = list(effective_changes.values()) + [task_id]
    cur = conn.execute(f"UPDATE tasks SET {set_clause} WHERE id = ?", values)
    if cur.rowcount and effective_fields:
        upsert_field_versions(
            conn,
            task_id,
            effective_fields,
            ts,
            machine_id=machine_id,
            old_values={field: old_values[field] for field in effective_fields},
            new_values={field: raw_changes[field] for field in effective_fields},
            tool_name=tool_name,
            actor_type=actor_type,
            actor_id=actor_id,
            source_kind=source_kind,
            source_ref=source_ref or task_id,
            provenance_map=provenance_map,
            record_events=record_events,
        )
    return {
        "updated": cur.rowcount,
        "changed_fields": effective_fields,
        "updated_at": ts,
        "old_values": {field: old_values[field] for field in effective_fields},
        "new_values": {field: raw_changes[field] for field in effective_fields},
    }


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


def get_status_version(
    conn: sqlite3.Connection, task_id: str
) -> tuple[int, str | None] | None:
    """Return the ABA-safe logical token for a task's status field."""
    row = conn.execute(
        "SELECT updated_order, source_event_id FROM task_field_versions "
        "WHERE task_id=? AND field_name='status'",
        (task_id,),
    ).fetchone()
    if row is None:
        return None
    source_event_id = row[1]
    if not isinstance(source_event_id, str) or not source_event_id.strip():
        source_event_id = None
    return int(row[0] or 0), source_event_id


def upsert_field_versions(
    conn: sqlite3.Connection,
    task_id: str,
    fields: tuple | list,
    timestamp: str | None = None,
    machine_id: str | None = None,
    old_values: dict[str, Any] | None = None,
    new_values: dict[str, Any] | None = None,
    *,
    tool_name: str | None = None,
    actor_type: str = "system",
    actor_id: str | None = None,
    source_kind: str = "task",
    source_ref: str | None = None,
    provenance_map: dict[str, dict[str, Any]] | None = None,
    record_events: bool = True,
) -> None:
    """Upsert field versions for the given fields.

    Args:
        old_values: {field_name: previous_value} — truncated to 500 chars.
        new_values: {field_name: new_value} — truncated to 500 chars.
    """
    ts = timestamp or now_iso()
    mid = machine_id or MACHINE_ID
    _old = old_values or {}
    _new = new_values or {}
    _prov = provenance_map or {}
    has_old = _sqlite_has_column(conn, "task_field_versions", "old_value")
    has_new = _sqlite_has_column(conn, "task_field_versions", "new_value")
    has_order = _sqlite_has_column(conn, "task_field_versions", "updated_order")
    has_event = _sqlite_has_column(conn, "task_field_versions", "source_event_id")
    for field in fields:
        ov = _old.get(field)
        nv = _new.get(field)
        # Truncate to 500 chars to avoid bloat
        ov_str = str(ov)[:500] if ov is not None else None
        nv_str = str(nv)[:500] if nv is not None else None
        event_meta: dict[str, Any] | None = None
        if record_events:
            prov = _prov.get(field, {})
            event_meta = record_memory_event(
                conn,
                event_type="task_field_set",
                aggregate_kind="task",
                aggregate_id=task_id,
                field_name=field,
                actor_type=actor_type,
                actor_id=actor_id,
                machine_id=mid,
                tool_name=tool_name,
                event_ts=ts,
                old_value=ov,
                new_value=nv,
                payload={
                    "task_id": task_id,
                    "field_name": field,
                    "old_value": ov,
                    "new_value": nv,
                },
                source_kind=prov.get("source_kind", source_kind),
                source_ref=prov.get("source_ref", source_ref or task_id),
                source_excerpt=prov.get("excerpt"),
                source_start=prov.get("start"),
                source_end=prov.get("end"),
            )

        _store_task_field_version(
            conn,
            task_id,
            field,
            updated_at=ts,
            updated_by=mid,
            old_value=ov_str if has_old else None,
            new_value=nv_str if has_new else None,
            updated_order=int((event_meta or {}).get("logical_clock") or 0)
            if has_order
            else None,
            source_event_id=(event_meta or {}).get("event_id") if has_event else None,
        )


def _store_task_field_version(
    conn: sqlite3.Connection,
    task_id: str,
    field_name: str,
    *,
    updated_at: str,
    updated_by: str,
    old_value: str | None = None,
    new_value: str | None = None,
    updated_order: int | None = None,
    source_event_id: str | None = None,
) -> None:
    columns = ["task_id", "field_name", "updated_at", "updated_by"]
    values: list[Any] = [task_id, field_name, updated_at, updated_by]
    if _sqlite_has_column(conn, "task_field_versions", "old_value"):
        columns.append("old_value")
        values.append(old_value)
    if _sqlite_has_column(conn, "task_field_versions", "new_value"):
        columns.append("new_value")
        values.append(new_value)
    if _sqlite_has_column(conn, "task_field_versions", "updated_order"):
        columns.append("updated_order")
        values.append(int(updated_order or 0))
    if _sqlite_has_column(conn, "task_field_versions", "source_event_id"):
        columns.append("source_event_id")
        values.append(source_event_id)
    placeholders = ", ".join("?" for _ in columns)
    conn.execute(
        f"INSERT OR REPLACE INTO task_field_versions ({', '.join(columns)}) "
        f"VALUES ({placeholders})",
        values,
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


def _task_field_version_select(
    conn: sqlite3.Connection,
) -> tuple[str, bool, bool]:
    cols = ["task_id", "field_name", "updated_at", "updated_by"]
    if _sqlite_has_column(conn, "task_field_versions", "new_value"):
        cols.append("new_value")
    has_order = _sqlite_has_column(conn, "task_field_versions", "updated_order")
    has_event = _sqlite_has_column(conn, "task_field_versions", "source_event_id")
    if has_order:
        cols.append("updated_order")
    if has_event:
        cols.append("source_event_id")
    return ", ".join(cols), has_order, has_event


def _field_version_entry(
    row: sqlite3.Row,
    *,
    has_order: bool,
    has_event: bool,
    current_value: Any = _FIELD_VALUE_MISSING,
) -> list[Any]:
    entry: list[Any] = [row["updated_at"], row["updated_by"]]
    if has_order:
        entry.append(row["updated_order"])
    if has_event:
        entry.append(row["source_event_id"])
    if current_value is not _FIELD_VALUE_MISSING:
        while len(entry) < 4:
            entry.append(0 if len(entry) == 2 else None)
        entry.append(current_value)
    return entry


def _normalize_task_status_value(value: Any) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    if raw == "completed":
        raw = "done"
    elif raw == "open":
        raw = "not_started"
    return raw if raw in TASK_STATUSES else None


def _field_ts_entry_from_status(
    updated_at: str,
    updated_by: str,
    updated_order: int = 0,
    source_event_id: str | None = None,
    value: Any = _FIELD_VALUE_MISSING,
) -> list[Any]:
    entry: list[Any] = [str(updated_at or ""), str(updated_by or "")]
    if int(updated_order or 0) or source_event_id is not None:
        entry.append(int(updated_order or 0))
    if source_event_id is not None:
        if len(entry) == 2:
            entry.append(int(updated_order or 0))
        entry.append(source_event_id)
    if value is not _FIELD_VALUE_MISSING:
        while len(entry) < 4:
            entry.append(0 if len(entry) == 2 else None)
        entry.append(value)
    return entry


def _field_ts_explicit_value(remote_fts: dict, field: str) -> Any:
    """Return a value embedded in _field_ts, or a sentinel when absent."""
    entry = remote_fts.get(field)
    if isinstance(entry, dict):
        for key in ("value", "new_value"):
            if key in entry:
                return entry[key]
        return _FIELD_VALUE_MISSING
    if isinstance(entry, (list, tuple)) and len(entry) >= 5:
        return entry[4]
    return _FIELD_VALUE_MISSING


def _machine_aliases(machine_id: str | None) -> set[str]:
    raw = str(machine_id or "").strip().lower()
    if not raw:
        return set()
    aliases = {raw}
    compact = raw.replace("_", "-")
    aliases.add(compact)
    if compact in {"rmanov", "windows-rmanov"}:
        aliases.update({"rmanov", "windows-rmanov"})
    if compact.endswith("-rmanov"):
        aliases.add("rmanov")
    return aliases


def _source_machine_matches_field_writer(
    source_machine_id: str | None,
    updated_by: str | None,
) -> bool:
    source_aliases = _machine_aliases(source_machine_id)
    writer_aliases = _machine_aliases(updated_by)
    return bool(source_aliases and writer_aliases and source_aliases & writer_aliases)


def _legacy_payload_value_is_authoritative(
    *,
    field: str,
    source_machine_id: str | None,
    updated_by: str | None,
) -> bool:
    """Allow legacy value repair only from the machine that wrote the field.

    Older peers exported _field_ts as [timestamp, machine] without the value or
    event id. If another peer later re-emits the same field timestamp with a
    stale row value, accepting that row value flips status/section back. The
    only safe legacy fallback is to trust the payload value from the writer
    machine itself; modern peers should use explicit _field_ts value/event data.
    """
    return field in FIELD_TS_VALUE_FIELDS and _source_machine_matches_field_writer(
        source_machine_id, updated_by
    )


def _legacy_terminal_status_row_can_promote(
    *,
    remote_value: Any,
    local_value: Any,
    source_machine_id: str | None,
    remote_updated_at: str | None,
    remote_field_ts: str | None,
    local_updated_at: str | None,
    has_explicit_authority: bool,
    legacy_value_authority: bool,
) -> bool:
    """Let legacy row-level terminal closures repair stale status metadata.

    Older writers could update tasks.status without updating task_field_versions.
    That is dangerous for active states, because a peer can re-emit a stale
    `not_started` row forever. Terminal states are different: if the exporting
    source row says done/archived/cancelled and its row timestamp is newer than
    the stale status field timestamp, promote that closure once into field
    history so later stale active payloads cannot resurrect it.
    """
    remote_status = _normalize_task_status_value(remote_value)
    local_status = _normalize_task_status_value(local_value)
    if remote_status not in TASK_ACTIVE_EXCLUSIONS:
        return False
    if local_status in TASK_ACTIVE_EXCLUSIONS:
        return False
    if has_explicit_authority or legacy_value_authority:
        return False
    if not str(source_machine_id or "").strip():
        return False
    if not str(remote_updated_at or "").strip():
        return False
    if _timestamp_is_newer(local_updated_at, remote_updated_at):
        return False
    return _timestamp_is_newer(remote_updated_at, remote_field_ts)


def _build_event_lookup_by_id(
    events: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None,
) -> dict[str, dict[str, Any]]:
    lookup: dict[str, dict[str, Any]] = {}
    if not events:
        return lookup
    for event in events:
        event_id = str(event.get("event_id") or "")
        if event_id:
            lookup[event_id] = dict(event)
    return lookup


def _build_task_field_event_heads(
    events: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None,
    field_name: str,
) -> dict[str, dict[str, Any]]:
    heads: dict[str, dict[str, Any]] = {}
    if not events:
        return heads
    for event in events:
        if (
            event.get("aggregate_kind") != "task"
            or event.get("field_name") != field_name
        ):
            continue
        aggregate_id = str(event.get("aggregate_id") or "")
        if not aggregate_id:
            continue
        candidate = dict(event)
        current = heads.get(aggregate_id)
        if current is None or _event_sort_key(
            candidate.get("event_ts"),
            candidate.get("machine_id"),
            int(candidate.get("logical_clock") or 0),
        ) > _event_sort_key(
            current.get("event_ts"),
            current.get("machine_id"),
            int(current.get("logical_clock") or 0),
        ):
            heads[aggregate_id] = candidate
    return heads


def _load_memory_events_by_id(
    conn: sqlite3.Connection,
    event_ids: list[str],
) -> dict[str, dict[str, Any]]:
    if not event_ids or not _sqlite_table_exists(conn, "memory_events"):
        return {}
    placeholders = ",".join("?" * len(event_ids))
    rows = conn.execute(
        "SELECT event_id, aggregate_id, field_name, machine_id, logical_clock, "
        "event_ts, new_value FROM memory_events WHERE event_id IN ("
        + placeholders
        + ")",
        tuple(event_ids),
    ).fetchall()
    return {row["event_id"]: dict(row) for row in rows}


def _load_task_field_event_heads(
    conn: sqlite3.Connection,
    task_ids: list[str],
    field_name: str,
) -> dict[str, dict[str, Any]]:
    if not task_ids or not _sqlite_table_exists(conn, "memory_events"):
        return {}
    placeholders = ",".join("?" * len(task_ids))
    rows = conn.execute(
        "SELECT event_id, aggregate_id, field_name, machine_id, logical_clock, "
        "event_ts, new_value FROM memory_events WHERE aggregate_kind = 'task' "
        "AND field_name = ? AND aggregate_id IN (" + placeholders + ")",
        (field_name, *task_ids),
    ).fetchall()
    heads: dict[str, dict[str, Any]] = {}
    for row in rows:
        aggregate_id = row["aggregate_id"]
        candidate = dict(row)
        current = heads.get(aggregate_id)
        if current is None or _event_sort_key(
            candidate.get("event_ts"),
            candidate.get("machine_id"),
            int(candidate.get("logical_clock") or 0),
        ) > _event_sort_key(
            current.get("event_ts"),
            current.get("machine_id"),
            int(current.get("logical_clock") or 0),
        ):
            heads[aggregate_id] = candidate
    return heads


def _resolve_task_status_authority(
    current_status: Any,
    *,
    field_updated_at: str | None = None,
    field_updated_by: str | None = None,
    field_updated_order: int = 0,
    source_event_id: str | None = None,
    source_new_value: Any = None,
    event_by_id: dict[str, dict[str, Any]] | None = None,
    event_head: dict[str, Any] | None = None,
) -> dict[str, Any]:
    raw_status = _normalize_task_status_value(current_status)
    best = {
        "value": raw_status or str(current_status or ""),
        "updated_at": str(field_updated_at or ""),
        "updated_by": str(field_updated_by or ""),
        "updated_order": int(field_updated_order or 0),
        "source_event_id": source_event_id,
    }

    candidates: list[dict[str, Any]] = []
    field_value = _normalize_task_status_value(source_new_value)
    if field_value is not None and field_updated_at:
        candidates.append(
            {
                "value": field_value,
                "updated_at": str(field_updated_at or ""),
                "updated_by": str(field_updated_by or ""),
                "updated_order": int(field_updated_order or 0),
                "source_event_id": source_event_id,
                "_sort_key": _field_version_sort_key(
                    str(field_updated_at or ""),
                    str(field_updated_by or ""),
                    int(field_updated_order or 0),
                ),
            }
        )

    seen_events: set[str] = set()
    for event in (
        (event_by_id or {}).get(source_event_id) if source_event_id else None,
        event_head,
    ):
        if not event:
            continue
        event_id = str(event.get("event_id") or "")
        if event_id and event_id in seen_events:
            continue
        if event_id:
            seen_events.add(event_id)
        event_value = _normalize_task_status_value(event.get("new_value"))
        if event_value is None:
            continue
        candidates.append(
            {
                "value": event_value,
                "updated_at": str(event.get("event_ts") or ""),
                "updated_by": str(event.get("machine_id") or ""),
                "updated_order": int(event.get("logical_clock") or 0),
                "source_event_id": event_id or None,
                "_sort_key": _event_sort_key(
                    event.get("event_ts"),
                    event.get("machine_id"),
                    int(event.get("logical_clock") or 0),
                ),
            }
        )

    if not candidates:
        return best

    chosen = max(candidates, key=lambda item: item["_sort_key"])
    return {
        "value": chosen["value"],
        "updated_at": chosen["updated_at"],
        "updated_by": chosen["updated_by"],
        "updated_order": chosen["updated_order"],
        "source_event_id": chosen["source_event_id"],
    }


def _compute_authoritative_task_statuses(
    conn: sqlite3.Connection,
    tasks: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    task_map = {
        str(task.get("id") or ""): task
        for task in tasks
        if isinstance(task, dict) and task.get("id")
    }
    task_ids = list(task_map)
    if not task_ids:
        return {}

    placeholders = ",".join("?" * len(task_ids))
    status_rows: dict[str, sqlite3.Row] = {}
    if _sqlite_table_exists(conn, "task_field_versions"):
        cols = ["task_id", "updated_at", "updated_by"]
        has_new = _sqlite_has_column(conn, "task_field_versions", "new_value")
        has_order = _sqlite_has_column(conn, "task_field_versions", "updated_order")
        has_event = _sqlite_has_column(conn, "task_field_versions", "source_event_id")
        if has_new:
            cols.append("new_value")
        if has_order:
            cols.append("updated_order")
        if has_event:
            cols.append("source_event_id")
        rows = conn.execute(
            f"SELECT {', '.join(cols)} FROM task_field_versions "
            "WHERE field_name = 'status' AND task_id IN (" + placeholders + ")",
            tuple(task_ids),
        ).fetchall()
        status_rows = {row["task_id"]: row for row in rows}
    else:
        has_new = has_order = has_event = False

    event_ids = [
        str(row["source_event_id"])
        for row in status_rows.values()
        if has_event and row["source_event_id"]
    ]
    events_by_id = _load_memory_events_by_id(conn, event_ids)
    event_heads = _load_task_field_event_heads(conn, task_ids, "status")

    resolved: dict[str, dict[str, Any]] = {}
    for tid, task in task_map.items():
        row = status_rows.get(tid)
        state = _resolve_task_status_authority(
            task.get("status"),
            field_updated_at=row["updated_at"] if row else None,
            field_updated_by=row["updated_by"] if row else None,
            field_updated_order=int(row["updated_order"] or 0)
            if row and has_order
            else 0,
            source_event_id=row["source_event_id"] if row and has_event else None,
            source_new_value=row["new_value"] if row and has_new else None,
            event_by_id=events_by_id,
            event_head=event_heads.get(tid),
        )
        resolved[tid] = state
    return resolved


def canonicalize_exported_task_statuses(
    conn: sqlite3.Connection,
    tasks: list[dict[str, Any]],
) -> None:
    status_map = _compute_authoritative_task_statuses(conn, tasks)
    for task in tasks:
        tid = str(task.get("id") or "")
        if not tid:
            continue
        state = status_map.get(tid)
        if not state:
            continue
        resolved_status = _normalize_task_status_value(state.get("value"))
        if resolved_status is None:
            continue
        task["status"] = resolved_status
        if "_field_ts" in task and state.get("updated_at"):
            task["_field_ts"]["status"] = _field_ts_entry_from_status(
                state.get("updated_at", ""),
                state.get("updated_by", ""),
                int(state.get("updated_order") or 0),
                state.get("source_event_id"),
                resolved_status,
            )


def export_task_files(
    conn: sqlite3.Connection,
    bridge_dir: str,
    changed_since: str | None = None,
    *,
    attachment_root: str | None = None,
) -> list[str]:
    """Export active tasks plus recent tombstones to per-task bridge files."""
    tasks_dir = Path(bridge_dir) / "tasks"
    tasks_dir.mkdir(exist_ok=True)
    attachments_dir = Path(bridge_dir) / "attachments"
    attachments_dir.mkdir(exist_ok=True)

    def _cleanup_stale_generated_files(
        active_stems: set[str],
        active_attachment_paths: set[str],
    ) -> None:
        for stale in tasks_dir.iterdir():
            if stale.suffix == ".json" and stale.stem not in active_stems:
                stale.unlink()
        for stale in attachments_dir.rglob("*"):
            if not stale.is_file():
                continue
            rel = stale.relative_to(attachments_dir).as_posix()
            if rel not in active_attachment_paths:
                stale.unlink()
        for maybe_empty in sorted(attachments_dir.rglob("*"), reverse=True):
            if maybe_empty.is_dir():
                try:
                    maybe_empty.rmdir()
                except OSError:
                    pass

    cutoff = (datetime.now(timezone.utc) - timedelta(days=_TOMBSTONE_DAYS)).isoformat()
    # Push-aware tombstone window: a tombstone stays export-eligible until it has
    # been successfully pushed AND aged past retention measured FROM the push.
    # tombstone_pushed_at IS NULL  -> never pushed -> always export (never drop an
    #                                 un-pushed deletion, even if updated_at is old).
    # tombstone_pushed_at > cutoff -> pushed but still inside the retention window.
    # This keeps export-eligibility the exact complement of Tier-2 hard-delete
    # eligibility (see TaskDAO.purge_done), so no tombstone can age out of export
    # before it has propagated.
    export_filter = (
        "WHERE (status NOT IN ('archived', 'cancelled') "
        "OR (status IN ('archived', 'cancelled') "
        "AND (tombstone_pushed_at IS NULL OR tombstone_pushed_at > ?)))"
    )
    if changed_since:
        # Incremental export: normally only rows touched since the last push are
        # re-emitted. But an UN-PUSHED tombstone must ride along regardless of
        # updated_at, else the incremental AND-clause would silently drop an aged
        # un-pushed deletion that the export window gate above just kept eligible
        # (the archive-while-offline >30d resurrection class). Already-pushed
        # in-window tombstones need no ride-along: their bridge file already
        # exists (cleanup runs only on full export) so peers already have them.
        rows = conn.execute(
            f"SELECT {TASK_EXPORT_COLS} FROM tasks "
            f"{export_filter} AND (updated_at >= ? "
            "OR (status IN ('archived', 'cancelled') "
            "AND tombstone_pushed_at IS NULL)) "
            "ORDER BY created_at",
            (cutoff, changed_since),
        ).fetchall()
    else:
        rows = conn.execute(
            f"SELECT {TASK_EXPORT_COLS} FROM tasks {export_filter} ORDER BY created_at",
            (cutoff,),
        ).fetchall()

    exported: list[str] = []
    task_ids = []
    task_map: dict[str, dict] = {}
    for row in rows:
        tid = row["id"]
        if not isinstance(tid, str) or not tid:
            continue
        task_ids.append(tid)
        task_map[tid] = dict(row)
    if not task_ids:
        if not changed_since:
            _cleanup_stale_generated_files(set(), set())
        return exported

    # Batch fetch all field versions in one query
    ph = ",".join("?" * len(task_ids))
    fv_select, has_order, has_event = _task_field_version_select(conn)
    fv_rows = conn.execute(
        f"SELECT {fv_select} FROM task_field_versions WHERE task_id IN ({ph})",
        task_ids,
    ).fetchall()
    fv_map: dict[str, dict] = {}
    for fvr in fv_rows:
        current_value = _FIELD_VALUE_MISSING
        field_name = fvr["field_name"]
        if field_name in FIELD_TS_VALUE_FIELDS:
            current_value = task_map.get(fvr["task_id"], {}).get(field_name)
        fv_map.setdefault(fvr["task_id"], {})[field_name] = _field_version_entry(
            fvr,
            has_order=has_order,
            has_event=has_event,
            current_value=current_value,
        )

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

    attachment_map: dict[str, list[dict]] = {}
    active_attachment_paths: set[str] = set()
    if _sqlite_table_exists(conn, "task_attachments"):
        attachment_rows = conn.execute(
            "SELECT attachment_id, task_id, file_name, stored_relpath, media_type, file_size, "
            "status, created_at, updated_at "
            "FROM task_attachments WHERE task_id IN ({})".format(ph),
            task_ids,
        ).fetchall()
        for ar in attachment_rows:
            meta = {
                "attachment_id": ar["attachment_id"],
                "file_name": ar["file_name"],
                "stored_relpath": ar["stored_relpath"],
                "media_type": ar["media_type"],
                "file_size": int(ar["file_size"] or 0),
                "status": ar["status"],
                "created_at": ar["created_at"],
                "updated_at": ar["updated_at"],
            }
            attachment_map.setdefault(ar["task_id"], []).append(meta)
            if ar["status"] == "active" and ar["stored_relpath"]:
                active_attachment_paths.add(ar["stored_relpath"])
                try:
                    src_path = _local_attachment_path(
                        ar["stored_relpath"], attachment_root
                    )
                    dst_path = _bridge_attachment_path(ar["stored_relpath"], bridge_dir)
                except ValueError:
                    continue
                if src_path.exists():
                    _copy_attachment_file(src_path, dst_path)
                elif not dst_path.exists():
                    dst_path.parent.mkdir(parents=True, exist_ok=True)

    tasks_for_export: list[dict[str, Any]] = []
    for tid in task_ids:
        task = task_map[tid]
        task["_field_ts"] = fv_map.get(tid, {})
        task["_links"] = link_map.get(tid, [])
        task["_attachments"] = attachment_map.get(tid, [])
        tasks_for_export.append(task)

    canonicalize_exported_task_statuses(conn, tasks_for_export)

    for tid in task_ids:
        task = task_map[tid]
        task_path = _task_storage_path(tid, bridge_dir)

        # Content-aware export: preserve bridge descriptions/notes if local is NULL
        if task_path.exists():
            try:
                existing = json_loads(task_path.read_text(encoding="utf-8"))
                if not is_archived_duplicate_redirect_task(task):
                    for content_field in CONTENT_FIELDS:
                        local_content = task.get(content_field)
                        existing_content = existing.get(content_field)
                        if not has_meaningful_content(
                            local_content
                        ) and has_meaningful_content(existing_content):
                            task[content_field] = existing_content
                        elif is_suspicious_content_shrink(
                            existing_content, local_content
                        ):
                            task[content_field] = existing_content
            except (ValueError, OSError):
                pass

        task_path.write_text(json_dumps(task), encoding="utf-8")
        exported.append(tid)

    # Clean stale files only during full export (changed_since=None).
    # During incremental export, task_ids is partial — cleanup would delete valid files.
    if not changed_since:
        active_stems = {_task_storage_stem(tid) for tid in task_ids}
        _cleanup_stale_generated_files(active_stems, active_attachment_paths)

    return exported


def mark_tombstones_pushed(
    conn: sqlite3.Connection,
    exported_ids: list[str],
    pushed_at: str,
) -> int:
    """Stamp ``tombstone_pushed_at`` on tombstones that were just pushed.

    Call ONLY after a successful push, passing the exact id list returned by
    ``export_task_files`` for that push. Correct-by-construction: it stamps only
    rows that (a) were actually written into this push payload, (b) are currently
    archived/cancelled tombstones, and (c) are not already stamped. This is the
    push-success signal that makes a tombstone eligible to age out of the export
    window and become Tier-2 hard-deletable (see ``TaskDAO.purge_done``).

    Never re-derive the id set from a fresh ``WHERE`` here: a tombstone excluded
    from the payload (e.g. dropped by an incremental ``changed_since`` clause)
    must NOT be marked pushed, or it could be hard-deleted without ever having
    propagated — resurrecting on a peer. Retention is measured from this
    ``pushed_at``, so stamping a never-exported row is the one unsafe move.

    Returns the number of tombstones newly stamped.
    """
    ids = [tid for tid in exported_ids if isinstance(tid, str) and tid]
    if not ids:
        return 0
    stamped = 0
    # Chunk to stay under SQLite's variable limit on very large payloads.
    for start in range(0, len(ids), 500):
        chunk = ids[start : start + 500]
        ph = ",".join("?" * len(chunk))
        cur = conn.execute(
            f"UPDATE tasks SET tombstone_pushed_at = ? "
            f"WHERE id IN ({ph}) "
            "AND status IN ('archived', 'cancelled') "
            "AND tombstone_pushed_at IS NULL",
            (pushed_at, *chunk),
        )
        stamped += cur.rowcount or 0
    return stamped


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

    # Tombstones: archived/cancelled rows still inside the push-aware retention
    # window. Keyed off tombstone_pushed_at so the index agrees with the per-task
    # export (export_task_files): NULL (un-pushed) is always listed; a pushed
    # tombstone is listed until retention elapses FROM the push.
    cutoff = (datetime.now(timezone.utc) - timedelta(days=_TOMBSTONE_DAYS)).isoformat()
    tombstone_rows = conn.execute(
        f"SELECT {meta_cols} FROM tasks "
        "WHERE status IN ('archived', 'cancelled') "
        "AND (tombstone_pushed_at IS NULL OR tombstone_pushed_at > ?) "
        "ORDER BY updated_at",
        (cutoff,),
    ).fetchall()

    # Batch-fetch field versions for all tasks + tombstones (avoid N+1)
    all_ids = [r["id"] for r in rows] + [r["id"] for r in tombstone_rows]
    row_by_id = {r["id"]: r for r in rows}
    row_by_id.update({r["id"]: r for r in tombstone_rows})
    fv_map: dict[str, dict[str, list]] = {}
    if all_ids:
        ph = ",".join("?" * len(all_ids))
        fv_select, has_order, has_event = _task_field_version_select(conn)
        fv_rows = conn.execute(
            f"SELECT {fv_select} FROM task_field_versions WHERE task_id IN ({ph})",
            all_ids,
        ).fetchall()
        for fvr in fv_rows:
            field_name = fvr["field_name"]
            current_value = _FIELD_VALUE_MISSING
            if field_name in FIELD_TS_VALUE_FIELDS:
                source_row = row_by_id.get(fvr["task_id"])
                if source_row is not None and field_name in source_row.keys():
                    current_value = source_row[field_name]
            fv_map.setdefault(fvr["task_id"], {})[fvr["field_name"]] = (
                _field_version_entry(
                    fvr,
                    has_order=has_order,
                    has_event=has_event,
                    current_value=current_value,
                )
            )

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

    canonicalize_exported_task_statuses(conn, tasks)

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


def _parse_field_ts(
    remote_fts: dict,
    field: str,
    fallback_ts: str,
) -> tuple[str, str, int, str | None]:
    """Extract field version metadata from bridge payload with backward compat."""
    entry = remote_fts.get(field)
    if isinstance(entry, dict):
        return (
            str(entry.get("updated_at") or fallback_ts),
            str(entry.get("updated_by") or MACHINE_ID),
            int(entry.get("updated_order") or 0),
            entry.get("source_event_id"),
        )
    if isinstance(entry, (list, tuple)) and len(entry) >= 4:
        return str(entry[0]), str(entry[1]), int(entry[2] or 0), entry[3]
    if isinstance(entry, (list, tuple)) and len(entry) >= 3:
        return str(entry[0]), str(entry[1]), int(entry[2] or 0), None
    if isinstance(entry, (list, tuple)) and len(entry) >= 2:
        return str(entry[0]), str(entry[1]), 0, None
    # Backward compat: no _field_ts → task-level updated_at.
    # Use MACHINE_ID (not "") so old peers don't systematically lose ties.
    return fallback_ts, MACHINE_ID, 0, None


def _field_ts_has_explicit_authority(remote_fts: dict, field: str) -> bool:
    """True when _field_ts carries a value or event pointer for this field."""
    if _field_ts_explicit_value(remote_fts, field) is not _FIELD_VALUE_MISSING:
        return True
    entry = remote_fts.get(field)
    if isinstance(entry, dict):
        return bool(entry.get("source_event_id"))
    if isinstance(entry, (list, tuple)) and len(entry) >= 4:
        return bool(entry[3])
    return False


def _field_version_sort_key(
    updated_at: str,
    updated_by: str,
    updated_order: int = 0,
) -> tuple[int, Any, str]:
    """Prefer packed HLC clocks; fall back to timestamp ordering for legacy peers."""
    if int(updated_order or 0) >= _HLC_PACKED_MIN:
        return (1, int(updated_order), str(updated_by or ""))
    return (0, parse_iso_datetime_for_compare(updated_at), str(updated_by or ""))


def merge_import_tasks(
    conn: sqlite3.Connection,
    remote_tasks: list[dict],
    import_content: bool = False,
    remote_events: list[dict] | None = None,
) -> tuple[int, int]:
    """Per-field causal/LWW merge. Returns (new_count, updated_field_count).

    Preferred rule: (logical_clock, machine_id) when updated_order exists.
    Legacy fallback: (timestamp, machine_id) for older bridge peers.
    All writes should also exist in memory_events, so overwritten values remain auditable.

    import_content=False: only merge metadata fields (for index.json pull).
    import_content=True: also merge description/notes (for on-demand load).
    remote_events: optional bridge payload event ledger; used to resolve stale
    remote[field] values against newer field/event status metadata.
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

    # Pre-fetch: collect remote IDs that exist locally + their field versions
    remote_ids = [r.get("id") for r in tasks_sorted if r.get("id")]
    remote_id_set = {rid for rid in remote_ids if isinstance(rid, str) and rid}
    remote_event_by_id = _build_event_lookup_by_id(remote_events)
    remote_status_heads = _build_task_field_event_heads(remote_events, "status")
    if remote_ids:
        placeholders = ",".join("?" * len(remote_ids))
        existing_rows = conn.execute(
            f"SELECT id, updated_at FROM tasks WHERE id IN ({placeholders})",
            remote_ids,
        ).fetchall()
        existing_map = {r["id"]: r for r in existing_rows}

        fv_select, has_order, has_event = _task_field_version_select(conn)
        fv_rows = conn.execute(
            f"SELECT {fv_select} FROM task_field_versions WHERE task_id IN ({placeholders})",
            remote_ids,
        ).fetchall()
        fv_map: dict[str, dict[str, tuple[str, str, int, str | None]]] = {}
        for r in fv_rows:
            fv_map.setdefault(r["task_id"], {})[r["field_name"]] = (
                r["updated_at"],
                r["updated_by"],
                int(r["updated_order"]) if has_order else 0,
                r["source_event_id"] if has_event else None,
            )

        # Pre-fetch full task rows for content field checks
        task_rows = conn.execute(
            f"SELECT * FROM tasks WHERE id IN ({placeholders})", remote_ids
        ).fetchall()
        task_content_map = {r["id"]: dict(r) for r in task_rows}
    else:
        existing_map = {}
        fv_map = {}
        task_content_map = {}

    local_status_authority = _compute_authoritative_task_statuses(
        conn, list(task_content_map.values())
    )

    for remote in tasks_sorted:
        tid = remote.get("id")
        if not isinstance(tid, str) or not tid:
            continue

        sanitize_task_enums(remote)
        remote["project"] = normalize_project_name(remote.get("project"))
        remote_fts = remote.get("_field_ts", {})
        fallback_ts = remote.get("updated_at", "")
        source_machine_id = (
            remote.get("_source_machine_id")
            or remote.get("source_machine")
            or remote.get("machine_id")
        )
        parent_id = remote.get("parent_id")
        if (
            isinstance(parent_id, str)
            and parent_id
            and parent_id not in existing_map
            and parent_id not in remote_id_set
        ):
            parent_id = None
        (
            remote_status_ts,
            remote_status_by,
            remote_status_order,
            remote_status_event_id,
        ) = _parse_field_ts(remote_fts, "status", fallback_ts)
        remote_status_explicit_value = _field_ts_explicit_value(remote_fts, "status")
        status_has_legacy_value_authority = (
            isinstance(remote_fts, dict)
            and "status" in remote_fts
            and _legacy_payload_value_is_authoritative(
                field="status",
                source_machine_id=source_machine_id,
                updated_by=remote_status_by,
            )
        )
        remote_status_source_value = remote_status_explicit_value
        if (
            remote_status_source_value is _FIELD_VALUE_MISSING
            and status_has_legacy_value_authority
        ):
            remote_status_source_value = remote.get("status")
        remote_status_state = _resolve_task_status_authority(
            remote.get("status"),
            field_updated_at=remote_status_ts,
            field_updated_by=remote_status_by,
            field_updated_order=remote_status_order,
            source_event_id=remote_status_event_id,
            source_new_value=remote_status_source_value,
            event_by_id=remote_event_by_id,
            event_head=remote_status_heads.get(tid),
        )
        resolved_remote_status = _normalize_task_status_value(
            remote_status_state.get("value")
        )
        if resolved_remote_status is not None:
            remote["status"] = resolved_remote_status
            remote_fts = dict(remote_fts or {})
            status_has_explicit_value = (
                remote_status_explicit_value is not _FIELD_VALUE_MISSING
            )
            status_has_event_authority = bool(
                remote_status_event_id
                and (
                    remote_status_event_id in remote_event_by_id
                    or remote_status_heads.get(tid)
                )
            )
            if remote_status_state.get("updated_at"):
                if (
                    status_has_explicit_value
                    or status_has_event_authority
                    or status_has_legacy_value_authority
                ):
                    remote_fts["status"] = _field_ts_entry_from_status(
                        remote_status_state.get("updated_at", ""),
                        remote_status_state.get("updated_by", ""),
                        int(remote_status_state.get("updated_order") or 0),
                        remote_status_state.get("source_event_id"),
                        resolved_remote_status,
                    )
                remote["_field_ts"] = remote_fts
                if _timestamp_is_newer(
                    remote_status_state.get("updated_at", ""), fallback_ts
                ):
                    remote["updated_at"] = remote_status_state["updated_at"]
                    fallback_ts = remote["updated_at"]

        # Clock skew detection + clamping: prevent future timestamps from
        # permanently winning all LWW merges (LW-02 fix)
        if _timestamp_is_newer(fallback_ts, now):
            try:
                delta = (
                    datetime.fromisoformat(fallback_ts) - datetime.fromisoformat(now)
                ).total_seconds()
                if delta > 5:
                    if not _clock_skew_warned:
                        _log.warning(
                            "Clock skew detected: remote is %.1fs ahead (task %s). "
                            "Clamping future timestamps to now.",
                            delta,
                            tid,
                        )
                        _clock_skew_warned = True
                    # Clamp all future field timestamps to now
                    fallback_ts = now
                    if _timestamp_is_newer(remote.get("updated_at", ""), now):
                        remote["updated_at"] = now
                    fts = remote.get("_field_ts", {})
                    for f_key, f_val in fts.items():
                        if (
                            isinstance(f_val, list)
                            and f_val
                            and _timestamp_is_newer(str(f_val[0]), now)
                        ):
                            f_val[0] = now
            except (ValueError, TypeError):
                pass

        # Handle tombstones — only merge status field
        if remote.get("_tombstone"):
            existing = existing_map.get(tid)
            if existing:
                remote_ts, remote_by, remote_order, remote_event_id = _parse_field_ts(
                    remote_fts, "status", fallback_ts
                )
                local_fv_data = fv_map.get(tid, {}).get("status")
                local_ts = local_fv_data[0] if local_fv_data else ""
                local_by = local_fv_data[1] if local_fv_data else ""
                local_order = local_fv_data[2] if local_fv_data else 0

                tombstone_wins = _field_version_sort_key(
                    remote_ts, remote_by, remote_order
                ) > _field_version_sort_key(local_ts, local_by, local_order)

                # Fallback: if field timestamps are equal, tombstone wins
                # when its updated_at is newer (archival may not update field_versions)
                if not tombstone_wins and remote_ts == local_ts:
                    remote_updated = remote.get("updated_at", "")
                    local_updated = existing["updated_at"] or ""
                    remote_has_explicit_status_ts = isinstance(remote_fts, dict) and (
                        "status" in remote_fts
                    )
                    local_has_status_ts = bool(local_fv_data)
                    if (
                        not remote_has_explicit_status_ts
                        and not local_has_status_ts
                        and remote_updated > local_updated
                    ):
                        tombstone_wins = True
                        _log.info(
                            "Tombstone fallback: %s wins via updated_at (%s > %s)",
                            tid[:12],
                            remote_updated[:19],
                            local_updated[:19],
                        )

                if tombstone_wins:
                    local_status = task_content_map.get(tid, {}).get("status")
                    row_updated_at = existing["updated_at"] or ""
                    if local_status != remote["status"]:
                        row_updated_at = (
                            _max_iso_timestamp(row_updated_at, remote_ts, fallback_ts)
                            or row_updated_at
                            or remote_ts
                            or fallback_ts
                        )
                    # Absorbing a remote tombstone: this is a fresh local
                    # tombstone state that THIS machine has not pushed, so clear
                    # any prior push stamp. It stays export-eligible (and not
                    # Tier-2 deletable) until this machine itself pushes it.
                    conn.execute(
                        "UPDATE tasks SET status = ?, updated_at = ?, "
                        "tombstone_pushed_at = NULL WHERE id = ?",
                        (remote["status"], row_updated_at, tid),
                    )
                    _store_task_field_version(
                        conn,
                        tid,
                        "status",
                        updated_at=remote_ts or fallback_ts,
                        updated_by=remote_by or MACHINE_ID,
                        updated_order=remote_order,
                        source_event_id=remote_event_id,
                    )
                    updated_fields += 1
            else:
                desc = remote.get("description") if import_content else None
                notes = remote.get("notes") if import_content else None
                created_at = remote.get("created_at") or fallback_ts or now
                updated_at = remote.get("updated_at") or fallback_ts or created_at
                tombstone_status = remote.get("status", "archived")
                if tombstone_status not in (*TASK_HIDDEN_STATUSES, "done"):
                    tombstone_status = "archived"
                conn.execute(
                    "INSERT OR IGNORE INTO tasks "
                    "(id, title, description, status, priority, section, due_date, "
                    "project, parent_id, notes, recurring, reminder_at, type, assignee, shared_by, "
                    "visibility, publish_requested_at, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        tid,
                        remote.get("title") or "",
                        desc,
                        tombstone_status,
                        remote.get("priority", "medium"),
                        remote.get("section", "inbox"),
                        remote.get("due_date"),
                        remote.get("project"),
                        parent_id,
                        notes,
                        remote.get("recurring"),
                        remote.get("reminder_at"),
                        remote.get("type", "task"),
                        remote.get("assignee"),
                        remote.get("shared_by"),
                        remote.get("visibility", "private"),
                        remote.get("publish_requested_at"),
                        created_at,
                        updated_at,
                    ),
                )
                for field in MERGEABLE_FIELDS:
                    fts, fby, forder, fevent = _parse_field_ts(
                        remote_fts, field, fallback_ts
                    )
                    _store_task_field_version(
                        conn,
                        tid,
                        field,
                        updated_at=fts,
                        updated_by=fby,
                        updated_order=forder,
                        source_event_id=fevent,
                    )
                new_count += 1
            continue

        # Match by UUID only — authoritative in LWW model
        existing = existing_map.get(tid)

        if existing:
            local_id = existing["id"]
            local_fvs = fv_map.get(local_id, {})
            local_updated_at = existing["updated_at"] or ""
            remote_updated_at = remote.get("updated_at", "")

            # Per-field LWW merge
            fields_to_update: dict[str, Any] = {}
            local_status_state = local_status_authority.get(local_id)
            local_status_value = _normalize_task_status_value(
                (local_status_state or {}).get("value")
            )
            if (
                local_status_value is not None
                and local_status_value
                != task_content_map.get(local_id, {}).get("status")
            ):
                fields_to_update["status"] = local_status_value
                task_content_map[local_id]["status"] = local_status_value
                status_ts = str((local_status_state or {}).get("updated_at") or "")
                semantic_update_timestamps = [status_ts] if status_ts else []
                updated_fields += 1
            else:
                semantic_update_timestamps = []
            for field in fields_to_merge:
                if field not in remote:
                    continue
                # Skip fields already corrected by status authority (PF-01 fix)
                if field in fields_to_update:
                    continue
                remote_val = remote.get(field)
                remote_ts, remote_by, remote_order, remote_event_id = _parse_field_ts(
                    remote_fts, field, fallback_ts
                )
                explicit_value = _field_ts_explicit_value(remote_fts, field)
                has_explicit_authority = (
                    explicit_value is not _FIELD_VALUE_MISSING
                    or bool(remote_event_id and remote_event_id in remote_event_by_id)
                )
                remote_has_field_ts = (
                    isinstance(remote_fts, dict) and field in remote_fts
                )
                if explicit_value is not _FIELD_VALUE_MISSING:
                    remote_val = explicit_value

                local_fv = local_fvs.get(field)
                local_val = task_content_map.get(local_id, {}).get(field)
                local_ts = local_fv[0] if local_fv else ""
                local_by = local_fv[1] if local_fv else ""
                local_order = local_fv[2] if local_fv else 0
                local_event_id = local_fv[3] if local_fv and len(local_fv) > 3 else None
                remote_key = _field_version_sort_key(remote_ts, remote_by, remote_order)
                local_key = _field_version_sort_key(local_ts, local_by, local_order)
                legacy_value_authority = (
                    remote_has_field_ts
                    and _legacy_payload_value_is_authoritative(
                        field=field,
                        source_machine_id=source_machine_id,
                        updated_by=remote_by,
                    )
                )
                legacy_terminal_promotion = _legacy_terminal_status_row_can_promote(
                    remote_value=remote_val,
                    local_value=local_val,
                    source_machine_id=source_machine_id,
                    remote_updated_at=remote_updated_at,
                    remote_field_ts=remote_ts,
                    local_updated_at=local_updated_at,
                    has_explicit_authority=has_explicit_authority,
                    legacy_value_authority=legacy_value_authority,
                )
                if legacy_terminal_promotion:
                    remote_ts = remote_updated_at or fallback_ts
                    remote_by = str(source_machine_id or remote_by or MACHINE_ID)
                    remote_order = _pack_logical_clock(_iso_to_epoch_ms(remote_ts), 0)
                    remote_event_id = None
                    remote_key = _field_version_sort_key(
                        remote_ts, remote_by, remote_order
                    )
                    legacy_value_authority = True
                remote_wins = remote_key > local_key
                remote_repairs_equal_key = (
                    remote_key == local_key
                    and local_val != remote_val
                    and (has_explicit_authority or legacy_value_authority)
                )

                if remote_wins or remote_repairs_equal_key:
                    local_task_status = _normalize_task_status_value(
                        task_content_map.get(local_id, {}).get("status")
                    )
                    remote_task_status = _normalize_task_status_value(
                        remote.get("status")
                    )
                    hidden_local_remote_reopens = (
                        local_task_status in TASK_HIDDEN_STATUSES
                        and remote_task_status not in TASK_HIDDEN_STATUSES
                    )
                    if hidden_local_remote_reopens and field in {
                        "status",
                        "section",
                        "priority",
                        "due_date",
                        "reminder_at",
                        "recurring",
                    }:
                        if local_val != remote_val:
                            record_memory_conflict(
                                conn,
                                aggregate_kind="task",
                                aggregate_id=local_id,
                                field_name=field,
                                local_value=local_val,
                                remote_value=remote_val,
                                local_updated_at=local_ts,
                                remote_updated_at=remote_ts,
                                local_updated_order=local_order,
                                remote_updated_order=remote_order,
                                local_source_event_id=local_event_id,
                                remote_source_event_id=remote_event_id,
                                winner="guard_local",
                                rationale=(
                                    "hidden terminal local task state blocks "
                                    "remote non-hidden resurrection"
                                ),
                            )
                        _log.warning(
                            "Bridge hidden-status guard: keeping local %s for task %s "
                            "(local status %s, remote status %s)",
                            field,
                            local_id,
                            local_task_status,
                            remote_task_status,
                        )
                        continue
                    if (
                        field in FIELD_TS_VALUE_FIELDS
                        and remote_has_field_ts
                        and not has_explicit_authority
                        and not legacy_value_authority
                    ):
                        if local_val != remote_val:
                            record_memory_conflict(
                                conn,
                                aggregate_kind="task",
                                aggregate_id=local_id,
                                field_name=field,
                                local_value=local_val,
                                remote_value=remote_val,
                                local_updated_at=local_ts,
                                remote_updated_at=remote_ts,
                                local_updated_order=local_order,
                                remote_updated_order=remote_order,
                                local_source_event_id=local_event_id,
                                remote_source_event_id=remote_event_id,
                                winner="guard_local",
                                rationale=(
                                    "legacy bridge field timestamp lacks value/event "
                                    "and payload source does not match field writer"
                                ),
                            )
                        _log.warning(
                            "LWW legacy value guard: keeping local %s for task %s "
                            "(payload source %s, field writer %s)",
                            field,
                            local_id,
                            source_machine_id,
                            remote_by,
                        )
                        continue
                    # Content protection: never nullify or drastically shrink local content
                    if field in CONTENT_FIELDS:
                        if has_meaningful_content(
                            local_val
                        ) and not has_meaningful_content(remote_val):
                            if local_val != remote_val:
                                record_memory_conflict(
                                    conn,
                                    aggregate_kind="task",
                                    aggregate_id=local_id,
                                    field_name=field,
                                    local_value=local_val,
                                    remote_value=remote_val,
                                    local_updated_at=local_ts,
                                    remote_updated_at=remote_ts,
                                    local_updated_order=local_order,
                                    remote_updated_order=remote_order,
                                    local_source_event_id=local_event_id,
                                    remote_source_event_id=remote_event_id,
                                    winner="guard_local",
                                    rationale="content protection kept non-empty local content",
                                )
                            _log.warning(
                                "LWW content protection: keeping local %s for task %s "
                                "(remote is empty but local has %d chars)",
                                field,
                                local_id,
                                content_length(local_val),
                            )
                            continue
                        if is_suspicious_content_shrink(local_val, remote_val):
                            record_memory_conflict(
                                conn,
                                aggregate_kind="task",
                                aggregate_id=local_id,
                                field_name=field,
                                local_value=local_val,
                                remote_value=remote_val,
                                local_updated_at=local_ts,
                                remote_updated_at=remote_ts,
                                local_updated_order=local_order,
                                remote_updated_order=remote_order,
                                local_source_event_id=local_event_id,
                                remote_source_event_id=remote_event_id,
                                winner="guard_local",
                                rationale="shrink guard kept larger local content",
                            )
                            _log.warning(
                                "LWW shrink guard: keeping local %s for task %s "
                                "(remote would shrink %d -> %d chars)",
                                field,
                                local_id,
                                content_length(local_val),
                                content_length(remote_val),
                            )
                            continue
                    _store_task_field_version(
                        conn,
                        local_id,
                        field,
                        updated_at=remote_ts,
                        updated_by=remote_by,
                        old_value=str(local_val)[:500]
                        if local_val is not None
                        else None,
                        new_value=str(remote_val)[:500]
                        if remote_val is not None
                        else None,
                        updated_order=remote_order,
                        source_event_id=remote_event_id,
                    )
                    if local_val != remote_val:
                        fields_to_update[field] = remote_val
                        semantic_update_timestamps.append(remote_ts or fallback_ts)
                        updated_fields += 1
                        record_memory_conflict(
                            conn,
                            aggregate_kind="task",
                            aggregate_id=local_id,
                            field_name=field,
                            local_value=local_val,
                            remote_value=remote_val,
                            local_updated_at=local_ts,
                            remote_updated_at=remote_ts,
                            local_updated_order=local_order,
                            remote_updated_order=remote_order,
                            local_source_event_id=local_event_id,
                            remote_source_event_id=remote_event_id,
                            winner="remote",
                            rationale=(
                                "legacy terminal row promoted stale status field version"
                                if legacy_terminal_promotion
                                else "remote field version outranked local field version"
                            ),
                        )
                elif local_val != remote_val and (local_ts or remote_ts):
                    record_memory_conflict(
                        conn,
                        aggregate_kind="task",
                        aggregate_id=local_id,
                        field_name=field,
                        local_value=local_val,
                        remote_value=remote_val,
                        local_updated_at=local_ts,
                        remote_updated_at=remote_ts,
                        local_updated_order=local_order,
                        remote_updated_order=remote_order,
                        local_source_event_id=local_event_id,
                        remote_source_event_id=remote_event_id,
                        winner="local",
                        rationale="local field version outranked remote field version",
                    )

            # NULL-fill: adopt remote content fields when local is NULL
            # (non-LWW — only fills gaps, never overwrites existing content)
            # Only applied when import_content=True to respect metadata-only sync.
            for content_field in CONTENT_FIELDS if import_content else ():
                if content_field in fields_to_update:
                    continue  # already handled by LWW above
                remote_val = remote.get(content_field)
                if not remote_val:
                    continue
                local_content = task_content_map.get(local_id, {})
                if (
                    local_id in task_content_map
                    and local_content.get(content_field) is None
                ):
                    fields_to_update[content_field] = remote_val
                    remote_ts, _, _, _ = _parse_field_ts(
                        remote_fts, content_field, fallback_ts
                    )
                    semantic_update_timestamps.append(remote_ts or fallback_ts)
                    updated_fields += 1

            if fields_to_update:
                # Validate field names against allowlist (defense-in-depth)
                safe_fields = {
                    k: v for k, v in fields_to_update.items() if k in MERGEABLE_FIELDS
                }
                if safe_fields:
                    semantic_updated_at = _max_iso_timestamp(
                        local_updated_at,
                        *semantic_update_timestamps,
                    )
                    if semantic_updated_at:
                        safe_fields["updated_at"] = semantic_updated_at
                    # A merged status change yields a new local tombstone state
                    # that THIS machine has not pushed; clear the prior push stamp
                    # so it must be re-pushed before aging out of export / Tier-2.
                    if "status" in safe_fields:
                        safe_fields["tombstone_pushed_at"] = None
                    set_clause = ", ".join(f"{k} = ?" for k in safe_fields)
                    values = list(safe_fields.values()) + [local_id]
                    conn.execute(f"UPDATE tasks SET {set_clause} WHERE id = ?", values)
                    # PF-03 fix: record merge in event ledger
                    for merged_field in safe_fields:
                        if merged_field == "updated_at":
                            continue
                        record_memory_event(
                            conn,
                            event_type="merge",
                            aggregate_kind="task",
                            aggregate_id=local_id,
                            field_name=merged_field,
                            new_value=str(safe_fields[merged_field])
                            if safe_fields[merged_field] is not None
                            else None,
                        )
            elif _timestamp_is_newer(remote_updated_at, local_updated_at):
                _log.debug(
                    "Bridge metadata-only freshness ignored for task %s "
                    "(remote updated_at %s > local %s, no field winner)",
                    local_id,
                    remote_updated_at,
                    local_updated_at,
                )
        else:
            # New task — insert (content only if import_content)
            # Note: cancelled tasks still exist as rows (soft-delete), so they're
            # handled by the `if existing:` branch above — LWW field versioning
            # prevents remote from overwriting the newer local cancelled status.

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
                    parent_id,
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
            # Seed field versions from remote _field_ts (batch insert)
            for field in MERGEABLE_FIELDS:
                fts, fby, forder, fevent = _parse_field_ts(
                    remote_fts, field, fallback_ts
                )
                _store_task_field_version(
                    conn,
                    tid,
                    field,
                    updated_at=fts,
                    updated_by=fby,
                    updated_order=forder,
                    source_event_id=fevent,
                )
            new_count += 1

    # Import task-entity links from remote tasks (reuse `now` from above)
    for rt in remote_tasks:
        remote_links = rt.get("_links")
        if not remote_links:
            continue
        tid = rt.get("id", "")
        if not isinstance(tid, str) or not tid:
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
    if not isinstance(task_id, str) or not task_id:
        return None
    real_base = os.path.realpath(bridge_dir)
    task_file = _task_storage_path(task_id, bridge_dir)
    if not os.path.realpath(task_file).startswith(real_base):
        return None  # path traversal attempt
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
            if not isinstance(tid, str) or not tid:
                continue
            if "_field_ts" not in task:
                task["_field_ts"] = {}
            (tmp_dir / f"{_task_storage_stem(tid)}.json").write_text(
                json_dumps(task),
                encoding="utf-8",
            )
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


def export_entity_files(
    conn: sqlite3.Connection, bridge_dir: str
) -> tuple[list[int], list[sqlite3.Row]]:
    """Export shared entities to per-entity JSON files in entities/ directory.

    Each file: entities/{id}.json with full observations.
    Returns (exported_ids, entity_rows) — pass rows to export_entities_index().
    """
    entities_dir = Path(bridge_dir) / "entities"
    entities_dir.mkdir(exist_ok=True)

    rows = conn.execute(
        f"SELECT {ENTITY_EXPORT_COLS} FROM entities "
        "WHERE project LIKE 'shared%' ORDER BY name"
    ).fetchall()

    if not rows:
        return [], []

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

    return exported, list(rows)


def export_entities_index(
    conn: sqlite3.Connection, bridge_dir: str, rows: list | None = None
) -> int:
    """Write entities_index.json — metadata only, no observations.

    Pass rows from export_entity_files() to avoid a redundant query.
    """
    if rows is None:
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
    real_base = os.path.realpath(bridge_dir)
    fpath = Path(bridge_dir) / "entities" / f"{eid_str}.json"
    if not os.path.realpath(fpath).startswith(real_base):
        return None  # path traversal attempt
    if not fpath.exists():
        return None
    try:
        return json_loads(fpath.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None


def load_entities_from_files(bridge_dir: str) -> list[dict]:
    """Read entities from per-entity files using entities_index.json as manifest.

    Returns list of entity dicts (with observations). Falls back to index
    metadata if per-entity file is missing.
    """
    index_path = Path(bridge_dir) / "entities_index.json"
    if not index_path.exists():
        return []
    try:
        index_data = json_loads(index_path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return []
    entities = []
    for meta in index_data.get("entities", []):
        eid = meta.get("id")
        if eid is None:
            continue
        content = load_entity_content(eid, bridge_dir)
        entities.append(content if content else meta)
    return entities


def collect_legacy_bridge_tasks(payload: dict[str, Any]) -> list[dict]:
    """Collect legacy task arrays from shared.json-style bridge payloads."""
    source_machine_id = payload.get("machine_id")
    tasks = [dict(task) for task in payload.get("tasks", []) if isinstance(task, dict)]
    for key, value in payload.items():
        if (
            key.endswith("_tasks")
            and key not in {"tasks", "shared_tasks"}
            and isinstance(value, list)
        ):
            tasks.extend(dict(task) for task in value if isinstance(task, dict))
    for task in tasks:
        task.setdefault("_source_machine_id", source_machine_id)
    return tasks


def load_remote_entities_for_import(
    bridge_dir: str,
    payload: dict[str, Any],
    logger: logging.Logger | None = None,
) -> list[dict]:
    """Load remote bridge entities, falling back to shared.json on manifest errors."""
    index_path = Path(bridge_dir) / "entities_index.json"
    if not index_path.exists():
        return list(payload.get("entities", []))
    try:
        json_loads(index_path.read_text(encoding="utf-8"))
    except (ValueError, OSError, TypeError) as exc:
        if logger is not None:
            logger.warning(
                "entities_index.json read failed: %s; falling back to shared.json entities",
                exc,
            )
        return list(payload.get("entities", []))
    return load_entities_from_files(bridge_dir)


def load_remote_tasks_for_merge(
    bridge_dir: str,
    payload: dict[str, Any],
    logger: logging.Logger | None = None,
) -> tuple[list[dict], bool]:
    """Load bridge tasks, hydrating per-task content and falling back on manifest errors.

    Returns (tasks, loaded_from_index_json).
    """
    index_path = Path(bridge_dir) / "index.json"
    if not index_path.exists():
        return collect_legacy_bridge_tasks(payload), False

    try:
        idx_data = json_loads(index_path.read_text(encoding="utf-8"))
    except (ValueError, OSError, TypeError) as exc:
        if logger is not None:
            logger.warning(
                "index.json read failed: %s; falling back to shared.json tasks",
                exc,
            )
        return collect_legacy_bridge_tasks(payload), False

    source_machine_id = idx_data.get("machine_id") or payload.get("machine_id")
    remote_tasks = [
        dict(task) for task in idx_data.get("tasks", []) if isinstance(task, dict)
    ]
    enriched = 0
    for task in remote_tasks:
        task.setdefault("_source_machine_id", source_machine_id)
        content = load_task_content(task.get("id", ""), bridge_dir)
        if not content:
            continue
        for field in CONTENT_FIELDS:
            if field in content:
                task[field] = content[field]
        for field in TASK_FILE_HYDRATION_FIELDS:
            if field in content:
                task[field] = content[field]
        task.setdefault("_source_machine_id", source_machine_id)
        if content.get("description") or content.get("notes"):
            enriched += 1

    if enriched and logger is not None:
        logger.info(
            "bridge tasks: enriched %d tasks with content from per-task files",
            enriched,
        )
    return remote_tasks, True


def import_bridge_entities_and_relations(
    conn: sqlite3.Connection,
    entities: list[dict],
    relations: list[dict],
) -> tuple[int, int, int]:
    """Merge bridge entities/observations/relations into the local DB."""
    now = now_iso()
    new_entities = 0
    new_observations = 0
    new_relations = 0

    for ent in entities:
        cur = conn.execute(
            "INSERT OR IGNORE INTO entities "
            "(name, entity_type, project, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                ent["name"],
                ent["entityType"],
                ent.get("project"),
                ent.get("createdAt", now),
                ent.get("updatedAt", now),
            ),
        )
        new_entities += cur.rowcount

        eid = get_entity_id(conn, ent["name"])
        if not eid:
            continue
        for obs in ent.get("observations", []):
            content = obs["content"] if isinstance(obs, dict) else obs
            created = obs.get("createdAt", now) if isinstance(obs, dict) else now
            cur2 = conn.execute(
                "INSERT OR IGNORE INTO observations (entity_id, content, created_at) "
                "VALUES (?, ?, ?)",
                (eid, content, created),
            )
            new_observations += cur2.rowcount
        fts_sync_entity(conn, eid)

    for rel in relations:
        from_id = get_entity_id(conn, rel["from"])
        to_id = get_entity_id(conn, rel["to"])
        if not from_id or not to_id:
            continue
        cur3 = conn.execute(
            "INSERT OR IGNORE INTO relations "
            "(from_id, to_id, relation_type, created_at) VALUES (?, ?, ?, ?)",
            (
                from_id,
                to_id,
                rel["relationType"],
                rel.get("createdAt", now),
            ),
        )
        new_relations += cur3.rowcount

    return new_entities, new_observations, new_relations


def import_bridge_knowledge_ratings(
    conn: sqlite3.Connection,
    ratings: list[dict],
) -> int:
    """Merge bridge knowledge ratings into the local DB."""
    imported = 0
    for rating in ratings:
        cur = conn.execute(
            "INSERT OR IGNORE INTO knowledge_ratings "
            "(entity_name, rater_id, content_hash, specificity, falsifiability, "
            "internal_consistency, novelty, verification_outcome, usefulness, "
            "verification_context, rated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                rating.get("entity_name"),
                rating.get("rater_id"),
                rating.get("content_hash"),
                rating.get("specificity"),
                rating.get("falsifiability"),
                rating.get("internal_consistency"),
                rating.get("novelty"),
                rating.get("verification_outcome"),
                rating.get("usefulness"),
                rating.get("verification_context"),
                rating.get("rated_at") or now_iso(),
            ),
        )
        imported += cur.rowcount
    return imported


def _build_remote_event_heads(
    events: list[dict] | None,
    aggregate_kind: str,
) -> dict[str, dict[str, Any]]:
    heads: dict[str, dict[str, Any]] = {}
    if not events:
        return heads
    for event in events:
        if event.get("aggregate_kind") != aggregate_kind:
            continue
        aggregate_id = str(event.get("aggregate_id") or "")
        if not aggregate_id:
            continue
        current = heads.get(aggregate_id)
        if current is None or _event_sort_key(
            event.get("event_ts"),
            event.get("machine_id"),
            int(event.get("logical_clock") or 0),
        ) > _event_sort_key(
            current.get("event_ts"),
            current.get("machine_id"),
            int(current.get("logical_clock") or 0),
        ):
            heads[aggregate_id] = event
    return heads


def _load_local_event_heads(
    conn: sqlite3.Connection,
    aggregate_kind: str,
    aggregate_ids: list[str],
) -> dict[str, dict[str, Any]]:
    if not aggregate_ids or not _sqlite_table_exists(conn, "memory_events"):
        return {}
    placeholders = ",".join("?" * len(aggregate_ids))
    rows = conn.execute(
        "SELECT aggregate_id, machine_id, logical_clock, event_ts "
        "FROM memory_events WHERE aggregate_kind = ? AND aggregate_id IN ("
        + placeholders
        + ")",
        (aggregate_kind, *aggregate_ids),
    ).fetchall()
    heads: dict[str, dict[str, Any]] = {}
    for row in rows:
        aggregate_id = row["aggregate_id"]
        current = heads.get(aggregate_id)
        candidate = dict(row)
        if current is None or _event_sort_key(
            candidate.get("event_ts"),
            candidate.get("machine_id"),
            int(candidate.get("logical_clock") or 0),
        ) > _event_sort_key(
            current.get("event_ts"),
            current.get("machine_id"),
            int(current.get("logical_clock") or 0),
        ):
            heads[aggregate_id] = candidate
    return heads


def _remote_row_is_newer(
    *,
    local_updated_at: str | None,
    remote_updated_at: str | None,
    local_event_head: dict[str, Any] | None,
    remote_event_head: dict[str, Any] | None,
) -> bool:
    if local_event_head or remote_event_head:
        local_key = _event_sort_key(
            (local_event_head or {}).get("event_ts") or local_updated_at,
            (local_event_head or {}).get("machine_id"),
            int((local_event_head or {}).get("logical_clock") or 0),
        )
        remote_key = _event_sort_key(
            (remote_event_head or {}).get("event_ts") or remote_updated_at,
            (remote_event_head or {}).get("machine_id"),
            int((remote_event_head or {}).get("logical_clock") or 0),
        )
        return remote_key > local_key
    return (remote_updated_at or "") > (local_updated_at or "")


def export_candidate_claims(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    if not _sqlite_table_exists(conn, "candidate_claims"):
        return []
    rows = conn.execute(
        "SELECT claim_id, chunk_id, subject, predicate, object_text, object_type, "
        "claim_scope, confidence, status, requires_human, promoted_to_fact_id, "
        "created_at, updated_at "
        "FROM candidate_claims ORDER BY created_at, claim_id"
    ).fetchall()
    return [dict(r) for r in rows]


def export_context_chunks(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    if not _sqlite_table_exists(conn, "context_chunks"):
        return []
    rows = conn.execute(
        "SELECT chunk_id, session_id, entity_id, source_type, source_ref, source_hash, "
        "title, body, language, state, enrich_policy, materiality_score, "
        "last_human_update_at, last_ai_attempt_at, created_at, updated_at "
        "FROM context_chunks ORDER BY created_at, chunk_id"
    ).fetchall()
    return [dict(r) for r in rows]


def import_context_chunks(
    conn: sqlite3.Connection,
    chunks: list[dict],
    *,
    remote_event_heads: dict[str, dict[str, Any]] | None = None,
) -> int:
    if not _sqlite_table_exists(conn, "context_chunks"):
        return 0
    chunk_ids = [str(ch.get("chunk_id") or "") for ch in chunks if ch.get("chunk_id")]
    local_rows: dict[str, sqlite3.Row] = {}
    if chunk_ids:
        placeholders = ",".join("?" * len(chunk_ids))
        rows = conn.execute(
            "SELECT chunk_id, updated_at FROM context_chunks WHERE chunk_id IN ("
            + placeholders
            + ")",
            tuple(chunk_ids),
        ).fetchall()
        local_rows = {r["chunk_id"]: r for r in rows}
    local_heads = _load_local_event_heads(conn, "chunk", chunk_ids)
    imported = 0
    for chunk in chunks:
        chunk_id = chunk.get("chunk_id")
        if not chunk_id:
            continue
        local_row = local_rows.get(chunk_id)
        if local_row and not _remote_row_is_newer(
            local_updated_at=local_row["updated_at"],
            remote_updated_at=chunk.get("updated_at"),
            local_event_head=local_heads.get(chunk_id),
            remote_event_head=(remote_event_heads or {}).get(chunk_id),
        ):
            continue
        conn.execute(
            "INSERT INTO context_chunks "
            "(chunk_id, session_id, entity_id, source_type, source_ref, source_hash, "
            "title, body, language, state, enrich_policy, materiality_score, "
            "last_human_update_at, last_ai_attempt_at, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(chunk_id) DO UPDATE SET "
            "session_id = excluded.session_id, "
            "entity_id = excluded.entity_id, "
            "source_type = excluded.source_type, "
            "source_ref = excluded.source_ref, "
            "source_hash = excluded.source_hash, "
            "title = excluded.title, "
            "body = excluded.body, "
            "language = excluded.language, "
            "state = excluded.state, "
            "enrich_policy = excluded.enrich_policy, "
            "materiality_score = excluded.materiality_score, "
            "last_human_update_at = excluded.last_human_update_at, "
            "last_ai_attempt_at = excluded.last_ai_attempt_at, "
            "updated_at = excluded.updated_at",
            (
                chunk.get("chunk_id"),
                chunk.get("session_id"),
                chunk.get("entity_id"),
                chunk.get("source_type"),
                chunk.get("source_ref"),
                chunk.get("source_hash"),
                chunk.get("title"),
                chunk.get("body", ""),
                chunk.get("language"),
                chunk.get("state", "no_enrich"),
                chunk.get("enrich_policy", "manual"),
                chunk.get("materiality_score", 0.0),
                chunk.get("last_human_update_at"),
                chunk.get("last_ai_attempt_at"),
                chunk.get("created_at") or now_iso(),
                chunk.get("updated_at") or now_iso(),
            ),
        )
        imported += 1
    return imported


def export_context_annotations(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    if not _sqlite_table_exists(conn, "context_annotations"):
        return []
    rows = conn.execute(
        "SELECT annotation_id, chunk_id, author_type, annotation_type, body, "
        "source_hash_seen, created_at FROM context_annotations "
        "ORDER BY created_at, annotation_id"
    ).fetchall()
    return [dict(r) for r in rows]


def import_context_annotations(
    conn: sqlite3.Connection, annotations: list[dict]
) -> int:
    if not _sqlite_table_exists(conn, "context_annotations"):
        return 0
    imported = 0
    for ann in annotations:
        conn.execute(
            "INSERT OR IGNORE INTO context_annotations "
            "(annotation_id, chunk_id, author_type, annotation_type, body, source_hash_seen, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                ann.get("annotation_id"),
                ann.get("chunk_id"),
                ann.get("author_type"),
                ann.get("annotation_type"),
                ann.get("body", ""),
                ann.get("source_hash_seen"),
                ann.get("created_at") or now_iso(),
            ),
        )
        imported += conn.execute("SELECT changes()").fetchone()[0]
    return imported


def export_context_questions(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    if not _sqlite_table_exists(conn, "context_questions"):
        return []
    rows = conn.execute(
        "SELECT question_id, chunk_id, question_text, question_type, priority_score, "
        "state, answered_by, answered_at, answer_text, created_at "
        "FROM context_questions ORDER BY created_at, question_id"
    ).fetchall()
    return [dict(r) for r in rows]


def import_context_questions(
    conn: sqlite3.Connection,
    questions: list[dict],
    *,
    remote_event_heads: dict[str, dict[str, Any]] | None = None,
) -> int:
    if not _sqlite_table_exists(conn, "context_questions"):
        return 0
    question_ids = [
        str(q.get("question_id") or "") for q in questions if q.get("question_id")
    ]
    local_rows: dict[str, sqlite3.Row] = {}
    if question_ids:
        placeholders = ",".join("?" * len(question_ids))
        rows = conn.execute(
            "SELECT question_id, answered_at, created_at FROM context_questions "
            "WHERE question_id IN (" + placeholders + ")",
            tuple(question_ids),
        ).fetchall()
        local_rows = {r["question_id"]: r for r in rows}
    local_heads = _load_local_event_heads(conn, "question", question_ids)
    imported = 0
    for question in questions:
        question_id = question.get("question_id")
        if not question_id:
            continue
        local_row = local_rows.get(question_id)
        local_updated_at = (
            (local_row["answered_at"] or local_row["created_at"]) if local_row else None
        )
        remote_updated_at = question.get("answered_at") or question.get("created_at")
        if local_row and not _remote_row_is_newer(
            local_updated_at=local_updated_at,
            remote_updated_at=remote_updated_at,
            local_event_head=local_heads.get(question_id),
            remote_event_head=(remote_event_heads or {}).get(question_id),
        ):
            continue
        conn.execute(
            "INSERT INTO context_questions "
            "(question_id, chunk_id, question_text, question_type, priority_score, "
            "state, answered_by, answered_at, answer_text, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(question_id) DO UPDATE SET "
            "chunk_id = excluded.chunk_id, "
            "question_text = excluded.question_text, "
            "question_type = excluded.question_type, "
            "priority_score = excluded.priority_score, "
            "state = excluded.state, "
            "answered_by = excluded.answered_by, "
            "answered_at = excluded.answered_at, "
            "answer_text = excluded.answer_text",
            (
                question.get("question_id"),
                question.get("chunk_id"),
                question.get("question_text", ""),
                question.get("question_type", ""),
                question.get("priority_score", 0.0),
                question.get("state", "open"),
                question.get("answered_by"),
                question.get("answered_at"),
                question.get("answer_text"),
                question.get("created_at") or now_iso(),
            ),
        )
        imported += 1
    return imported


def import_candidate_claims(
    conn: sqlite3.Connection,
    claims: list[dict],
    *,
    remote_event_heads: dict[str, dict[str, Any]] | None = None,
) -> int:
    if not _sqlite_table_exists(conn, "candidate_claims"):
        return 0
    claim_ids = [
        str(claim.get("claim_id") or "") for claim in claims if claim.get("claim_id")
    ]
    local_rows: dict[str, sqlite3.Row] = {}
    if claim_ids:
        placeholders = ",".join("?" * len(claim_ids))
        rows = conn.execute(
            "SELECT claim_id, updated_at FROM candidate_claims WHERE claim_id IN ("
            + placeholders
            + ")",
            tuple(claim_ids),
        ).fetchall()
        local_rows = {r["claim_id"]: r for r in rows}
    local_heads = _load_local_event_heads(conn, "claim", claim_ids)
    imported = 0
    for claim in claims:
        claim_id = claim.get("claim_id")
        row = local_rows.get(claim_id)
        if row and not _remote_row_is_newer(
            local_updated_at=row["updated_at"],
            remote_updated_at=claim.get("updated_at"),
            local_event_head=local_heads.get(str(claim_id)),
            remote_event_head=(remote_event_heads or {}).get(str(claim_id)),
        ):
            continue
        conn.execute(
            "INSERT INTO candidate_claims "
            "(claim_id, chunk_id, subject, predicate, object_text, object_type, "
            "claim_scope, confidence, status, requires_human, promoted_to_fact_id, "
            "created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(claim_id) DO UPDATE SET "
            "chunk_id = excluded.chunk_id, "
            "subject = excluded.subject, "
            "predicate = excluded.predicate, "
            "object_text = excluded.object_text, "
            "object_type = excluded.object_type, "
            "claim_scope = excluded.claim_scope, "
            "confidence = excluded.confidence, "
            "status = excluded.status, "
            "requires_human = excluded.requires_human, "
            "promoted_to_fact_id = excluded.promoted_to_fact_id, "
            "updated_at = excluded.updated_at",
            (
                claim.get("claim_id"),
                claim.get("chunk_id"),
                claim.get("subject"),
                claim.get("predicate"),
                claim.get("object_text"),
                claim.get("object_type", "text"),
                claim.get("claim_scope", "memory"),
                claim.get("confidence", 0.0),
                claim.get("status", "candidate"),
                claim.get("requires_human", 1),
                claim.get("promoted_to_fact_id"),
                claim.get("created_at") or now_iso(),
                claim.get("updated_at") or now_iso(),
            ),
        )
        imported += 1
    return imported


def export_claim_evidence(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    if not _sqlite_table_exists(conn, "claim_evidence"):
        return []
    cols = [
        "evidence_id",
        "claim_id",
        "evidence_type",
        "evidence_ref",
        "weight",
        "excerpt",
        "created_at",
    ]
    if _sqlite_has_column(conn, "claim_evidence", "source_start"):
        cols.append("source_start")
    if _sqlite_has_column(conn, "claim_evidence", "source_end"):
        cols.append("source_end")
    rows = conn.execute(
        f"SELECT {', '.join(cols)} FROM claim_evidence ORDER BY created_at, evidence_id"
    ).fetchall()
    return [dict(r) for r in rows]


def import_claim_evidence(conn: sqlite3.Connection, evidence_rows: list[dict]) -> int:
    if not _sqlite_table_exists(conn, "claim_evidence"):
        return 0
    has_start = _sqlite_has_column(conn, "claim_evidence", "source_start")
    has_end = _sqlite_has_column(conn, "claim_evidence", "source_end")
    imported = 0
    for ev in evidence_rows:
        columns = [
            "evidence_id",
            "claim_id",
            "evidence_type",
            "evidence_ref",
            "weight",
            "excerpt",
            "created_at",
        ]
        values: list[Any] = [
            ev.get("evidence_id"),
            ev.get("claim_id"),
            ev.get("evidence_type"),
            ev.get("evidence_ref"),
            ev.get("weight", 1.0),
            ev.get("excerpt"),
            ev.get("created_at") or now_iso(),
        ]
        if has_start:
            columns.append("source_start")
            values.append(ev.get("source_start"))
        if has_end:
            columns.append("source_end")
            values.append(ev.get("source_end"))
        placeholders = ", ".join("?" for _ in columns)
        conn.execute(
            f"INSERT OR IGNORE INTO claim_evidence ({', '.join(columns)}) "
            f"VALUES ({placeholders})",
            values,
        )
        imported += conn.execute("SELECT changes()").fetchone()[0]
    return imported


def export_canonical_facts(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    if not _sqlite_table_exists(conn, "canonical_facts"):
        return []
    cols = [
        "fact_id",
        "subject",
        "predicate",
        "object_text",
        "object_type",
        "fact_scope",
        "provenance_summary",
        "confidence",
        "validation_mode",
        "source_claim_id",
        "created_at",
        "updated_at",
    ]
    for col in (
        "valid_from",
        "valid_to",
        "superseded_by_fact_id",
        "contradiction_count",
    ):
        if _sqlite_has_column(conn, "canonical_facts", col):
            cols.append(col)
    rows = conn.execute(
        f"SELECT {', '.join(cols)} FROM canonical_facts ORDER BY created_at, fact_id"
    ).fetchall()
    return [dict(r) for r in rows]


def import_canonical_facts(
    conn: sqlite3.Connection,
    facts: list[dict],
    *,
    remote_event_heads: dict[str, dict[str, Any]] | None = None,
) -> int:
    if not _sqlite_table_exists(conn, "canonical_facts"):
        return 0
    fact_ids = [str(fact.get("fact_id") or "") for fact in facts if fact.get("fact_id")]
    local_rows: dict[str, sqlite3.Row] = {}
    if fact_ids:
        placeholders = ",".join("?" * len(fact_ids))
        rows = conn.execute(
            "SELECT fact_id, updated_at FROM canonical_facts WHERE fact_id IN ("
            + placeholders
            + ")",
            tuple(fact_ids),
        ).fetchall()
        local_rows = {r["fact_id"]: r for r in rows}
    local_heads = _load_local_event_heads(conn, "fact", fact_ids)
    has_valid_from = _sqlite_has_column(conn, "canonical_facts", "valid_from")
    has_valid_to = _sqlite_has_column(conn, "canonical_facts", "valid_to")
    has_superseded = _sqlite_has_column(
        conn, "canonical_facts", "superseded_by_fact_id"
    )
    has_contradiction_count = _sqlite_has_column(
        conn, "canonical_facts", "contradiction_count"
    )
    imported = 0
    for fact in facts:
        fact_id = fact.get("fact_id")
        row = local_rows.get(fact_id)
        if row and not _remote_row_is_newer(
            local_updated_at=row["updated_at"],
            remote_updated_at=fact.get("updated_at"),
            local_event_head=local_heads.get(str(fact_id)),
            remote_event_head=(remote_event_heads or {}).get(str(fact_id)),
        ):
            continue
        columns = [
            "fact_id",
            "subject",
            "predicate",
            "object_text",
            "object_type",
            "fact_scope",
            "provenance_summary",
            "confidence",
            "validation_mode",
            "source_claim_id",
            "created_at",
            "updated_at",
        ]
        values: list[Any] = [
            fact.get("fact_id"),
            fact.get("subject"),
            fact.get("predicate"),
            fact.get("object_text"),
            fact.get("object_type", "text"),
            fact.get("fact_scope", "memory"),
            fact.get("provenance_summary", ""),
            fact.get("confidence", 0.0),
            fact.get("validation_mode", "imported"),
            fact.get("source_claim_id"),
            fact.get("created_at") or now_iso(),
            fact.get("updated_at") or now_iso(),
        ]
        if has_valid_from:
            columns.append("valid_from")
            values.append(fact.get("valid_from"))
        if has_valid_to:
            columns.append("valid_to")
            values.append(fact.get("valid_to"))
        if has_superseded:
            columns.append("superseded_by_fact_id")
            values.append(fact.get("superseded_by_fact_id"))
        if has_contradiction_count:
            columns.append("contradiction_count")
            values.append(fact.get("contradiction_count", 0))
        placeholders = ", ".join("?" for _ in columns)
        update_columns = [
            col for col in columns if col not in {"fact_id", "created_at"}
        ]
        update_sql = ", ".join(f"{col} = excluded.{col}" for col in update_columns)
        conn.execute(
            f"INSERT INTO canonical_facts ({', '.join(columns)}) "
            f"VALUES ({placeholders}) "
            f"ON CONFLICT(fact_id) DO UPDATE SET {update_sql}",
            values,
        )
        imported += 1
    return imported


def export_provenance_links(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    if not _sqlite_table_exists(conn, "provenance_links"):
        return []
    rows = conn.execute(
        "SELECT provenance_id, subject_kind, subject_ref, source_kind, source_ref, "
        "span_start, span_end, excerpt, confidence, created_at "
        "FROM provenance_links ORDER BY created_at, provenance_id"
    ).fetchall()
    return [dict(r) for r in rows]


def import_provenance_links(conn: sqlite3.Connection, links: list[dict]) -> int:
    if not _sqlite_table_exists(conn, "provenance_links"):
        return 0
    imported = 0
    for link in links:
        conn.execute(
            "INSERT OR IGNORE INTO provenance_links "
            "(provenance_id, subject_kind, subject_ref, source_kind, source_ref, "
            "span_start, span_end, excerpt, confidence, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                link.get("provenance_id"),
                link.get("subject_kind"),
                link.get("subject_ref"),
                link.get("source_kind"),
                link.get("source_ref"),
                link.get("span_start"),
                link.get("span_end"),
                link.get("excerpt"),
                link.get("confidence", 1.0),
                link.get("created_at") or now_iso(),
            ),
        )
        imported += conn.execute("SELECT changes()").fetchone()[0]
    return imported


def export_knowledge_links(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    if not _sqlite_table_exists(conn, "knowledge_links"):
        return []
    rows = conn.execute(
        "SELECT link_id, subject_kind, subject_ref, relation_type, object_kind, "
        "object_ref, rationale, created_at, active "
        "FROM knowledge_links ORDER BY created_at, link_id"
    ).fetchall()
    return [dict(r) for r in rows]


def import_knowledge_links(conn: sqlite3.Connection, links: list[dict]) -> int:
    if not _sqlite_table_exists(conn, "knowledge_links"):
        return 0
    imported = 0
    for link in links:
        conn.execute(
            "INSERT OR REPLACE INTO knowledge_links "
            "(link_id, subject_kind, subject_ref, relation_type, object_kind, "
            "object_ref, rationale, created_at, active) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                link.get("link_id"),
                link.get("subject_kind"),
                link.get("subject_ref"),
                link.get("relation_type"),
                link.get("object_kind"),
                link.get("object_ref"),
                link.get("rationale"),
                link.get("created_at") or now_iso(),
                link.get("active", 1),
            ),
        )
        imported += 1
    return imported


_MEMORY_EVENTS_EXPORT_SQL = (
    "SELECT event_id, event_type, aggregate_kind, aggregate_id, field_name, "
    "actor_type, actor_id, machine_id, tool_name, logical_clock, event_ts, "
    "old_value, new_value, payload_json, parent_event_id, source_kind, "
    "source_ref, source_excerpt, source_start, source_end "
    "FROM memory_events ORDER BY machine_id, logical_clock"
)


def export_memory_events(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    if not _sqlite_table_exists(conn, "memory_events"):
        return []
    rows = conn.execute(_MEMORY_EVENTS_EXPORT_SQL).fetchall()
    return [dict(r) for r in rows]


def write_memory_events_file_streaming(
    conn: sqlite3.Connection,
    bridge_dir: str,
) -> tuple[str, int]:
    """Atomically stream ``memory_events.json`` without retaining the ledger.

    The previous bridge path built both a 453k-row list of dictionaries and a
    311 MB serialized string in the tray process.  On CPython that transient
    allocation left 1-5 GB of arenas resident.  Row-wise serialization keeps
    the exact JSON-array transport contract and bounds working memory to one
    SQLite row plus the file buffer.
    """
    em_dir = Path(bridge_dir) / "extended_memory"
    em_dir.mkdir(parents=True, exist_ok=True)
    file_path = em_dir / "memory_events.json"
    tmp_path = file_path.with_suffix(".json.tmp")
    count = 0
    # orjson emits compact arrays; stdlib fallback uses a space after commas.
    # Matching the active serializer keeps generated bytes stable across this
    # migration, not merely JSON-equivalent.
    separator = "," if json_dumps([0, 1]) == "[0,1]" else ", "
    try:
        with tmp_path.open("w", encoding="utf-8") as fh:
            fh.write("[")
            if _sqlite_table_exists(conn, "memory_events"):
                for row in conn.execute(_MEMORY_EVENTS_EXPORT_SQL):
                    if count:
                        fh.write(separator)
                    fh.write(json_dumps(dict(row)))
                    count += 1
            fh.write("]")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, file_path)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return "extended_memory/memory_events.json", count


def import_memory_events(conn: sqlite3.Connection, events: list[dict]) -> int:
    if not _sqlite_table_exists(conn, "memory_events"):
        return 0
    imported = 0
    for event in events:
        conn.execute(
            "INSERT OR IGNORE INTO memory_events "
            "(event_id, event_type, aggregate_kind, aggregate_id, field_name, actor_type, "
            "actor_id, machine_id, tool_name, logical_clock, event_ts, old_value, "
            "new_value, payload_json, parent_event_id, source_kind, source_ref, "
            "source_excerpt, source_start, source_end) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event.get("event_id"),
                event.get("event_type"),
                event.get("aggregate_kind"),
                event.get("aggregate_id"),
                event.get("field_name"),
                event.get("actor_type", "system"),
                event.get("actor_id"),
                event.get("machine_id", MACHINE_ID),
                event.get("tool_name", "remote.import"),
                event.get("logical_clock", 0),
                event.get("event_ts") or now_iso(),
                event.get("old_value"),
                event.get("new_value"),
                event.get("payload_json"),
                event.get("parent_event_id"),
                event.get("source_kind"),
                event.get("source_ref"),
                event.get("source_excerpt"),
                event.get("source_start"),
                event.get("source_end"),
            ),
        )
        delta = conn.execute("SELECT changes()").fetchone()[0]
        if delta:
            imported += delta
            observed_clock = int(event.get("logical_clock") or 0)
            event_mid = event.get("machine_id", MACHINE_ID)
            _observe_logical_clock(
                conn,
                observed_clock,
                machine_id=event_mid,
                updated_at=event.get("event_ts") or now_iso(),
            )
            if event_mid != MACHINE_ID:
                _observe_logical_clock(
                    conn,
                    observed_clock,
                    machine_id=MACHINE_ID,
                    updated_at=event.get("event_ts") or now_iso(),
                )
    return imported


def export_memory_audit_issues(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    if not _sqlite_table_exists(conn, "memory_audit_issues"):
        return []
    rows = conn.execute(
        "SELECT issue_id, issue_type, severity, subject_kind, subject_ref, "
        "details_json, status, first_detected_at, last_detected_at, resolved_at "
        "FROM memory_audit_issues ORDER BY last_detected_at, issue_id"
    ).fetchall()
    return [dict(r) for r in rows]


def import_memory_audit_issues(conn: sqlite3.Connection, issues: list[dict]) -> int:
    if not _sqlite_table_exists(conn, "memory_audit_issues"):
        return 0
    imported = 0
    for issue in issues:
        conn.execute(
            "INSERT OR REPLACE INTO memory_audit_issues "
            "(issue_id, issue_type, severity, subject_kind, subject_ref, details_json, "
            "status, first_detected_at, last_detected_at, resolved_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                issue.get("issue_id"),
                issue.get("issue_type"),
                issue.get("severity", "medium"),
                issue.get("subject_kind"),
                issue.get("subject_ref"),
                issue.get("details_json", "{}"),
                issue.get("status", "open"),
                issue.get("first_detected_at") or now_iso(),
                issue.get("last_detected_at") or now_iso(),
                issue.get("resolved_at"),
            ),
        )
        imported += 1
    return imported


def export_memory_artifacts(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    if not _sqlite_table_exists(conn, "memory_artifacts"):
        return []
    rows = conn.execute(
        "SELECT artifact_id, artifact_key, artifact_kind, scope_kind, scope_ref, title, "
        "body, confidence, status, valid_from, valid_to, source_event_id, created_at, updated_at "
        "FROM memory_artifacts ORDER BY updated_at, artifact_id"
    ).fetchall()
    return [dict(r) for r in rows]


def import_memory_artifacts(conn: sqlite3.Connection, artifacts: list[dict]) -> int:
    if not _sqlite_table_exists(conn, "memory_artifacts"):
        return 0
    imported = 0
    for artifact in artifacts:
        conn.execute(
            "INSERT INTO memory_artifacts ("
            "artifact_id, artifact_key, artifact_kind, scope_kind, scope_ref, title, body, "
            "confidence, status, valid_from, valid_to, source_event_id, created_at, updated_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(artifact_key) DO UPDATE SET "
            "artifact_kind = excluded.artifact_kind, "
            "scope_kind = excluded.scope_kind, "
            "scope_ref = excluded.scope_ref, "
            "title = excluded.title, "
            "body = excluded.body, "
            "confidence = excluded.confidence, "
            "status = excluded.status, "
            "valid_from = excluded.valid_from, "
            "valid_to = excluded.valid_to, "
            "source_event_id = excluded.source_event_id, "
            "updated_at = excluded.updated_at",
            (
                artifact.get("artifact_id") or _new_event_id(),
                artifact.get("artifact_key"),
                artifact.get("artifact_kind"),
                artifact.get("scope_kind"),
                artifact.get("scope_ref"),
                artifact.get("title"),
                artifact.get("body", ""),
                artifact.get("confidence", 1.0),
                artifact.get("status", "active"),
                artifact.get("valid_from"),
                artifact.get("valid_to"),
                artifact.get("source_event_id"),
                artifact.get("created_at") or now_iso(),
                artifact.get("updated_at") or now_iso(),
            ),
        )
        imported += 1
    return imported


def export_memory_conflicts(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    if not _sqlite_table_exists(conn, "memory_conflicts"):
        return []
    rows = conn.execute(
        "SELECT conflict_id, conflict_key, aggregate_kind, aggregate_id, field_name, "
        "local_value, remote_value, local_updated_at, remote_updated_at, "
        "local_updated_order, remote_updated_order, local_source_event_id, remote_source_event_id, "
        "winner, status, rationale, created_at, updated_at, resolved_at "
        "FROM memory_conflicts ORDER BY updated_at, conflict_id"
    ).fetchall()
    return [dict(r) for r in rows]


def import_memory_conflicts(conn: sqlite3.Connection, conflicts: list[dict]) -> int:
    if not _sqlite_table_exists(conn, "memory_conflicts"):
        return 0
    imported = 0
    for conflict in conflicts:
        conn.execute(
            "INSERT INTO memory_conflicts ("
            "conflict_id, conflict_key, aggregate_kind, aggregate_id, field_name, "
            "local_value, remote_value, local_updated_at, remote_updated_at, "
            "local_updated_order, remote_updated_order, local_source_event_id, remote_source_event_id, "
            "winner, status, rationale, created_at, updated_at, resolved_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(conflict_key) DO UPDATE SET "
            "aggregate_kind = excluded.aggregate_kind, "
            "aggregate_id = excluded.aggregate_id, "
            "field_name = excluded.field_name, "
            "local_value = excluded.local_value, "
            "remote_value = excluded.remote_value, "
            "local_updated_at = excluded.local_updated_at, "
            "remote_updated_at = excluded.remote_updated_at, "
            "local_updated_order = excluded.local_updated_order, "
            "remote_updated_order = excluded.remote_updated_order, "
            "local_source_event_id = excluded.local_source_event_id, "
            "remote_source_event_id = excluded.remote_source_event_id, "
            "winner = excluded.winner, "
            "status = excluded.status, "
            "rationale = excluded.rationale, "
            "updated_at = excluded.updated_at, "
            "resolved_at = excluded.resolved_at",
            (
                conflict.get("conflict_id") or _new_event_id(),
                conflict.get("conflict_key"),
                conflict.get("aggregate_kind"),
                conflict.get("aggregate_id"),
                conflict.get("field_name"),
                conflict.get("local_value"),
                conflict.get("remote_value"),
                conflict.get("local_updated_at"),
                conflict.get("remote_updated_at"),
                conflict.get("local_updated_order", 0),
                conflict.get("remote_updated_order", 0),
                conflict.get("local_source_event_id"),
                conflict.get("remote_source_event_id"),
                conflict.get("winner", "local"),
                conflict.get("status", "open"),
                conflict.get("rationale"),
                conflict.get("created_at") or now_iso(),
                conflict.get("updated_at") or now_iso(),
                conflict.get("resolved_at"),
            ),
        )
        imported += 1
    return imported


def export_memory_audit_state(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    if not _sqlite_table_exists(conn, "memory_audit_state"):
        return []
    rows = conn.execute(
        "SELECT runner_name, cadence_minutes, last_started_at, last_finished_at, "
        "next_run_after, last_status, last_summary_json, updated_at "
        "FROM memory_audit_state ORDER BY runner_name"
    ).fetchall()
    return [dict(r) for r in rows]


def import_memory_audit_state(conn: sqlite3.Connection, rows: list[dict]) -> int:
    if not _sqlite_table_exists(conn, "memory_audit_state"):
        return 0
    imported = 0
    for row in rows:
        conn.execute(
            "INSERT INTO memory_audit_state ("
            "runner_name, cadence_minutes, last_started_at, last_finished_at, next_run_after, "
            "last_status, last_summary_json, updated_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(runner_name) DO UPDATE SET "
            "cadence_minutes = excluded.cadence_minutes, "
            "last_started_at = excluded.last_started_at, "
            "last_finished_at = excluded.last_finished_at, "
            "next_run_after = excluded.next_run_after, "
            "last_status = excluded.last_status, "
            "last_summary_json = excluded.last_summary_json, "
            "updated_at = excluded.updated_at",
            (
                row.get("runner_name"),
                row.get("cadence_minutes", 60),
                row.get("last_started_at"),
                row.get("last_finished_at"),
                row.get("next_run_after"),
                row.get("last_status", "never"),
                row.get("last_summary_json"),
                row.get("updated_at") or now_iso(),
            ),
        )
        imported += 1
    return imported


def write_extended_memory_files(
    bridge_dir: str,
    extended_memory: dict[str, list],
    *,
    skip_keys: set[str] | frozenset[str] | None = None,
) -> list[str]:
    """Write each extended memory section to extended_memory/<key>.json.

    Returns list of written file paths (relative to bridge_dir).
    """
    em_dir = Path(bridge_dir) / "extended_memory"
    em_dir.mkdir(exist_ok=True)
    written: list[str] = []
    skipped = set(skip_keys or ())
    for key in EXTENDED_MEMORY_KEYS:
        if key in skipped:
            continue
        data = extended_memory.get(key, [])
        file_path = em_dir / f"{key}.json"
        tmp_path = file_path.with_suffix(".json.tmp")
        tmp_path.write_text(json_dumps(data), encoding="utf-8")
        os.replace(tmp_path, file_path)
        written.append(f"extended_memory/{key}.json")
    return written


# --- Kanban render payload (preview-only; NEVER a transport/import source) ---
# Transport (shared.json/index.json/tasks/*.json) keeps FULL bodies; this is a
# separate derived artifact the Kanban PWA reads, so a 540KB single note can no
# longer choke the browser render. Surface contract sets pull=False -> import
# never reads it. Full body always recoverable from memory.db / transport.
KANBAN_BIG_THRESHOLD = 20000  # active note size that triggers truncation
KANBAN_PREVIEW_MAX = 1000  # truncate cap for active big notes
KANBAN_COLLAPSE_MAX = 500  # collapse cap for non-active (done/archived/someday)
_KANBAN_NONACTIVE_STATUS = {"done", "archived"}
_KANBAN_NONACTIVE_SECTION = {"someday", "archive"}


def _kanban_preview_task(task: dict) -> dict:
    """Render-safe COPY of a task for the Kanban payload (never mutates input).

    Truncates only ``description``; non-active notes collapse broadly (the real
    size lever), active >20KB truncate, small active pass through full. Truncated
    copies carry _mirror_preview / _full_len / _full_hash so a preview can never
    be mistaken for the authoritative body.
    """
    desc = task.get("description") or ""
    status = str(task.get("status") or "")
    section = str(task.get("section") or "")
    if status in _KANBAN_NONACTIVE_STATUS or section in _KANBAN_NONACTIVE_SECTION:
        cap = KANBAN_COLLAPSE_MAX
    elif len(desc) > KANBAN_BIG_THRESHOLD:
        cap = KANBAN_PREVIEW_MAX
    else:
        return dict(task)
    out = dict(task)
    out["description"] = desc[:cap]
    out["_mirror_preview"] = True
    out["_full_len"] = len(desc)
    out["_full_hash"] = hashlib.sha256(desc.encode("utf-8")).hexdigest()
    return out


def write_kanban_payload(bridge_dir: str, payload: dict) -> str:
    """Write kanban_payload.json -- render-only preview of the bridge payload.

    Mirrors the shared.json shape but truncates task descriptions. Transport
    artifacts are untouched. Atomic tmp+replace. Validates JSON before publish
    (parse-or-regenerate guard: a corrupt union-merged file is rebuilt from the
    DB on the next export, which calls this). Returns the relative path written.
    """
    kb = dict(payload)
    kb["_render_only"] = True
    kb["tasks"] = [_kanban_preview_task(t) for t in payload.get("tasks", [])]
    text = json_dumps(kb)
    json_loads(text)  # guard: never publish unparseable JSON
    path = Path(bridge_dir) / "kanban_payload.json"
    tmp_path = path.with_suffix(".json.tmp")
    tmp_path.write_text(text, encoding="utf-8")
    os.replace(tmp_path, path)
    return "kanban_payload.json"


def ensure_kanban_payload_parseable(
    bridge_dir: str,
    payload: dict | None = None,
    logger: logging.Logger | None = None,
) -> str:
    """Load-side parse-or-regenerate guard for kanban_payload.json.

    The render artifact merges with ``merge=union``, which can splice two JSON
    documents into an unparseable file after a pull. A corrupt preview must
    NEVER be served (Pages/PWA reads it) and must NEVER block sync: if the file
    exists but fails to parse, regenerate it from the freshly loaded transport
    ``payload`` (shared.json shape). A missing file is left alone -- the next
    export creates it. Best-effort by design: never raises.

    Returns a status string: ``ok`` (valid, untouched), ``missing`` (no file,
    no-op), ``regenerated`` (corrupt -> rebuilt from transport payload),
    ``skipped`` (corrupt but no transport payload to rebuild from), or
    ``failed`` (regeneration attempt itself failed; logged, non-fatal).
    """
    path = Path(bridge_dir) / "kanban_payload.json"
    try:
        if not path.exists():
            return "missing"
        json_loads(path.read_text(encoding="utf-8"))
        return "ok"
    except (ValueError, OSError, TypeError) as exc:
        if logger:
            logger.warning(
                "kanban_payload.json failed to parse (%s) -- "
                "parse-or-regenerate guard engaged",
                exc,
            )
    if not isinstance(payload, dict) or not payload:
        if logger:
            logger.warning(
                "kanban_payload.json corrupt but no transport payload "
                "available; leaving for the next export to regenerate"
            )
        return "skipped"
    try:
        write_kanban_payload(bridge_dir, payload)
        if logger:
            logger.info(
                "kanban_payload.json regenerated from transport payload "
                "after corrupt merge artifact"
            )
        return "regenerated"
    except Exception as exc:  # noqa: BLE001 - render artifact is best-effort
        if logger:
            logger.warning(
                "kanban_payload regeneration failed (non-fatal, transport "
                "unaffected): %s",
                exc,
            )
        return "failed"


def load_extended_memory_files(
    bridge_dir: str,
    logger: logging.Logger | None = None,
    *,
    skip_keys: set[str] | frozenset[str] | None = None,
) -> dict[str, list]:
    """Load extended memory sections from extended_memory/<key>.json files.

    Returns dict of key -> list. Missing files are omitted (caller falls back to payload).
    """
    em_dir = Path(bridge_dir) / "extended_memory"
    result: dict[str, list] = {}
    skipped = set(skip_keys or ())
    for key in EXTENDED_MEMORY_KEYS:
        if key in skipped:
            continue
        file_path = em_dir / f"{key}.json"
        if not file_path.exists():
            continue
        try:
            data = json_loads(file_path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                result[key] = data
            else:
                if logger:
                    logger.warning(
                        "extended_memory/%s.json: expected list, got %s",
                        key,
                        type(data).__name__,
                    )
        except (ValueError, OSError, TypeError) as exc:
            if logger:
                logger.warning("extended_memory/%s.json read failed: %s", key, exc)
    return result


def _iter_json_array_file(
    file_path: Path,
    *,
    chunk_size: int = 1 << 20,
):
    """Yield a top-level JSON array one value at a time with bounded memory."""
    decoder = json.JSONDecoder()
    with file_path.open("r", encoding="utf-8") as fh:
        buffer = ""
        pos = 0
        eof = False

        def refill() -> None:
            nonlocal buffer, pos, eof
            buffer = buffer[pos:]
            pos = 0
            chunk = fh.read(chunk_size)
            if chunk:
                buffer += chunk
            else:
                eof = True

        def skip_space() -> None:
            nonlocal pos
            while True:
                while pos < len(buffer) and buffer[pos].isspace():
                    pos += 1
                if pos < len(buffer) or eof:
                    return
                refill()

        refill()
        skip_space()
        if pos >= len(buffer) or buffer[pos] != "[":
            raise ValueError(f"{file_path}: expected a top-level JSON array")
        pos += 1
        first = True
        while True:
            skip_space()
            if pos >= len(buffer):
                if eof:
                    raise ValueError(f"{file_path}: unterminated JSON array")
                refill()
                continue
            if buffer[pos] == "]":
                pos += 1
                skip_space()
                if pos < len(buffer) or not eof:
                    while not eof:
                        refill()
                        skip_space()
                    if pos < len(buffer):
                        raise ValueError(f"{file_path}: trailing JSON data")
                return
            if not first:
                if buffer[pos] != ",":
                    raise ValueError(f"{file_path}: expected ',' between array items")
                pos += 1
                skip_space()
            while True:
                try:
                    value, end = decoder.raw_decode(buffer, pos)
                    pos = end
                    break
                except json.JSONDecodeError as exc:
                    if eof:
                        raise ValueError(f"{file_path}: invalid JSON array") from exc
                    if len(buffer) > 32 * 1024 * 1024:
                        raise ValueError(
                            f"{file_path}: JSON item exceeds 32 MiB or is malformed"
                        ) from exc
                    refill()
            yield value
            first = False


def _collect_causal_event_subset(
    events,
    *,
    required_event_ids: set[str] | frozenset[str] | None = None,
) -> list[dict]:
    """Retain only event heads/explicit refs needed by non-ledger merges."""
    required = set(required_event_ids or ())
    selected: dict[str, dict] = {}
    heads: dict[tuple[str, str], dict] = {}
    for event in events:
        _update_causal_event_maps(event, required, selected, heads)
    for event in heads.values():
        event_id = str(event.get("event_id") or "")
        selected[event_id or f"head:{len(selected)}"] = event
    return list(selected.values())


def _update_causal_event_maps(
    event: dict,
    required: set[str],
    selected: dict[str, dict],
    heads: dict[tuple[str, str], dict],
) -> None:
    if not isinstance(event, dict):
        return
    event_id = str(event.get("event_id") or "")
    if event_id and event_id in required:
        selected[event_id] = event
    aggregate_kind = str(event.get("aggregate_kind") or "")
    field_name = str(event.get("field_name") or "")
    if aggregate_kind not in {"chunk", "question", "claim", "fact"} and not (
        aggregate_kind == "task" and field_name == "status"
    ):
        return
    aggregate_id = str(event.get("aggregate_id") or "")
    if not aggregate_id:
        return
    key = (aggregate_kind, aggregate_id)
    current = heads.get(key)
    if current is None or _event_sort_key(
        event.get("event_ts"),
        event.get("machine_id"),
        int(event.get("logical_clock") or 0),
    ) > _event_sort_key(
        current.get("event_ts"),
        current.get("machine_id"),
        int(current.get("logical_clock") or 0),
    ):
        heads[key] = event


def import_memory_events_file_streaming(
    conn: sqlite3.Connection,
    file_path: str | Path,
    *,
    required_event_ids: set[str] | frozenset[str] | None = None,
    batch_size: int = 1000,
) -> tuple[int, list[dict]]:
    """Stream-import a memory ledger and return its small causal head subset."""
    path = Path(file_path)
    batch: list[dict] = []
    processed = 0
    required = set(required_event_ids or ())
    selected: dict[str, dict] = {}
    heads: dict[tuple[str, str], dict] = {}
    for value in _iter_json_array_file(path):
        if not isinstance(value, dict):
            raise ValueError(f"{path}: memory event must be a JSON object")
        batch.append(value)
        _update_causal_event_maps(value, required, selected, heads)
        if len(batch) >= max(1, int(batch_size)):
            processed += import_memory_events(conn, batch)
            batch.clear()
    if batch:
        processed += import_memory_events(conn, batch)
    for event in heads.values():
        event_id = str(event.get("event_id") or "")
        selected[event_id or f"head:{len(selected)}"] = event
    return processed, list(selected.values())


def task_source_event_ids(tasks: list[dict] | None) -> set[str]:
    """Extract explicit source-event references needed during task LWW merge."""
    event_ids: set[str] = set()
    for task in tasks or []:
        field_versions = task.get("_field_ts")
        if not isinstance(field_versions, dict):
            continue
        for entry in field_versions.values():
            event_id = ""
            if isinstance(entry, dict):
                event_id = str(entry.get("source_event_id") or "")
            elif isinstance(entry, (list, tuple)) and len(entry) >= 4:
                event_id = str(entry[3] or "")
            if event_id:
                event_ids.add(event_id)
    return event_ids


def import_remote_bridge_data(
    conn: sqlite3.Connection,
    bridge_dir: str,
    remote_payload: dict[str, Any],
    logger: logging.Logger | None = None,
    *,
    remote_task_event_ids: set[str] | frozenset[str] | None = None,
    event_subset_out: list[dict] | None = None,
) -> dict[str, int]:
    """Import remote entities, relations, and knowledge ratings from bridge data.

    Returns {"entities": N, "relations": N, "ratings": N, ...}.
    """
    result = {
        "entities": 0,
        "relations": 0,
        "ratings": 0,
        "chunks": 0,
        "annotations": 0,
        "questions": 0,
        "claims": 0,
        "claim_evidence": 0,
        "facts": 0,
        "provenance": 0,
        "knowledge_links": 0,
        "events": 0,
        "audit_issues": 0,
        "artifacts": 0,
        "conflicts": 0,
        "audit_state": 0,
    }
    # v5: augment payload with extended memory from separate files
    # memory_events can exceed 300 MB. It has a dedicated streaming path below;
    # never materialize it via read_text()+json_loads in the long-lived process.
    em_files = load_extended_memory_files(
        bridge_dir, logger, skip_keys={"memory_events"}
    )
    for key in EXTENDED_MEMORY_KEYS:
        if not remote_payload.get(key):
            if key in em_files:
                remote_payload[key] = em_files[key]

    inline_remote_events = (
        remote_payload.get("memory_events", [])
        if isinstance(remote_payload.get("memory_events"), list)
        else []
    )
    remote_events: list[dict] = []
    if inline_remote_events:
        try:
            result["events"] = import_memory_events(conn, inline_remote_events)
            remote_events = _collect_causal_event_subset(
                inline_remote_events,
                required_event_ids=remote_task_event_ids,
            )
        except sqlite3.Error as exc:
            if logger:
                logger.warning("Memory event import failed: %s", exc)
    else:
        event_file = Path(bridge_dir) / "extended_memory" / "memory_events.json"
        if event_file.exists():
            try:
                result["events"], remote_events = import_memory_events_file_streaming(
                    conn,
                    event_file,
                    required_event_ids=remote_task_event_ids,
                )
            except (OSError, TypeError, ValueError, sqlite3.Error) as exc:
                if logger:
                    logger.warning("Streaming memory event import failed: %s", exc)
    if event_subset_out is not None:
        event_subset_out.extend(remote_events)
    chunk_heads = _build_remote_event_heads(remote_events, "chunk")
    question_heads = _build_remote_event_heads(remote_events, "question")
    claim_heads = _build_remote_event_heads(remote_events, "claim")
    fact_heads = _build_remote_event_heads(remote_events, "fact")

    remote_entities = load_remote_entities_for_import(
        bridge_dir, remote_payload, logger
    )
    remote_relations = remote_payload.get("relations", [])
    if not isinstance(remote_relations, list):
        remote_relations = []
    if remote_entities or remote_relations:
        try:
            n_ent, _, n_rel = import_bridge_entities_and_relations(
                conn,
                remote_entities,
                remote_relations,
            )
            result["entities"] = n_ent
            result["relations"] = n_rel
        except (sqlite3.Error, KeyError, TypeError, ValueError) as exc:
            if logger:
                logger.warning("Entity/relation merge failed: %s", exc)

    remote_ratings = remote_payload.get("knowledge_ratings", [])
    if isinstance(remote_ratings, list) and remote_ratings:
        try:
            result["ratings"] = import_bridge_knowledge_ratings(conn, remote_ratings)
        except sqlite3.Error as exc:
            if logger:
                logger.warning("Rating merge failed: %s", exc)

    if isinstance(remote_payload.get("context_chunks"), list):
        try:
            result["chunks"] = import_context_chunks(
                conn,
                remote_payload["context_chunks"],
                remote_event_heads=chunk_heads,
            )
        except sqlite3.Error as exc:
            if logger:
                logger.warning("Context chunk import failed: %s", exc)

    if isinstance(remote_payload.get("context_annotations"), list):
        try:
            result["annotations"] = import_context_annotations(
                conn, remote_payload["context_annotations"]
            )
        except sqlite3.Error as exc:
            if logger:
                logger.warning("Context annotation import failed: %s", exc)

    if isinstance(remote_payload.get("context_questions"), list):
        try:
            result["questions"] = import_context_questions(
                conn,
                remote_payload["context_questions"],
                remote_event_heads=question_heads,
            )
        except sqlite3.Error as exc:
            if logger:
                logger.warning("Context question import failed: %s", exc)

    if isinstance(remote_payload.get("candidate_claims"), list):
        try:
            result["claims"] = import_candidate_claims(
                conn,
                remote_payload["candidate_claims"],
                remote_event_heads=claim_heads,
            )
        except sqlite3.Error as exc:
            if logger:
                logger.warning("Candidate claim import failed: %s", exc)

    if isinstance(remote_payload.get("claim_evidence"), list):
        try:
            result["claim_evidence"] = import_claim_evidence(
                conn, remote_payload["claim_evidence"]
            )
        except sqlite3.Error as exc:
            if logger:
                logger.warning("Claim evidence import failed: %s", exc)

    if isinstance(remote_payload.get("canonical_facts"), list):
        try:
            result["facts"] = import_canonical_facts(
                conn,
                remote_payload["canonical_facts"],
                remote_event_heads=fact_heads,
            )
        except sqlite3.Error as exc:
            if logger:
                logger.warning("Canonical fact import failed: %s", exc)

    if isinstance(remote_payload.get("provenance_links"), list):
        try:
            result["provenance"] = import_provenance_links(
                conn, remote_payload["provenance_links"]
            )
        except sqlite3.Error as exc:
            if logger:
                logger.warning("Provenance import failed: %s", exc)

    if isinstance(remote_payload.get("knowledge_links"), list):
        try:
            result["knowledge_links"] = import_knowledge_links(
                conn, remote_payload["knowledge_links"]
            )
        except sqlite3.Error as exc:
            if logger:
                logger.warning("Knowledge link import failed: %s", exc)

    if isinstance(remote_payload.get("memory_audit_issues"), list):
        try:
            result["audit_issues"] = import_memory_audit_issues(
                conn, remote_payload["memory_audit_issues"]
            )
        except sqlite3.Error as exc:
            if logger:
                logger.warning("Audit issue import failed: %s", exc)

    if isinstance(remote_payload.get("memory_artifacts"), list):
        try:
            result["artifacts"] = import_memory_artifacts(
                conn, remote_payload["memory_artifacts"]
            )
        except sqlite3.Error as exc:
            if logger:
                logger.warning("Memory artifact import failed: %s", exc)

    if isinstance(remote_payload.get("memory_conflicts"), list):
        try:
            result["conflicts"] = import_memory_conflicts(
                conn, remote_payload["memory_conflicts"]
            )
        except sqlite3.Error as exc:
            if logger:
                logger.warning("Memory conflict import failed: %s", exc)

    if isinstance(remote_payload.get("memory_audit_state"), list):
        try:
            result["audit_state"] = import_memory_audit_state(
                conn, remote_payload["memory_audit_state"]
            )
        except sqlite3.Error as exc:
            if logger:
                logger.warning("Memory audit state import failed: %s", exc)

    return result


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
