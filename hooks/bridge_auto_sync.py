#!/usr/bin/env python3
"""PostToolUse hook: Auto bridge sync on memory changes.

Detects MCP memory write operations and triggers bridge_push
in a background subprocess with 10-minute debounce.
Also checks for notifications from previous sync attempts.
"""

import json
import os
import subprocess
import sys
import time
from typing import TextIO

DIRTY_FLAG = os.path.expanduser("~/.claude/memory/.bridge_dirty")
LAST_SYNC = os.path.expanduser("~/.claude/memory/.bridge_last_sync")
NOTIFY_FILE = os.path.expanduser("~/.claude/memory/.bridge_notification")
DEBOUNCE_SECONDS = 600
WORKER_SCRIPT = os.path.expanduser(
    "~/.claude/mcp_servers/sqlite_memory/hooks/bridge_sync_worker.py"
)
WORKER_FALLBACK_SCRIPT = os.path.expanduser("~/.claude/hooks/bridge_sync_worker.py")

WRITE_TOOLS = {
    "mcp__sqlite_memory__create_entities",
    "mcp__sqlite_memory__add_observations",
    "mcp__sqlite_memory__delete_entities",
    "mcp__sqlite_memory__delete_observations",
    "mcp__sqlite_memory__create_relations",
    "mcp__sqlite_memory__delete_relations",
    "mcp__sqlite_tasks__create_task_or_note",
    "mcp__sqlite_tasks__update_task",
    "mcp__sqlite_tasks__archive_done_tasks",
    "mcp__sqlite_tasks__bump_overdue_priority",
    "mcp__sqlite_bridge__assign_task",
    "mcp__sqlite_bridge__review_shared_tasks",
    "mcp__sqlite_bridge__process_recurring_tasks",
    "mcp__sqlite_collab__manage_collaborators",
    "mcp__sqlite_collab__share_knowledge",
    "mcp__sqlite_collab__review_shared_knowledge",
    "mcp__sqlite_collab__request_publish",
    "mcp__sqlite_collab__cancel_publish",
    "mcp__sqlite_collab__rate_public_knowledge",
    "mcp__sqlite_collab__update_verification",
    "mcp__sqlite_entity__merge_entities",
    # Unified server names after sqlite_unified migration
    "mcp__sqlite_unified__create_entities",
    "mcp__sqlite_unified__add_observations",
    "mcp__sqlite_unified__delete_entities",
    "mcp__sqlite_unified__delete_observations",
    "mcp__sqlite_unified__create_relations",
    "mcp__sqlite_unified__delete_relations",
    "mcp__sqlite_unified__create_task_or_note",
    "mcp__sqlite_unified__update_task",
    "mcp__sqlite_unified__archive_done_tasks",
    "mcp__sqlite_unified__bump_overdue_priority",
    "mcp__sqlite_unified__assign_task",
    "mcp__sqlite_unified__review_shared_tasks",
    "mcp__sqlite_unified__process_recurring_tasks",
    "mcp__sqlite_unified__manage_collaborators",
    "mcp__sqlite_unified__share_knowledge",
    "mcp__sqlite_unified__review_shared_knowledge",
    "mcp__sqlite_unified__request_publish",
    "mcp__sqlite_unified__cancel_publish",
    "mcp__sqlite_unified__rate_public_knowledge",
    "mcp__sqlite_unified__update_verification",
    "mcp__sqlite_unified__merge_entities",
}

LEVEL_PREFIX = {
    "info": "BRIDGE SYNC",
    "warning": "BRIDGE WARNING",
    "error": "BRIDGE ERROR",
}


def _load_event(stream: TextIO) -> dict:
    try:
        data = json.load(stream)
    except (json.JSONDecodeError, OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _load_notification() -> str | None:
    if not os.path.exists(NOTIFY_FILE):
        return None
    try:
        with open(NOTIFY_FILE, encoding="utf-8") as f:
            note = json.load(f)
        level = note.get("level", "info")
        msg = note.get("message", "")
        ts = note.get("timestamp", "")
        if level in ("warning", "error"):
            return f"{LEVEL_PREFIX.get(level, 'BRIDGE')}: {msg} ({ts})"
        return None
    except (json.JSONDecodeError, OSError, ValueError):
        return None
    finally:
        try:
            os.unlink(NOTIFY_FILE)
        except OSError:
            pass


def _emit_context(stream: TextIO, message: str) -> None:
    result = {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": message,
        }
    }
    print(json.dumps(result, ensure_ascii=False), file=stream)


def _mark_dirty(now: float) -> None:
    try:
        with open(DIRTY_FLAG, "w", encoding="utf-8") as f:
            f.write(str(now))
    except OSError:
        pass


def _should_sync(now: float) -> bool:
    try:
        if os.path.exists(LAST_SYNC):
            with open(LAST_SYNC, encoding="utf-8") as f:
                last = float(f.read().strip())
            if now - last < DEBOUNCE_SECONDS:
                return False
    except (OSError, ValueError):
        pass
    return True


def _resolve_worker_script() -> str | None:
    candidates = []
    for path in (WORKER_SCRIPT, WORKER_FALLBACK_SCRIPT):
        if path and path not in candidates:
            candidates.append(path)
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


def _launch_worker() -> str | None:
    worker_path = _resolve_worker_script()
    if worker_path is None:
        return (
            "BRIDGE WARNING: auto-sync worker missing: "
            f"{WORKER_SCRIPT} (fallback: {WORKER_FALLBACK_SCRIPT})"
        )
    try:
        subprocess.Popen(
            [sys.executable, worker_path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:
        return f"BRIDGE WARNING: failed to start auto-sync worker: {exc}"
    return None


def main(stdin: TextIO | None = None, stdout: TextIO | None = None) -> int:
    in_stream = stdin or sys.stdin
    out_stream = stdout or sys.stdout
    data = _load_event(in_stream)
    tool_name = data.get("tool_name", "")
    notification_msg = _load_notification()

    if tool_name not in WRITE_TOOLS:
        if notification_msg:
            _emit_context(out_stream, notification_msg)
        return 0

    now = time.time()
    _mark_dirty(now)

    extra_messages: list[str] = []
    if notification_msg:
        extra_messages.append(notification_msg)
    if _should_sync(now):
        launch_msg = _launch_worker()
        if launch_msg:
            extra_messages.append(launch_msg)

    if extra_messages:
        _emit_context(out_stream, "\n".join(extra_messages))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
