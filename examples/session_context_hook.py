#!/usr/bin/env python3
"""
Example: session_context.py hook extension for SQLite Memory MCP Server.

Add this block to your existing ~/.claude/hooks/session_context.py
BEFORE the final print(json.dumps(...)) line.

This injects the last 2 session summaries into the SessionStart context,
giving Claude automatic awareness of recent work across sessions.
"""
import os
import sqlite3


def get_recent_sessions(db_path: str, limit: int = 2) -> list[str]:
    """Retrieve recent session summaries from SQLite memory."""
    if not os.path.exists(db_path):
        return []

    lines = []
    try:
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        cur = conn.execute(
            "SELECT summary, project, started_at FROM sessions "
            "ORDER BY started_at DESC LIMIT ?",
            (limit,),
        )
        sessions = cur.fetchall()
        conn.close()

        if sessions:
            lines.append("LAST SESSIONS:")
            for summary, project, started in sessions:
                proj = f" [{project}]" if project else ""
                lines.append(f"  - {started}{proj}: {summary or 'no summary'}")
    except Exception:
        pass  # Silent fail — don't break session start

    return lines


# --- Integration snippet (add to your session_context.py) ---
#
# # Session recall from SQLite memory
# try:
#     db_path = os.path.expanduser('~/.claude/memory/memory.db')
#     session_lines = get_recent_sessions(db_path, limit=2)
#     lines.extend(session_lines)
# except Exception:
#     pass
