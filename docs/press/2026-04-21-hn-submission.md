# Hacker News submission — 2026-04-21

## Title (one line, under 80 chars)

Show HN: sqlite-memory-mcp – local-first MCP memory with a gated premium runtime

## Body (target: ~200 words, technical gaps for discussion)

sqlite-memory-mcp is a local-first memory server for MCP-speaking agents
(Claude Code, Codex, etc.) backed by SQLite with WAL, FTS5, an optional
sqlite-vec hybrid search layer, and a cross-machine bridge sync.

v3.5.0 adds something I have not seen cleanly separated elsewhere: a premium
runtime boundary that lives inside the public-core repo. The OSS side ships
the airlock — entitlement contract, signed-entitlement loader, gate audit
table, revocation table, and a boot hook that mounts a private extension only
after the gate has ruled. The actual premium logic (password-protected
operator views, client briefing surface, commitment radar) lives in a separate
private runtime behind that gate.

The result is that trust lives where the code is visible. Revocations are
honored at every gate check without restarting the server. Every decision
writes a row.

I am curious what HN thinks about two things specifically:

1. Putting the entitlement gate in the public-core repo instead of in the
   private runtime. Net positive for auditability, or a bad idea for other
   reasons I am missing?
2. Using SQLite + WAL + FTS5 as the base, with sqlite-vec for hybrid search,
   instead of DuckDB or a dedicated vector DB for agent memory.

Repo: https://github.com/RMANOV/sqlite-memory-mcp
Tag: https://github.com/RMANOV/sqlite-memory-mcp/releases/tag/v3.5.0
