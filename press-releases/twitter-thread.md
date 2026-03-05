# X Article — Adapted from Medium

**Title:** SQLite Memory MCP Server v0.4.0 — WAL + FTS5 + GTD Task Management for Claude Code

**Subtitle:** Drop-in replacement for the official MCP memory with concurrent safety, ranked search, task management, and cross-machine sync

---

**COPY BELOW THIS LINE**

---

The official Claude MCP memory server stores everything in a JSONL file. Open two Claude Code windows and write to it simultaneously — congratulations, you have a race condition and a corrupted knowledge graph.

I replaced it with SQLite WAL mode. Then added FTS5 BM25 search, session tracking, a full GTD task manager, a native desktop tray app, and cross-machine sync. ~2,460 lines across 8 Python files, one required dependency.

**GitHub:** github.com/RMANOV/sqlite-memory-mcp


## The Problem

`@modelcontextprotocol/server-memory` stores everything in `memory.json`. No file locking. No transactions. Two concurrent sessions writing to the same file = data corruption.

This is not theoretical. It happened to me enough times to build a proper fix.


## Three PRAGMAs That Fix Everything

```sql
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
PRAGMA busy_timeout=10000;
```

WAL mode: readers never block writers, writers never block readers. 10+ concurrent Claude Code sessions, same `.db` file, ACID transactions. No corruption. Ever.


## 21 MCP Tools

**Drop-in compatible (1-9):** All 9 tools from the official memory server work identically — same names, same argument shapes. Your existing prompts keep working.

**Session tracking (10-12):** `session_recall(last_n=3)` at the start of every session returns what you were working on, which files were active, a summary from last session. Context continuity without reading conversation history.

**Task management (13-18):** Full GTD workflow built in. `create_task`, `update_task`, `query_tasks`, `task_digest`, `archive_done_tasks`, `bump_overdue_priority`. Five sections (inbox/today/next/someday/waiting), four priority levels, subtasks, recurring tasks. All in the same SQLite file.

**Bridge sync (19-21):** Work laptop in the morning, desktop at night. `bridge_push` / `bridge_pull` sync your knowledge graph and tasks between machines via a private git repo. No server, no cloud — just a git remote with last-write-wins conflict resolution.


## FTS5 BM25 Search

The official server does substring matching on JSONL. This maintains a `memory_fts` virtual table synced on every write, with BM25 ranking. `search_nodes("WAL mode")` returns the most relevant entities first, not insertion-order substring matches.


## Task Tray — Native Desktop App

`task_tray.py` is a PyQt6 system tray app that reads/writes directly to `memory.db`. Overdue badge on the tray icon, compact popup for quick task toggling, full tabbed window for management. Auto-refreshes every 30 seconds. Changes sync instantly between the tray app and Claude Code sessions — same database, WAL concurrency.


## Kanban Board

`task_report.py` generates a static HTML kanban board from your task database. Priority color-coding, section columns, overdue highlighting. Deploy to GitHub Pages — your AI agent's task list, visualized and shareable.


## The Stack

8 Python files sharing a `db_utils.py` module for constants and connection setup. One required dependency: `fastmcp` for the MCP protocol layer. `PyQt6` optional (only for the tray app). `sqlite3` is stdlib — no binaries, no Docker, no daemon.

Backup: `cp memory.db memory.db.bak`. That's it.

Drop into `settings.json`, it auto-migrates your existing `memory.json`. The 9 original tools are fully compatible — the 12 additional tools are opt-in.


## What's Next

- Rust/PyO3 rewrite of the hot path (FTS sync + BM25 query)
- Embedding-based vector search via `sqlite-vec` to complement BM25
- First-class hook integration for auto-saving sessions and auto-tagging tasks

Python 3.10+. MIT. v0.4.0 out now.

github.com/RMANOV/sqlite-memory-mcp

---

**Post text (for sharing the article):**

The official Claude MCP memory server corrupts data with 2+ concurrent sessions. I replaced it with SQLite WAL mode + FTS5 search + a GTD task manager + a native desktop tray app + cross-machine sync. 21 MCP tools, native GUI, 5 automation scripts. v0.4.0 out now.

#MCP #Claude #SQLite #Python #AI #LLM #ModelContextProtocol #GTD #TaskManagement #OpenSource #DeveloperTools
