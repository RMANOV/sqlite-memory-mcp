# X Article — Optimized from Medium/dev.to

**Title:** I Replaced Claude's Memory Server With SQLite. Here's What Changed.

**Subtitle:** 21 MCP tools, native desktop app, cross-machine sync. Zero Docker, zero cloud, zero corruption.

---

**COPY BELOW THIS LINE**

---

The official Claude MCP memory server stores your knowledge graph in a JSONL file.

Open two Claude Code windows. Write to it simultaneously. Congratulations — your AI's memory is now corrupted.

This isn't theoretical. It happened to me enough times that I built a proper replacement.

**GitHub:** github.com/RMANOV/sqlite-memory-mcp


## The Fix: Three Lines of SQL

```sql
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
PRAGMA busy_timeout=10000;
```

That's it. WAL mode: readers never block writers. Writers never block readers. 10+ concurrent Claude Code sessions. Same `.db` file. ACID transactions. No corruption. Ever.

`sqlite3` is Python stdlib. No binaries. No Docker. No daemon.


## What You Get: 21 MCP Tools

**Drop-in compatible (1-9).** All 9 tools from the official memory server — same names, same argument shapes. Your existing prompts keep working. Swap the config, done.

**Session tracking (10-12).** `session_recall(last_n=3)` at session start returns what you were working on, which files were active, your summary from last time. Context continuity without reading conversation history.

**Task management (13-18).** Full GTD workflow. Five sections (inbox → today → next → someday → waiting), four priority levels, subtasks, recurring tasks. All in the same SQLite file that holds your knowledge graph.

**Bridge sync (19-21).** Work laptop in the morning, desktop at night. `bridge_push` / `bridge_pull` sync your knowledge graph AND tasks between machines via a private git repo. No cloud. No API keys. Just git.


## New in v0.4.0: Native Desktop App

`task_tray.py` — a PyQt6 system tray app that reads/writes directly to `memory.db`.

What it does:
- Overdue badge on the tray icon (red circle, count)
- Left-click: compact popup with Today + Overdue tasks, checkbox toggle, quick-add
- Right-click: full tabbed window — Today / Inbox / Next / All
- Auto-refreshes every 30 seconds

The killer feature: changes sync instantly between the tray app and your Claude Code sessions. Same database, WAL concurrency. Edit a task in the GUI, Claude sees it. Claude creates a task, the tray shows it.


## BM25 Search vs. Substring Matching

The official server does substring matching on JSONL. "Find me something about WAL" scans every line looking for a substring.

This server maintains an FTS5 virtual table synced on every write. `search_nodes("WAL mode")` returns the most relevant entities first — ranked by BM25, not insertion order.

FTS5 gotcha that took me an hour: it has no `ON CONFLICT` clause. If you INSERT without deleting first, you get duplicate rows in the index that accumulate silently. The fix is explicit DELETE + INSERT on every sync.


## The Shared Module Pattern

v0.4.0 consolidates all duplicated code into `db_utils.py`:

```python
from db_utils import (
    get_conn, now_iso, is_overdue,
    TASK_SECTIONS, TASK_PRIORITIES,
    build_priority_order_sql,
)
```

Before: 7 files each defining their own DB connection, constants, timestamp helpers. The Task Tray had priorities in descending order. The server had them ascending. The overdue check in the tray used string comparison (`"2026-03-01" < "2026-03-05"`) — fragile and wrong for edge cases.

After: one module, one truth. Net result: 93 lines of duplication eliminated.


## Five Automation Scripts

| Script | What it does |
|--------|-------------|
| `auto_archive.py` | Archive done tasks older than N days |
| `daily_digest.py` | Markdown task briefing to stdout |
| `overdue_bump.py` | Escalate priority of overdue tasks |
| `recurring_tasks.py` | Recreate tasks on schedule (daily/weekly/monthly) |
| `task_report.py` | Static HTML Kanban board for GitHub Pages |

All support `--dry-run`. All use `db_utils`. Zero external dependencies.


## Kanban Board on GitHub Pages

`task_report.py` generates a self-contained HTML file:
- One column per GTD section
- Priority color-coding (grey → blue → orange → red)
- Overdue highlighting
- Subtask count badges

Push it to a gh-pages branch. Your AI agent's task list, visualized and shareable. No server required.


## The Stack

```
8 Python files
1 shared module (db_utils.py)
1 required dependency (fastmcp)
1 optional dependency (PyQt6, for the tray app)
0 Docker containers
0 API keys
0 daemon processes
```

Backup: `cp memory.db memory.db.bak`

Auto-migrates your existing `memory.json` on first run. The 9 original tools are fully compatible — the 12 additional tools are opt-in.


## 15 Bug Fixes in This Release

The interesting ones:

- `bridge_push()` checked `stderr` for "nothing to commit" — but git writes that to `stdout`. The early-return path was dead code. Every no-op commit triggered an unnecessary `git push`.

- The Task Tray's `_refresh_all()` called `get_summary()` twice per update — once for the icon badge, once for the tooltip. Same DB query, same result, double the work.

- `TASK_ALLOWED_UPDATE_FIELDS` was missing `parent_id`. You could create subtasks, but reassigning them to a different parent via the tray app would raise `ValueError`.


## What's Next

- **Rust/PyO3** hot path for FTS sync + BM25 query
- **`sqlite-vec`** embedding search to complement BM25 keyword matching
- **Hook integration** for auto-saving sessions and auto-tagging tasks

Python 3.10+. MIT license. v0.4.0 out now.

github.com/RMANOV/sqlite-memory-mcp

---

**Post text (for sharing the article):**

The official Claude MCP memory server corrupts data with 2+ concurrent sessions.

I replaced it with SQLite WAL mode. Then added FTS5 search, a GTD task manager, a native desktop tray app, and cross-machine sync.

21 MCP tools. Native GUI. 5 automation scripts. Zero Docker.

v0.4.0: github.com/RMANOV/sqlite-memory-mcp

#MCP #Claude #SQLite #Python #AI #OpenSource #ModelContextProtocol #GTD
