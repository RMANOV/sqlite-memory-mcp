# Show HN: SQLite MCP Memory Server — WAL + FTS5 for AI agent memory

**GitHub:** https://github.com/RMANOV/sqlite-memory-mcp

---

Built a persistent memory server for Claude Code (MCP protocol) that uses SQLite as the storage backend instead of a JSONL file.

**The problem:** Anthropic's official `@modelcontextprotocol/server-memory` stores everything in a flat `memory.json` file. Two concurrent Claude Code sessions write to it with no coordination. File locks on Linux are advisory. Corruption is a matter of when.

**The fix:** SQLite WAL mode. Three PRAGMAs and you get concurrent multi-process reads and writes, ACID transactions, and a busy timeout that makes the second writer wait instead of fail.

```sql
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
PRAGMA busy_timeout=10000;
```

**FTS5 BM25 ranked search** is the other main feature. The official server does substring matching on the JSONL. This server maintains a `memory_fts` virtual table (FTS5, `unicode61` tokenizer, `remove_diacritics 2`) that covers entity names, types, and all observation text. Every write triggers a sync. Results are BM25-ranked.

**Session tracking** is what I built this for personally. `session_save` stores a snapshot (ID, project, summary, active files). `session_recall(last_n=3)` at the start of a new session gives you back what you were working on. Context continuity without reading conversation history.

---

**What it is:**

- Drop-in replacement for `@modelcontextprotocol/server-memory` — all 9 tools, same signatures
- 3 additional tools: `session_save`, `session_recall`, `search_by_project`
- Single Python file (~750 lines), single dependency (`fastmcp` for MCP protocol layer)
- `sqlite3` is stdlib — no binaries, no Docker, no daemon
- Auto-migrates existing `memory.json` on first run

**Schema:** 4 tables (entities, observations, relations, sessions) + 1 FTS5 virtual table. `ON DELETE CASCADE` means deleting an entity cleans up its observations and relations. Deduplication via `UNIQUE` constraints + `INSERT OR IGNORE`.

**Backup:** `cp memory.db memory.db.bak`

---

**Compared to the alternatives:**

The existing SQLite-based MCP memory servers I found (`@pepk/sqlite-mcp-server`, `memory-mcp`) skip WAL mode — so you still get SQLITE_BUSY errors with concurrent sessions. They also skip FTS5, falling back to `LIKE '%query%'` queries that can't rank results.

ChromaDB/Qdrant-backed servers give you vector search but require Docker and a running daemon. For an AI coding assistant's memory layer, that's a lot of infrastructure for what is essentially a key-value store with search.

The cloud-backed options (Mem0, etc.) add latency, API keys, and vendor lock-in to something that should be a local file.

---

**One implementation detail that tripped me up:** FTS5 has no `ON CONFLICT` clause, so upserts need to be explicit DELETE + INSERT. Doing an FTS5 INSERT without first deleting creates duplicate entries in the index that accumulate silently. The `rowid` stays in sync with `entities.id` so the FTS table always has at most one row per entity.

---

Python 3.10+. MIT.

Happy to discuss the WAL checkpoint strategy, FTS5 query sanitization (wrapping user input tokens in double-quotes to avoid syntax errors), or the session schema design.
