# SQLite Memory MCP Server

A production-quality SQLite-backed MCP Memory server with WAL concurrent safety (10+ sessions), FTS5 BM25 search, and session tracking.

Drop-in compatible with `@modelcontextprotocol/server-memory` (9/9 tools) plus 12 additional tools for session tracking, task management, cross-project search, and cross-machine bridge sync.

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
- **Task management** -- Structured task CRUD with typed queries, priorities, sections, due dates, and recurring tasks
- **Kanban board** -- Optional HTML report generator for visual task overview via GitHub Pages
- **Cross-project sharing** -- Optional `project` field scopes entities; omit it to share across all projects
- **Cross-machine sync** -- Bridge tools push/pull shared entities between machines via a private git repo
- **Drop-in compatible** -- All 9 tools from `@modelcontextprotocol/server-memory` work identically, plus 12 new tools
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
| Task management | built-in | no | no | no | no | no | no | no | no |
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
├──────────────┤               │  21 MCP Tools      │
│  Claude Code  │◄─────────────►│  ┌──────────────┐ │
│  (session 2)  │               │  │  SQLite WAL   │ │
├──────────────┤               │  │  memory.db    │ │
│  Claude Code  │◄─────────────►│  │  FTS5 index   │ │
│  (session N)  │               │  └──────────────┘ │
└──────────────┘               └──────────────────┘
```

Each Claude Code session spawns its own `server.py` process via stdio. All processes connect to the same `memory.db` file. SQLite WAL mode allows concurrent reads and writes without blocking.

## Schema

The database has 5 tables and 1 FTS5 virtual table:

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

-- Structured task management
CREATE TABLE IF NOT EXISTS tasks (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    description TEXT DEFAULT NULL,
    status      TEXT NOT NULL DEFAULT 'not_started',
    priority    TEXT DEFAULT 'medium',
    section     TEXT DEFAULT 'inbox',
    due_date    TEXT DEFAULT NULL,
    project     TEXT DEFAULT NULL,
    parent_id   TEXT DEFAULT NULL REFERENCES tasks(id),
    notes       TEXT DEFAULT NULL,
    recurring   TEXT DEFAULT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
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
- `tasks.id` is a UUID (TEXT), not an integer -- tasks are identified by UUID for stability across machines.

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

### Task Management Tools

| # | Tool | Description |
|---|------|-------------|
| 13 | `create_task` | Create a new task. Params: `title` (required), `description`, `section` (inbox/today/next/someday/waiting), `priority` (low/medium/high/critical), `due_date` (YYYY-MM-DD), `project`, `parent_id`, `notes`, `recurring`. Returns UUID. |
| 14 | `update_task` | Update a task's fields by UUID. All fields optional except `task_id`. Partial update -- only provided fields are changed. |
| 15 | `query_tasks` | Query tasks with optional filters: `section`, `status`, `priority`, `project`, `parent_id`, `overdue_only`, `limit`. Filters combined with AND. Returns markdown table + JSON. |
| 16 | `task_digest` | Generate a formatted session-start digest. Shows pending/in-progress tasks grouped by section plus overdue tasks highlighted separately. Params: `sections`, `include_overdue`, `limit`. |
| 17 | `archive_done_tasks` | Archive completed tasks older than N days. Param: `older_than_days` (default 7). Moves `status='done'` tasks to `status='archived'`. |
| 18 | `bump_overdue_priority` | Escalate priority of overdue tasks. Param: `target_priority` (default `high`). Only bumps tasks whose current priority is lower than the target. |

### Bridge Tools (cross-machine sync)

| # | Tool | Description |
|---|------|-------------|
| 19 | `bridge_push` | Push entities tagged with `project LIKE 'shared%'` to a bridge git repo as JSON. Payload v2 includes tasks. Git commit + push. |
| 20 | `bridge_pull` | Pull shared entities and tasks from the bridge git repo. Git pull + import with dedup via UNIQUE constraints. Last-write-wins for tasks (compared by `updated_at`). |
| 21 | `bridge_status` | Show sync status -- local shared entities vs repo contents, with diff summary. |

## Bridge Sync (Cross-Machine)

Share knowledge graph entities between machines (e.g., personal laptop + work computer) via a private git repo.

### How it works

1. Tag entities for sharing by setting `project` to any value starting with `"shared"` (e.g., `"shared"`, `"shared:trading"`, `"shared:hooks"`)
2. `bridge_push()` exports all shared entities + their observations and inter-relations to `shared.json` in a local git repo, then commits and pushes. The v2 payload also includes shared tasks.
3. `bridge_pull()` on the other machine does `git pull` + imports new entities/observations/relations (UNIQUE constraints handle dedup). Tasks use last-write-wins based on `updated_at` comparison.
4. `bridge_status()` shows what's in sync vs only-local vs only-remote

### Setup

```bash
# One-time setup on each machine
mkdir -p ~/.claude/memory/bridge
cd ~/.claude/memory/bridge
git init

# Create a private GitHub repo
gh repo create memory-bridge --private
git remote add origin https://github.com/YOUR_USER/memory-bridge.git

# Initialize
echo '{}' > shared.json
git add shared.json
git commit -m "init: bridge repo"
git push -u origin main
```

On the second machine, clone instead of init:

```bash
git clone https://github.com/YOUR_USER/memory-bridge.git ~/.claude/memory/bridge
```

Add `BRIDGE_REPO` to your MCP server config in `~/.claude/settings.json`:

```json
"sqlite_memory": {
  "command": "python3",
  "args": ["/path/to/server.py"],
  "env": {
    "SQLITE_MEMORY_DB": "/home/user/.claude/memory/memory.db",
    "BRIDGE_REPO": "/home/user/.claude/memory/bridge"
  }
}
```

### Usage

```python
# Tag an entity for sharing
create_entities([{
    "name": "WAL-mode-pattern",
    "entityType": "TechnicalInsight",
    "project": "shared:sqlite",
    "observations": ["SQLite WAL mode enables concurrent readers + writers"]
}])

# Push to bridge repo
bridge_push()  # pushes all project LIKE 'shared%'

# On another machine: pull
bridge_pull()  # imports new entities with dedup

# Check sync status
bridge_status()
```

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

## Task Management

Structured task tracking built directly into the memory server. No external service required.

### Section-based workflow

Tasks are organized into five sections following a GTD-style workflow:

| Section | Purpose |
|---------|---------|
| `inbox` | Unprocessed tasks (default) |
| `today` | Tasks to complete today |
| `next` | Next actions queue |
| `someday` | Deferred / maybe |
| `waiting` | Blocked on someone else |

### Priority levels

Four priority levels: `low`, `medium` (default), `high`, `critical`. The `query_tasks` and `task_digest` tools always sort by priority descending, then by `due_date` ascending.

### Statuses

`not_started` (default), `in_progress`, `done`, `archived`, `cancelled`.

### Example usage

```python
# Create a task
create_task(
    title="Review pull request #42",
    section="today",
    priority="high",
    due_date="2026-03-05",
    project="sqlite-memory-mcp"
)

# Query pending tasks for today
query_tasks(section="today", status="not_started")

# Mark a task in progress
update_task(task_id="<uuid>", status="in_progress")

# Get a session-start digest
task_digest(sections=["today", "inbox"], include_overdue=True)

# Archive done tasks older than 3 days
archive_done_tasks(older_than_days=3)

# Escalate overdue tasks to high priority
bump_overdue_priority(target_priority="high")
```

### Subtasks

Link a task to a parent via `parent_id`:

```python
parent = create_task(title="Implement feature X")
# parent returns {"task_id": "<parent-uuid>", ...}

create_task(
    title="Write tests for feature X",
    parent_id="<parent-uuid>"
)
```

Query subtasks with `query_tasks(parent_id="<parent-uuid>")`.

### Recurring tasks

Pass a JSON recurrence config in the `recurring` field:

```python
create_task(
    title="Weekly review",
    section="today",
    recurring='{"every": "week", "day": "monday"}'
)
```

The automation script `~/notion_automations/recurring_tasks.py` reads this field and recreates tasks on schedule.

### Automation scripts

Four scripts automate routine task hygiene:

| Script | Function |
|--------|----------|
| `daily_digest.py` | Sends formatted task digest at session start |
| `auto_archive.py` | Archives done tasks older than 7 days |
| `overdue_bump.py` | Escalates overdue tasks to `high` priority |
| `recurring_tasks.py` | Recreates recurring tasks on schedule |

All scripts are pure stdlib Python operating directly on `memory.db` via SQL -- zero external dependencies.

## Kanban Board

`task_report.py` generates a static HTML kanban board from the tasks table:

```bash
python task_report.py
# Writes: index.html
```

The generated `index.html` shows tasks grouped by section as kanban columns, with priority color-coding. Commit it to the bridge repo to publish via GitHub Pages.

```bash
# Publish to GitHub Pages
cp index.html ~/.claude/memory/bridge/
cd ~/.claude/memory/bridge
git add index.html
git commit -m "chore: update kanban board"
git push
```

Enable GitHub Pages on the bridge repo (Settings > Pages > Branch: main) to get a live URL.

## License

MIT License. See [LICENSE](LICENSE) for details.
