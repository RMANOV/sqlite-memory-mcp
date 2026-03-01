# LinkedIn Article

**Title:** Building a Production-Quality MCP Memory Server with SQLite

**Subtitle:** How SQLite WAL mode and FTS5 solve the concurrent-session problem for AI coding assistants

---

**COPY BELOW THIS LINE**

---

The Model Context Protocol (MCP) ships with an official memory server that stores a knowledge graph in a JSONL file.

It works. Until you open a second terminal window.

Then it's a data corruption waiting to happen.

I replaced it with a SQLite-backed server that handles 10+ concurrent Claude Code sessions, adds BM25-ranked full-text search, and tracks session context across restarts. Single Python file, ~750 lines, one external dependency.

**GitHub:** https://github.com/RMANOV/sqlite-memory-mcp


## The Problem

Anthropic's @modelcontextprotocol/server-memory stores everything in a flat JSONL file. Two concurrent sessions writing to the same file? No coordination. Python's file writes aren't atomic across processes. On Linux, file locks are advisory.

For a single session this is fine. For anyone running multiple terminal windows — and that's most developers using Claude Code seriously — this is a ticking time bomb.


## Why SQLite

SQLite is the most deployed database in the world. It ships inside Python as stdlib. Its WAL (Write-Ahead Logging) mode solves concurrent-writers. Its FTS5 extension provides full-text search with BM25 ranking.

No Docker. No daemon. No API keys. One .db file.

Backup? cp memory.db memory.db.bak. Done.


## The Three PRAGMAs That Make It Work

Every connection to the database sets these before doing anything else:

    PRAGMA journal_mode=WAL;
    PRAGMA foreign_keys=ON;
    PRAGMA busy_timeout=10000;

**WAL mode** — readers never block writers, writers never block readers, multiple readers proceed concurrently.

**foreign_keys=ON** — not on by default in SQLite. Without this, ON DELETE CASCADE silently does nothing.

**busy_timeout=10000** — if a write lock is held, wait 10 seconds instead of failing immediately. MCP tool calls complete in milliseconds, so this is effectively infinite.


## Architecture

Each Claude Code session spawns its own server.py process via stdio. All processes connect to the same memory.db file. SQLite WAL mode handles concurrency at the filesystem level — no application-level locking needed.

The connection manager uses short-lived connections (open, execute, commit, close) rather than a persistent pool. This avoids WAL checkpoint accumulation and connection pooling complexity across processes.


## Schema Design Decisions

Four tables + one FTS5 virtual table:

**entities** — name (unique), type, optional project field, timestamps
**observations** — content attached to entities, deduplicated via UNIQUE constraint
**relations** — directed typed relations between entities, CASCADE deletes
**sessions** — session snapshots with ID, project, summary, active files

Design principle: deduplication at the database level, not in application code. All inserts use INSERT OR IGNORE. The database enforces uniqueness — the application never checks for duplicates.


## FTS5 and BM25 Search

The memory_fts virtual table covers entity names, types, and all observations concatenated. Every write triggers a sync via DELETE + INSERT (FTS5 has no ON CONFLICT — skipping the DELETE creates silent duplicate index entries that accumulate).

Query sanitization wraps each user token in double quotes and joins with OR. Users get broad matching without needing to know FTS5 syntax. BM25 ranking returns the most relevant results first.


## Session Tracking — The Feature I Actually Built This For

At the end of a session:

    session_save(session_id, project, summary, active_files)

At the start of the next session:

    session_recall(last_n=3)

Returns what you were working on, which files were active, and the summary you wrote. Context continuity without reading conversation history.

You can hook this into Claude Code's session events for automatic tracking.


## Drop-in Compatibility

All 9 tools from @modelcontextprotocol/server-memory work with identical signatures:

create_entities, add_observations, create_relations,
delete_entities, delete_observations, delete_relations,
read_graph, search_nodes, open_nodes

Plus 3 new: session_save, session_recall, search_by_project.

Existing prompts that reference these tools keep working without modification.


## The Competitive Landscape

I evaluated 8 existing MCP memory servers before building this. None provided all three: SQLite WAL concurrent safety + FTS5 BM25 search + session tracking.

The vector-search-based options (ChromaDB, Qdrant) require Docker and a running daemon. The cloud options (Mem0) add latency and API keys. The existing SQLite options skip WAL mode — so you still get SQLITE_BUSY errors with concurrent sessions.


## What's Next

Considering for future versions:
- Rust/PyO3 rewrite of the hot path (FTS sync + BM25 query)
- Optional embedding-based search via sqlite-vec
- First-class hook integration for automatic session tracking


## Try It

    git clone https://github.com/RMANOV/sqlite-memory-mcp.git
    pip install fastmcp
    claude mcp add sqlite_memory python3 /path/to/server.py

Python 3.10+. MIT license. Single file, one dependency.

If you're using the official memory server and hitting the concurrent-session problem, this should be a drop-in fix.

---

#MCP #ModelContextProtocol #Claude #SQLite #Python #AI #LLM #DeveloperTools #OpenSource #ArtificialIntelligence #MachineLearning #FullTextSearch #WAL #FTS5 #ConcurrentProgramming
