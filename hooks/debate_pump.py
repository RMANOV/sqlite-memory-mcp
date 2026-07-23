#!/usr/bin/env python3
"""Small resident debate wake pump.

This complements client hooks. Hooks are the low-latency fast path, but they
only run when the posting client supports them. The pump watches the debate DB
for new addressed messages and feeds them through the same wake resolver/action
name, so duplicate suppression is shared with PostToolUse.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import re
import sqlite3
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


HOOK_DIR = Path(__file__).resolve().parent


def _default_repo() -> Path:
    """Prefer the repo this hook actually lives in; keep the legacy Linux
    default for deployments where hooks are copied out of the repo tree."""
    candidate = HOOK_DIR.parent
    if (candidate / "debate.py").is_file():
        return candidate
    return Path("/home/rmanov/sqlite-memory-mcp")


REPO = Path(os.environ.get("DEBATE_REPO", str(_default_repo())))
IS_WINDOWS = sys.platform == "win32"
for _import_root in (HOOK_DIR, REPO):
    _import_path = str(_import_root)
    if _import_path not in sys.path:
        sys.path.insert(0, _import_path)
_ROLE_RE = re.compile(r"^[A-Z][A-Z0-9_]+$")
_HUMAN_ROLES = {"HUMAN", "OPERATOR"}
DB_PATH = Path(
    os.environ.get("SQLITE_MEMORY_DB", os.path.expanduser("~/.claude/memory/memory.db"))
)
LOG_PATH = Path(
    os.environ.get(
        "DEBATE_PUMP_LOG",
        os.path.expanduser("~/.claude/memory/debate_pump.jsonl"),
    )
)
STATE_PATH = Path(
    os.environ.get(
        "DEBATE_PUMP_STATE",
        os.path.expanduser("~/.claude/memory/debate_pump_state.json"),
    )
)
# Default next to the state file so tests that redirect DEBATE_PUMP_STATE
# inherit heartbeat isolation instead of writing into the production dir.
HEARTBEAT_PATH = Path(
    os.environ.get(
        "DEBATE_PUMP_HEARTBEAT",
        str(STATE_PATH.with_name("debate_pump_heartbeat.json")),
    )
)
POST_SCHEMA_VERSION = "debate_post_with_recipients.v1"

STOP = False
STOP_EVENT = threading.Event()
CHILDREN: set[int] = set()
LOG_MAX_BYTES = int(os.environ.get("DEBATE_PUMP_LOG_MAX_BYTES", str(20 * 1024 * 1024)))
LOG_KEEP = max(1, int(os.environ.get("DEBATE_PUMP_LOG_KEEP", "3")))
DEFAULT_ACTION_KINDS = (
    "Q,A,DECISION,STATE,PING,CLAIM,CHALLENGE,EVIDENCE,REBUT,"
    "CONCEDE,VERIFY,DISSENT,ESCALATE"
)


def _split_csv_values(values: list[str] | str | None) -> list[str]:
    if values is None:
        return []
    raw_values = values if isinstance(values, list) else [values]
    out: list[str] = []
    for raw in raw_values:
        out.extend(part.strip() for part in str(raw).split(",") if part.strip())
    return out


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _log(event: str, **fields: Any) -> None:
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        if LOG_PATH.exists() and LOG_PATH.stat().st_size >= LOG_MAX_BYTES:
            LOG_PATH.with_name(f"{LOG_PATH.name}.{LOG_KEEP}").unlink(missing_ok=True)
            for index in range(LOG_KEEP - 1, 0, -1):
                older = LOG_PATH.with_name(f"{LOG_PATH.name}.{index}")
                if older.exists():
                    older.replace(LOG_PATH.with_name(f"{LOG_PATH.name}.{index + 1}"))
            LOG_PATH.replace(LOG_PATH.with_name(f"{LOG_PATH.name}.1"))
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {"ts": _now(), "event": event, **fields},
                    ensure_ascii=False,
                    default=repr,
                )
                + "\n"
            )
    except Exception as exc:
        # Logging is diagnostic: a rotation/open failure must never terminate
        # the resident routing pump.  stderr is captured by the user journal.
        try:
            sys.stderr.write(
                json.dumps(
                    {
                        "ts": _now(),
                        "event": "pump_log_fallback",
                        "failed_event": event,
                        "error": repr(exc),
                        "fields": fields,
                    },
                    ensure_ascii=False,
                    default=repr,
                )
                + "\n"
            )
            sys.stderr.flush()
        except Exception:
            pass


def _load_state() -> dict[str, str]:
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        "last_ts": str(data.get("last_ts") or ""),
        "last_msg_id": str(data.get("last_msg_id") or ""),
    }


def _save_state(last_ts: str, last_msg_id: str) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(
        json.dumps({"last_ts": last_ts, "last_msg_id": last_msg_id}, indent=2) + "\n",
        encoding="utf-8",
    )
    tmp.replace(STATE_PATH)


_SELF_CREATE_TIME: float | None = None


def _self_create_time() -> float | None:
    global _SELF_CREATE_TIME
    if _SELF_CREATE_TIME is None:
        try:
            import psutil

            _SELF_CREATE_TIME = float(psutil.Process(os.getpid()).create_time())
        except Exception:
            _SELF_CREATE_TIME = 0.0
    return _SELF_CREATE_TIME or None


def _write_heartbeat(last_ts: str, last_msg_id: str) -> None:
    """Durable liveness marker: pid + create_time let ``debate_ops status``
    distinguish running / stale (file present, process gone or reused PID) /
    stopped. Never fatal."""
    try:
        HEARTBEAT_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = HEARTBEAT_PATH.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "create_time": _self_create_time(),
                    "ts": _now(),
                    "last_ts": last_ts,
                    "last_msg_id": last_msg_id,
                    "live_children": len(CHILDREN),
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        tmp.replace(HEARTBEAT_PATH)
    except Exception as exc:
        _log("heartbeat_write_failed", error=repr(exc))


def _topic_clause(topics: list[str]) -> tuple[str, list[str]]:
    if not topics:
        return "", []
    placeholders = ",".join("?" for _ in topics)
    return f"AND m.topic_id IN ({placeholders})", topics


def _kind_clause(kinds: list[str]) -> tuple[str, list[str]]:
    if not kinds:
        return "", []
    placeholders = ",".join("?" for _ in kinds)
    return f"AND m.kind IN ({placeholders})", kinds


def _table_exists(con: sqlite3.Connection, table_name: str) -> bool:
    return (
        con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,),
        ).fetchone()
        is not None
    )


def _blind_commit_clause(has_blind_table: bool) -> str:
    if not has_blind_table:
        return ""
    return (
        "AND NOT EXISTS (SELECT 1 FROM debate_blind_commits bc "
        " WHERE bc.msg_id=m.msg_id AND bc.released_at IS NULL) "
    )


def _fetch_new(
    last_ts: str,
    last_msg_id: str,
    topics: list[str],
    kinds: list[str],
    limit: int,
    con: sqlite3.Connection | None = None,
    has_blind_table: bool | None = None,
) -> list[sqlite3.Row]:
    topic_sql, topic_params = _topic_clause(topics)
    kind_sql, kind_params = _kind_clause(kinds)
    owns_connection = con is None
    if con is None:
        con = sqlite3.connect(DB_PATH)
        con.row_factory = sqlite3.Row
    try:
        if has_blind_table is None:
            has_blind_table = _table_exists(con, "debate_blind_commits")
        blind_sql = _blind_commit_clause(has_blind_table)
        return con.execute(
            "SELECT DISTINCT m.msg_id, m.topic_id, m.ts "
            "FROM debate_messages m "
            "JOIN debates d ON d.topic_id = m.topic_id "
            "JOIN debate_message_recipients r ON r.msg_id = m.msg_id "
            "WHERE (m.ts > ? OR (m.ts = ? AND m.msg_id > ?)) "
            "AND d.state IN ('INIT','ACTIVE') "
            f"{blind_sql} "
            f"{topic_sql} "
            f"{kind_sql} "
            "ORDER BY m.ts ASC, m.msg_id ASC LIMIT ?",
            [last_ts, last_ts, last_msg_id, *topic_params, *kind_params, limit],
        ).fetchall()
    finally:
        if owns_connection:
            con.close()


def _fetch_pending_deliveries(
    topics: list[str],
    kinds: list[str],
    limit: int,
    con: sqlite3.Connection | None = None,
    has_delivery_queue: bool | None = None,
    has_blind_table: bool | None = None,
) -> list[sqlite3.Row]:
    """Read the durable recipient queue independently of the legacy cursor.

    The post transaction inserts ``debate_delivery_queue`` rows for every
    addressed recipient before commit.  The kernel event only says "work
    changed"; this queue says exactly which committed messages are pending.
    Reading it before the cursor path prevents an older in-flight or
    temporarily-unbound trigger from hiding a newly addressed message
    (head-of-line blocking).

    Newest addressed rows are returned first even when their posting MCP process
    predates the queue table and therefore left no delivery row.
    This compatibility fast path means existing agent sessions need no restart.
    A small oldest queued slice is appended for starvation-free crash recovery.
    Claim/wake uniqueness remains the dispatch idempotency authority.
    """
    if limit <= 0:
        return []
    topic_sql, topic_params = _topic_clause(topics)
    kind_sql, kind_params = _kind_clause(kinds)
    owns_connection = con is None
    if con is None:
        con = sqlite3.connect(DB_PATH)
        con.row_factory = sqlite3.Row
    try:
        if has_delivery_queue is None:
            has_delivery_queue = _table_exists(con, "debate_delivery_queue")
        if not has_delivery_queue:
            return []  # mixed-version upgrade window
        if has_blind_table is None:
            has_blind_table = _table_exists(con, "debate_blind_commits")
        blind_sql = _blind_commit_clause(has_blind_table)
        pending_select_sql = (
            "SELECT DISTINCT m.msg_id, m.topic_id, m.ts, q.enqueued_at "
            "FROM debate_messages m "
            "JOIN debates d ON d.topic_id = m.topic_id "
            "JOIN debate_delivery_queue q ON q.msg_id = m.msg_id "
            "WHERE q.completed_at IS NULL "
            "AND d.state IN ('INIT','ACTIVE') "
            f"{blind_sql} {topic_sql} {kind_sql} "
        )
        params = [*topic_params, *kind_params]
        newest_queued = con.execute(
            pending_select_sql + "ORDER BY q.enqueued_at DESC, m.msg_id DESC LIMIT ?",
            [*params, limit],
        ).fetchall()
        latest_addressed = con.execute(
            "SELECT DISTINCT m.msg_id, m.topic_id, m.ts, NULL AS enqueued_at "
            "FROM debate_messages m "
            "JOIN debates d ON d.topic_id = m.topic_id "
            "JOIN debate_message_recipients r ON r.msg_id = m.msg_id "
            "WHERE NOT EXISTS (SELECT 1 FROM debate_delivery_queue q "
            " WHERE q.msg_id=m.msg_id AND q.recipient=r.recipient) "
            "AND d.state IN ('INIT','ACTIVE') "
            f"{blind_sql} {topic_sql} {kind_sql} "
            "ORDER BY m.ts DESC, m.msg_id DESC LIMIT ?",
            [*params, max(limit, 64)],
        ).fetchall()
        fairness_limit = min(16, max(1, limit // 4))
        oldest = (
            con.execute(
                pending_select_sql + "ORDER BY q.enqueued_at ASC, m.msg_id ASC LIMIT ?",
                [*params, fairness_limit],
            ).fetchall()
            if len(newest_queued) == limit
            else []
        )
        seen: set[str] = set()
        out: list[sqlite3.Row] = []
        for row in [*newest_queued, *latest_addressed, *oldest]:
            msg_id = str(row["msg_id"])
            if msg_id not in seen:
                seen.add(msg_id)
                out.append(row)
        return out
    finally:
        if owns_connection:
            con.close()


def _complete_pending_deliveries(msg_id: str, *, enqueued_at: str | None = None) -> int:
    """Acknowledge the durable queue only after the trigger is terminal."""
    con = sqlite3.connect(DB_PATH)
    try:
        has_delivery_queue = (
            con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='debate_delivery_queue'"
            ).fetchone()
            is not None
        )
        if not has_delivery_queue:
            return 0
        queued_at = enqueued_at or _now()
        con.execute(
            "INSERT OR IGNORE INTO debate_delivery_queue "
            "(msg_id, recipient, enqueued_at, completed_at) "
            "SELECT msg_id, recipient, ?, NULL "
            "FROM debate_message_recipients WHERE msg_id = ?",
            (queued_at, msg_id),
        )
        cur = con.execute(
            "UPDATE debate_delivery_queue "
            "SET completed_at = COALESCE(completed_at, ?) "
            "WHERE msg_id = ? AND completed_at IS NULL",
            (_now(), msg_id),
        )
        con.commit()
        return int(cur.rowcount)
    finally:
        con.close()


def _filter_targets(out: dict[str, Any], suppressed_roles: set[str]) -> dict[str, Any]:
    if not suppressed_roles:
        return out
    filtered = dict(out)
    targets = []
    skipped = []
    for target in out.get("targets", []):
        role = str(target.get("target_role") or "").upper()
        if role in suppressed_roles:
            skipped.append(target)
            continue
        targets.append(target)
    filtered["targets"] = targets
    if skipped:
        _log("targets_suppressed_by_pump_policy", skipped=skipped)
    return filtered


def _recipient_bindings(
    con: sqlite3.Connection, msg_id: str, suppressed_roles: set[str]
) -> list[tuple[str, str]]:
    """Resolve (recipient_role, bound_session_id) for a trigger's recipients,
    skipping suppressed roles and recipients without an active binding."""
    msg = con.execute(
        "SELECT topic_id, vehicle FROM debate_messages WHERE msg_id = ?",
        (msg_id,),
    ).fetchone()
    if msg is None:
        return []
    if str(msg["vehicle"] or "analysis") == "implementation":
        return []  # fail-closed vehicle: never a bounded wake-worker
    rows = con.execute(
        "SELECT recipient, recipient_mode FROM debate_message_recipients "
        "WHERE msg_id = ? ORDER BY recipient",
        (msg_id,),
    ).fetchall()
    out: list[tuple[str, str]] = []
    for row in rows:
        recipient = str(row["recipient"] or "").strip()
        if recipient.upper() in suppressed_roles:
            continue
        if recipient.upper() in _HUMAN_ROLES:
            # Human work is projected into Task Tray's Waiting on Me tab;
            # it must never consume a local LLM worker slot.
            continue
        if row["recipient_mode"] == "normal":
            binding = con.execute(
                "SELECT session_id FROM debate_role_bindings "
                "WHERE topic_id = ? AND role = ? AND state = 'active' "
                "ORDER BY generation DESC LIMIT 1",
                (msg["topic_id"], recipient),
            ).fetchone()
            role = recipient
        else:
            binding = con.execute(
                "SELECT role, session_id FROM debate_role_bindings "
                "WHERE topic_id = ? AND session_id = ? AND state = 'diagnostic' "
                "ORDER BY generation DESC LIMIT 1",
                (msg["topic_id"], recipient),
            ).fetchone()
            role = str(binding["role"]) if binding else recipient
        if binding is None:
            continue
        out.append((role, str(binding["session_id"])))
    return out


def _has_unbound_addressed_recipient(
    con: sqlite3.Connection, msg_id: str, suppressed_roles: set[str]
) -> bool:
    """True if the trigger addresses a role-mode recipient that currently has
    NO active binding (and is not suppressed / not an impl vehicle).

    Advocate BLOCK #2: such a message is genuinely pending — a worker just
    cannot take it yet — so the cursor must NOT treat it as terminal and skip
    it. It is distinct from a message whose only recipients are suppressed
    (e.g. CONDUCTOR) or direct-session, which IS terminal for wake purposes."""
    msg = con.execute(
        "SELECT topic_id, vehicle FROM debate_messages WHERE msg_id = ?",
        (msg_id,),
    ).fetchone()
    if msg is None or str(msg["vehicle"] or "analysis") == "implementation":
        return False
    rows = con.execute(
        "SELECT recipient, recipient_mode FROM debate_message_recipients "
        "WHERE msg_id = ?",
        (msg_id,),
    ).fetchall()
    for row in rows:
        recipient = str(row["recipient"] or "").strip()
        if recipient.upper() in suppressed_roles:
            continue
        if recipient.upper() in _HUMAN_ROLES:
            continue
        if row["recipient_mode"] != "normal":
            continue  # diagnostic/direct-session is not a role-wake target
        if not _ROLE_RE.fullmatch(recipient):
            continue  # a literal session-id recipient, not a role
        binding = con.execute(
            "SELECT 1 FROM debate_role_bindings "
            "WHERE topic_id = ? AND role = ? AND state = 'active' LIMIT 1",
            (msg["topic_id"], recipient),
        ).fetchone()
        if binding is None:
            return True
    return False


def _recipient_claim_state(
    con: sqlite3.Connection, topic_id: str, role: str, trigger_msg_id: str
) -> str | None:
    """Latest worker-claim state for (topic, role, trigger), or None."""
    row = con.execute(
        "SELECT state FROM debate_worker_claims "
        "WHERE topic_id = ? AND role = ? AND trigger_msg_id = ? "
        "ORDER BY claimed_at DESC LIMIT 1",
        (topic_id, role, trigger_msg_id),
    ).fetchone()
    return str(row["state"]) if row else None


def _recipient_has_terminal_reply(
    con: sqlite3.Connection, topic_id: str, role: str, trigger_msg_id: str
) -> bool:
    return (
        con.execute(
            "SELECT 1 FROM debate_messages "
            "WHERE topic_id = ? AND role = ? AND reply_to = ? "
            "AND kind IN ('A','STATUS','CLAIM','CHALLENGE','EVIDENCE','REBUT',"
            "'CONCEDE','VERIFY','DISSENT','ESCALATE') LIMIT 1",
            (topic_id, role, trigger_msg_id),
        ).fetchone()
        is not None
    )


def _estimate_worker_demand(msg_id: str, suppressed_roles: set[str]) -> int:
    """Recipients that need a NEW worker spawn right now (throttle input).

    A recipient is NOT counted (already covered) when: its worker claim is
    active (in-flight — do not double-spawn) or completed, OR a terminal
    reply exists, OR the wake result is notified/terminal_no_action. It IS
    counted when the claim is retired/absent and no terminal reply exists —
    i.e. a dead worker whose work must be re-dispatched (advocate BLOCK
    critical #1: a stale 'dispatched' wake_log row must not suppress forever).
    """
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        msg = con.execute(
            "SELECT topic_id FROM debate_messages WHERE msg_id = ?",
            (msg_id,),
        ).fetchone()
        if msg is None:
            return 0
        topic_id = str(msg["topic_id"])
        action = os.environ.get("DEBATE_WAKE_ACTION_NAME", "post_tool_use_wake")
        demand = 0
        for role, session_id in _recipient_bindings(con, msg_id, suppressed_roles):
            if _recipient_has_terminal_reply(con, topic_id, role, msg_id):
                continue
            claim_state = _recipient_claim_state(con, topic_id, role, msg_id)
            if claim_state in {"active", "completed"}:
                continue  # in-flight or done — no new spawn needed
            if claim_state == "retired":
                demand += 1  # proven-dead worker → re-dispatch (advocate #1)
                continue
            # No claim recorded. A prior dispatched/notified/terminal wake row
            # means the launcher is mid-flight (claim about to land) — covered.
            # Otherwise this recipient was never dispatched → needs first spawn.
            latest = con.execute(
                "SELECT result FROM debate_wake_log "
                "WHERE trigger_msg_id = ? AND target_session_id = ? "
                "AND action = ? ORDER BY created_at DESC LIMIT 1",
                (msg_id, session_id, action),
            ).fetchone()
            if latest is not None and str(latest["result"]) in {
                "dispatched",
                "notified",
                "terminal_no_action",
            }:
                continue
            demand += 1
        return demand
    finally:
        con.close()


def _trigger_is_terminal(msg_id: str, suppressed_roles: set[str]) -> bool:
    """Cursor-advance gate (advocate BLOCK critical #1): the pump cursor may
    pass a trigger ONLY when every recipient reached a terminal outcome.

    Terminal = a reply/STATUS exists, OR the worker claim is completed, OR
    the wake result is notified/terminal_no_action, OR there is no eligible
    binding. A merely-dispatched trigger with an active (in-flight) claim is
    NOT terminal — the cursor stays behind it so a dead worker is re-examined
    and re-dispatched rather than silently skipped. A retired claim without a
    reply is likewise NOT terminal (pending re-dispatch)."""
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        msg = con.execute(
            "SELECT topic_id FROM debate_messages WHERE msg_id = ?",
            (msg_id,),
        ).fetchone()
        if msg is None:
            return True  # unknown message: nothing to block on
        topic_id = str(msg["topic_id"])
        # A role-mode recipient with no active binding = genuinely pending
        # work a worker cannot take yet. NOT terminal (advocate BLOCK #2):
        # holding the cursor keeps the addressed message visible instead of
        # silently skipping it. (Escalation of a chronically-unbound target
        # is tracked separately — a held cursor is safe, a skip is not.)
        if _has_unbound_addressed_recipient(con, msg_id, suppressed_roles):
            return False
        bindings = _recipient_bindings(con, msg_id, suppressed_roles)
        if not bindings:
            # No eligible worker recipient at all: impl vehicle, or only
            # suppressed / direct-session recipients. Nothing to wake.
            return True
        action = os.environ.get("DEBATE_WAKE_ACTION_NAME", "post_tool_use_wake")
        for role, session_id in bindings:
            if _recipient_has_terminal_reply(con, topic_id, role, msg_id):
                continue
            if _recipient_claim_state(con, topic_id, role, msg_id) == "completed":
                continue
            latest = con.execute(
                "SELECT result FROM debate_wake_log "
                "WHERE trigger_msg_id = ? AND target_session_id = ? "
                "AND action = ? ORDER BY created_at DESC LIMIT 1",
                (msg_id, session_id, action),
            ).fetchone()
            if latest is not None and str(latest["result"]) in {
                "notified",
                "terminal_no_action",
            }:
                continue
            return False  # this recipient is not resolved yet
        return True
    finally:
        con.close()


def _count_launched(dispatch_result: dict[str, Any] | None) -> int:
    if not isinstance(dispatch_result, dict):
        return 0
    return sum(
        1
        for item in dispatch_result.get("launches", [])
        if isinstance(item, dict)
        and isinstance(item.get("launch"), dict)
        and item["launch"].get("launched")
    )


def _dispatch_row(row: sqlite3.Row, suppressed_roles: set[str]) -> int:
    import debate_wake

    os.environ.setdefault("DEBATE_WAKE_ACTION", "agent")
    os.environ.setdefault("DEBATE_WAKE_BUDGET", "1")
    os.environ.setdefault("DEBATE_WAKE_ACTION_NAME", "post_tool_use_wake")

    tool_response = {
        "msg_id": row["msg_id"],
        "topic_id": row["topic_id"],
        "schema_version": POST_SCHEMA_VERSION,
    }
    out = debate_wake._handle_tool_response(tool_response)
    if isinstance(out, dict):
        before = set(CHILDREN)
        dispatch_result = debate_wake._maybe_dispatch(
            tool_response, _filter_targets(out, suppressed_roles)
        )
        tracked_launched = _track_launched_children(before)
        return max(_count_launched(dispatch_result), tracked_launched)
    return 0


def _throttle_reason(
    *,
    estimated_worker_demand: int,
    launched_this_scan: int,
    live_children: int,
    max_workers_per_scan: int,
    max_concurrent_workers: int,
) -> str | None:
    if estimated_worker_demand <= 0:
        return None
    if max_workers_per_scan > 0:
        remaining_scan = max_workers_per_scan - launched_this_scan
        if remaining_scan <= 0:
            return "max_workers_per_scan"
    if max_concurrent_workers > 0:
        remaining_concurrent = max_concurrent_workers - live_children
        if remaining_concurrent <= 0:
            return "max_concurrent_workers"
    return None


def _claim_reclaim_cutoff(stale_seconds: int) -> str:
    return (
        (datetime.now(timezone.utc) - timedelta(seconds=stale_seconds))
        .isoformat()
        .replace("+00:00", "Z")
    )


def _active_topic_ids(topics: list[str]) -> list[str]:
    if topics:
        return topics
    con = sqlite3.connect(DB_PATH)
    try:
        return [
            str(row[0])
            for row in con.execute(
                "SELECT topic_id FROM debates "
                "WHERE state IN ('INIT','ACTIVE') ORDER BY topic_id"
            ).fetchall()
        ]
    finally:
        con.close()


def _reclaim_stale_message_claims(
    *,
    topics: list[str],
    stale_seconds: int,
    minimum_age_seconds: int,
) -> None:
    if stale_seconds <= 0:
        return
    from db_utils import get_conn_immediate
    from debate import reclaim_stale_message_claims

    older_than_ts = _claim_reclaim_cutoff(stale_seconds)
    for topic_id in _active_topic_ids(topics):
        try:
            with get_conn_immediate() as conn:
                out = reclaim_stale_message_claims(
                    conn,
                    topic_id=topic_id,
                    older_than_ts=older_than_ts,
                    minimum_age_seconds=minimum_age_seconds,
                )
        except Exception as exc:
            _log(
                "message_claim_reclaim_failed",
                topic_id=topic_id,
                older_than_ts=older_than_ts,
                error=repr(exc),
            )
            continue
        if out.get("reclaimed_count") or out.get("completed_count"):
            _log("message_claim_reclaim", **out)


def _windows_pid_is_live_agent(pid: int, expected_create_time: float | None) -> bool:
    """Real Windows process liveness with PID-reuse protection.

    Identity = pid + create_time (spec REV 2.2): a reused PID whose create
    time differs from the recorded spawn receipt is NOT the old worker.
    If psutil is missing we cannot verify — treat the worker as live so a
    blind sweep never retires a genuinely running Windows worker.
    """
    try:
        import psutil
    except ImportError:
        _log("windows_liveness_psutil_missing", pid=pid)
        return True
    try:
        proc = psutil.Process(pid)
        if proc.status() == psutil.STATUS_ZOMBIE:
            return False
        if (
            expected_create_time is not None
            and abs(proc.create_time() - float(expected_create_time)) > 2.0
        ):
            return False  # PID reuse: different process wearing the old PID
        cmdline = " ".join(proc.cmdline() or [proc.name()]).lower()
    except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
        return False
    return "codex" in cmdline or "claude" in cmdline


def _pid_is_live_agent(pid: int, expected_create_time: float | None = None) -> bool:
    if IS_WINDOWS:
        return _windows_pid_is_live_agent(pid, expected_create_time)
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()
        if len(stat) > 2 and stat[2] == "Z":
            return False
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ")
    except (FileNotFoundError, PermissionError, ProcessLookupError, OSError):
        return False
    return b"codex" in cmdline or b"claude" in cmdline


def _machine_live_worker_count(topics: list[str]) -> int:
    """Count live wake-workers across active topics from durable spawn
    receipts, not just this process's CHILDREN set.

    Advocate BLOCK high-risk #1: after a pump restart, CHILDREN starts
    empty, so a worker that outlived the old pump would not count toward
    max_concurrent_workers — a restarted pump could exceed the cap. This
    DB-backed count reconciles the in-process view with reality (workers
    proven live by pid+create_time in their spawn receipt)."""
    live: set[str] = set()
    for topic_id in _active_topic_ids(topics):
        try:
            live |= _live_worker_session_ids(topic_id)
        except Exception as exc:
            _log("machine_live_worker_count_failed", topic_id=topic_id, error=repr(exc))
    return len(live)


def _safe_machine_live_worker_count(topics: list[str]) -> int:
    """Return the durable census without letting a transient read kill the pump."""
    try:
        return _machine_live_worker_count(topics)
    except Exception as exc:
        _log("machine_live_worker_count_failed", error=repr(exc))
        return len(CHILDREN)


def _live_worker_session_ids(topic_id: str) -> set[str]:
    """Resolve live derived workers from their durable real-spawn receipts."""
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "SELECT c.worker_session_id, w.details_json "
            "FROM debate_worker_claims c "
            "LEFT JOIN debate_wake_log w ON w.wake_id = ("
            " SELECT w2.wake_id FROM debate_wake_log w2 "
            " WHERE w2.topic_id=c.topic_id "
            "   AND w2.trigger_msg_id=c.trigger_msg_id "
            "   AND w2.target_session_id=c.worker_session_id "
            "   AND w2.action='external_agent_spawn' "
            " ORDER BY w2.created_at DESC, w2.wake_id DESC LIMIT 1"
            ") "
            "WHERE c.topic_id=? AND c.state='active'",
            (topic_id,),
        ).fetchall()
    finally:
        con.close()
    live: set[str] = set()
    for row in rows:
        try:
            details = json.loads(row["details_json"] or "{}")
            pid = int(details.get("pid") or 0)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        create_time: float | None
        try:
            create_time = float(details["create_time"])
        except (KeyError, TypeError, ValueError):
            create_time = None  # legacy receipt without identity — pid-only check
        if pid > 0 and _pid_is_live_agent(pid, create_time):
            live.add(str(row["worker_session_id"]))
    return live


def _recover_stale_worker_claims(
    *,
    topics: list[str],
    stale_seconds: int,
    minimum_age_seconds: int,
) -> None:
    if stale_seconds <= 0:
        return
    from db_utils import get_conn_immediate
    from debate import recover_stale_worker_claims

    older_than_ts = _claim_reclaim_cutoff(stale_seconds)
    for topic_id in _active_topic_ids(topics):
        try:
            live = _live_worker_session_ids(topic_id)
            with get_conn_immediate() as conn:
                out = recover_stale_worker_claims(
                    conn,
                    topic_id=topic_id,
                    older_than_ts=older_than_ts,
                    minimum_age_seconds=minimum_age_seconds,
                    live_worker_session_ids=live,
                )
        except Exception as exc:
            _log(
                "worker_claim_recovery_failed",
                topic_id=topic_id,
                older_than_ts=older_than_ts,
                error=repr(exc),
            )
            continue
        if out.get("retired_count") or out.get("completed_count"):
            _log("worker_claim_recovery", **out)


def _windows_child_pids() -> set[int]:
    try:
        import psutil

        return {p.pid for p in psutil.Process(os.getpid()).children(recursive=False)}
    except Exception:
        return set()


def _track_launched_children(before: set[int]) -> int:
    """Track direct child PIDs so the resident pump does not leave zombies."""
    if IS_WINDOWS:
        after = _windows_child_pids()
        launched = after - before
        CHILDREN.update(launched)
        return len(launched)
    try:
        after = {
            int(pid)
            for pid in os.listdir("/proc")
            if pid.isdigit()
            and Path("/proc").joinpath(pid, "stat").read_text().split()[3]
            == str(os.getpid())
        }
    except Exception:
        return 0
    launched = after - before
    CHILDREN.update(launched)
    return len(launched)


def _reap_children() -> None:
    if IS_WINDOWS:
        # No zombie state on Windows: prune exited children; a reused PID
        # that no longer looks like an agent process is pruned as well.
        for pid in list(CHILDREN):
            if not _pid_is_live_agent(pid):
                CHILDREN.discard(pid)
        return
    for pid in list(CHILDREN):
        try:
            waited, _status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            CHILDREN.discard(pid)
            continue
        except OSError:
            continue
        if waited:
            CHILDREN.discard(pid)


def _current_auto_budget() -> Any | None:
    if os.environ.get("DEBATE_RESOURCE_BUDGET", "auto") == "off":
        return None
    from debate_resource_budget import current_debate_resource_budget

    return current_debate_resource_budget()


def _protocol_maintenance(topics: list[str]) -> dict[str, list[dict[str, Any]]]:
    """Apply timeout and same-role recovery transitions in one DB commit."""
    from debate_protocol_v1 import sweep_missing_roles, transition_expired_protocols

    con = sqlite3.connect(DB_PATH, isolation_level=None)
    con.row_factory = sqlite3.Row
    try:
        con.execute("BEGIN IMMEDIATE")
        timed_out = transition_expired_protocols(con)
        recovered = sweep_missing_roles(con, topic_ids=topics)
        con.execute("COMMIT")
        return {"timed_out": timed_out, "role_recoveries": recovered}
    except Exception:
        if con.in_transaction:
            con.execute("ROLLBACK")
        raise
    finally:
        con.close()


def _fetch_released_blind_replay(
    last_ts: str,
    last_msg_id: str,
    topics: list[str],
    limit: int,
    con: sqlite3.Connection | None = None,
    has_blind_table: bool | None = None,
) -> list[sqlite3.Row]:
    """Recover blind CLAIMs that became visible after the main cursor passed.

    The global cursor may legitimately process a later bootstrap PING while an
    earlier independent CLAIM is hidden.  Once the second CLAIM releases the
    barrier, this projection replays only claims at/before that cursor.  The
    caller never moves the global cursor when processing these rows.
    """
    topic_sql, topic_params = _topic_clause(topics)
    owns_connection = con is None
    if con is None:
        con = sqlite3.connect(DB_PATH)
        con.row_factory = sqlite3.Row
    try:
        if has_blind_table is None:
            has_blind_table = _table_exists(con, "debate_blind_commits")
        if not has_blind_table:
            return []
        return con.execute(
            "SELECT DISTINCT m.msg_id,m.topic_id,m.ts "
            "FROM debate_blind_commits bc "
            "JOIN debate_messages m ON m.msg_id=bc.msg_id "
            "JOIN debates d ON d.topic_id=m.topic_id "
            "WHERE bc.released_at IS NOT NULL "
            "AND (m.ts < ? OR (m.ts = ? AND m.msg_id <= ?)) "
            "AND d.state IN ('INIT','ACTIVE') "
            f"{topic_sql} "
            "ORDER BY bc.released_at ASC,m.ts ASC,m.msg_id ASC LIMIT ?",
            [last_ts, last_ts, last_msg_id, *topic_params, limit],
        ).fetchall()
    finally:
        if owns_connection:
            con.close()


def _adaptive_scheduler_decision(
    *,
    queue_depth: int,
    live_workers: int,
    worker_capacity: int,
    retry_attempt: int,
    idle_sweep_attempt: int = 0,
    resource_blocked: bool = False,
    resource_interval: float = 0.0,
) -> dict[str, Any]:
    from debate_protocol_v1 import adaptive_wait_decision

    return adaptive_wait_decision(
        queue_depth=queue_depth,
        live_workers=live_workers,
        worker_capacity=worker_capacity,
        retry_attempt=retry_attempt,
        idle_sweep_attempt=idle_sweep_attempt,
        resource_blocked=resource_blocked,
        resource_interval=resource_interval,
        idle_sweep_seconds=WINDOWS_SWEEP_SECONDS,
        max_idle_sweep_seconds=WINDOWS_MAX_SWEEP_SECONDS,
    )


def _persist_scheduler_decision(
    decision: dict[str, Any],
    *,
    queue_depth: int,
    live_workers: int,
    resource_tier: str,
) -> None:
    from debate_protocol_v1 import record_scheduler_decision

    con = sqlite3.connect(DB_PATH)
    try:
        record_scheduler_decision(
            con,
            interval_seconds=float(decision["interval_seconds"]),
            reason=str(decision["reason"]),
            queue_depth=queue_depth,
            live_workers=live_workers,
            resource_tier=resource_tier,
        )
        con.commit()
    finally:
        con.close()


def _emit_scheduler_decision(
    decision: dict[str, Any],
    *,
    queue_depth: int,
    live_workers: int,
    resource_tier: str,
    previous_signature: tuple[object, ...] | None,
) -> tuple[object, ...]:
    signature = (
        float(decision["interval_seconds"]),
        str(decision["reason"]),
        int(queue_depth),
        int(live_workers),
        resource_tier,
    )
    # Queue/worker counts are part of the durable receipt, but avoid writing
    # identical decisions on every one-second active-lease tick.
    if signature != previous_signature:
        _log(
            "adaptive_scheduler",
            interval_seconds=signature[0],
            reason=signature[1],
            queue_depth=signature[2],
            live_workers=signature[3],
            resource_tier=signature[4],
        )
        try:
            _persist_scheduler_decision(
                decision,
                queue_depth=queue_depth,
                live_workers=live_workers,
                resource_tier=resource_tier,
            )
        except Exception as exc:
            _log("scheduler_decision_persist_failed", error=repr(exc))
    return signature


def _cap_positive(base: int, cap: int) -> int:
    if cap <= 0:
        return 0
    if base <= 0:
        return cap
    return min(base, cap)


def _clamp_wake_budget(configured: int, resource_cap: int) -> int:
    """Apply a transient resource cap without ratcheting the base downward."""
    return min(max(0, configured), max(0, resource_cap))


def _operator_wake_disabled() -> bool:
    """Read the emergency wake kill-switch before any scan-side DB work."""
    path = Path(
        os.environ.get(
            "DEBATE_WAKE_DISABLE_FILE",
            os.path.expanduser("~/.claude/memory/debate_wake.disable"),
        )
    )
    return path.exists()


def _handle_signal(_signum: int, _frame: Any) -> None:
    global STOP
    STOP = True
    STOP_EVENT.set()


def _another_pump_is_live() -> bool:
    """Singleton guard (Windows): Run-key autostart cannot express
    MultipleInstances=IgnoreNew, so the pump enforces it itself via the
    heartbeat's pid + create_time identity."""
    try:
        heartbeat = json.loads(HEARTBEAT_PATH.read_text(encoding="utf-8"))
    except Exception:
        return False
    pid = int(heartbeat.get("pid") or 0)
    if pid <= 0 or pid == os.getpid():
        return False
    try:
        import psutil

        proc = psutil.Process(pid)
        expected = heartbeat.get("create_time")
        if expected and abs(proc.create_time() - float(expected)) > 2.0:
            return False  # PID reuse — the old pump is gone
        cmdline = " ".join(proc.cmdline() or []).lower()
    except Exception:
        return False
    return "debate_pump" in cmdline


WINDOWS_SWEEP_SECONDS = float(os.environ.get("DEBATE_PUMP_WINDOWS_SWEEP_SECONDS", "30"))
WINDOWS_MAX_SWEEP_SECONDS = float(
    os.environ.get("DEBATE_PUMP_WINDOWS_MAX_SWEEP_SECONDS", "300")
)


def _wait_or_stop(seconds: float) -> bool:
    """Sleep until the next scan is due; returns True when stopping.

    Windows: block on the named kernel wake/stop events with a bounded
    timeout (default 30s). The post-commit SetEvent cuts the latency to
    near-zero; the timeout is the guaranteed sweep that replays anything
    committed while no event was delivered (crash-between-commit-and-signal
    recovery). Non-Windows keeps the plain interval sleep.
    """
    if IS_WINDOWS:
        try:
            from debate_wake_signal import wait_for_wake_or_stop
        except Exception as exc:
            _log("windows_wait_adapter_failed", error=repr(exc))
            return STOP_EVENT.wait(max(0.0, seconds))
        # Kernel wait in short slices so the thread-level STOP_EVENT
        # (signal handlers, tests) stays interruptible too: a wake/stop
        # event still cuts the wait to near-zero, and a STOP_EVENT set
        # between slices is honored within ~250ms.
        deadline = time.monotonic() + min(
            max(0.2, seconds), max(WINDOWS_SWEEP_SECONDS, WINDOWS_MAX_SWEEP_SECONDS)
        )
        while True:
            if STOP_EVENT.is_set():
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return STOP_EVENT.is_set()
            try:
                outcome = wait_for_wake_or_stop(min(0.25, remaining))
            except Exception as exc:
                _log("windows_wait_adapter_failed", error=repr(exc))
                return STOP_EVENT.wait(max(0.0, remaining))
            if outcome == "stop":
                global STOP
                STOP = True
                STOP_EVENT.set()
                return True
            if outcome == "wake":
                return STOP_EVENT.is_set()
            if outcome == "unsupported":
                return STOP_EVENT.wait(max(0.0, remaining))
    return STOP_EVENT.wait(max(0.0, seconds))


def main() -> int:
    global STOP
    STOP = False
    STOP_EVENT.clear()
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--topic",
        action="append",
        default=_split_csv_values(os.environ.get("DEBATE_PUMP_TOPICS", "")),
        help="topic filter; repeat or comma-separate; default from DEBATE_PUMP_TOPICS",
    )
    parser.add_argument(
        "--action-kind",
        action="append",
        default=None,
        help="message kinds that should wake agents; default excludes STATUS",
    )
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument(
        "--max-workers-per-scan",
        type=int,
        default=int(os.environ.get("DEBATE_PUMP_MAX_WORKERS_PER_SCAN", "2")),
        help="max launched workers per scan; <=0 disables this throttle",
    )
    parser.add_argument(
        "--max-concurrent-workers",
        type=int,
        default=int(os.environ.get("DEBATE_PUMP_MAX_CONCURRENT_WORKERS", "2")),
        help="max live workers launched by this pump; <=0 disables this throttle",
    )
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--since", default="")
    parser.add_argument(
        "--message-claim-reclaim-seconds",
        type=int,
        default=int(os.environ.get("DEBATE_MESSAGE_CLAIM_RECLAIM_SECONDS", "900")),
        help="reclaim standing=false DECISION claims older than this; <=0 disables",
    )
    parser.add_argument(
        "--message-claim-reclaim-interval",
        type=float,
        default=float(os.environ.get("DEBATE_MESSAGE_CLAIM_RECLAIM_INTERVAL", "60")),
        help="seconds between stale message-claim reclaim sweeps",
    )
    parser.add_argument(
        "--message-claim-reclaim-min-age-seconds",
        type=int,
        default=int(
            os.environ.get("DEBATE_MESSAGE_CLAIM_RECLAIM_MIN_AGE_SECONDS", "120")
        ),
        help="DAO guard against too-recent reclaim cutoffs",
    )
    parser.add_argument(
        "--worker-claim-recovery-seconds",
        type=int,
        default=int(os.environ.get("DEBATE_WORKER_CLAIM_RECOVERY_SECONDS", "900")),
        help="retire dead wake-worker claims older than this; <=0 disables",
    )
    parser.add_argument(
        "--worker-claim-recovery-interval",
        type=float,
        default=float(os.environ.get("DEBATE_WORKER_CLAIM_RECOVERY_INTERVAL", "60")),
        help="seconds between dead worker reconciliation sweeps",
    )
    parser.add_argument(
        "--worker-claim-recovery-min-age-seconds",
        type=int,
        default=int(
            os.environ.get("DEBATE_WORKER_CLAIM_RECOVERY_MIN_AGE_SECONDS", "120")
        ),
        help="DAO guard against too-recent worker recovery cutoffs",
    )
    parser.add_argument(
        "--suppress-role",
        action="append",
        default=None,
    )
    parser.add_argument(
        "--mcp-prefix",
        default="",
        help="MCP tool prefix for spawned claude workers (Task Scheduler has "
        "no env block, so the Windows install passes it as an argument), "
        "e.g. mcp__sqlite_unified__",
    )
    args = parser.parse_args()
    if args.mcp_prefix:
        os.environ["DEBATE_WAKE_MCP_PREFIX"] = args.mcp_prefix
    if IS_WINDOWS and not args.once:
        # Atomic OS mutex (advocate BLOCK high-risk #1): the heartbeat-file
        # guard is a read-check-act race — two pumps racing at logon could
        # both pass it. CreateMutexW is atomic; the loser exits. The
        # advisory heartbeat check stays as a cheap fast-path log.
        try:
            from debate_wake_signal import acquire_pump_singleton

            if not acquire_pump_singleton():
                _log("pump_singleton_held_exit", pid=os.getpid())
                return 0
        except Exception as exc:
            _log("pump_singleton_check_failed", error=repr(exc))
            if _another_pump_is_live():
                _log("pump_already_running_exit", pid=os.getpid())
                return 0

    topics = _split_csv_values(args.topic)
    action_kind_values = args.action_kind or os.environ.get(
        "DEBATE_PUMP_ACTION_KINDS", DEFAULT_ACTION_KINDS
    ).split(",")
    suppress_role_values = args.suppress_role or os.environ.get(
        "DEBATE_PUMP_SUPPRESS_ROLES", "CONDUCTOR"
    ).split(",")
    action_kinds = [k.upper() for k in _split_csv_values(action_kind_values)]
    suppressed_roles = {r.upper() for r in _split_csv_values(suppress_role_values)}
    max_workers_per_scan = max(0, args.max_workers_per_scan)
    max_concurrent_workers = max(0, args.max_concurrent_workers)
    default_wake_budget = max(
        1,
        max_workers_per_scan or 0,
        max_concurrent_workers or 0,
    )
    os.environ.setdefault("DEBATE_WAKE_BUDGET", str(default_wake_budget))
    configured_wake_budget = max(
        0, int(os.environ.get("DEBATE_WAKE_BUDGET", str(default_wake_budget)))
    )
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    state = _load_state()
    # Startup backlog sweep (advocate BLOCK critical #2): a missing or
    # corrupt state file previously defaulted the cursor to now(), so any
    # addressed message committed before the pump started was invisible
    # forever. On a fresh start with no persisted cursor, begin from epoch
    # so the durable backlog is swept; wake_log/worker_claim dedup in
    # _estimate_worker_demand makes re-examining handled messages a no-op,
    # not a re-dispatch. An explicit --since still wins.
    startup_backlog_sweep = not args.since and not state.get("last_ts")
    if args.since:
        last_ts = args.since
        last_msg_id = ""
    elif state.get("last_ts"):
        last_ts = state["last_ts"]
        last_msg_id = state.get("last_msg_id", "")
    else:
        last_ts = "1970-01-01T00:00:00Z"
        last_msg_id = ""
    _log(
        "pump_start",
        pid=os.getpid(),
        startup_backlog_sweep=startup_backlog_sweep,
        topics=topics,
        interval=args.interval,
        last_ts=last_ts,
        last_msg_id=last_msg_id,
        action_kinds=action_kinds,
        suppressed_roles=sorted(suppressed_roles),
        max_workers_per_scan=max_workers_per_scan,
        max_concurrent_workers=max_concurrent_workers,
        message_claim_reclaim_seconds=args.message_claim_reclaim_seconds,
        message_claim_reclaim_interval=args.message_claim_reclaim_interval,
        message_claim_reclaim_min_age_seconds=args.message_claim_reclaim_min_age_seconds,
        worker_claim_recovery_seconds=args.worker_claim_recovery_seconds,
        worker_claim_recovery_interval=args.worker_claim_recovery_interval,
        worker_claim_recovery_min_age_seconds=args.worker_claim_recovery_min_age_seconds,
    )
    last_claim_reclaim_at = 0.0
    last_worker_recovery_at = 0.0
    scheduler_retry_attempt = 0
    idle_sweep_attempt = 0
    last_scheduler_signature: tuple[object, ...] | None = None

    while not STOP:
        _reap_children()
        _write_heartbeat(last_ts, last_msg_id)
        if _operator_wake_disabled():
            _log("pump_paused_by_operator_disable")
            if args.once:
                break
            _wait_or_stop(max(30.0, args.interval))
            continue
        try:
            maintenance = _protocol_maintenance(topics)
            if maintenance["timed_out"] or maintenance["role_recoveries"]:
                _log("protocol_maintenance", **maintenance)
        except (sqlite3.OperationalError, ImportError) as exc:
            # Mixed-version upgrade window: old DB/schema keeps the legacy
            # pump path alive until init_db applies debate/v1 tables.
            _log("protocol_maintenance_unavailable", error=repr(exc))
        except Exception as exc:
            _log("protocol_maintenance_failed", error=repr(exc))
        if (
            args.message_claim_reclaim_seconds > 0
            and time.monotonic() - last_claim_reclaim_at
            >= max(1.0, args.message_claim_reclaim_interval)
        ):
            _reclaim_stale_message_claims(
                topics=topics,
                stale_seconds=args.message_claim_reclaim_seconds,
                minimum_age_seconds=args.message_claim_reclaim_min_age_seconds,
            )
            last_claim_reclaim_at = time.monotonic()
        if (
            not args.once
            and args.worker_claim_recovery_seconds > 0
            and time.monotonic() - last_worker_recovery_at
            >= max(1.0, args.worker_claim_recovery_interval)
        ):
            _recover_stale_worker_claims(
                topics=topics,
                stale_seconds=args.worker_claim_recovery_seconds,
                minimum_age_seconds=args.worker_claim_recovery_min_age_seconds,
            )
            last_worker_recovery_at = time.monotonic()
        rows: list[sqlite3.Row] = []
        baseline_live_workers = _safe_machine_live_worker_count(topics)
        throttled = False
        dispatch_failed = False
        scan_failed = False
        eligible_queue_depth = 0
        resource_tier = ""
        effective_max_concurrent_workers = max_concurrent_workers
        try:
            effective_action_kinds = action_kinds
            effective_limit = args.limit
            effective_max_workers_per_scan = max_workers_per_scan
            effective_max_concurrent_workers = max_concurrent_workers
            auto_budget = None
            try:
                auto_budget = _current_auto_budget()
            except Exception as exc:
                _log("pump_resource_budget_failed", error=repr(exc))
                auto_budget = None
            if auto_budget is not None:
                resource_tier = str(auto_budget.tier)
                _log("pump_resource_budget", **auto_budget.to_dict())
                if not auto_budget.allow_agent:
                    _log("pump_paused_by_resource_budget", **auto_budget.to_dict())
                    if args.once:
                        break
                    decision = _adaptive_scheduler_decision(
                        queue_depth=0,
                        live_workers=max(len(CHILDREN), baseline_live_workers),
                        worker_capacity=0,
                        retry_attempt=0,
                        resource_blocked=True,
                        resource_interval=auto_budget.interval_seconds,
                    )
                    last_scheduler_signature = _emit_scheduler_decision(
                        decision,
                        queue_depth=0,
                        live_workers=max(len(CHILDREN), baseline_live_workers),
                        resource_tier=resource_tier,
                        previous_signature=last_scheduler_signature,
                    )
                    _wait_or_stop(float(decision["interval_seconds"]))
                    continue
                effective_action_kinds = [
                    kind
                    for kind in action_kinds
                    if kind in set(auto_budget.action_kinds)
                ]
                effective_limit = (
                    min(args.limit, auto_budget.limit)
                    if args.limit > 0
                    else auto_budget.limit
                )
                effective_max_workers_per_scan = _cap_positive(
                    max_workers_per_scan,
                    auto_budget.max_workers_per_scan,
                )
                effective_max_concurrent_workers = _cap_positive(
                    max_concurrent_workers,
                    auto_budget.max_concurrent_workers,
                )
                os.environ["DEBATE_WAKE_BUDGET"] = str(
                    _clamp_wake_budget(configured_wake_budget, auto_budget.wake_budget)
                )
                if not effective_action_kinds or effective_limit <= 0:
                    _log(
                        "pump_paused_by_empty_resource_budget", **auto_budget.to_dict()
                    )
                    if args.once:
                        break
                    decision = _adaptive_scheduler_decision(
                        queue_depth=0,
                        live_workers=max(len(CHILDREN), baseline_live_workers),
                        worker_capacity=0,
                        retry_attempt=0,
                        resource_blocked=True,
                        resource_interval=auto_budget.interval_seconds,
                    )
                    last_scheduler_signature = _emit_scheduler_decision(
                        decision,
                        queue_depth=0,
                        live_workers=max(len(CHILDREN), baseline_live_workers),
                        resource_tier=resource_tier,
                        previous_signature=last_scheduler_signature,
                    )
                    _wait_or_stop(float(decision["interval_seconds"]))
                    continue

            scan_con = sqlite3.connect(DB_PATH)
            scan_con.row_factory = sqlite3.Row
            try:
                has_blind_table = _table_exists(scan_con, "debate_blind_commits")
                has_delivery_queue = _table_exists(scan_con, "debate_delivery_queue")
                targeted_rows = _fetch_pending_deliveries(
                    topics,
                    effective_action_kinds,
                    effective_limit,
                    scan_con,
                    has_delivery_queue,
                    has_blind_table,
                )
                regular_rows = _fetch_new(
                    last_ts,
                    last_msg_id,
                    topics,
                    effective_action_kinds,
                    effective_limit,
                    scan_con,
                    has_blind_table,
                )
                replay_rows = _fetch_released_blind_replay(
                    last_ts,
                    last_msg_id,
                    topics,
                    effective_limit,
                    scan_con,
                    has_blind_table,
                )
            finally:
                scan_con.close()
            rows_by_id: dict[str, dict[str, Any]] = {}
            rows = []
            for source_rows, targeted, replay, cursor_eligible in (
                (targeted_rows, True, False, False),
                (replay_rows, False, True, False),
                (regular_rows, False, False, True),
            ):
                for source_row in source_rows:
                    msg_id = str(source_row["msg_id"])
                    existing = rows_by_id.get(msg_id)
                    if existing is not None:
                        existing["_targeted_delivery"] = bool(
                            existing.get("_targeted_delivery") or targeted
                        )
                        existing["_blind_replay"] = bool(
                            existing.get("_blind_replay") or replay
                        )
                        existing["_cursor_eligible"] = bool(
                            existing.get("_cursor_eligible") or cursor_eligible
                        )
                        continue
                    item = {
                        **dict(source_row),
                        "_targeted_delivery": targeted,
                        "_blind_replay": replay,
                        "_cursor_eligible": cursor_eligible,
                    }
                    rows_by_id[msg_id] = item
                    rows.append(item)
            dispatched_rows = 0
            launched_this_scan = 0
            partial_pending = False
            # Machine-wide receipts reconcile a restarted pump's empty
            # CHILDREN set with workers that outlived it on every platform.
            # Workers launched during this scan are tracked by CHILDREN.
            for row in rows:
                _reap_children()
                live_children = max(len(CHILDREN), baseline_live_workers)
                msg_id = row["msg_id"]
                # Terminal → advance the cursor past it and keep scanning.
                if _trigger_is_terminal(msg_id, suppressed_roles):
                    if row.get("_targeted_delivery"):
                        completed = _complete_pending_deliveries(
                            msg_id, enqueued_at=str(row.get("ts") or _now())
                        )
                        if completed:
                            _log(
                                "pump_targeted_delivery_completed",
                                msg_id=msg_id,
                                recipients=completed,
                            )
                    if row.get("_cursor_eligible") and not row.get("_blind_replay"):
                        last_ts = row["ts"]
                        last_msg_id = msg_id
                        _save_state(last_ts, last_msg_id)
                    continue
                # Not terminal. Does it need a NEW worker spawn, or is one
                # already in-flight? demand counts only recipients with a
                # dead/absent claim + no reply (advocate BLOCK critical #1).
                estimated_worker_demand = _estimate_worker_demand(
                    msg_id, suppressed_roles
                )
                if estimated_worker_demand <= 0:
                    # In-flight worker exists; no new spawn. Hold the cursor
                    # behind this trigger until it becomes terminal — never
                    # re-dispatch a live worker.
                    partial_pending = True
                    _log(
                        "pump_trigger_in_flight_hold_cursor",
                        msg_id=msg_id,
                        topic_id=row["topic_id"],
                        live_children=live_children,
                        last_ts=last_ts,
                        last_msg_id=last_msg_id,
                    )
                    continue
                reason = _throttle_reason(
                    estimated_worker_demand=estimated_worker_demand,
                    launched_this_scan=launched_this_scan,
                    live_children=live_children,
                    max_workers_per_scan=effective_max_workers_per_scan,
                    max_concurrent_workers=effective_max_concurrent_workers,
                )
                if reason is not None:
                    throttled = True
                    eligible_queue_depth += 1
                    _log(
                        "pump_dispatch_throttled",
                        reason=reason,
                        msg_id=msg_id,
                        topic_id=row["topic_id"],
                        estimated_worker_demand=estimated_worker_demand,
                        launched_this_scan=launched_this_scan,
                        live_children=live_children,
                        max_workers_per_scan=effective_max_workers_per_scan,
                        max_concurrent_workers=effective_max_concurrent_workers,
                        last_ts=last_ts,
                        last_msg_id=last_msg_id,
                    )
                    break
                try:
                    launched = _dispatch_row(row, suppressed_roles)
                    launched_this_scan += launched
                    dispatched_rows += 1
                    if launched < estimated_worker_demand:
                        eligible_queue_depth += 1
                except Exception as exc:
                    dispatch_failed = True
                    eligible_queue_depth += 1
                    _log("dispatch_failed", msg_id=msg_id, error=repr(exc))
                    break
                # Just dispatched → in-flight, not terminal. Keep its durable
                # queue row pending, but continue to independent messages while
                # capacity remains. The unique worker claim prevents duplicate
                # execution for this (topic, role, trigger).
                partial_pending = True
                _log(
                    "pump_dispatched_hold_cursor",
                    msg_id=msg_id,
                    topic_id=row["topic_id"],
                    launched_this_scan=launched_this_scan,
                    live_children=live_children,
                    last_ts=last_ts,
                    last_msg_id=last_msg_id,
                )
                continue
            if rows:
                _log(
                    "scan_batch",
                    count=len(rows),
                    dispatched_rows=dispatched_rows,
                    launched_workers=launched_this_scan,
                    throttled=throttled,
                    partial_pending=partial_pending,
                    live_children=len(CHILDREN),
                    effective_action_kinds=effective_action_kinds,
                    effective_limit=effective_limit,
                    eligible_queue_depth=eligible_queue_depth,
                    last_ts=last_ts,
                    last_msg_id=last_msg_id,
                )
        except Exception as exc:
            scan_failed = True
            _log("scan_failed", error=repr(exc))
        if args.once:
            break
        if scan_failed or dispatch_failed or throttled:
            scheduler_retry_attempt = min(scheduler_retry_attempt + 1, 5)
            idle_sweep_attempt = 0
        else:
            scheduler_retry_attempt = 0
            if (
                eligible_queue_depth > 0
                or max(len(CHILDREN), baseline_live_workers) > 0
            ):
                idle_sweep_attempt = 0
        live_workers = max(len(CHILDREN), baseline_live_workers)
        decision = _adaptive_scheduler_decision(
            queue_depth=eligible_queue_depth,
            live_workers=live_workers,
            worker_capacity=effective_max_concurrent_workers,
            retry_attempt=scheduler_retry_attempt,
            idle_sweep_attempt=idle_sweep_attempt,
        )
        last_scheduler_signature = _emit_scheduler_decision(
            decision,
            queue_depth=eligible_queue_depth,
            live_workers=live_workers,
            resource_tier=resource_tier,
            previous_signature=last_scheduler_signature,
        )
        _wait_or_stop(float(decision["interval_seconds"]))
        if (
            eligible_queue_depth == 0
            and live_workers == 0
            and scheduler_retry_attempt == 0
        ):
            idle_sweep_attempt = min(idle_sweep_attempt + 1, 8)

    _reap_children()
    _log("pump_stop", pid=os.getpid(), last_ts=last_ts, last_msg_id=last_msg_id)
    if IS_WINDOWS and not args.once:
        try:
            # Release the singleton before publishing the stopped state.  The
            # lifecycle command may start the replacement as soon as the
            # heartbeat disappears; leaving release to interpreter teardown
            # creates a small but real stop -> start race.
            from debate_wake_signal import release_pump_singleton

            release_pump_singleton()
        except Exception as exc:
            _log("pump_singleton_release_failed", error=repr(exc))
    try:
        # Clean exit removes the heartbeat so status reads "stopped", not
        # "stale"; a crashed pump leaves it behind — which is the signal
        # that distinguishes the two.
        HEARTBEAT_PATH.unlink(missing_ok=True)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
