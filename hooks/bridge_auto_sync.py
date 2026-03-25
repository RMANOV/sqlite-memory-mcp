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

DIRTY_FLAG = os.path.expanduser("~/.claude/memory/.bridge_dirty")
LAST_SYNC = os.path.expanduser("~/.claude/memory/.bridge_last_sync")
NOTIFY_FILE = os.path.expanduser("~/.claude/memory/.bridge_notification")
DEBOUNCE_SECONDS = 600  # 10 minutes
WORKER_SCRIPT = os.path.expanduser("~/.claude/hooks/bridge_sync_worker.py")

WRITE_TOOLS = {
    # Core (sqlite_memory)
    "mcp__sqlite_memory__create_entities",
    "mcp__sqlite_memory__add_observations",
    "mcp__sqlite_memory__delete_entities",
    "mcp__sqlite_memory__delete_observations",
    "mcp__sqlite_memory__create_relations",
    "mcp__sqlite_memory__delete_relations",
    # Tasks (sqlite_tasks)
    "mcp__sqlite_tasks__create_task_or_note",
    "mcp__sqlite_tasks__update_task",
    "mcp__sqlite_tasks__archive_done_tasks",
    "mcp__sqlite_tasks__bump_overdue_priority",
    # Bridge (sqlite_bridge)
    "mcp__sqlite_bridge__bridge_push",
    "mcp__sqlite_bridge__assign_task",
    "mcp__sqlite_bridge__review_shared_tasks",
    "mcp__sqlite_bridge__process_recurring_tasks",
    # Collab (sqlite_collab)
    "mcp__sqlite_collab__manage_collaborators",
    "mcp__sqlite_collab__share_knowledge",
    "mcp__sqlite_collab__review_shared_knowledge",
    # Entity (sqlite_entity)
    "mcp__sqlite_entity__merge_entities",
}

LEVEL_PREFIX = {
    "info": "BRIDGE SYNC",
    "warning": "BRIDGE WARNING",
    "error": "BRIDGE ERROR",
}

data = json.load(sys.stdin)
tool_name = data.get("tool_name", "")

# --- Check for pending notifications (any tool call) ---
notification_msg = None
if os.path.exists(NOTIFY_FILE):
    try:
        with open(NOTIFY_FILE) as f:
            note = json.load(f)
        level = note.get("level", "info")
        msg = note.get("message", "")
        ts = note.get("timestamp", "")
        # Only surface warnings and errors; info is silent
        if level in ("warning", "error"):
            notification_msg = f"{LEVEL_PREFIX.get(level, 'BRIDGE')}: {msg} ({ts})"
        os.unlink(NOTIFY_FILE)
    except (json.JSONDecodeError, OSError):
        try:
            os.unlink(NOTIFY_FILE)
        except OSError:
            pass

# If this isn't a write tool, just deliver notification if any
if tool_name not in WRITE_TOOLS:
    if notification_msg:
        result = {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": notification_msg,
            }
        }
        print(json.dumps(result, ensure_ascii=False))
    sys.exit(0)

# --- Write tool detected: mark dirty + maybe sync ---

try:
    with open(DIRTY_FLAG, "w") as f:
        f.write(str(time.time()))
except OSError:
    pass

# Debounce check
should_sync = True
try:
    if os.path.exists(LAST_SYNC):
        with open(LAST_SYNC) as f:
            last = float(f.read().strip())
        if time.time() - last < DEBOUNCE_SECONDS:
            should_sync = False
except (OSError, ValueError):
    pass

if should_sync:
    try:
        subprocess.Popen(
            [sys.executable, WORKER_SCRIPT],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        pass

# Deliver notification if any
if notification_msg:
    result = {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": notification_msg,
        }
    }
    print(json.dumps(result, ensure_ascii=False))

sys.exit(0)
