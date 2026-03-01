# SQLite Memory MCP Server

A production-quality SQLite-backed MCP Memory server with WAL concurrent safety (10+ sessions), FTS5 BM25 search, and session tracking.

Drop-in compatible with `@modelcontextprotocol/server-memory` (9/9 tools) plus 3 new tools for session tracking and cross-project search.

## Why SQLite?

Existing MCP memory servers use JSONL files, cloud APIs, or heavyweight databases. Each has trade-offs that hurt real-world Claude Code usage:

- **JSONL files** (official MCP memory) -- file locks break with 2+ concurrent sessions. Data corruption is a matter of time.
- **Cloud APIs** (Mem0, Supabase) -- latency, API keys, privacy concerns, vendor lock-in.
- **Heavy databases** (Neo4j, ChromaDB, Qdrant) -- Docker, config files, resource overhead for what is essentially a key-value store with search.

SQLite hits the sweet spot:

- **Single file** -- `memory.db` is the entire database. Back it up with `cp`.
- **Zero config** -- No server process, no Docker, no API keys.
- **ACID transactions** -- Writes never corrupt, even on power loss.
- **WAL mode** -- Multiple concurrent readers and writers. 10+ Claude Code sessions, no conflicts.
- **FTS5** -- Full-text search with BM25 ranking built into the standard library.
- **stdlib** -- `sqlite3` ships with Python. No additional binary dependencies.

## Features

- **WAL mode** -- 10+ concurrent Claude Code sessions with no file locking conflicts
- **FTS5 BM25 ranked search** -- Full-text search across entity names, types, and observations with relevance ranking
- **Session tracking** -- Save and recall session snapshots for context continuity across restarts
- **Cross-project sharing** -- Optional `project` field scopes entities; omit it to share across all projects
- **Drop-in compatible** -- All 9 tools from `@modelcontextprotocol/server-memory` work identically, plus 3 new tools
- **Zero dependencies beyond stdlib** -- Only `fastmcp` for the MCP protocol; `sqlite3` is Python stdlib
- **Automatic FTS sync** -- Full-text index stays in sync with every write operation
- **JSONL migration** -- Optionally import existing `memory.json` knowledge graphs on first run

## Competitor Comparison

| Feature | sqlite-memory-mcp | Official MCP Memory | claude-mem0 | @pepk/sqlite | simple-memory | mcp-memory-service | memsearch | memory-mcp | MemoryGraph |
|---|---|---|---|---|---|---|---|---|---|
| Storage | SQLite | JSONL file | Mem0 Cloud | SQLite | JSON file | ChromaDB | Qdrant | SQLite | Neo4j |
| Concurrent 10+ sessions | WAL mode | file locks | cloud | no WAL | file locks | yes | yes | no | yes |
| FTS5 BM25 search | yes | substring | no | no | no | vector | vector | no | Cypher |
| Session tracking | built-in | no | no | no | no | no | no | no | no |
| Cross-project sharing | project field | no | no | no | no | no | no | no | no |
| Drop-in compatible | 9/9 tools | baseline | no | partial | no | no | no | partial | no |
| Setup effort | pip install | npx | API key + pip | pip | npx | Docker + pip | Docker + pip | pip | Docker + Neo4j |
| Dependencies | sqlite3 (stdlib) | Node.js | Cloud API | sqlite3 | Node.js | ChromaDB | Qdrant | sqlite3 | Neo4j |

## Installation

### Quick Start

```bash
# Clone
git clone https://github.com/rmanov/sqlite-memory-mcp.git
cd sqlite-memory-mcp

# Install dependencies
pip install fastmcp

# Add to Claude Code
claude mcp add sqlite_memory python3 /path/to/server.py
```

### Manual Configuration

Add to your `~/.claude/settings.json` under `mcpServers`:

```json
"sqlite_memory": {
  "command": "python3",
  "args": ["/path/to/sqlite-memory-mcp/server.py"],
  "env": {
    "SQLITE_MEMORY_DB": "/home/user/.claude/memory/memory.db"
  }
}
```

The `SQLITE_MEMORY_DB` environment variable controls where the database is stored. If omitted, it defaults to `~/.claude/memory/memory.db`.

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

Each Claude Code session spawns its own `server.py` process via stdio. All processes connect to the same `memory.db` file. SQLite WAL mode allows concurrent reads and writes without blocking.

## Schema

The database has 4 tables and 1 FTS5 virtual table:

```sql
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
PRAGMA busy_timeout=5000;

-- Core entity storage
CREATE TABLE IF NOT EXISTS entities (
    id          INTEGER PRIMARY KEY,
    name        TEXT    UNIQUE NOT NULL,
    entity_type TEXT    NOT NULL,
    project     TEXT    DEFAULT NULL,
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL
);

-- Observations attached to entities
CREATE TABLE IF NOT EXISTS observations (
    id          INTEGER PRIMARY KEY,
    entity_id   INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    content     TEXT    NOT NULL,
    created_at  TEXT    NOT NULL,
    UNIQUE(entity_id, content)
);

-- Directed relations between entities
CREATE TABLE IF NOT EXISTS relations (
    id            INTEGER PRIMARY KEY,
    from_id       INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    to_id         INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    relation_type TEXT    NOT NULL,
    created_at    TEXT    NOT NULL,
    UNIQUE(from_id, to_id, relation_type)
);

-- Session snapshots for context continuity
CREATE TABLE IF NOT EXISTS sessions (
    id           INTEGER PRIMARY KEY,
    session_id   TEXT    UNIQUE NOT NULL,
    project      TEXT    DEFAULT NULL,
    summary      TEXT    DEFAULT NULL,
    active_files TEXT    DEFAULT NULL,  -- JSON array
    started_at   TEXT    NOT NULL,
    ended_at     TEXT    DEFAULT NULL
);

-- Full-text search index (BM25 ranked)
CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
    name, entity_type, observations_text,
    tokenize = "unicode61 remove_diacritics 2"
);
```

**Design notes:**

- `entities.name` is `UNIQUE` -- one entity per name, enforced at the database level.
- `observations` uses `UNIQUE(entity_id, content)` -- duplicate observations are silently ignored via `INSERT OR IGNORE`.
- `relations` uses `UNIQUE(from_id, to_id, relation_type)` -- same deduplication pattern.
- `ON DELETE CASCADE` on foreign keys ensures deleting an entity cleans up all its observations and relations.
- `memory_fts` is a virtual table that concatenates entity name, type, and all observations into a single searchable document. It is synced on every write.

## Tool Reference

### Knowledge Graph Tools (drop-in compatible)

| # | Tool | Description |
|---|------|-------------|
| 1 | `create_entities` | Create new entities with observations. Accepts a list of `{name, entityType, observations}` objects. Deduplicates by name. |
| 2 | `add_observations` | Add observations to existing entities. Accepts `{entityName, contents}` pairs. Deduplicates by content. |
| 3 | `create_relations` | Create directed relations between entities. Accepts `{from, to, relationType}` triples. |
| 4 | `delete_entities` | Delete entities by name. Cascades to observations and relations. |
| 5 | `delete_observations` | Remove specific observations from an entity by content text. |
| 6 | `delete_relations` | Remove specific relations by `{from, to, relationType}`. |
| 7 | `read_graph` | Full knowledge graph dump. Returns all entities, observations, and relations. |
| 8 | `search_nodes` | FTS5 BM25 ranked search across entity names, types, and observations. |
| 9 | `open_nodes` | Retrieve specific entities by name, including their observations and inter-relations. |

### Extended Tools (new)

| # | Tool | Description |
|---|------|-------------|
| 10 | `session_save` | Save a session snapshot with session ID, project, summary, and active files. |
| 11 | `session_recall` | Recall the N most recent sessions, ordered by start time. |
| 12 | `search_by_project` | FTS5 BM25 search scoped to a specific project. |

## WAL Mode & Concurrency

SQLite's Write-Ahead Logging (WAL) mode is the key enabler for concurrent Claude Code sessions:

- **Without WAL** (default journal mode): Readers block writers, writers block readers. A single file lock means only one process can write at a time, and reads are blocked during writes.
- **With WAL**: Readers never block writers. Writers never block readers. Multiple readers can proceed concurrently. Only one writer at a time, but writers don't wait for readers.

This server sets three PRAGMAs at every connection:

```sql
PRAGMA journal_mode=WAL;     -- Enable write-ahead logging
PRAGMA foreign_keys=ON;      -- Enforce referential integrity
PRAGMA busy_timeout=5000;    -- Wait up to 5 seconds for write lock
```

The `busy_timeout` is critical: if two sessions try to write simultaneously, the second one waits up to 5 seconds instead of failing immediately. In practice, MCP tool calls are fast enough that contention is rare.

**Result:** 10+ concurrent Claude Code sessions can read and write the same `memory.db` without corruption or blocking.

## FTS5 Search Examples

The `search_nodes` tool uses SQLite FTS5 with BM25 ranking. Queries support the standard FTS5 syntax:

```
# Simple term search
search_nodes("fastmcp")

# Phrase search
search_nodes('"WAL mode"')

# Boolean AND (implicit)
search_nodes("sqlite concurrency")

# Boolean OR
search_nodes("sqlite OR postgres")

# Prefix search
search_nodes("bug*")

# Negation
search_nodes("memory NOT cache")

# Column-specific search
search_nodes("name:server")
search_nodes("entity_type:BugFix")
```

Results are ranked by BM25 relevance score. The FTS5 index covers entity names, entity types, and the full text of all observations concatenated together.

## Session Tracking

Session tracking enables context continuity across Claude Code restarts.

### Saving a session

At the end of a session (or periodically), save a snapshot:

```
session_save(
  session_id="abc-123",
  project="sqlite-memory-mcp",
  summary="Implemented FTS5 search with BM25 ranking. Fixed WAL pragma ordering.",
  active_files=[
    "server.py",
    "README.md"
  ]
)
```

### Recalling recent sessions

At the start of a new session, recall what happened recently:

```
session_recall(last_n=3)
```

Returns the 3 most recent sessions with their summaries, projects, active files, and timestamps.

### Hook integration

You can extend your Claude Code session hook (`~/.claude/hooks/session_context.py`) to automatically recall recent sessions and inject them into the system prompt. See `examples/session_context_hook.py` for a reference implementation.

## License

MIT License. See [LICENSE](LICENSE) for details.
