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
import sqlite3
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


HOOK_DIR = Path(__file__).resolve().parent
REPO = Path(os.environ.get("DEBATE_REPO", "/home/rmanov/sqlite-memory-mcp"))
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
POST_SCHEMA_VERSION = "debate_post_with_recipients.v1"

STOP = False
STOP_EVENT = threading.Event()
CHILDREN: set[int] = set()
LOG_MAX_BYTES = int(os.environ.get("DEBATE_PUMP_LOG_MAX_BYTES", str(20 * 1024 * 1024)))
LOG_KEEP = max(1, int(os.environ.get("DEBATE_PUMP_LOG_KEEP", "3")))


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


def _fetch_new(
    last_ts: str,
    last_msg_id: str,
    topics: list[str],
    kinds: list[str],
    limit: int,
) -> list[sqlite3.Row]:
    topic_sql, topic_params = _topic_clause(topics)
    kind_sql, kind_params = _kind_clause(kinds)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        return con.execute(
            "SELECT DISTINCT m.msg_id, m.topic_id, m.ts "
            "FROM debate_messages m "
            "JOIN debates d ON d.topic_id = m.topic_id "
            "JOIN debate_message_recipients r ON r.msg_id = m.msg_id "
            "WHERE (m.ts > ? OR (m.ts = ? AND m.msg_id > ?)) "
            "AND d.state IN ('INIT','ACTIVE') "
            f"{topic_sql} "
            f"{kind_sql} "
            "ORDER BY m.ts ASC, m.msg_id ASC LIMIT ?",
            [last_ts, last_ts, last_msg_id, *topic_params, *kind_params, limit],
        ).fetchall()
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


def _estimate_worker_demand(msg_id: str, suppressed_roles: set[str]) -> int:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        msg = con.execute(
            "SELECT topic_id, vehicle FROM debate_messages WHERE msg_id = ?",
            (msg_id,),
        ).fetchone()
        if msg is None:
            return 0
        # ``implementation`` is deliberately refused by the bounded wake
        # router: it requires a conductor-approved edit-capable vehicle.  It
        # therefore creates no per-session wake result/worker claim.  Treat it
        # as zero bounded-worker demand here so the resident pump can advance
        # past the typed refusal instead of retrying the same message forever.
        if str(msg["vehicle"] or "analysis") == "implementation":
            return 0
        rows = con.execute(
            "SELECT recipient, recipient_mode FROM debate_message_recipients "
            "WHERE msg_id = ? ORDER BY recipient",
            (msg_id,),
        ).fetchall()
        demand = 0
        action = os.environ.get("DEBATE_WAKE_ACTION_NAME", "post_tool_use_wake")
        for row in rows:
            recipient = str(row["recipient"] or "").strip()
            if recipient.upper() in suppressed_roles:
                continue
            binding: sqlite3.Row | None = None
            if row["recipient_mode"] == "normal":
                binding = con.execute(
                    "SELECT session_id FROM debate_role_bindings "
                    "WHERE topic_id = ? AND role = ? AND state = 'active' "
                    "ORDER BY generation DESC LIMIT 1",
                    (msg["topic_id"], recipient),
                ).fetchone()
            else:
                binding = con.execute(
                    "SELECT session_id FROM debate_role_bindings "
                    "WHERE topic_id = ? AND session_id = ? AND state = 'diagnostic' "
                    "ORDER BY generation DESC LIMIT 1",
                    (msg["topic_id"], recipient),
                ).fetchone()
            if binding is None:
                continue
            latest = con.execute(
                "SELECT result FROM debate_wake_log "
                "WHERE trigger_msg_id = ? AND target_session_id = ? "
                "AND action = ? ORDER BY created_at DESC LIMIT 1",
                (msg_id, binding["session_id"], action),
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
    sys.path.insert(0, str(HOOK_DIR))
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
    return (datetime.now(timezone.utc) - timedelta(seconds=stale_seconds)).isoformat().replace(
        "+00:00", "Z"
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
    sys.path.insert(0, str(REPO))
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


def _pid_is_live_agent(pid: int) -> bool:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()
        if len(stat) > 2 and stat[2] == "Z":
            return False
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ")
    except (FileNotFoundError, PermissionError, ProcessLookupError, OSError):
        return False
    return b"codex" in cmdline or b"claude" in cmdline


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
        if pid > 0 and _pid_is_live_agent(pid):
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
    sys.path.insert(0, str(REPO))
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


def _track_launched_children(before: set[int]) -> int:
    """Track direct child PIDs so the resident pump does not leave zombies."""
    try:
        after = {
            int(pid)
            for pid in os.listdir("/proc")
            if pid.isdigit()
            and Path("/proc") .joinpath(pid, "stat").read_text().split()[3] == str(os.getpid())
        }
    except Exception:
        return 0
    launched = after - before
    CHILDREN.update(launched)
    return len(launched)


def _reap_children() -> None:
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
    sys.path.insert(0, str(HOOK_DIR))
    from debate_resource_budget import current_debate_resource_budget

    return current_debate_resource_budget()


def _cap_positive(base: int, cap: int) -> int:
    if cap <= 0:
        return 0
    if base <= 0:
        return cap
    return min(base, cap)


def _handle_signal(_signum: int, _frame: Any) -> None:
    global STOP
    STOP = True
    STOP_EVENT.set()


def _wait_or_stop(seconds: float) -> bool:
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
        default=int(os.environ.get("DEBATE_MESSAGE_CLAIM_RECLAIM_MIN_AGE_SECONDS", "120")),
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
        default=int(os.environ.get("DEBATE_WORKER_CLAIM_RECOVERY_MIN_AGE_SECONDS", "120")),
        help="DAO guard against too-recent worker recovery cutoffs",
    )
    parser.add_argument(
        "--suppress-role",
        action="append",
        default=None,
    )
    args = parser.parse_args()

    topics = _split_csv_values(args.topic)
    action_kind_values = args.action_kind or os.environ.get(
        "DEBATE_PUMP_ACTION_KINDS", "Q,A,DECISION,STATE"
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
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    state = _load_state()
    last_ts = args.since or state.get("last_ts") or _now()
    last_msg_id = "" if args.since else state.get("last_msg_id", "")
    _log(
        "pump_start",
        pid=os.getpid(),
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

    while not STOP:
        _reap_children()
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
        try:
            loop_interval = max(0.2, args.interval)
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
                _log("pump_resource_budget", **auto_budget.to_dict())
                if not auto_budget.allow_agent:
                    _log("pump_paused_by_resource_budget", **auto_budget.to_dict())
                    if args.once:
                        break
                    _wait_or_stop(max(loop_interval, auto_budget.interval_seconds))
                    continue
                effective_action_kinds = [
                    kind for kind in action_kinds if kind in set(auto_budget.action_kinds)
                ]
                effective_limit = min(args.limit, auto_budget.limit) if args.limit > 0 else auto_budget.limit
                effective_max_workers_per_scan = _cap_positive(
                    max_workers_per_scan,
                    auto_budget.max_workers_per_scan,
                )
                effective_max_concurrent_workers = _cap_positive(
                    max_concurrent_workers,
                    auto_budget.max_concurrent_workers,
                )
                os.environ["DEBATE_WAKE_BUDGET"] = str(
                    min(
                        int(os.environ.get("DEBATE_WAKE_BUDGET", str(default_wake_budget))),
                        max(0, auto_budget.wake_budget),
                    )
                )
                loop_interval = max(loop_interval, auto_budget.interval_seconds)
                if not effective_action_kinds or effective_limit <= 0:
                    _log("pump_paused_by_empty_resource_budget", **auto_budget.to_dict())
                    if args.once:
                        break
                    _wait_or_stop(loop_interval)
                    continue

            rows = _fetch_new(
                last_ts,
                last_msg_id,
                topics,
                effective_action_kinds,
                effective_limit,
            )
            dispatched_rows = 0
            launched_this_scan = 0
            throttled = False
            partial_pending = False
            for row in rows:
                _reap_children()
                estimated_worker_demand = _estimate_worker_demand(row["msg_id"], suppressed_roles)
                reason = _throttle_reason(
                    estimated_worker_demand=estimated_worker_demand,
                    launched_this_scan=launched_this_scan,
                    live_children=len(CHILDREN),
                    max_workers_per_scan=effective_max_workers_per_scan,
                    max_concurrent_workers=effective_max_concurrent_workers,
                )
                if reason is not None:
                    throttled = True
                    _log(
                        "pump_dispatch_throttled",
                        reason=reason,
                        msg_id=row["msg_id"],
                        topic_id=row["topic_id"],
                        estimated_worker_demand=estimated_worker_demand,
                        launched_this_scan=launched_this_scan,
                        live_children=len(CHILDREN),
                        max_workers_per_scan=effective_max_workers_per_scan,
                        max_concurrent_workers=effective_max_concurrent_workers,
                        last_ts=last_ts,
                        last_msg_id=last_msg_id,
                    )
                    break
                try:
                    launched_this_scan += _dispatch_row(row, suppressed_roles)
                    dispatched_rows += 1
                except Exception as exc:
                    _log("dispatch_failed", msg_id=row["msg_id"], error=repr(exc))
                    break
                remaining_worker_demand = _estimate_worker_demand(
                    row["msg_id"], suppressed_roles
                )
                if remaining_worker_demand > 0:
                    partial_pending = True
                    _log(
                        "pump_partial_dispatch_pending",
                        msg_id=row["msg_id"],
                        topic_id=row["topic_id"],
                        remaining_worker_demand=remaining_worker_demand,
                        launched_this_scan=launched_this_scan,
                        live_children=len(CHILDREN),
                        max_workers_per_scan=effective_max_workers_per_scan,
                        max_concurrent_workers=effective_max_concurrent_workers,
                        last_ts=last_ts,
                        last_msg_id=last_msg_id,
                    )
                    break
                last_ts = row["ts"]
                last_msg_id = row["msg_id"]
                _save_state(last_ts, last_msg_id)
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
                    last_ts=last_ts,
                    last_msg_id=last_msg_id,
                )
        except Exception as exc:
            _log("scan_failed", error=repr(exc))
        if args.once:
            break
        _wait_or_stop(loop_interval)

    _reap_children()
    _log("pump_stop", pid=os.getpid(), last_ts=last_ts, last_msg_id=last_msg_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
