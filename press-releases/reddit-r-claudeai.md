# [Tool] SQLite Memory MCP Server — WAL + FTS5, drop-in replacement for official MCP memory with actual concurrent session support

**GitHub:** https://github.com/RMANOV/sqlite-memory-mcp

---

## The MEMORY.md / memory.json file-lock problem

If you use Claude Code's official `@modelcontextprotocol/server-memory` (or the MEMORY.md pattern) across multiple terminal windows, you've probably hit this: two sessions open, one corrupts the other's writes, or you get stale reads because both are writing to the same JSONL file with no coordination.

The official memory server stores everything in `memory.json` — a flat file. Python's file writes aren't atomic across processes. On Linux, file locks are advisory. With 2+ concurrent Claude Code sessions, you're playing corruption roulette.

The MEMORY.md approach (markdown file, append writes) has the same problem plus worse search — you're grepping plaintext, not querying an index.

---

## What I built

A drop-in replacement that uses SQLite with WAL mode instead of a JSONL file.

**All 9 tools from the official memory server work identically** — same tool names, same argument shapes, same response format. If your prompts say "use the memory server to store X", they'll keep working without modification.

Plus 3 new tools:

- `session_save` — snapshot a session (ID, project, summary, active files)
- `session_recall` — recall the last N sessions at the start of a new one
- `search_by_project` — FTS5 search scoped to a specific project

---

## Why SQLite instead of the alternatives

| | sqlite-memory-mcp | Official MCP memory | Mem0/cloud | ChromaDB/Qdrant |
|---|---|---|---|---|
| Concurrent sessions | WAL mode (10+) | file locks (breaks at 2+) | cloud latency | Docker required |
| Search | FTS5 BM25 ranked | substring match | vector | vector |
| Setup | pip install fastmcp | npx | API key + billing | Docker + config |
| Data locality | single .db file | JSONL file | cloud | local but Docker |
| Migration | auto from memory.json | N/A | manual | manual |

SQLite's WAL mode gives you concurrent reads + writes to the same file from multiple processes. No locking issues. ACID transactions. The `busy_timeout` pragma makes the second writer wait politely instead of failing.

---

## The concurrent safety part (how WAL actually works here)

```sql
PRAGMA journal_mode=WAL;      -- multiple readers + writers, no blocking
PRAGMA foreign_keys=ON;       -- CASCADE deletes work correctly
PRAGMA busy_timeout=10000;    -- wait 10s before SQLITE_BUSY instead of failing
```

With these three PRAGMAs, 10+ Claude Code sessions can read and write `memory.db` simultaneously. Each session spawns its own `server.py` process via stdio — they all connect to the same file. WAL mode ensures they don't step on each other.

---

## Setup

```bash
git clone https://github.com/RMANOV/sqlite-memory-mcp.git
pip install fastmcp
```

Add to `~/.claude/settings.json`:

```json
"mcpServers": {
  "sqlite_memory": {
    "command": "python3",
    "args": ["/home/youruser/sqlite-memory-mcp/server.py"],
    "env": {
      "SQLITE_MEMORY_DB": "/home/youruser/.claude/memory/memory.db"
    }
  }
}
```

If you have an existing `memory.json`, it auto-migrates on first run and renames the old file to `memory.json.migrated`.

---

## FTS5 search

The `search_nodes` tool uses SQLite's built-in FTS5 engine with BM25 ranking. It searches across entity names, entity types, and the full text of all observations:

```
search_nodes("WAL mode")          # phrase search
search_nodes("sqlite OR postgres") # boolean OR
search_nodes("bug*")               # prefix matching
search_nodes("entity_type:BugFix") # column-specific
```

Results are ranked by relevance, not insertion order. Substring matching returns a flat list; BM25 returns the most relevant hits first.

---

## Session tracking

The session tools are the part I use most. At the start of every Claude Code session:

```
session_recall(last_n=3)
```

Returns what I was working on, which files were active, and a summary I wrote at the end of the previous session. Context continuity without reading through conversation history.

At session end:

```
session_save(
  session_id="2026-03-01-evening",
  project="my-project",
  summary="Fixed the FTS sync bug. BM25 ranking now correct for multi-word queries.",
  active_files=["server.py", "README.md"]
)
```

---

## Python, no external dependencies

Single file (`server.py`, ~750 lines). Only dependency is `fastmcp` for the MCP protocol layer. `sqlite3` is Python stdlib — no additional binaries, no system packages, no Docker.

Backup: `cp memory.db memory.db.bak`. That's it.

MIT license.

---

Happy to answer questions about the WAL implementation or the FTS5 query sanitization if anyone's curious.
