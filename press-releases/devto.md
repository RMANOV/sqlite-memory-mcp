---
title: Building a Production-Quality MCP Memory Server with SQLite
published: true
description: How to build a concurrent, searchable, persistent memory layer for Claude Code using SQLite WAL mode and FTS5 — replacing the file-lock-prone JSONL approach.
tags: mcp, sqlite, python, claudeai
cover_image:
---

# Building a Production-Quality MCP Memory Server with SQLite

The Model Context Protocol (MCP) ships with an official memory server that stores a knowledge graph in a JSONL file. It works. Until you open a second terminal window, then it's a data corruption waiting to happen.

This post walks through how I replaced it with a SQLite-backed server that handles 10+ concurrent Claude Code sessions, adds BM25-ranked full-text search, and tracks session context across restarts. The final result is a single ~750-line Python file with one external dependency.

GitHub: https://github.com/RMANOV/sqlite-memory-mcp

---

## The problem with the official memory server

Anthropic's `@modelcontextprotocol/server-memory` stores everything in `~/.claude/memory/memory.json`, a JSONL file where each line is either an entity or a relation:

```json
{"type": "entity", "name": "FastMCP", "entityType": "Library", "observations": [...]}
{"type": "relation", "from": "sqlite-memory-mcp", "to": "FastMCP", "relationType": "depends_on"}
```

The problem: when two Claude Code sessions write to this file simultaneously, there's no coordination. Python's `open()` + `write()` on Linux is not atomic across processes. File locks are advisory — Python doesn't acquire them by default.

For a single session this is fine. For anyone who works with multiple terminal windows, this is a problem.

The MEMORY.md approach (a markdown file Claude reads at session start) has similar issues: append-writes from multiple sessions, no structured search, context grows unboundedly.

---

## Why SQLite

SQLite is the most deployed database in the world. It ships inside Python as `sqlite3`. Its WAL (Write-Ahead Logging) mode solves the concurrent-writers problem. Its FTS5 extension provides full-text search with BM25 ranking — no additional dependencies, no Docker, no daemon.

```
Single .db file  →  cp memory.db memory.db.bak   (backup done)
Zero config      →  no server, no API keys, no ports
WAL mode         →  concurrent readers + writers from N processes
FTS5             →  BM25 ranked search, built into stdlib sqlite3
ACID             →  transactions never corrupt on power loss
```

The tradeoff: SQLite doesn't scale to thousands of concurrent writers. For an AI coding assistant's memory layer — where writes happen maybe dozens of times per hour — this is never a constraint.

---

## Architecture

```
┌──────────────┐     stdio      ┌──────────────────┐
│  Claude Code  │◄──────────────►│  FastMCP Server   │
│  (session 1)  │               │                    │
├──────────────┤               │  12 MCP Tools      │
│  Claude Code  │◄─────────────►│  ┌──────────────┐ │
│  (session 2)  │               │  │  SQLite WAL   │ │
├──────────────┤               │  │  memory.db    │ │
│  Claude Code  │◄─────────────►│  │  FTS5 index   │ │
│  (session N)  │               │  └──────────────┘ │
└──────────────┘               └──────────────────┘
```

Each Claude Code session spawns its own `server.py` process via stdio. All processes connect to the same `memory.db` file. SQLite WAL mode handles concurrency at the filesystem level — no application-level locking needed.

---

## The WAL mode setup

Every connection to the database sets these PRAGMAs before doing anything else:

```python
_PRAGMAS = (
    "PRAGMA journal_mode=WAL;",
    "PRAGMA foreign_keys=ON;",
    "PRAGMA busy_timeout=10000;",
    "PRAGMA wal_autocheckpoint=100;",
)
```

Breaking these down:

**`journal_mode=WAL`** is the key one. In the default rollback journal mode, readers and writers block each other. In WAL mode:
- Readers never block writers
- Writers never block readers
- Multiple readers proceed concurrently
- Only one writer at a time, but they don't wait for readers

**`foreign_keys=ON`** is not on by default in SQLite. Without this, `ON DELETE CASCADE` silently does nothing. You need this for correct behavior when deleting entities.

**`busy_timeout=10000`** means: if a write lock is held by another process, wait up to 10 seconds before returning `SQLITE_BUSY`. Without this, two sessions writing simultaneously will fail immediately. With it, the second writer queues up. MCP tool calls complete in milliseconds, so 10 seconds is effectively infinite.

**`wal_autocheckpoint=100`** limits WAL file growth. After 100 frames are written, SQLite automatically checkpoints (moves WAL data back to the main file). Without this, long-running write workloads accumulate large WAL files.

The connection manager uses context managers to ensure commit/rollback and connection close happen correctly:

```python
@contextmanager
def _get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    for pragma in _PRAGMAS:
        conn.execute(pragma)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
```

Short-lived connections (open, execute, commit, close) rather than a persistent pool. This avoids WAL checkpoint accumulation and the complexity of connection pooling across processes.

---

## Schema design

```sql
-- Core entity storage
CREATE TABLE entities (
    id          INTEGER PRIMARY KEY,
    name        TEXT    UNIQUE NOT NULL,
    entity_type TEXT    NOT NULL,
    project     TEXT    DEFAULT NULL,   -- optional project scoping
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL
);

-- Observations attached to entities
CREATE TABLE observations (
    id          INTEGER PRIMARY KEY,
    entity_id   INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    content     TEXT    NOT NULL,
    created_at  TEXT    NOT NULL,
    UNIQUE(entity_id, content)          -- dedup at DB level
);

-- Directed relations between entities
CREATE TABLE relations (
    id            INTEGER PRIMARY KEY,
    from_id       INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    to_id         INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    relation_type TEXT    NOT NULL,
    created_at    TEXT    NOT NULL,
    UNIQUE(from_id, to_id, relation_type)
);

-- Session snapshots for context continuity
CREATE TABLE sessions (
    id           INTEGER PRIMARY KEY,
    session_id   TEXT    UNIQUE NOT NULL,
    project      TEXT    DEFAULT NULL,
    summary      TEXT    DEFAULT NULL,
    active_files TEXT    DEFAULT NULL,  -- JSON array stored as TEXT
    started_at   TEXT    NOT NULL,
    ended_at     TEXT    DEFAULT NULL
);

-- Full-text search index (BM25 ranked)
CREATE VIRTUAL TABLE memory_fts USING fts5(
    name, entity_type, observations_text,
    tokenize = "unicode61 remove_diacritics 2"
);
```

Design decisions worth noting:

**Deduplication via constraints, not application code.** Both `observations` and `relations` have `UNIQUE` constraints. All inserts use `INSERT OR IGNORE`. The application never needs to check for duplicates — the database enforces it.

**`ON DELETE CASCADE` on foreign keys.** Deleting an entity cascades to its observations and relations. Combined with `foreign_keys=ON` pragma, this means `delete_entities` is a single delete that cleans up everything.

**`project` field for scoping.** Entities can optionally belong to a project. Omit it for global entities (shared across all projects). This enables `search_by_project` to scope FTS5 queries to a single project.

**`active_files` stored as JSON text.** SQLite doesn't have an array type. Rather than a separate table for files (overkill), or a `TEXT` with a custom delimiter (fragile), I store a JSON array as TEXT and parse it in Python. Simple and works.

---

## FTS5 and BM25 search

The `memory_fts` virtual table is the search index. It stores three columns:
- `name` — entity name
- `entity_type` — entity type (e.g., "Person", "Library", "BugFix")
- `observations_text` — all observations for this entity, concatenated with newlines

Every write to entities or observations triggers a sync:

```python
def _fts_sync(conn: sqlite3.Connection, entity_id: int) -> None:
    row = conn.execute(
        "SELECT id, name, entity_type FROM entities WHERE id = ?",
        (entity_id,),
    ).fetchone()
    if row is None:
        conn.execute("DELETE FROM memory_fts WHERE rowid = ?", (entity_id,))
        return

    obs_rows = conn.execute(
        "SELECT content FROM observations WHERE entity_id = ? ORDER BY id",
        (entity_id,),
    ).fetchall()
    obs_text = "\n".join(r["content"] for r in obs_rows)

    # FTS5 has no ON CONFLICT — must DELETE then INSERT for upsert semantics
    conn.execute("DELETE FROM memory_fts WHERE rowid = ?", (entity_id,))
    conn.execute(
        "INSERT INTO memory_fts(rowid, name, entity_type, observations_text) "
        "VALUES (?, ?, ?, ?)",
        (row["id"], row["name"], row["entity_type"], obs_text),
    )
```

The `rowid` in `memory_fts` is kept in sync with `entities.id`. This makes the JOIN in `search_by_project` efficient — no secondary index needed.

**Important:** FTS5 has no `ON CONFLICT` or `UPSERT` syntax. If you do an `INSERT INTO memory_fts` for an existing `rowid`, you get two rows in the index. The DELETE + INSERT pattern prevents this.

### Query sanitization

Raw user input can contain FTS5 special characters (`*`, `"`, `OR`, `AND`, parentheses) that cause syntax errors. The sanitizer wraps each token in double quotes:

```python
def _fts_query(raw: str) -> str:
    tokens = raw.split()
    if not tokens:
        return '""'
    escaped = ['"' + t.replace('"', '""') + '"' for t in tokens]
    return " OR ".join(escaped)
```

`search_nodes("WAL mode")` becomes `"WAL" OR "mode"` — broad matching, no syntax errors. Users who want exact phrase search can pass `'"WAL mode"'` — the outer quotes are stripped by Python, leaving an FTS5 phrase query.

### Using BM25 ranking

```sql
SELECT rowid, name, entity_type, rank
FROM memory_fts
WHERE memory_fts MATCH ?
ORDER BY rank
LIMIT 50
```

FTS5 `rank` is negative BM25 score (lower = more relevant), so `ORDER BY rank` gives most relevant first without `DESC`. This is counterintuitive but correct.

---

## Session tracking

The `sessions` table stores snapshots of Claude Code sessions. This solves a specific problem: at the start of a new Claude Code window, the AI has no idea what you were working on.

At the end of a session (or periodically during it):

```python
# Claude calls:
session_save(
    session_id="2026-03-01-work",
    project="sqlite-memory-mcp",
    summary="Implemented FTS5 sync. Fixed the BM25 double-row bug. Added wal_autocheckpoint.",
    active_files=["server.py", "README.md", "pyproject.toml"]
)
```

At the start of the next session:

```python
# Claude calls:
session_recall(last_n=3)
```

Returns:
```json
{
  "sessions": [
    {
      "session_id": "2026-03-01-work",
      "project": "sqlite-memory-mcp",
      "summary": "Implemented FTS5 sync. Fixed the BM25 double-row bug. Added wal_autocheckpoint.",
      "active_files": ["server.py", "README.md", "pyproject.toml"],
      "started_at": "2026-03-01T18:23:11+00:00",
      "ended_at": "2026-03-01T22:47:03+00:00"
    }
  ],
  "count": 1
}
```

Context continuity without reading conversation history. You can also hook into Claude Code's session events to call `session_save` automatically — see `examples/session_context_hook.py` in the repo.

---

## The 12 tools

### Drop-in compatible (tools 1-9)

All tool names, argument shapes, and return formats match `@modelcontextprotocol/server-memory` exactly. If your prompts reference these tools, they keep working:

```
create_entities    add_observations   create_relations
delete_entities    delete_observations  delete_relations
read_graph         search_nodes         open_nodes
```

### Extended tools (10-12)

```
session_save(session_id, ?project, ?summary, ?active_files)
session_recall(last_n=5)
search_by_project(query, project)
```

---

## Installation and configuration

```bash
git clone https://github.com/RMANOV/sqlite-memory-mcp.git
cd sqlite-memory-mcp
pip install fastmcp
```

Add to `~/.claude/settings.json`:

```json
{
  "mcpServers": {
    "sqlite_memory": {
      "command": "python3",
      "args": ["/absolute/path/to/sqlite-memory-mcp/server.py"],
      "env": {
        "SQLITE_MEMORY_DB": "/home/youruser/.claude/memory/memory.db"
      }
    }
  }
}
```

If you have an existing `memory.json`, the server auto-migrates it on first run and renames the old file to `memory.json.migrated`.

---

## What's next

A few things I'm considering for future versions:

- **Rust rewrite of the hot path** — the FTS sync + BM25 query is fast enough in Python for this use case, but a PyO3 extension for the DB layer would be interesting
- **Embedding-based search** — FTS5 keyword search misses semantic matches; an optional vector search path (sqlite-vec or similar) would complement BM25
- **Hook integration** — first-class support for auto-saving sessions via Claude Code hooks

---

Python 3.10+, MIT license. Single file, one external dependency (`fastmcp`).

If you're using the official memory server and hitting the concurrent-session problem, this should be a drop-in fix.
