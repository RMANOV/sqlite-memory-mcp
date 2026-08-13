# SQLite Memory MCP Server

## Governed cross-agent memory for coding agents

Claude and Codex can share one provenance-rich knowledge graph with approval-aware promotion workflows.

- **Hybrid retrieval** — BM25/FTS5 keyword search fused with optional semantic (sqlite-vec) results via Reciprocal Rank Fusion, so recall does not depend on exact keywords.
- **Provenance + reviewable promotion** — memory mutations carry provenance, and candidate claims move to canonical facts through an approval-aware promotion gate (`human_confirmed`, plus policy-gated multi-evidence) instead of silent rewrites.
- **Cross-agent MCP memory with bridge sync** — one local SQLite knowledge graph any MCP client can read and write, with bridge tools that sync shared entities across machines.

[![CI](https://github.com/RMANOV/sqlite-memory-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/RMANOV/sqlite-memory-mcp/actions/workflows/ci.yml)

It is a production-quality, local-first MCP memory stack: a single SQLite file under WAL concurrency (10+ sessions), FTS5 BM25 search, session tracking, task management, bridge sync, collaboration workflows, and a native system-tray task manager. The core 9 knowledge-graph tools are drop-in compatible with `@modelcontextprotocol/server-memory`; companion FastMCP micro-servers add more tools for sessions, tasks, bridge sync, collaboration, entity linking, and intelligence/multi-agent workflows. A PyQt6 desktop app and standalone automation scripts ship alongside. See the [Tool Reference](#tool-reference) for the exact per-server tool counts.

### Technical deep-dives

- **Medium:** [The Amnesiac That Learned to Remember](https://medium.com/@r.manov/the-amnesiac-that-learned-to-remember-4fe4342db89d)
- **Dev.to:** [The Amnesiac That Learned to Remember — Building a Brain for Claude Code](https://dev.to/ruslan_manov/the-amnesiac-that-learned-to-remember-building-a-brain-for-claude-code-1ok6)
- **Dev.to:** [How a SQLite WAL Fix Grew into a 54-Tool MCP Memory Stack](https://dev.to/ruslan_manov/how-a-sqlite-wal-fix-grew-into-a-54-tool-mcp-memory-stack-4nkl)

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
- **Hybrid search (BM25 + semantic)** -- FTS5 keyword search fused with optional sqlite-vec cosine similarity via Reciprocal Rank Fusion, then re-ranked with 6 contextual signals (recency, project affinity, graph proximity, observation richness, canonical facts, active session)
- **Session tracking** -- Save and recall session snapshots for context continuity across restarts
- **Task management** -- Structured task CRUD with typed queries, priorities, sections, due dates, and recurring tasks
- **Kanban board** -- Optional HTML report generator for visual task overview via GitHub Pages
- **Cross-project sharing** -- Optional `project` field scopes entities; omit it to share across all projects
- **Cross-machine sync** -- Bridge tools push/pull shared entities between machines via a private git repo
- **Provenance + approval-aware promotion** -- Mutations carry provenance; candidate claims promote to canonical facts through a review gate (`human_confirmed` / policy-gated multi-evidence). See [Advanced & operator topics](#advanced--operator-topics)
- **Drop-in compatible core** -- All 9 tools from `@modelcontextprotocol/server-memory` work identically in `sqlite_memory`, with many more tools available from companion servers (see [Tool Reference](#tool-reference) for exact per-server counts)
- **Zero required dependencies beyond stdlib** -- Only `fastmcp` is required for MCP protocol; `sqlite3` is Python stdlib. Optional `orjson`, `sqlite-vec`, and `sentence-transformers` add speed and semantic search
- **Automatic FTS sync** -- Full-text index stays in sync with every write operation
- **JSONL migration** -- Optionally import existing `memory.json` knowledge graphs on first run

## FAQ: how is this different from sqlite.ai / sqlite-vector?

[sqlite.ai](https://www.sqlite.ai/) is adjacent, not identical. It is a broader
SQLite platform around cloud sync, extensions, AI inference, vector search,
agent memory, and MCP tooling. Its related projects include
[sqlite-memory](https://github.com/sqliteai/sqlite-memory), a Markdown-based
agent memory system, and [sqlite-vector](https://github.com/sqliteai/sqlite-vector),
a vector-search extension for embedded SQLite workloads.

sqlite-memory-mcp is focused on **local-first MCP memory governance for coding
agents**, not on vector search as the center of the product:

- WAL-backed task, session, entity, and note memory in one local SQLite file
- FTS5-first retrieval, with vector search as an optional backend
- cross-machine bridge sync for private multi-machine workflows
- event/provenance tracking for memory mutations
- reviewable consolidation instead of silent memory rewriting
- debate/protocol workflows for conductor, executor, and devil's advocate agents
- an explicit OSS/premium runtime boundary with signed entitlement, manifest,
  and policy checks

`sqlite-vec` is therefore not the product center; it is one possible local
retrieval backend. If `sqlite-vector` proves better for this workload, it can
become a candidate backend. The harder problem this project targets is memory
governance: how agents remember, revise, sync, debate, and promote durable
context without turning the memory store into an unreviewable pile of
contradictions.

## Advanced & operator topics

The features above are the core. The capabilities below are deliberately kept out of the hero because they matter to operators, not first-time users. Each links to its canonical document.

### Core path vs advanced path

This section is the public summary of what is on the everyday **core path**
versus the **advanced / optional path**, plus the allowed / forbidden
external-claim set and the D2 (code-change) triggers.

- **Core path (load-bearing, start here):** `sqlite_memory`, `sqlite_tasks`,
  `sqlite_session`; provenance / knowledge-links; bridge resilience; reflect /
  audit discipline; addressed debate routing; entity hygiene.
- **Advanced / optional path (opt in as needed):** `sqlite_collab` / P2P
  (advanced / optional shared-knowledge surface); premium / airlock (an
  operator / private-runtime boundary, excluded from external-facing feature
  claims); the vector semantic backend (an optional backend with FTS5 fallback);
  and the advanced `sqlite_intel` / debate operations.

These labels describe posture and emphasis. Nothing here is removed, deprecated,
disabled, or scheduled for removal; every server and tool below stays present
and supported. The **7-microserver split is current MCP-visibility / ergonomics
design, not a consolidation target** — the [Tool Reference](#tool-reference) and
its server / tool counts are unchanged.

### Intelligence v2 — claims, governance, and provenance

The `sqlite_intel` server turns raw memory into reviewable knowledge. It extracts candidate claims, queues clarifications, records human answers, and promotes claims to canonical facts through an approval-aware gate (`promote_candidate`: `human_confirmed` always allowed; `multi_evidence` is policy-gated; sensitive scopes require explicit human confirmation). Every mutation can carry a provenance link, and `audit_memory` / `replay_memory` make the history inspectable. Consolidation runs through `reflect_audit` (Phase 0.5) — deterministic SQL with no LLM cost per run. See [`docs/REFLECT_AUDIT_DEMO.md`](docs/REFLECT_AUDIT_DEMO.md).

### Debate / multi-agent protocol

For workflows that coordinate multiple agents (conductor, executor, devil's advocate) across sessions, the `sqlite_intel` debate tools provide a single per-topic channel with role-aware watermarks, claim/reclaim, and escalation. This is an advanced coordination layer, not required for memory use. See [`docs/DEBATE_PROTOCOL.md`](docs/DEBATE_PROTOCOL.md) and [`docs/ops/DEBATE_OPERATIONS.md`](docs/ops/DEBATE_OPERATIONS.md).

### Optional private-runtime boundary

This OSS repo includes a public contract for separately configured private extensions. It defines entitlement, artifact-manifest, control-policy, audit, revoke, and bootstrap surfaces, but does not include private business logic, private entitlements, signing keys, proprietary connectors, or private ranking/governance rules. Private extensions are not loaded by default; they require an explicit configured entrypoint, local owner approval, and the relevant signed entitlement / manifest / policy checks.

Operator wiring and the public contract are documented in:

- [`docs/ops/PREMIUM_BOUNDARY.md`](docs/ops/PREMIUM_BOUNDARY.md) — operator wiring and verification
- [`docs/ops/RELEASE_CONFIDENCE.md`](docs/ops/RELEASE_CONFIDENCE.md) — `v3.7.2` release-quality checklist
- [`premium_contract.py`](premium_contract.py) — public contract for the private repo
- [`docs/premium/entitlement.schema.json`](docs/premium/entitlement.schema.json) — entitlement schema
- [`docs/premium/private_extension_contract.md`](docs/premium/private_extension_contract.md) — private extension contract
- [`templates/private_premium_repo/`](templates/private_premium_repo/) — public-safe bootstrap template

Pricing is intentionally not published here; serious prospects receive a scoped questionnaire, then a customized offer.

### External claim boundary (frozen claim-set)

For external / diligence material, the project commits to the bounded
claim-set summarized here.

**Allowed:** local-first governed cross-agent memory; provenance /
knowledge-links and approval-aware promotion; the deterministic `reflect_audit`
audit gate; addressed debate routing bounded to addressed messages, cursors,
`no_action`, role watermarks, and audit logs; hybrid retrieval with an FTS5
baseline and an optional vector backend (FTS5 fallback); entity hygiene / merge
with an audit trail; and bridge cross-machine resilience as conflict / recovery
discipline against no-resurrect / no-data-loss failures. Posture: application-
enforced append-only governance, local-first, civilian-dogfooded,
single-operator, test-backed.

**Forbidden:** defence validation / accreditation / certification; "immutable" /
WORM / tamper-evident or hash-chain claims (until shipped); shipped
STRIX ↔ `sqlite_memory` integration; edge / on-hardware deployment; premium /
airlock as a named external feature; an absolute no-data-loss guarantee; vector
search as the product center or a required baseline; and unbounded "production"
claims (only production-quality, single-operator, no external customers or
deployment).

## Competitor Comparison

| Feature | sqlite-memory-mcp | Official MCP Memory | claude-mem0 | @pepk/sqlite | simple-memory | mcp-memory-service | memsearch | memory-mcp | MemoryGraph |
|---|---|---|---|---|---|---|---|---|---|
| Storage | SQLite | JSONL file | Mem0 Cloud | SQLite | JSON file | ChromaDB | Qdrant | SQLite | Neo4j |
| Concurrent 10+ sessions | WAL mode | file locks | cloud | no WAL | file locks | yes | yes | no | yes |
| Hybrid search (BM25 + vector) | yes (RRF fusion) | substring | no | no | no | vector only | vector only | no | Cypher only |
| Session tracking | built-in | no | no | no | no | no | no | no | no |
| Task management | built-in | no | no | no | no | no | no | no | no |
| Cross-project sharing | project field | no | no | no | no | no | no | no | no |
| Drop-in compatible | 9/9 tools | baseline | no | partial | no | no | no | partial | no |
| Setup effort | pip install | npx | API key + pip | pip | npx | Docker + pip | Docker + pip | pip | Docker + Neo4j |
| Dependencies | sqlite3 (stdlib) | Node.js | Cloud API | sqlite3 | Node.js | ChromaDB | Qdrant | sqlite3 | Neo4j |

### Where this sits in the ecosystem

- **Beads.** sqlite-memory-mcp can sit beside [Beads](https://github.com/steveyegge/beads). Beads is an issue/work-tracking layer for agents; sqlite-memory-mcp is a governed memory layer. There is no shipped Beads adapter — the `ready_context` tool offers a `ready`/`prime` work surface that is the cross-project/cross-machine analog of `bd ready` / `bd prime`, so the two can coexist in the same workflow.
- **Codex Memories.** OpenAI's Codex has its own memory feature, and the "agent memory" category is gaining mindshare fast. sqlite-memory-mcp is not pitched as a 1:1 replacement; it targets a different point in the design space — a local-first, multi-agent, provenance-governed knowledge graph that any MCP client can share, rather than a single-agent built-in. The category risk is real, which is precisely why the governance and cross-agent surface matter.

## Convergent evolution: sqlite-memory-mcp vs GBrain

[GBrain](https://github.com/garrytan/gbrain) — Garry Tan's structured knowledge layer for AI agents — launched **2026-04-10**. It and sqlite-memory-mcp arrived independently at the same architectural conclusions: local-first storage, hybrid lexical + vector search fused via Reciprocal Rank Fusion, rule-based zero-LLM entity extraction, and a memory-consolidation cycle (GBrain calls it *dream*, sqlite-memory-mcp calls it *reflect*). When two solo founders converge on the same architecture, the design space is real.

The two projects ship **different bets** for different deployments. Public git history establishes that sqlite-memory-mcp's hybrid search shipped on **2026-03-18** (commit [`feat(search): add hybrid semantic search via sqlite-vec + RRF fusion`](https://github.com/RMANOV/sqlite-memory-mcp/commits/main/vec_search.py)) — twenty-three days before GBrain's first public release.

| Axis | GBrain | sqlite-memory-mcp |
|---|---|---|
| Initial public release | 2026-04-10 | **2026-03-01** (v0.1.0, 40-day lead) |
| Hybrid search (BM25 + vector + RRF) | shipped 2026-04-10 | **shipped 2026-03-18** (23-day lead) |
| Storage primitive | Markdown files in git + PGLite (embedded Postgres) + pgvector | Single SQLite file (FTS5 + sqlite-vec) + bridge git repo |
| Infrastructure footprint | Postgres runtime + git remote + LLM API | Single binary, single file, optional local embeddings |
| Embeddings | OpenAI API (network call per page write) | sentence-transformers, fully local |
| Memory consolidation | "dream cycle" (uses LLM) | `reflect_audit` Phase 0.5 — **deterministic SQL, no LLM cost** |
| Per-candidate review | atomic store-level output | per-row accept / reject / defer with apply snapshots |
| Cross-machine sync | git remote of the brain repo | bridge JSON + per-field LWW-Register CRDT; conflict/recovery regressions and operational discipline address no-resurrect / no-data-loss failure modes without claiming an absolute data-loss guarantee |
| Source of truth | Markdown (human-readable) | SQLite + JSON bridge exports (machine-portable) |
| Air-gapped / regulated deployment | blocked by OpenAI embedding requirement | **fully supported** (no external network in hot path) |
| Companion stack | GStack (Garry's Claude Code setup) | MCP-native, works with any MCP client (Claude Code, Codex) |

Where each one wins:

- **GBrain** is right for teams that want a markdown-first knowledge base, are happy paying for OpenAI embeddings on every page write, and benefit from Garry Tan's distribution. The forthcoming hosted [`gbrain.io`](https://gbrain.io) targets teams that don't want to run their own runtime.
- **sqlite-memory-mcp** is right for solo developers, privacy-first / offline / embedded deployments, regulated environments where data cannot reach OpenAI (DoD, healthcare, finance), and anyone who needs the consolidation pipeline to run on a Raspberry Pi or inside an air-gapped network. The deterministic Phase 0.5 audit produces real candidate counts with **zero LLM cost per run**.

This is convergent validation, not derivative work. The architecture is decided; the markets diverge.

## Installation

### Two-minute install + demo

Use this path when you want to verify the install before wiring Claude Code:

```bash
git clone https://github.com/RMANOV/sqlite-memory-mcp.git
cd sqlite-memory-mcp
python -m venv .venv
source .venv/bin/activate
pip install -e ".[gui,dev]"

# Verify Python, FastMCP, SQLite schema, DB write access, and optionally
# whether Claude Code and Codex list the local sqlite MCP servers.
sqlite-memory-doctor --db /tmp/sqlite-memory-mcp-demo.db --check-gui --check-claude-mcp --check-codex-mcp

# Seed a safe demo DB with one entity, one task, one note, a reminder,
# and a recurring schedule. This does not touch your real memory.db.
sqlite-memory-demo --db /tmp/sqlite-memory-mcp-demo.db --reset

# Optional desktop demo against the demo DB.
SQLITE_MEMORY_DB=/tmp/sqlite-memory-mcp-demo.db task-tray
```

If `sqlite-memory-doctor` is clean and the tray opens the demo DB, the local
install is healthy enough to connect to Claude Code.

### Claude Code quick start

```bash
# Clone
git clone https://github.com/rmanov/sqlite-memory-mcp.git
cd sqlite-memory-mcp

# Install from source
pip install -e .

# Optional extras
# pip install -e ".[gui,vector,speed]"

# Add the core drop-in server
claude mcp add --scope user sqlite_memory -- python /path/to/server.py

# Add companion servers for the full OSS tool stack
claude mcp add --scope user sqlite_tasks -- python /path/to/task_server.py
claude mcp add --scope user sqlite_session -- python /path/to/session_server.py
claude mcp add --scope user sqlite_bridge -- python /path/to/bridge_server.py
claude mcp add --scope user sqlite_collab -- python /path/to/collab_server.py
claude mcp add --scope user sqlite_entity -- python /path/to/entity_server.py
claude mcp add --scope user sqlite_intel -- python /path/to/intel_server.py

# Optional: run the full stack as one all-in-one server instead
claude mcp add --scope user sqlite_unified -- python /path/to/unified_server.py
```

If you install the package instead of running from a checkout, the same servers are available as console scripts:

```bash
claude mcp add --scope user sqlite_memory -- sqlite-memory-core
claude mcp add --scope user sqlite_tasks -- sqlite-memory-tasks
claude mcp add --scope user sqlite_session -- sqlite-memory-session
claude mcp add --scope user sqlite_bridge -- sqlite-memory-bridge
claude mcp add --scope user sqlite_collab -- sqlite-memory-collab
claude mcp add --scope user sqlite_entity -- sqlite-memory-entity
claude mcp add --scope user sqlite_intel -- sqlite-memory-intel

# Optional all-in-one server
claude mcp add --scope user sqlite_unified -- sqlite-memory-unified
```

Codex can use the same console-script servers:

```bash
codex mcp add sqlite_memory -- sqlite-memory-core
codex mcp add sqlite_tasks -- sqlite-memory-tasks
codex mcp add sqlite_session -- sqlite-memory-session
codex mcp add sqlite_bridge -- sqlite-memory-bridge
codex mcp add sqlite_collab -- sqlite-memory-collab
codex mcp add sqlite_entity -- sqlite-memory-entity
codex mcp add sqlite_intel -- sqlite-memory-intel

# Optional all-in-one server
codex mcp add sqlite_unified -- sqlite-memory-unified
```

### Manual Configuration

Prefer `claude mcp add --scope user ...` above and verify with
`claude mcp list`; prefer `codex mcp add ...` and verify with
`codex mcp list` for Codex. Some Claude Code builds no longer surface legacy
`~/.claude/settings.json` `mcpServers` entries in `claude mcp list`, and Codex
uses its own `~/.codex/config.toml`, so one client's manual block does not
prove the other client can load the servers.

If you need a manual fallback, add these server/file pairs to your
`~/.claude/settings.json` under `mcpServers`:

| MCP server name | Python entry file | Purpose |
|---|---|---|
| `sqlite_memory` | `server.py` | Core 9 drop-in memory tools |
| `sqlite_tasks` | `task_server.py` | Task CRUD, digest, archive, overdue bump |
| `sqlite_session` | `session_server.py` | Session recall, project search, health, resume |
| `sqlite_bridge` | `bridge_server.py` | Cross-machine bridge sync, sharing review |
| `sqlite_collab` | `collab_server.py` | Collaborator and public-knowledge workflows |
| `sqlite_entity` | `entity_server.py` | Task-entity linking and merge helpers |
| `sqlite_intel` | `intel_server.py` | Context assessment and enrichment tools |
| `sqlite_unified` | `unified_server.py` | Optional all-in-one server that mounts the full OSS tool stack |

Each server should share the same environment values:

```json
"env": {
  "SQLITE_MEMORY_DB": "/home/user/.claude/memory/memory.db",
  "BRIDGE_REPO": "/home/user/.claude/memory/bridge"
}
```

The `SQLITE_MEMORY_DB` environment variable controls where the database is stored. If omitted, it defaults to `~/.claude/memory/memory.db`. `BRIDGE_REPO` is only needed for bridge/collab flows.

## Architecture

The system is intentionally split into micro-servers because Claude Code exposes only a limited number of tools per MCP server.

- `server.py` exposes the 9 drop-in knowledge-graph tools.
- `task_server.py`, `session_server.py`, `bridge_server.py`, `collab_server.py`, `entity_server.py`, and `intel_server.py` expose the remaining tools; see the [Tool Reference](#tool-reference) for the exact per-server breakdown.
- All MCP servers, the Task Tray GUI, and the automation scripts share the same `memory.db`.
- `db_utils.py` and `schema.py` are the shared source of truth for connections, migrations, and common helpers.
- SQLite WAL mode handles concurrency across all of these processes.

## Schema

The core schema includes the tables below, plus additional tables for task field-version tracking, bridge sync metadata, collaborators, public knowledge review, context packing, ratings, and entity/task links:

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

Tools are exposed as `@mcp.tool()` endpoints grouped by MCP server. The counts below are the exact number of tools registered in each server file (reproduce with `grep -c '@mcp.tool(' <server>.py`):

| MCP server | Tool count | Tools |
|---|---:|---|
| `sqlite_memory` | 9 | `create_entities`, `add_observations`, `create_relations`, `delete_entities`, `delete_observations`, `delete_relations`, `read_graph`, `search_nodes`, `open_nodes` |
| `sqlite_session` | 5 | `session_save`, `session_recall`, `search_by_project`, `knowledge_health`, `resume_context` |
| `sqlite_tasks` | 9 | `create_task_or_note`, `upsert_note_by_title_project`, `update_task`, `query_tasks`, `find_by_title`, `task_digest`, `archive_done_tasks`, `bump_overdue_priority`, `ready_context` |
| `sqlite_bridge` | 7 | `bridge_push`, `bridge_pull`, `bridge_status`, `bridge_doctor`, `assign_task`, `review_shared_tasks`, `process_recurring_tasks` |
| `sqlite_collab` | 9 | `manage_collaborators`, `share_knowledge`, `review_shared_knowledge`, `request_publish`, `cancel_publish`, `search_public_knowledge`, `rate_public_knowledge`, `get_knowledge_ratings`, `update_verification` |
| `sqlite_entity` | 7 | `link_task_entity`, `unlink_task_entity`, `get_task_links`, `get_entity_tasks`, `suggest_task_links`, `find_entity_overlaps`, `merge_entities` |
| `sqlite_intel` | 46 | **14 intelligence / governance:** `assess_context`, `queue_clarification`, `record_human_answer`, `extract_candidate_claims`, `promote_candidate`, `build_context_pack`, `explain_impact`, `audit_memory`, `replay_memory`, `govern_fact`, `list_memory_issues`, `enrich_context`, `export_to_gbrain`, `import_from_gbrain`. **+ 32 reflect / debate** multi-agent coordination tools (`reflect_*`, `debate_*`) — see [Advanced & operator topics](#advanced--operator-topics) |

**Total: 92 tools** across the seven micro-servers (9 + 5 + 9 + 7 + 9 + 7 + 46). The optional `sqlite_unified` all-in-one server mounts the same set rather than adding new tools.

## Bridge Sync (Cross-Machine)

Share knowledge graph entities between machines (e.g., personal laptop + work computer) via a private git repo.

### How it works

1. Tag entities for sharing by setting `project` to any value starting with `"shared"` (e.g., `"shared"`, `"shared:trading"`, `"shared:hooks"`)
2. `bridge_push()` first runs a bridge repo safety preflight, then exports shared data to `shared.json`, `index.json`, `tasks/`, and `entities/`, and finally commits and pushes. The v2 payload also includes shared tasks. (`shared.js` — a `window.__BRIDGE_DATA__` mirror of `shared.json` — was derived, never public API, and had no consumer; it is no longer generated or pushed, and a stale tracked copy is untracked automatically.)
3. `bridge_pull()` on the other machine also runs the same repo safety preflight, does `git pull`, and imports new entities/observations/relations. Task metadata comes from `index.json`, while `description` and `notes` are hydrated from per-task files before the LWW merge. Shared knowledge, public knowledge, and imported ratings are accepted only when they stay bound to a known collaborator identity.
4. `bridge_status()` shows what's in sync vs only-local vs only-remote

Auto-sync only overwrites bridge-generated artifacts (`shared.json`, `index.json`, `tasks/`, `entities/`, `public_knowledge/`, plus legacy `shared.js` leftovers). If the bridge repo contains user-managed dirty files such as `index.html`, or if generated artifacts were replaced with symlinks/escaped paths, sync now blocks instead of discarding or following them.

If `index.json` is missing or unreadable while `shared.json` still carries tasks, the sync fails closed: the legacy shared.json fallback has no tombstone manifest, so merging it could resurrect deletions made on other peers. The pull reports `legacy_fallback_blocked` and a full sync refuses to push.

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

Add `BRIDGE_REPO` to the MCP servers that participate in sharing (`sqlite_bridge`, `sqlite_collab`, and usually the rest of the stack so they all see the same paths):

```json
"sqlite_bridge": {
  "command": "python",
  "args": ["/path/to/bridge_server.py"],
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

Session tracking lives on the `sqlite_session` MCP server and enables context continuity across Claude Code restarts.

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

Structured task tracking lives on the `sqlite_tasks` MCP server. No external service required.

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
create_task_or_note(
    title="Review pull request #42",
    section="today",
    priority="high",
    due_date="2026-03-05",
    project="sqlite-memory-mcp"
)

# Query pending tasks for today
query_tasks(section="today", status="not_started")

# Idempotently save or update a research/decision note by title + project
upsert_note_by_title_project(
    title="2026-05-04 | sqlite-memory-mcp | MCP research triangulation",
    project="sqlite-memory-mcp",
    description="Main long-form note body..."
)

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
parent = create_task_or_note(title="Implement feature X")
# parent returns {"task_id": "<parent-uuid>", ...}

create_task_or_note(
    title="Write tests for feature X",
    parent_id="<parent-uuid>"
)
```

Query subtasks with `query_tasks(parent_id="<parent-uuid>")`.

### Recurring tasks

Pass a JSON recurrence config in the `recurring` field:

```python
create_task_or_note(
    title="Weekly review",
    section="today",
    recurring='{"every": "week", "day": "monday"}'
)
```

The automation script `recurring_tasks.py` reads this field and recreates tasks on schedule.

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

## Task Tray (Desktop App)

`task_tray.py` is a native PyQt6 system tray application for visual task management:

- **System tray icon** with overdue badge counter
- **Compact popup** (left-click) -- Today + Overdue tasks, checkbox toggle, quick-add
- **Full window** (right-click > Open Full Window) -- tabbed view with Today / Inbox / Next / All
- **Create/edit dialog** -- task/note type, status, section, priority, due date, reminder, recurring schedule, project, notes, and attachments in one pass
- **Background bridge sync ownership at tray-app level** -- DB watchers, periodic pull, recurring maintenance, and purge no longer depend on opening the full window
- **Auto-refresh** every 30 seconds when visible
- **Window geometry** persisted via QSettings

```bash
# Install PyQt6 (one-time)
pip install PyQt6

# Run
task-tray

# Bridge health / recovery smoke
python3 bin/bridge_ops.py doctor
python3 bin/bridge_ops.py refresh-hooks
python3 bin/bridge_ops.py smoke
```

The tray app reads/writes directly to `memory.db` via `db_utils.py`, so changes are immediately visible in Claude Code sessions and vice versa.

### Shared Module -- `db_utils.py`

All Python files share constants and helpers via `db_utils.py`:

```python
from db_utils import (
    DB_PATH, BRIDGE_REPO,
    TASK_SECTIONS, TASK_PRIORITIES, TASK_STATUSES,
    PRIORITY_RANK, PRIORITY_COLORS,
    get_conn, now_iso, parse_iso_date, is_overdue,
    build_priority_order_sql, priority_sort_key,
)
```

This eliminates duplication of DB connection setup, task constants, and timestamp helpers across `server.py`, `task_tray.py`, and the utility scripts.

## License

MIT License. See [LICENSE](LICENSE) for details.
