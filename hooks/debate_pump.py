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
import time
from datetime import datetime, timezone
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
CHILDREN: set[int] = set()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _log(event: str, **fields: Any) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": _now(), "event": event, **fields}, ensure_ascii=False) + "\n")


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
            "JOIN debate_message_recipients r ON r.msg_id = m.msg_id "
            "WHERE (m.ts > ? OR (m.ts = ? AND m.msg_id > ?)) "
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


def _dispatch_row(row: sqlite3.Row, suppressed_roles: set[str]) -> None:
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
        debate_wake._maybe_dispatch(tool_response, _filter_targets(out, suppressed_roles))
        _track_launched_children(before)


def _track_launched_children(before: set[int]) -> None:
    """Track direct child PIDs so the resident pump does not leave zombies."""
    try:
        after = {
            int(pid)
            for pid in os.listdir("/proc")
            if pid.isdigit()
            and Path("/proc") .joinpath(pid, "stat").read_text().split()[3] == str(os.getpid())
        }
    except Exception:
        return
    CHILDREN.update(after - before)


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


def _handle_signal(_signum: int, _frame: Any) -> None:
    global STOP
    STOP = True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--topic", action="append", default=[])
    parser.add_argument(
        "--action-kind",
        action="append",
        default=os.environ.get("DEBATE_PUMP_ACTION_KINDS", "Q,DECISION,STATE").split(","),
        help="message kinds that should wake agents; default excludes STATUS",
    )
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--since", default="")
    parser.add_argument(
        "--suppress-role",
        action="append",
        default=os.environ.get("DEBATE_PUMP_SUPPRESS_ROLES", "CONDUCTOR").split(","),
    )
    args = parser.parse_args()

    topics = [t for t in args.topic if t]
    action_kinds = [k.strip().upper() for k in args.action_kind if k and k.strip()]
    suppressed_roles = {r.strip().upper() for r in args.suppress_role if r and r.strip()}
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
    )

    while not STOP:
        _reap_children()
        try:
            rows = _fetch_new(last_ts, last_msg_id, topics, action_kinds, args.limit)
            for row in rows:
                try:
                    _dispatch_row(row, suppressed_roles)
                except Exception as exc:
                    _log("dispatch_failed", msg_id=row["msg_id"], error=repr(exc))
                last_ts = row["ts"]
                last_msg_id = row["msg_id"]
                _save_state(last_ts, last_msg_id)
            if rows:
                _log("scan_batch", count=len(rows), last_ts=last_ts, last_msg_id=last_msg_id)
        except Exception as exc:
            _log("scan_failed", error=repr(exc))
        if args.once:
            break
        time.sleep(max(0.2, args.interval))

    _reap_children()
    _log("pump_stop", pid=os.getpid(), last_ts=last_ts, last_msg_id=last_msg_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
