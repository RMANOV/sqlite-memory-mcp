# v0.1.0 — SQLite Memory MCP Server

**SQLite-backed MCP Memory Server with WAL concurrent safety, FTS5 BM25 search, and session tracking.**

Drop-in compatible with `@modelcontextprotocol/server-memory` (9/9 tools) plus 3 new tools for session tracking and cross-project search.

---

## The problem this solves

The official `@modelcontextprotocol/server-memory` uses a JSONL file (`memory.json`) for storage. This works fine for a single session. As soon as you open a second Claude Code window, you're writing to the same file from two processes with no coordination. File locks on Linux/macOS are advisory, not mandatory. Data corruption is a question of when, not if.

The workaround most people use is keeping one Claude Code session open at a time, which defeats the purpose of a persistent memory layer.

This server uses SQLite in WAL mode. WAL (Write-Ahead Logging) gives you concurrent readers + writers against the same file, ACID transactions, and a 5-second busy timeout so two sessions writing simultaneously queue up rather than crash. No Docker, no API keys, no daemon process.

---

## What's included

### Knowledge graph tools (drop-in compatible with official MCP memory)

| Tool | Description |
|------|-------------|
| `create_entities` | Create entities with observations. Deduplicates by name. |
| `add_observations` | Add observations to existing entities. Deduplicates by content. |
| `create_relations` | Create directed relations between entities. |
| `delete_entities` | Delete entities + CASCADE to observations and relations. |
| `delete_observations` | Remove specific observations by content text. |
| `delete_relations` | Remove specific relations by `{from, to, relationType}`. |
| `read_graph` | Full knowledge graph dump. |
| `search_nodes` | FTS5 BM25-ranked full-text search. |
| `open_nodes` | Retrieve entities by name with inter-relations. |

### Extended tools (new in this server)

| Tool | Description |
|------|-------------|
| `session_save` | Save a session snapshot with ID, project, summary, active files. |
| `session_recall` | Recall the N most recent sessions ordered by start time. |
| `search_by_project` | FTS5 BM25 search scoped to a specific project. |

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

Each Claude Code session spawns its own `server.py` process via stdio. All processes connect to the same `memory.db`. WAL mode handles concurrency without coordination overhead.

---

## Schema

4 tables + 1 FTS5 virtual table:

```sql
-- Core entities
CREATE TABLE entities (
    id          INTEGER PRIMARY KEY,
    name        TEXT    UNIQUE NOT NULL,
    entity_type TEXT    NOT NULL,
    project     TEXT    DEFAULT NULL,   -- optional project scoping
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL
);

-- Observations per entity
CREATE TABLE observations (
    id          INTEGER PRIMARY KEY,
    entity_id   INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    content     TEXT    NOT NULL,
    created_at  TEXT    NOT NULL,
    UNIQUE(entity_id, content)          -- deduplication enforced at DB level
);

-- Directed relations
CREATE TABLE relations (
    id            INTEGER PRIMARY KEY,
    from_id       INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    to_id         INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    relation_type TEXT    NOT NULL,
    created_at    TEXT    NOT NULL,
    UNIQUE(from_id, to_id, relation_type)
);

-- Session snapshots
CREATE TABLE sessions (
    id           INTEGER PRIMARY KEY,
    session_id   TEXT    UNIQUE NOT NULL,
    project      TEXT    DEFAULT NULL,
    summary      TEXT    DEFAULT NULL,
    active_files TEXT    DEFAULT NULL,  -- JSON array
    started_at   TEXT    NOT NULL,
    ended_at     TEXT    DEFAULT NULL
);

-- Full-text search (BM25 ranked)
CREATE VIRTUAL TABLE memory_fts USING fts5(
    name, entity_type, observations_text,
    tokenize = "unicode61 remove_diacritics 2"
);
```

---

## Installation

```bash
git clone https://github.com/RMANOV/sqlite-memory-mcp.git
cd sqlite-memory-mcp
pip install fastmcp
```

Add to `~/.claude/settings.json`:

```json
"mcpServers": {
  "sqlite_memory": {
    "command": "python3",
    "args": ["/path/to/sqlite-memory-mcp/server.py"],
    "env": {
      "SQLITE_MEMORY_DB": "/home/user/.claude/memory/memory.db"
    }
  }
}
```

Or use the CLI:

```bash
claude mcp add sqlite_memory python3 /path/to/server.py
```

---

## Migrating from official MCP memory

If you have an existing `~/.claude/memory/memory.json`, the server will automatically detect it on first run and migrate all entities and relations to SQLite, then rename the old file to `memory.json.migrated`. No data loss, no manual steps.

---

## WAL mode details

Three PRAGMAs are set on every connection:

```sql
PRAGMA journal_mode=WAL;      -- concurrent readers + writers
PRAGMA foreign_keys=ON;       -- enforce referential integrity
PRAGMA busy_timeout=10000;    -- wait up to 10s for write lock instead of SQLITE_BUSY
```

The `busy_timeout` is what makes multi-session use reliable. If two sessions write simultaneously, the second waits up to 10 seconds. MCP tool calls are fast enough that this never actually times out in practice.

---

## Dependencies

- Python 3.10+
- `fastmcp>=2.0.0` (the MCP protocol layer)
- `sqlite3` (Python stdlib — no additional binaries)

---

## License

MIT
