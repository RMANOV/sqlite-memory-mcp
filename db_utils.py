"""Shared DB connection, constants, and query helpers for sqlite-memory-mcp.

Single source of truth for task constants, DB connection setup, and common
utilities used by server.py, task_tray.py, and utility scripts.
"""

from __future__ import annotations

import hashlib
import inspect
import itertools
import json
import logging
import os
import re
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
        ".bridge_sync.lock",  # legacy in-repo lock path; safe to discard
    }
)
BRIDGE_GENERATED_DIRS = frozenset({"tasks", "entities", "public_knowledge"})


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


def _bridge_status_path(line: str) -> str:
    """Extract the repo-relative path from a git status --porcelain line."""
    path = line[3:].strip()
    if " -> " in path:
        path = path.split(" -> ", 1)[1]
    return path.replace("\\", "/").strip("/")


def is_generated_bridge_path(path: str) -> bool:
    """Return True for files/directories regenerated by bridge sync."""
    rel = path.replace("\\", "/").strip("/")
    if rel in BRIDGE_GENERATED_FILES:
        return True
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


def ensure_bridge_repo_ready(repo_dir: str) -> tuple[bool, str | None]:
    """Prepare the bridge repo for sync without discarding user-managed files.

    Safe behavior:
    - detached HEAD -> try to return to main
    - conflicts in generated artifacts -> auto-reset to origin/main
    - edits in generated artifacts -> discard/rebuild
    - edits in user-managed files -> block sync and surface the paths
    """
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

        git_run(repo_dir, "rebase", "--abort")
        git_run(repo_dir, "merge", "--abort")
        git_run(repo_dir, "fetch", "origin")
        reset = git_run(repo_dir, "reset", "--hard", "origin/main")
        if reset.returncode != 0:
            detail = (reset.stderr or reset.stdout).strip()
            return False, f"failed to reset generated bridge conflicts: {detail}"

        status = git_run(repo_dir, "status", "--porcelain")
        if status.returncode != 0:
            detail = (status.stderr or status.stdout).strip()
            return (
                False,
                f"cannot re-check bridge status: {detail or 'unknown git error'}",
            )
        lines = [ln for ln in status.stdout.splitlines() if ln.strip()]
        if not lines:
            return True, None

    dirty_paths = [_bridge_status_path(ln) for ln in lines]
    unsafe = [p for p in dirty_paths if not is_generated_bridge_path(p)]
    if unsafe:
        shown = ", ".join(unsafe[:3])
        return False, f"commit or stash bridge repo edits before sync: {shown}"

    # Generated artifacts are safe to rebuild from DB state.
    restore = git_run(
        repo_dir,
        "checkout",
        "--",
        "shared.json",
        "shared.js",
        "index.json",
        "entities_index.json",
        ".bridge_sync.lock",
        "tasks",
        "entities",
        "public_knowledge",
    )
    clean = git_run(
        repo_dir,
        "clean",
        "-fd",
        "--",
        "shared.json",
        "shared.js",
        "index.json",
        "entities_index.json",
        ".bridge_sync.lock",
        "tasks",
        "entities",
        "public_knowledge",
    )
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
    unsafe = [p for p in remaining if not is_generated_bridge_path(p)]
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
    return any(r["name"] == column_name for r in rows)


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
    """Monotonic per-machine counter stored in DB, independent of wall clock."""
    mid = machine_id or MACHINE_ID
    now = updated_at or now_iso()
    if not _sqlite_table_exists(conn, "memory_cursors"):
        return max(1, int(floor or 0) + 1)
    row = conn.execute(
        "SELECT last_clock FROM memory_cursors WHERE machine_id = ?",
        (mid,),
    ).fetchone()
    current = int(row["last_clock"]) if row else 0
    next_clock = max(current, int(floor or 0)) + 1
    conn.execute(
        "INSERT INTO memory_cursors (machine_id, last_clock, updated_at) "
        "VALUES (?, ?, ?) "
        "ON CONFLICT(machine_id) DO UPDATE SET "
        "last_clock = excluded.last_clock, updated_at = excluded.updated_at",
        (mid, next_clock, now),
    )
    return next_clock


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
    def promote_pending_public(
        conn: sqlite3.Connection,
        cutoff_ts: str,
        updated_at: str | None = None,
    ) -> int:
        """Promote pending_public tasks to public. Returns count promoted."""
        ts = updated_at or now_iso()
        cur = conn.execute(
            "UPDATE tasks SET visibility = 'public', updated_at = ? "
            "WHERE visibility = 'pending_public' AND publish_requested_at <= ?",
            (ts, cutoff_ts),
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
    mid = machine_id or _next_machine_id()
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
    has_order = _sqlite_has_column(conn, "task_field_versions", "updated_order")
    has_event = _sqlite_has_column(conn, "task_field_versions", "source_event_id")
    if has_order:
        cols.append("updated_order")
    if has_event:
        cols.append("source_event_id")
    return ", ".join(cols), has_order, has_event


def _field_version_entry(
    row: sqlite3.Row, *, has_order: bool, has_event: bool
) -> list[Any]:
    entry: list[Any] = [row["updated_at"], row["updated_by"]]
    if has_order:
        entry.append(row["updated_order"])
    if has_event:
        entry.append(row["source_event_id"])
    return entry


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
    fv_select, has_order, has_event = _task_field_version_select(conn)
    fv_rows = conn.execute(
        f"SELECT {fv_select} FROM task_field_versions WHERE task_id IN ({ph})",
        task_ids,
    ).fetchall()
    fv_map: dict[str, dict] = {}
    for fvr in fv_rows:
        fv_map.setdefault(fvr["task_id"], {})[fvr["field_name"]] = _field_version_entry(
            fvr,
            has_order=has_order,
            has_event=has_event,
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
        fv_select, has_order, has_event = _task_field_version_select(conn)
        fv_rows = conn.execute(
            f"SELECT {fv_select} FROM task_field_versions WHERE task_id IN ({ph})",
            all_ids,
        ).fetchall()
        for fvr in fv_rows:
            fv_map.setdefault(fvr["task_id"], {})[fvr["field_name"]] = (
                _field_version_entry(
                    fvr,
                    has_order=has_order,
                    has_event=has_event,
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


def _field_version_sort_key(
    updated_at: str,
    updated_by: str,
    updated_order: int = 0,
) -> tuple[int, Any, str]:
    """Prefer logical clocks; fall back to timestamp ordering for legacy peers."""
    if int(updated_order or 0) > 0:
        return (1, int(updated_order), str(updated_by or ""))
    return (0, str(updated_at or ""), str(updated_by or ""))


def merge_import_tasks(
    conn: sqlite3.Connection,
    remote_tasks: list[dict],
    import_content: bool = False,
) -> tuple[int, int]:
    """Per-field causal/LWW merge. Returns (new_count, updated_field_count).

    Preferred rule: (logical_clock, machine_id) when updated_order exists.
    Legacy fallback: (timestamp, machine_id) for older bridge peers.
    All writes should also exist in memory_events, so overwritten values remain auditable.

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

    # Pre-fetch: collect remote IDs that exist locally + their field versions
    remote_ids = [r.get("id") for r in tasks_sorted if r.get("id")]
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
            for field in fields_to_merge:
                if field not in remote:
                    continue
                remote_val = remote.get(field)
                remote_ts, remote_by, remote_order, remote_event_id = _parse_field_ts(
                    remote_fts, field, fallback_ts
                )

                local_fv = local_fvs.get(field)
                local_ts = local_fv[0] if local_fv else ""
                local_by = local_fv[1] if local_fv else ""
                local_order = local_fv[2] if local_fv else 0

                if _field_version_sort_key(
                    remote_ts, remote_by, remote_order
                ) > _field_version_sort_key(local_ts, local_by, local_order):
                    # Content protection: never nullify or drastically shrink local content
                    if field in CONTENT_FIELDS:
                        local_val = task_content_map.get(local_id, {}).get(field)
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
                    _store_task_field_version(
                        conn,
                        local_id,
                        field,
                        updated_at=remote_ts,
                        updated_by=remote_by,
                        updated_order=remote_order,
                        source_event_id=remote_event_id,
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
                local_content = task_content_map.get(local_id, {})
                if (
                    local_id in task_content_map
                    and local_content.get(content_field) is None
                ):
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
            elif remote_updated_at > local_updated_at:
                conn.execute(
                    "UPDATE tasks SET updated_at = ? WHERE id = ?",
                    (remote_updated_at, local_id),
                )
                existing_map[tid] = {
                    **existing_map[tid],
                    "updated_at": remote_updated_at,
                }
                updated_fields += 1
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
    real_base = os.path.realpath(bridge_dir)
    task_file = Path(bridge_dir) / "tasks" / f"{task_id}.json"
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
    tasks = list(payload.get("tasks", []))
    for key, value in payload.items():
        if (
            key.endswith("_tasks")
            and key not in {"tasks", "shared_tasks"}
            and isinstance(value, list)
        ):
            tasks.extend(value)
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

    remote_tasks = idx_data.get("tasks", [])
    enriched = 0
    for task in remote_tasks:
        if task.get("_tombstone"):
            continue
        content = load_task_content(task.get("id", ""), bridge_dir)
        if not content:
            continue
        for field in CONTENT_FIELDS:
            if field in content:
                task[field] = content[field]
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


def import_candidate_claims(conn: sqlite3.Connection, claims: list[dict]) -> int:
    if not _sqlite_table_exists(conn, "candidate_claims"):
        return 0
    imported = 0
    for claim in claims:
        row = conn.execute(
            "SELECT updated_at FROM candidate_claims WHERE claim_id = ?",
            (claim.get("claim_id"),),
        ).fetchone()
        if row and (row["updated_at"] or "") >= (claim.get("updated_at") or ""):
            continue
        conn.execute(
            "INSERT OR REPLACE INTO candidate_claims "
            "(claim_id, chunk_id, subject, predicate, object_text, object_type, "
            "claim_scope, confidence, status, requires_human, promoted_to_fact_id, "
            "created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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


def import_canonical_facts(conn: sqlite3.Connection, facts: list[dict]) -> int:
    if not _sqlite_table_exists(conn, "canonical_facts"):
        return 0
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
        row = conn.execute(
            "SELECT updated_at FROM canonical_facts WHERE fact_id = ?",
            (fact.get("fact_id"),),
        ).fetchone()
        if row and (row["updated_at"] or "") >= (fact.get("updated_at") or ""):
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
        conn.execute(
            f"INSERT OR REPLACE INTO canonical_facts ({', '.join(columns)}) "
            f"VALUES ({placeholders})",
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


def export_memory_events(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    if not _sqlite_table_exists(conn, "memory_events"):
        return []
    rows = conn.execute(
        "SELECT event_id, event_type, aggregate_kind, aggregate_id, field_name, "
        "actor_type, actor_id, machine_id, tool_name, logical_clock, event_ts, "
        "old_value, new_value, payload_json, parent_event_id, source_kind, "
        "source_ref, source_excerpt, source_start, source_end "
        "FROM memory_events ORDER BY machine_id, logical_clock"
    ).fetchall()
    return [dict(r) for r in rows]


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
            _next_logical_clock(
                conn,
                event.get("machine_id", MACHINE_ID),
                floor=int(event.get("logical_clock") or 0),
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


def import_remote_bridge_data(
    conn: sqlite3.Connection,
    bridge_dir: str,
    remote_payload: dict[str, Any],
    logger: logging.Logger | None = None,
) -> dict[str, int]:
    """Import remote entities, relations, and knowledge ratings from bridge data.

    Returns {"entities": N, "relations": N, "ratings": N, ...}.
    """
    result = {
        "entities": 0,
        "relations": 0,
        "ratings": 0,
        "claims": 0,
        "claim_evidence": 0,
        "facts": 0,
        "provenance": 0,
        "knowledge_links": 0,
        "events": 0,
        "audit_issues": 0,
    }

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

    if isinstance(remote_payload.get("candidate_claims"), list):
        try:
            result["claims"] = import_candidate_claims(
                conn, remote_payload["candidate_claims"]
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
                conn, remote_payload["canonical_facts"]
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

    if isinstance(remote_payload.get("memory_events"), list):
        try:
            result["events"] = import_memory_events(
                conn, remote_payload["memory_events"]
            )
        except sqlite3.Error as exc:
            if logger:
                logger.warning("Memory event import failed: %s", exc)

    if isinstance(remote_payload.get("memory_audit_issues"), list):
        try:
            result["audit_issues"] = import_memory_audit_issues(
                conn, remote_payload["memory_audit_issues"]
            )
        except sqlite3.Error as exc:
            if logger:
                logger.warning("Audit issue import failed: %s", exc)

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
