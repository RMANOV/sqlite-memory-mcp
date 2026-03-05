# [Project] sqlite-memory-mcp v0.4.0 — SQLite WAL + FTS5 + Task Management + Bridge Sync, drop-in for @modelcontextprotocol/server-memory

**GitHub:** https://github.com/RMANOV/sqlite-memory-mcp
**Language:** Python
**License:** MIT
**Version:** v0.4.0

---

## Overview

SQLite-backed MCP Memory server. Implements all 9 tools from `@modelcontextprotocol/server-memory` with identical signatures, plus 12 new tools (21 total). Uses SQLite WAL mode for concurrent multi-session safety and FTS5 for BM25-ranked full-text search. v0.4.0 adds GTD task management, a Kanban board generator, and cross-machine bridge sync.

---

## Adding to Claude Code

In `~/.claude/settings.json`:

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

Or with the Claude Code CLI:

```bash
claude mcp add sqlite_memory python3 /absolute/path/to/sqlite-memory-mcp/server.py
```

`SQLITE_MEMORY_DB` is optional — defaults to `~/.claude/memory/memory.db`.

---

## Tool Compatibility

### Tools 1-9: Drop-in compatible with @modelcontextprotocol/server-memory

```
create_entities(entities: list[{name, entityType, observations, ?project}])
add_observations(observations: list[{entityName, contents}])
create_relations(relations: list[{from, to, relationType}])
delete_entities(entityNames: list[str])
delete_observations(deletions: list[{entityName, observations}])
delete_relations(relations: list[{from, to, relationType}])
read_graph() -> {entities, relations}
search_nodes(query: str) -> {entities}    # FTS5 BM25 ranked
open_nodes(names: list[str]) -> {entities, relations}
```

All argument shapes and return formats match the official server. Existing prompts that reference these tool names work without modification.

### Tools 10-12: Session (new)

```
session_save(session_id, ?project, ?summary, ?active_files)
session_recall(last_n=5) -> {sessions}
search_by_project(query, project) -> {entities}
```

### Tools 13-18: Task Management (new in v0.4.0)

```
create_task(title, ?section, ?priority, ?due_date, ?parent_id, ?recurrence)
update_task(task_id, ?title, ?section, ?priority, ?status, ?due_date)
query_tasks(?section, ?priority, ?status, ?due_before) -> {tasks}
task_digest() -> {overdue, today, upcoming}
archive_done_tasks() -> {archived_count}
bump_overdue_priority() -> {bumped_count}
```

Sections: `inbox` / `today` / `next` / `someday` / `waiting`
Priorities: `low` / `medium` / `high` / `critical`
Subtasks: `parent_id` links to parent task. Recurring tasks via JSON recurrence config.

### Tools 19-21: Bridge Sync (new in v0.4.0)

```
bridge_push(?include_tasks=True) -> {status}
bridge_pull() -> {entities_merged, tasks_merged}
bridge_status() -> {last_push, last_pull, remote_url}
```

Cross-machine sync via a git repo. Pushes/pulls both knowledge graph entities and tasks. Conflict resolution: last-write-wins by `updated_at`.

---

## Why SQLite over the official JSONL approach

The official memory server writes a JSONL file with no inter-process coordination. Two concurrent MCP sessions writing to the same `memory.json` will eventually corrupt it. On Linux, file locks are advisory — Python doesn't acquire them by default.

This server sets three PRAGMAs on every SQLite connection:

```python
_PRAGMAS = (
    "PRAGMA journal_mode=WAL;",
    "PRAGMA foreign_keys=ON;",
    "PRAGMA busy_timeout=10000;",
    "PRAGMA wal_autocheckpoint=100;",
)
```

WAL mode allows multiple concurrent readers and writers without blocking. `busy_timeout=10000` means a second writer waits up to 10 seconds instead of returning `SQLITE_BUSY`. In practice this never times out because MCP tool calls complete in milliseconds.

The connection manager:

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

Short-lived connections (open, do work, close) rather than a persistent connection pool. This avoids the WAL checkpoint accumulation problem that appears with long-lived connections under heavy write load.

---

## FTS5 Implementation

The `memory_fts` virtual table covers entity names, types, and all observations concatenated:

```sql
CREATE VIRTUAL TABLE memory_fts USING fts5(
    name, entity_type, observations_text,
    tokenize = "unicode61 remove_diacritics 2"
);
```

FTS sync on every write:

```python
def _fts_sync(conn, entity_id):
    obs_rows = conn.execute(
        "SELECT content FROM observations WHERE entity_id = ? ORDER BY id",
        (entity_id,),
    ).fetchall()
    obs_text = "\n".join(r["content"] for r in obs_rows)
    conn.execute("DELETE FROM memory_fts WHERE rowid = ?", (entity_id,))
    conn.execute(
        "INSERT INTO memory_fts(rowid, name, entity_type, observations_text) "
        "VALUES (?, ?, ?, ?)",
        (entity_id, name, entity_type, obs_text),
    )
```

FTS5 has no `ON CONFLICT` — the DELETE + INSERT pattern ensures idempotent upserts.

Query sanitization to prevent FTS5 syntax errors from user input:

```python
def _fts_query(raw: str) -> str:
    tokens = raw.split()
    escaped = ['"' + t.replace('"', '""') + '"' for t in tokens]
    return " OR ".join(escaped)
```

Each token is double-quoted, then joined with OR. Users get broad matching without needing to know FTS5 query syntax. Advanced users who want phrase search can pass `'"exact phrase"'` directly — the sanitizer only wraps unquoted tokens.

---

## Schema

```sql
CREATE TABLE entities (
    id          INTEGER PRIMARY KEY,
    name        TEXT    UNIQUE NOT NULL,
    entity_type TEXT    NOT NULL,
    project     TEXT    DEFAULT NULL,
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL
);

CREATE TABLE observations (
    id          INTEGER PRIMARY KEY,
    entity_id   INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    content     TEXT    NOT NULL,
    created_at  TEXT    NOT NULL,
    UNIQUE(entity_id, content)
);

CREATE TABLE relations (
    id            INTEGER PRIMARY KEY,
    from_id       INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    to_id         INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    relation_type TEXT    NOT NULL,
    created_at    TEXT    NOT NULL,
    UNIQUE(from_id, to_id, relation_type)
);

CREATE TABLE sessions (
    id           INTEGER PRIMARY KEY,
    session_id   TEXT    UNIQUE NOT NULL,
    project      TEXT    DEFAULT NULL,
    summary      TEXT    DEFAULT NULL,
    active_files TEXT    DEFAULT NULL,
    started_at   TEXT    NOT NULL,
    ended_at     TEXT    DEFAULT NULL
);
```

Deduplication is enforced at the database level via `UNIQUE` constraints + `INSERT OR IGNORE`. The server never has to check for duplicates in application code — the DB handles it.

---

## Logging

```python
LOG_PATH = Path.home() / ".claude" / "memory" / "server.log"
logger = logging.getLogger("sqlite-memory")
logger.setLevel(logging.DEBUG)
_fh = logging.FileHandler(LOG_PATH, encoding="utf-8")
```

All logging goes to file only — stdout/stderr would break MCP stdio transport. Common mistake with MCP servers.

---

## Migration from memory.json

```python
def _migrate_jsonl():
    json_path = Path.home() / ".claude" / "memory" / "memory.json"
    if not json_path.exists():
        return
    # ... parse JSONL, insert into SQLite, rename to memory.json.migrated
```

One-time migration on first run if `memory.json` exists. Handles the official server's format: `{"type": "entity", ...}` and `{"type": "relation", ...}` lines.

---

## Installation

```bash
git clone https://github.com/RMANOV/sqlite-memory-mcp.git
cd sqlite-memory-mcp
pip install fastmcp>=2.0.0
python3 server.py  # test run
```

Dependencies: Python 3.10+, `fastmcp>=2.0.0`, `sqlite3` (stdlib). Single file (`server.py`, ~2,460 lines across 8 files).

---

Questions about the WAL implementation, FTS5 sync strategy, or session tracking schema welcome.
