# Usage: Install, Configure, and Core Tools

A concise, self-contained guide to getting `sqlite-memory-mcp` running and
wiring it into Claude Code or Codex. For background and design rationale, see
the project `README.md`.

`sqlite-memory-mcp` is a local-first, SQLite-backed memory layer for coding
agents. All data lives in one SQLite database file with WAL-mode concurrency
and FTS5 full-text search. There is no server to host and no external service
to sign up for.

---

## 1. Install

```bash
git clone https://github.com/RMANOV/sqlite-memory-mcp.git
cd sqlite-memory-mcp
python -m venv .venv
source .venv/bin/activate

# Core install (MCP servers + CLI)
pip install -e .

# Optional extras:
#   gui     -> PyQt6 Task Tray desktop app
#   vector  -> sqlite-vec + sentence-transformers (hybrid semantic search)
#   speed   -> optional performance dependencies
#   dev     -> test/lint tooling
# pip install -e ".[gui,vector,speed,dev]"
```

### Verify the install

```bash
# Checks Python, FastMCP, SQLite schema, DB write access, and (optionally)
# whether Claude Code / Codex can list the local MCP servers.
sqlite-memory-doctor --db /tmp/sqlite-memory-mcp-demo.db \
  --check-gui --check-claude-mcp --check-codex-mcp

# Seed a throwaway demo DB (does NOT touch your real memory.db):
sqlite-memory-demo --db /tmp/sqlite-memory-mcp-demo.db --reset
```

If the doctor is clean, the install is healthy enough to connect to an agent.

---

## 2. Configure (wire into your agent)

The project ships as a set of focused MCP micro-servers that share one database.
The split exists because some agent clients expose only a limited number of
tools per MCP server, so the surface is divided into themed servers you can
enable independently.

### Claude Code

Add the core server (drop-in compatible with `@modelcontextprotocol/server-memory`):

```bash
claude mcp add --scope user sqlite_memory -- sqlite-memory-core
```

Add the companion servers you want:

```bash
claude mcp add --scope user sqlite_tasks   -- sqlite-memory-tasks
claude mcp add --scope user sqlite_session -- sqlite-memory-session
claude mcp add --scope user sqlite_bridge  -- sqlite-memory-bridge
claude mcp add --scope user sqlite_collab  -- sqlite-memory-collab
claude mcp add --scope user sqlite_entity  -- sqlite-memory-entity
claude mcp add --scope user sqlite_intel   -- sqlite-memory-intel
```

Verify with `claude mcp list`.

If you are running from a source checkout rather than an installed package,
replace the console script with `python /path/to/<server>.py` (for example
`-- python /path/to/server.py`).

### Codex

```bash
codex mcp add sqlite_memory  -- sqlite-memory-core
codex mcp add sqlite_tasks   -- sqlite-memory-tasks
codex mcp add sqlite_session -- sqlite-memory-session
codex mcp add sqlite_bridge  -- sqlite-memory-bridge
codex mcp add sqlite_collab  -- sqlite-memory-collab
codex mcp add sqlite_entity  -- sqlite-memory-entity
codex mcp add sqlite_intel   -- sqlite-memory-intel
```

Verify with `codex mcp list`. Codex stores its config in
`~/.codex/config.toml`; Claude Code uses its own config, so configuring one
client does not configure the other.

### Shared environment

Every server should see the same database path:

```json
"env": {
  "SQLITE_MEMORY_DB": "/home/user/.claude/memory/memory.db",
  "BRIDGE_REPO": "/home/user/.claude/memory/bridge"
}
```

- `SQLITE_MEMORY_DB` — where the database lives. Defaults to
  `~/.claude/memory/memory.db` if unset.
- `BRIDGE_REPO` — only needed for cross-machine bridge / collaboration flows.

### One-server option

If your client tolerates a larger tool surface, you can run everything as a
single all-in-one server instead of the split above:

```bash
claude mcp add --scope user sqlite_unified -- sqlite-memory-unified
# or
codex mcp add sqlite_unified -- sqlite-memory-unified
```

---

## 3. Core tool groups

Tools are grouped by MCP server. See the README Tool Reference for the
per-server tool list. This section describes what each group is *for* and how
mature it is. For the canonical core-path vs advanced-path operator map and the
frozen external-claim set, see
[`docs/ops/CORE_VS_ADVANCED_PATH.md`](ops/CORE_VS_ADVANCED_PATH.md). The labels
below describe posture and emphasis only; nothing here is removed, deprecated,
or disabled.

### Stable / core path (start here)

| Server | What it does |
|---|---|
| `sqlite_memory` (`server.py`) | The 9 core knowledge-graph tools: create/read/delete entities, observations, and relations; `read_graph`, `search_nodes`, `open_nodes`. Drop-in compatible with the official MCP memory server. |
| `sqlite_tasks` (`task_server.py`) | Task and note CRUD, querying with sort, digests, archiving done tasks, overdue priority bumping, idempotent note upsert. |
| `sqlite_session` (`session_server.py`) | Session save/recall, project search, resume context, knowledge health. |

These three cover the everyday memory loop — remember entities and facts,
track tasks and notes, and recall context across sessions. They are the most
exercised and the safest place to begin.

The `sqlite_bridge` server is a special case: while it lives in the advanced
table below, it is a **core operational-resilience path** whenever you sync
across more than one machine — health / conflict / recovery discipline rather
than an optional extra.

### Advanced / optional (opt in as needed)

| Server | What it does | Notes |
|---|---|---|
| `sqlite_bridge` (`bridge_server.py`) | Cross-machine sync over a private git repo: push/pull, task assignment, shared-task review, bridge self-checks. | **Core resilience spine when multi-machine** — conflict / recovery discipline against no-resurrect / no-data-loss failures (not an absolute no-data-loss guarantee). Requires `BRIDGE_REPO` and a one-time setup per machine. |
| `sqlite_collab` (`collab_server.py`) | P2P knowledge sharing, public-knowledge search, ratings, verification, publish requests. | **Advanced / optional** shared-knowledge surface, for multi-user / shared-knowledge scenarios. |
| `sqlite_entity` (`entity_server.py`) | Task-entity linking, overlap detection, entity merging. | Entity hygiene; useful once your graph is large enough to need de-duplication. |
| `sqlite_intel` (`intel_server.py`) | Intelligence layer: context assessment/enrichment, claim extraction and promotion, fact governance, memory audit, reflect/consolidation, and the multi-agent **Debate Protocol**. | **Governance / audit plus power-user** surface, not required for baseline memory. The largest and most advanced surface; the debate tools coordinate multiple agents — treat them as power-user features. See `docs/DEBATE_PROTOCOL.md` and `docs/ops/DEBATE_OPERATIONS.md`. |

### Search

Search defaults to SQLite FTS5 BM25 ranking over entity names, types, and
observations — available with the core install, no extra setup.

If you install the `vector` extra, search additionally fuses sqlite-vec vector
results with the BM25 ranking via Reciprocal Rank Fusion (RRF). The vector
backend is **optional, with FTS5 fallback**: without the extra, search
transparently falls back to pure FTS5; nothing breaks. Vector search is not the
product center or a required baseline.

```bash
# FTS5 term / phrase / boolean queries work out of the box via search_nodes.
# Hybrid BM25 + vector ranking activates automatically when the vector extra
# is installed and the embedding model is available.
```

---

## 4. Where to go next

- `README.md` — full feature overview, architecture, schema, and the canonical
  Tool Reference.
- `docs/ops/CORE_VS_ADVANCED_PATH.md` — canonical core-path vs advanced-path
  operator map and the frozen external-claim set.
- `docs/DEBATE_PROTOCOL.md` — multi-agent debate protocol design.
- `docs/ops/BRIDGE_OPERATIONS.md` — running cross-machine bridge sync.
- `examples/basic_usage.md` — a worked end-to-end example.
- `bin/task` — a dead-simple CLI for task management against the same DB.
