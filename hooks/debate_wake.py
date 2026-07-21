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
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


HOOK_DIR = Path(__file__).resolve().parent


def _default_repo() -> Path:
    candidate = HOOK_DIR.parent
    if (candidate / "debate.py").is_file():
        return candidate
    return Path("/home/rmanov/sqlite-memory-mcp")


REPO = Path(os.environ.get("DEBATE_REPO", str(_default_repo())))
IS_WINDOWS = sys.platform == "win32"
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
LOG_MAX_BYTES = int(os.environ.get("DEBATE_WAKE_LOG_MAX_BYTES", str(20 * 1024 * 1024)))
LOG_KEEP = max(1, int(os.environ.get("DEBATE_WAKE_LOG_KEEP", "3")))


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
    try:
        subprocess.run(
            ["notify-send", title, body],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2,
            check=False,
        )
    except Exception:
        pass


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
2. The launcher has already written exactly one human-readable RECEIVED line
   to stdout/log for this wake. Do not print a second RECEIVED line.
3. If there is no substantive work, call debate_worker_no_action with this
   topic_id, role, worker_session_id (the session_id shown above),
   trigger_msg_id, and a short reason. Then reply NO_ACTION and do not post.
4. If there is work, post at most one focused debate response with
   debate_post_with_recipients. Do not use bare debate_post: unaddressed
   messages are invisible to the pump.
5. Address the response to the role(s) named by the trigger, normally
   CONDUCTOR and any review role that must see it.
6. Quote the trigger msg_id or the specific msg_id(s) you read.
7. After a successful post, advance this role/session/topic cursor to the latest
   message you substantively handled, normally the trigger msg_id. After
   debate_worker_no_action, do not call debate_signal_advance separately.
8. Do not edit files, run unrelated commands, or broaden scope.
9. Stop after that one response and cursor advance.
"""


def _brief_text(text: str, limit: int = 96) -> str:
    normalized = str(text or "").replace("\n", " ").replace("\t", " ")
    words = " ".join(normalized.split())
    if len(words) <= limit:
        return words
    return words[: max(0, limit - 3)].rstrip() + "..."


def _default_write_to(role: str) -> str:
    if role == "EXECUTOR":
        return "CONDUCTOR,ADVOCATE"
    if role == "ADVOCATE":
        return "CONDUCTOR,EXECUTOR"
    if role == "CONDUCTOR":
        return "EXECUTOR,ADVOCATE"
    return "CONDUCTOR"


def _receipt_event(
    target: dict[str, Any], trigger_msg_id: str, topic_id: str
) -> dict[str, Any]:
    role = str(target.get("target_role") or "")
    sender = "UNKNOWN"
    what = "process addressed debate message"
    try:
        import sqlite3

        db_path = os.environ.get(
            "SQLITE_MEMORY_DB", os.path.expanduser("~/.claude/memory/memory.db")
        )
        con = sqlite3.connect(db_path)
        try:
            row = con.execute(
                "SELECT role, body FROM debate_messages WHERE msg_id = ?",
                (trigger_msg_id,),
            ).fetchone()
            if row:
                sender = str(row[0] or "UNKNOWN")
                what = _brief_text(str(row[1] or what))
        finally:
            con.close()
    except Exception as exc:
        _log("receipt_lookup_failed", msg_id=trigger_msg_id, error=repr(exc))

    return {
        "event": "agent_wake_receipt",
        "from_role": sender,
        "from_msg_id": trigger_msg_id,
        "topic_id": topic_id,
        "target_role": role,
        "target_session_id": str(target.get("target_session_id") or ""),
        "target_runtime": str(target.get("target_runtime") or ""),
        "what": what,
        "will": "check_inbox_and_post_or_no_action",
        "write_to": _default_write_to(role),
    }


def _receipt_line(event: dict[str, Any]) -> str:
    return (
        f"RECEIVED from={event['from_role']}/{event['from_msg_id']} "
        f"to={event['target_role']}/{event['target_session_id']} "
        f"topic={event['topic_id']} what={event['what']} "
        f"will={event['will']} write_to={event['write_to']}"
    )


def _record_receipt_event(event: dict[str, Any]) -> None:
    try:
        sys.path.insert(0, str(HOOK_DIR))
        from debate_agent_events import record_receipt

        record_receipt(event)
    except Exception as exc:
        _log(
            "agent_receipt_event_failed",
            msg_id=event.get("from_msg_id"),
            target_session_id=event.get("target_session_id"),
            error=repr(exc),
        )


def _agent_command(
    target: dict[str, Any], trigger_msg_id: str, topic_id: str
) -> list[str] | None:
    runtime = str(target.get("target_runtime") or "")
    if runtime in {"cc", "claude"}:
        # The MCP server name differs per deployment (sqlite_intel on the
        # Fedora stack, sqlite_unified on Windows); the tool suffixes do not.
        prefix = os.environ.get("DEBATE_WAKE_MCP_PREFIX", "mcp__sqlite_intel__")
        allowed = ",".join(
            prefix + suffix
            for suffix in (
                "debate_signal_check",
                "debate_post_with_recipients",
                "debate_signal_advance",
                "debate_binding_list",
                "debate_worker_claim",
                "debate_worker_no_action",
            )
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
            "--ephemeral",
            "--config",
            'model_reasoning_effort="low"',
        ]
    return None


def _record_real_spawn(
    *,
    trigger_msg_id: str,
    topic_id: str,
    target: dict[str, Any],
    pid: int,
    log_path: Path,
    create_time: float | None = None,
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
    source_target_session_id = str(
        target.get("source_target_session_id") or target_session_id
    )
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
                "create_time": create_time,
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


def _mark_source_wake_result(
    target: dict[str, Any],
    trigger_msg_id: str,
    result: str,
) -> None:
    sys.path.insert(0, str(REPO))
    try:
        from db_utils import get_conn_immediate
    except Exception as exc:
        _log("source_wake_mark_import_failed", msg_id=trigger_msg_id, error=repr(exc))
        return

    action = os.environ.get("DEBATE_WAKE_ACTION_NAME", "post_tool_use_wake")
    target_session_id = str(
        target.get("source_target_session_id") or target.get("target_session_id") or ""
    )
    if not target_session_id:
        return
    try:
        with get_conn_immediate() as conn:
            conn.execute(
                "UPDATE debate_wake_log SET result = ? "
                "WHERE wake_id = ("
                " SELECT wake_id FROM debate_wake_log "
                " WHERE trigger_msg_id = ? AND target_session_id = ? "
                " AND action = ? AND result = 'dry_run' "
                " ORDER BY created_at DESC LIMIT 1"
                ")",
                (result, trigger_msg_id, target_session_id, action),
            )
    except Exception as exc:
        _log("source_wake_mark_failed", msg_id=trigger_msg_id, error=repr(exc))


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
        _log(
            "worker_claim_failed", msg_id=trigger_msg_id, error=repr(exc), target=target
        )
        return {**target, "claim_error": repr(exc)}

    out = dict(target)
    out["source_target_session_id"] = parent_session_id
    out["parent_session_id"] = parent_session_id
    out["target_session_id"] = claim["worker_session_id"]
    out["worker_claim"] = claim
    if claim.get("no_action"):
        out["result"] = "worker_claim_completed"
    return out


def _launch_agent(
    target: dict[str, Any], trigger_msg_id: str, topic_id: str
) -> dict[str, Any]:
    target = _claim_worker_target(target, trigger_msg_id, topic_id)
    if target.get("result") == "worker_claim_completed":
        return {"launched": False, "reason": "worker_claim_completed", "target": target}
    if target.get("claim_error"):
        return {"launched": False, "reason": "worker_claim_failed", "target": target}

    cmd = _agent_command(target, trigger_msg_id, topic_id)
    if cmd is None:
        return {"launched": False, "reason": "unsupported_runtime"}

    # Explicit executable resolution: bare names depend on the caller's PATH
    # semantics (and on Windows would silently miss .cmd shims); a missing
    # runtime must be a typed refusal, not a spawn exception.
    resolved = shutil.which(cmd[0])
    if not resolved:
        _log("agent_executable_not_found", executable=cmd[0], msg_id=trigger_msg_id)
        return {
            "launched": False,
            "reason": "executable_not_found",
            "executable": cmd[0],
        }
    cmd = [resolved, *cmd[1:]]

    AGENT_LOG_DIR.mkdir(parents=True, exist_ok=True)
    _prune_agent_logs()
    role = str(target.get("target_role") or "role").lower()
    session_id = str(target.get("target_session_id") or "session")
    log_path = (
        AGENT_LOG_DIR
        / f"{_now().replace(':', '').replace('.', '-')}-{role}-{trigger_msg_id}.log"
    )
    env = os.environ.copy()
    env.update(
        {
            "DEBATE_ROLE": str(target.get("target_role") or ""),
            "DEBATE_SESSION_ID": session_id,
            "DEBATE_TOPICS": topic_id,
            "DEBATE_WAKE_PARENT_MSG_ID": trigger_msg_id,
            # Wake workers fetch their authoritative inbox with MCP tools.
            # Bypass the legacy global-subscription prompt wrapper so a
            # worker cannot inherit unrelated role bodies or stale backlog.
            "CODEX_DEBATE_WRAPPER_BYPASS": "1",
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
        receipt_event = _receipt_event(target, trigger_msg_id, topic_id)
        receipt_line = _receipt_line(receipt_event)
        receipt_event["receipt_line"] = receipt_line
        log.write((receipt_line + "\n").encode("utf-8"))
        log.flush()
        _record_receipt_event(receipt_event)
        popen_kwargs: dict[str, Any] = {
            "cwd": str(REPO),
            "env": env,
            "stdin": subprocess.PIPE,
            "stdout": log,
            "stderr": subprocess.STDOUT,
        }
        if IS_WINDOWS:
            # Hidden bounded worker: no console window; own process group so
            # stopping the pump never takes in-flight workers down with it.
            popen_kwargs["creationflags"] = (
                subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
            )
        else:
            popen_kwargs["start_new_session"] = True
        proc = subprocess.Popen(cmd, **popen_kwargs)
        if proc.stdin is not None:
            proc.stdin.write(
                _wake_prompt(target, trigger_msg_id, topic_id).encode("utf-8")
            )
            proc.stdin.close()
    create_time: float | None = None
    try:
        import psutil

        create_time = float(psutil.Process(proc.pid).create_time())
    except Exception:
        create_time = None  # identity check degrades to pid-only for this spawn
    audit = _record_real_spawn(
        trigger_msg_id=trigger_msg_id,
        topic_id=topic_id,
        target=target,
        pid=proc.pid,
        log_path=log_path,
        create_time=create_time,
    )
    return {"launched": True, "pid": proc.pid, "log": str(log_path), "audit": audit}


def _prune_agent_logs() -> None:
    """Keep the newest N spawn logs so the log dir stays bounded."""
    keep = max(1, int(os.environ.get("DEBATE_WAKE_AGENT_LOG_KEEP", "50")))
    try:
        logs = sorted(
            AGENT_LOG_DIR.glob("*.log"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for stale in logs[keep:]:
            stale.unlink(missing_ok=True)
    except Exception as exc:
        _log("agent_log_prune_failed", error=repr(exc))


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


def _maybe_dispatch(
    tool_response: dict[str, Any], out: dict[str, Any]
) -> dict[str, Any]:
    targets = out.get("targets", []) if isinstance(out, dict) else []
    mode = os.environ.get("DEBATE_WAKE_ACTION", "dry_run")
    resource_budget = None
    if os.environ.get("DEBATE_RESOURCE_BUDGET", "auto") != "off":
        try:
            sys.path.insert(0, str(HOOK_DIR))
            from debate_resource_budget import current_debate_resource_budget

            resource_budget = current_debate_resource_budget()
            _log(
                "wake_resource_budget",
                msg_id=tool_response.get("msg_id"),
                **resource_budget.to_dict(),
            )
            if mode == "agent" and not resource_budget.allow_agent:
                mode = "dry_run"
        except Exception as exc:
            _log(
                "wake_resource_budget_failed",
                msg_id=tool_response.get("msg_id"),
                error=repr(exc),
            )
            if mode == "agent":
                mode = "dry_run"
    disable_file = Path(
        os.environ.get(
            "DEBATE_WAKE_DISABLE_FILE",
            os.path.expanduser("~/.claude/memory/debate_wake.disable"),
        )
    )
    if disable_file.exists():
        _log(
            "wake_agent_disabled_by_file",
            msg_id=tool_response.get("msg_id"),
            disable_file=str(disable_file),
            requested_mode=mode,
        )
        mode = "dry_run"
    _log(
        "wake_resolved",
        msg_id=tool_response.get("msg_id"),
        targets=targets,
        suppressed=out.get("suppressed") if isinstance(out, dict) else None,
        mode=mode,
    )

    # v3.13 impl-notify: implementation-tagged triggers resolve NOTIFY-ONLY
    # targets (never worker spawns — `targets` stays empty for them).  Send
    # the desktop signal and write the per-target audit row so hook+pump
    # rescans dedupe on result='impl_notified'.  Honors the kill-switch file;
    # a resource-budget downgrade does NOT suppress it (notify-send is free).
    notify_targets = out.get("notify_targets", []) if isinstance(out, dict) else []
    if (
        notify_targets
        and os.environ.get("DEBATE_WAKE_ACTION", "dry_run") in {"agent", "notify"}
        and not disable_file.exists()
    ):
        trigger_msg_id = str(tool_response.get("msg_id") or "")
        notified_targets = []
        for target in notify_targets:
            # KNOWN LIMITATION (ADVOCATE 3fd85c9584e3 minor 2): _notify is a
            # no-op without DISPLAY, yet the impl_notified row below still
            # writes, permanently deduping (trigger, session, action) — on a
            # headless host the desktop signal is lost. Acceptable while the
            # debate runs on a desktop; revisit before any headless deploy.
            _notify(target, trigger_msg_id)
            notified_targets.append(target)
            try:
                sys.path.insert(0, str(REPO))
                from db_utils import get_conn_immediate
                from debate import _insert_wake_log

                with get_conn_immediate() as conn:
                    _insert_wake_log(
                        conn,
                        trigger_msg_id=trigger_msg_id,
                        topic_id=str(tool_response.get("topic_id") or ""),
                        recipient=str(target.get("recipient") or ""),
                        action=os.environ.get(
                            "DEBATE_WAKE_ACTION_NAME", "post_tool_use_wake"
                        ),
                        result="impl_notified",
                        target_role=target.get("target_role"),
                        target_session_id=target.get("target_session_id"),
                        target_runtime=target.get("target_runtime"),
                        details={"channel": "notify-send"},
                    )
            except Exception as exc:  # noqa: BLE001 — audit must not kill the hook
                _log(
                    "impl_notify_audit_failed",
                    msg_id=trigger_msg_id,
                    error=repr(exc),
                )
        _log(
            "impl_notify_only",
            msg_id=trigger_msg_id,
            notified=len(notified_targets),
            targets=notified_targets,
        )

    if mode == "notify":
        notified = 0
        for target in targets:
            if target.get("result") != "suppressed":
                _notify(target, str(tool_response.get("msg_id") or ""))
                _mark_source_wake_result(
                    target, str(tool_response.get("msg_id") or ""), "notified"
                )
                notified += 1
        return {"mode": mode, "notified": notified, "launches": []}

    if mode != "agent":
        return {"mode": mode, "launches": []}

    budget = int(
        os.environ.get(
            "DEBATE_WAKE_REMAINING", os.environ.get("DEBATE_WAKE_BUDGET", "1")
        )
    )
    if resource_budget is not None:
        budget = min(budget, max(0, resource_budget.wake_budget))
    if budget <= 0:
        _log("agent_budget_exhausted", msg_id=tool_response.get("msg_id"))
        return {"mode": mode, "launches": [], "reason": "agent_budget_exhausted"}

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
        _log(
            "agent_launch_skipped",
            reason="unknown_topic",
            msg_id=tool_response.get("msg_id"),
        )
        return {"mode": mode, "launches": [], "reason": "unknown_topic"}

    launches = []
    remaining = budget
    for target in targets:
        if target.get("result") == "suppressed":
            continue
        if remaining <= 0:
            _log(
                "agent_budget_exhausted_mid_dispatch",
                msg_id=tool_response.get("msg_id"),
                skipped_target=target,
            )
            break
        launch = _launch_agent(target, str(tool_response.get("msg_id") or ""), topic_id)
        if launch.get("launched"):
            remaining -= 1
            _mark_source_wake_result(
                target, str(tool_response.get("msg_id") or ""), "dispatched"
            )
        elif launch.get("reason") == "worker_claim_completed":
            _mark_source_wake_result(
                target, str(tool_response.get("msg_id") or ""), "terminal_no_action"
            )
        launches.append({"target": target, "launch": launch})
    _log(
        "agent_launches",
        msg_id=tool_response.get("msg_id"),
        launches=launches,
        remaining_budget=remaining,
    )
    return {"mode": mode, "launches": launches}


def _agent_budget_remaining() -> int:
    try:
        return int(
            os.environ.get(
                "DEBATE_WAKE_REMAINING",
                os.environ.get("DEBATE_WAKE_BUDGET", "1"),
            )
        )
    except ValueError:
        return 0


def _agent_resolution_disabled(tool_response: dict[str, Any]) -> bool:
    mode = os.environ.get("DEBATE_WAKE_ACTION", "dry_run")
    if mode != "agent":
        return False
    disable_file = Path(
        os.environ.get(
            "DEBATE_WAKE_DISABLE_FILE",
            os.path.expanduser("~/.claude/memory/debate_wake.disable"),
        )
    )
    if disable_file.exists():
        _log(
            "agent_resolution_disabled_by_file",
            msg_id=tool_response.get("msg_id"),
            disable_file=str(disable_file),
        )
        return True
    if os.environ.get("DEBATE_RESOURCE_BUDGET", "auto") == "off":
        return False
    try:
        sys.path.insert(0, str(HOOK_DIR))
        from debate_resource_budget import current_debate_resource_budget

        budget = current_debate_resource_budget()
    except Exception as exc:
        _log(
            "agent_resolution_resource_budget_failed",
            msg_id=tool_response.get("msg_id"),
            error=repr(exc),
        )
        return True
    if not budget.allow_agent:
        _log(
            "agent_resolution_disabled_by_resource_budget",
            msg_id=tool_response.get("msg_id"),
            **budget.to_dict(),
        )
        return True
    return False


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

    if _agent_resolution_disabled(tool_response):
        return 0

    if (
        os.environ.get("DEBATE_WAKE_ACTION", "dry_run") == "agent"
        and _agent_budget_remaining() <= 0
    ):
        # Spawned agents intentionally run with zero remaining wake budget
        # to prevent recursive in-process fan-out. Do not resolve/write
        # wake_log rows here: the resident pump is the catch-up path and
        # must be able to claim the normal action without seeing a false
        # duplicate from the exhausted child hook.
        _log(
            "agent_budget_exhausted_pre_resolution", msg_id=tool_response.get("msg_id")
        )
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
        if _agent_resolution_disabled(tool_response):
            continue
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
