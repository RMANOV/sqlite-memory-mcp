# Twitter/X Thread

---

**Tweet 1 — Hook**

The official Claude MCP memory server uses a flat JSONL file. Open 2 Claude Code windows and write to it simultaneously. Congratulations, you have a race condition and a corrupted knowledge graph.

I replaced it with SQLite WAL mode.

github.com/RMANOV/sqlite-memory-mcp

---

**Tweet 2 — Problem**

@modelcontextprotocol/server-memory stores everything in memory.json. No file locking. No transactions. Two concurrent sessions write to the same file → data corruption.

This is not a theoretical problem. It's happened to me enough times to write a proper fix.

---

**Tweet 3 — Solution**

sqlite-memory-mcp: SQLite backend, WAL mode, 3 PRAGMAs that solve everything:

```sql
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
PRAGMA busy_timeout=10000;
```

10+ concurrent Claude Code sessions. Same .db file. ACID transactions. No corruption.

#SQLite #MCP #Claude

---

**Tweet 4 — Technical detail**

Also added FTS5 BM25-ranked full-text search. The official server does substring matching on JSONL. This maintains a memory_fts virtual table synced on every write. search_nodes("WAL mode") returns BM25-ranked results, not insertion-order substring matches.

#FTS5 #FullTextSearch

---

**Tweet 5 — Session tracking**

The feature I built this for: session_recall(last_n=3) at the start of every session returns what I was working on, which files were active, a summary I wrote at end of last session.

Context continuity without reading conversation history. Works across restarts.

---

**Tweet 6 — Drop-in**

All 9 tools from the official memory server work identically — same names, same argument shapes. Plus 3 new ones: session_save, session_recall, search_by_project.

Drop into settings.json, it migrates your memory.json automatically. That's it.

---

**Tweet 7 — Stack**

Single Python file (~750 lines). One dependency: fastmcp for the MCP protocol layer. sqlite3 is stdlib.

Backup: cp memory.db memory.db.bak

MIT.

github.com/RMANOV/sqlite-memory-mcp

#Python #MCP #ModelContextProtocol #AI #LLM #Claude #SQLite #AIMemory
