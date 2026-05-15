#!/usr/bin/env python3
"""PostToolUse hook for debate wake target resolution.

Fast path for the event-driven orchestration layer:
  debate_post_with_recipients tool call -> parse tool response -> resolve
  active role bindings -> write debate_wake_log.

This hook is intentionally signal-only by default. It never posts debate
messages, and real wake actions must be enabled explicitly via
DEBATE_WAKE_ACTION.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REPO = Path(os.environ.get("DEBATE_REPO", "/home/rmanov/sqlite-memory-mcp"))
LOG_PATH = Path(
    os.environ.get(
        "DEBATE_WAKE_HOOK_LOG",
        os.path.expanduser("~/.claude/memory/debate_wake_hook.jsonl"),
    )
)
AGENT_LOG_DIR = Path(
    os.environ.get(
        "DEBATE_WAKE_AGENT_LOG_DIR",
        os.path.expanduser("~/.claude/memory/debate_wake_agents"),
    )
)
TARGET_TOOL = "mcp__sqlite_intel__debate_post_with_recipients"
POST_SCHEMA_VERSION = "debate_post_with_recipients.v1"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _log(event: str, **fields: Any) -> None:
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {"ts": _now(), "event": event, **fields}
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _json_maybe(value: Any) -> Any:
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return value
        try:
            return json.loads(text)
        except Exception:
            return value
    return value


def _unwrap_tool_response(value: Any) -> dict[str, Any] | None:
    """Accept Claude hook shapes and MCP result wrappers.

    Observed MCP output often arrives as {"result": "{\"msg_id\": ...}"},
    but hook schemas differ across Claude versions. Keep this recursive and
    conservative so schema drift fails closed in the DAO layer.
    """
    value = _json_maybe(value)
    if isinstance(value, dict):
        if "msg_id" in value or "schema_version" in value:
            return value
        for key in ("result", "tool_response", "tool_output", "output"):
            if key in value:
                unwrapped = _unwrap_tool_response(value[key])
                if unwrapped is not None:
                    return unwrapped
    return None


def _extract_tool_response(hook_payload: dict[str, Any]) -> dict[str, Any] | None:
    for key in ("tool_response", "tool_output", "response", "result"):
        if key in hook_payload:
            out = _unwrap_tool_response(hook_payload[key])
            if out is not None:
                return out
    return _unwrap_tool_response(hook_payload)


def _notify(target: dict[str, Any], trigger_msg_id: str) -> None:
    if not os.environ.get("DISPLAY"):
        return
    title = f"Debate wake: {target.get('target_role') or 'role'}"
    body = f"msg={trigger_msg_id} session={target.get('target_session_id') or '-'}"
    subprocess.run(
        ["notify-send", title, body],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=2,
        check=False,
    )


def _wake_prompt(target: dict[str, Any], trigger_msg_id: str, topic_id: str) -> str:
    role = target.get("target_role") or "UNKNOWN"
    session_id = target.get("target_session_id") or ""
    return f"""You are {role} in the local debate protocol.

Autonomous wake trigger:
- topic_id: {topic_id}
- trigger_msg_id: {trigger_msg_id}
- session_id: {session_id}

Task:
1. Check the debate inbox for this role/session/topic.
2. If there is no substantive work, reply NO_ACTION and do not post.
3. If there is work, post at most one focused debate response with
   debate_post_with_recipients. Do not use bare debate_post: unaddressed
   messages are invisible to the pump.
4. Address the response to the role(s) named by the trigger, normally
   CONDUCTOR and any review role that must see it.
5. Quote the trigger msg_id or the specific msg_id(s) you read.
6. After a successful post, advance this role/session/topic cursor to the latest
   message you substantively handled, normally the trigger msg_id.
7. Do not edit files, run unrelated commands, or broaden scope.
8. Stop after that one response and cursor advance.
"""


def _agent_command(target: dict[str, Any], trigger_msg_id: str, topic_id: str) -> list[str] | None:
    runtime = str(target.get("target_runtime") or "")
    if runtime == "cc":
        allowed = ",".join(
            [
                "mcp__sqlite_intel__debate_signal_check",
                "mcp__sqlite_intel__debate_post_with_recipients",
                "mcp__sqlite_intel__debate_signal_advance",
                "mcp__sqlite_intel__debate_binding_list",
                "mcp__sqlite_intel__debate_worker_claim",
            ]
        )
        return [
            "claude",
            "-p",
            "--permission-mode",
            "auto",
            "--allowedTools",
            allowed,
        ]
    if runtime == "codex":
        return [
            "codex",
            "exec",
            "--cd",
            str(REPO),
            "--dangerously-bypass-approvals-and-sandbox",
            ]
    return None


def _record_real_spawn(
    *,
    trigger_msg_id: str,
    topic_id: str,
    target: dict[str, Any],
    pid: int,
    log_path: Path,
) -> dict[str, Any] | None:
    sys.path.insert(0, str(REPO))
    try:
        from db_utils import get_conn_immediate
        from debate import DEBATE_WAKE_SCHEMA_VERSION, json_dumps, new_msg_id, now_iso
    except Exception as exc:
        _log("real_spawn_audit_import_failed", msg_id=trigger_msg_id, error=repr(exc))
        return None

    action = os.environ.get("DEBATE_WAKE_SPAWN_ACTION_NAME", "external_agent_spawn")
    source_action = os.environ.get("DEBATE_WAKE_ACTION_NAME", "post_tool_use_wake")
    target_session_id = str(target.get("target_session_id") or "")
    source_target_session_id = str(target.get("source_target_session_id") or target_session_id)
    target_role = str(target.get("target_role") or "")
    target_runtime = str(target.get("target_runtime") or "")
    launched_at = now_iso()

    try:
        with get_conn_immediate() as conn:
            source = conn.execute(
                "SELECT wake_id FROM debate_wake_log "
                "WHERE trigger_msg_id = ? AND target_session_id = ? "
                "AND action = ? ORDER BY created_at DESC LIMIT 1",
                (trigger_msg_id, source_target_session_id, source_action),
            ).fetchone()
            wake_id = new_msg_id()
            while conn.execute(
                "SELECT 1 FROM debate_wake_log WHERE wake_id = ? LIMIT 1",
                (wake_id,),
            ).fetchone():
                wake_id = new_msg_id()
            details = {
                "source_wake_id": source["wake_id"] if source else None,
                "pid": pid,
                "launcher_pid": os.getpid(),
                "log": str(log_path),
                "launched_at": launched_at,
                "source_action": source_action,
                "source_target_session_id": source_target_session_id,
                "parent_session_id": target.get("parent_session_id"),
                "worker_claim": target.get("worker_claim"),
            }
            conn.execute(
                "INSERT OR IGNORE INTO debate_wake_log "
                "(wake_id, trigger_msg_id, topic_id, recipient, target_role, "
                " target_session_id, target_runtime, binding_generation, action, "
                " result, schema_version, details_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    wake_id,
                    trigger_msg_id,
                    topic_id,
                    str(target.get("recipient") or ""),
                    target_role,
                    target_session_id,
                    target_runtime,
                    None,
                    action,
                    "real_spawn",
                    DEBATE_WAKE_SCHEMA_VERSION,
                    json_dumps(details),
                    launched_at,
                ),
            )
        return {
            "wake_id": wake_id,
            "result": "real_spawn",
            "pid": pid,
            "log": str(log_path),
            "launched_at": launched_at,
            "source_wake_id": details["source_wake_id"],
        }
    except Exception as exc:
        _log("real_spawn_audit_failed", msg_id=trigger_msg_id, error=repr(exc))
        return None


def _claim_worker_target(
    target: dict[str, Any], trigger_msg_id: str, topic_id: str
) -> dict[str, Any]:
    """Convert a role wake target into an idempotent derived worker target."""
    if target.get("recipient") != target.get("target_role"):
        return target
    parent_session_id = str(target.get("target_session_id") or "")
    role = str(target.get("target_role") or "")
    if not parent_session_id or not role:
        return target

    sys.path.insert(0, str(REPO))
    try:
        from db_utils import get_conn_immediate
        from debate import claim_worker_session
    except Exception as exc:
        _log("worker_claim_import_failed", msg_id=trigger_msg_id, error=repr(exc))
        return {**target, "claim_error": "import_failed"}

    try:
        with get_conn_immediate() as conn:
            claim = claim_worker_session(
                conn,
                topic_id=topic_id,
                role=role,
                parent_session_id=parent_session_id,
                trigger_msg_id=trigger_msg_id,
                details={"source": "debate_wake_hook"},
            )
    except Exception as exc:
        _log("worker_claim_failed", msg_id=trigger_msg_id, error=repr(exc), target=target)
        return {**target, "claim_error": repr(exc)}

    out = dict(target)
    out["source_target_session_id"] = parent_session_id
    out["parent_session_id"] = parent_session_id
    out["target_session_id"] = claim["worker_session_id"]
    out["worker_claim"] = claim
    if claim.get("no_action"):
        out["result"] = "worker_claim_completed"
    return out


def _launch_agent(target: dict[str, Any], trigger_msg_id: str, topic_id: str) -> dict[str, Any]:
    target = _claim_worker_target(target, trigger_msg_id, topic_id)
    if target.get("result") == "worker_claim_completed":
        return {"launched": False, "reason": "worker_claim_completed", "target": target}
    if target.get("claim_error"):
        return {"launched": False, "reason": "worker_claim_failed", "target": target}

    cmd = _agent_command(target, trigger_msg_id, topic_id)
    if cmd is None:
        return {"launched": False, "reason": "unsupported_runtime"}

    AGENT_LOG_DIR.mkdir(parents=True, exist_ok=True)
    role = str(target.get("target_role") or "role").lower()
    session_id = str(target.get("target_session_id") or "session")
    log_path = AGENT_LOG_DIR / f"{_now().replace(':', '').replace('.', '-')}-{role}-{trigger_msg_id}.log"
    env = os.environ.copy()
    env.update(
        {
            "DEBATE_ROLE": str(target.get("target_role") or ""),
            "DEBATE_SESSION_ID": session_id,
            "DEBATE_TOPICS": topic_id,
            "DEBATE_WAKE_PARENT_MSG_ID": trigger_msg_id,
            "DEBATE_WAKE_REMAINING": str(
                max(
                    0,
                    int(
                        os.environ.get(
                            "DEBATE_WAKE_REMAINING",
                            os.environ.get("DEBATE_WAKE_BUDGET", "1"),
                        )
                    )
                    - 1,
                )
            ),
        }
    )
    with log_path.open("ab") as log:
        proc = subprocess.Popen(
            cmd,
            cwd=str(REPO),
            env=env,
            stdin=subprocess.PIPE,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        if proc.stdin is not None:
            proc.stdin.write(_wake_prompt(target, trigger_msg_id, topic_id).encode("utf-8"))
            proc.stdin.close()
    audit = _record_real_spawn(
        trigger_msg_id=trigger_msg_id,
        topic_id=topic_id,
        target=target,
        pid=proc.pid,
        log_path=log_path,
    )
    return {"launched": True, "pid": proc.pid, "log": str(log_path), "audit": audit}


def _handle_tool_response(tool_response: dict[str, Any]) -> dict[str, Any] | None:
    sys.path.insert(0, str(REPO))
    from db_utils import get_conn_immediate
    from debate import prepare_wake_dry_run

    with get_conn_immediate() as conn:
        return prepare_wake_dry_run(
            conn,
            tool_response=tool_response,
            action=os.environ.get("DEBATE_WAKE_ACTION_NAME", "post_tool_use_wake"),
        )


def _maybe_dispatch(tool_response: dict[str, Any], out: dict[str, Any]) -> None:
    targets = out.get("targets", []) if isinstance(out, dict) else []
    mode = os.environ.get("DEBATE_WAKE_ACTION", "dry_run")
    _log(
        "wake_resolved",
        msg_id=tool_response.get("msg_id"),
        targets=targets,
        suppressed=out.get("suppressed") if isinstance(out, dict) else None,
        mode=mode,
    )

    if mode == "notify":
        for target in targets:
            if target.get("result") != "suppressed":
                _notify(target, str(tool_response.get("msg_id") or ""))
        return

    if mode != "agent":
        return

    budget = int(
        os.environ.get("DEBATE_WAKE_REMAINING", os.environ.get("DEBATE_WAKE_BUDGET", "1"))
    )
    if budget <= 0:
        _log("agent_budget_exhausted", msg_id=tool_response.get("msg_id"))
        return

    topic_id = str(tool_response.get("topic_id") or "")
    if not topic_id:
        # prepare_wake_dry_run resolves by msg_id, but launcher prompt needs
        # the topic. Query cheaply only for agent mode.
        import sqlite3

        db_path = os.environ.get(
            "SQLITE_MEMORY_DB", os.path.expanduser("~/.claude/memory/memory.db")
        )
        con = sqlite3.connect(db_path)
        try:
            row = con.execute(
                "SELECT topic_id FROM debate_messages WHERE msg_id = ?",
                (str(tool_response.get("msg_id") or ""),),
            ).fetchone()
            topic_id = row[0] if row else ""
        finally:
            con.close()
    if not topic_id:
        _log("agent_launch_skipped", reason="unknown_topic", msg_id=tool_response.get("msg_id"))
        return

    launches = []
    for target in targets:
        if target.get("result") == "suppressed":
            continue
        launches.append(
            {
                "target": target,
                "launch": _launch_agent(
                    target, str(tool_response.get("msg_id") or ""), topic_id
                ),
            }
        )
    _log("agent_launches", msg_id=tool_response.get("msg_id"), launches=launches)


def _run_hook() -> int:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except Exception as exc:
        _log("invalid_hook_payload", error=str(exc))
        return 0

    tool_name = str(payload.get("tool_name") or payload.get("toolName") or "")
    if tool_name and tool_name != TARGET_TOOL:
        return 0

    tool_response = _extract_tool_response(payload)
    if tool_response is None:
        _log("missing_tool_response", tool_name=tool_name, keys=sorted(payload.keys()))
        return 0

    sys.path.insert(0, str(REPO))
    try:
        out = _handle_tool_response(tool_response)
    except Exception as exc:
        _log(
            "wake_resolution_failed",
            error=repr(exc),
            msg_id=tool_response.get("msg_id"),
            schema_version=tool_response.get("schema_version"),
        )
        return 0

    if isinstance(out, dict):
        _maybe_dispatch(tool_response, out)
    return 0


def _scan_since(since_ts: str) -> int:
    sys.path.insert(0, str(REPO))
    from db_utils import get_conn

    with get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT m.msg_id, m.topic_id FROM debate_messages m "
            "JOIN debate_message_recipients r ON r.msg_id = m.msg_id "
            "WHERE m.ts > ? ORDER BY m.ts ASC, m.msg_id ASC",
            (since_ts,),
        ).fetchall()

    count = 0
    for row in rows:
        tool_response = {
            "msg_id": row["msg_id"],
            "topic_id": row["topic_id"],
            "schema_version": POST_SCHEMA_VERSION,
        }
        try:
            out = _handle_tool_response(tool_response)
            if isinstance(out, dict):
                _maybe_dispatch(tool_response, out)
            count += 1
        except Exception as exc:
            _log("scan_wake_failed", msg_id=row["msg_id"], error=repr(exc))
    _log("scan_complete", since_ts=since_ts, count=count)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--scan-since")
    args, _unknown = parser.parse_known_args()
    if args.scan_since:
        return _scan_since(args.scan_since)
    return _run_hook()


if __name__ == "__main__":
    raise SystemExit(main())
