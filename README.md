# SQLite Memory MCP Server

## Technical deep-dives

- **Medium:** [The Amnesiac That Learned to Remember](https://medium.com/@r.manov/the-amnesiac-that-learned-to-remember-4fe4342db89d)
- **Dev.to:** [The Amnesiac That Learned to Remember — Building a Brain for Claude Code](https://dev.to/ruslan_manov/the-amnesiac-that-learned-to-remember-building-a-brain-for-claude-code-1ok6)
- **Dev.to:** [How a SQLite WAL Fix Grew into a 54-Tool MCP Memory Stack](https://dev.to/ruslan_manov/how-a-sqlite-wal-fix-grew-into-a-54-tool-mcp-memory-stack-4nkl)

A production-quality SQLite-backed MCP Memory stack with WAL concurrent safety (10+ sessions), FTS5 BM25 search, session tracking, task management, bridge sync, collaboration workflows, and a native system tray task manager.

Drop-in compatible with `@modelcontextprotocol/server-memory` for the core 9 knowledge-graph tools, with 47 additional tools split across companion FastMCP micro-servers for sessions, tasks, bridge sync, collaboration, entity linking, and intelligence workflows (56 OSS tools total). Includes a PyQt6 desktop app for visual task management and standalone automation scripts.

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
- **Premium runtime boundary** -- The OSS core can gate-load a separate private premium repo via signed entitlement checks, signed artifact manifests, signed control-plane policy, explicit owner approval, audit logging, cached revocation-aware policy fallback, and local revocation
- **Drop-in compatible core** -- All 9 tools from `@modelcontextprotocol/server-memory` work identically in `sqlite_memory`, with 47 more tools available from companion servers
- **Zero required dependencies beyond stdlib** -- Only `fastmcp` is required for MCP protocol; `sqlite3` is Python stdlib. Optional `orjson`, `sqlite-vec`, and `sentence-transformers` add speed and semantic search
- **Automatic FTS sync** -- Full-text index stays in sync with every write operation
- **JSONL migration** -- Optionally import existing `memory.json` knowledge graphs on first run

## Premium / Enterprise Boundary

This repository now includes the **public-core boundary** for a separate premium runtime.

What is in this OSS repo:

- entitlement-aware premium loader (`premium_runtime.py`)
- premium audit + revoke tables in the shared schema
- public contract for a separate private premium repo (`premium_contract.py`)
- premium entitlement schema (`docs/premium/entitlement.schema.json`)
- signed artifact manifest schema (`docs/premium/artifact_manifest.schema.json`)
- signed control-plane policy schema (`docs/premium/control_plane_policy.schema.json`)
- a public-safe bootstrap template for the separate private repo (`templates/private_premium_repo/`)

What is **not** in this OSS repo:

- private premium business logic
- private connectors and ingestion code
- customer entitlements
- signing keys
- proprietary ranking / governance rules

**Premium-only capabilities** are for paid, explicitly entitled users only. They are expected to live in a separate private repo and be loaded through the gated runtime only. Typical premium-only modules include:

- password-protected premium views and protected operator scopes for especially sensitive memory surfaces
- ACL / RBAC
- multi-mailbox ingestion
- action snapshots and client history overlays on top of the note/task layer
- canonical facts, provenance digests, and human-approved note promotion
- partner digests and management summaries
- advanced ranking / orchestration
- query templates and task-signal extraction
- governance / audit workflows beyond the OSS baseline
- premium tray/search surfaces for entitled operators, including a parameterized `Custom Design` tab

### Premium-only runtime behavior

Private premium extensions are **not loaded by default**.

The public runtime will only attempt to mount them when all of the following are true:

- a private premium entrypoint is configured
- a valid entitlement is provided
- the private artifact can satisfy the signed manifest / compatibility checks when enabled
- the signed control-plane policy allows the current manifest, entitlement, and protection phase when configured
- local owner approval is present for protected premium features
- the entitlement is not locally revoked

Without a valid entitlement and local approval path, the premium runtime stays off and private extensions are not mounted.

The host runtime can now source all three signed premium documents from a remote issuer/control service as well:

- entitlement via `SQLITE_MEMORY_PREMIUM_ENTITLEMENT_URL`
- artifact manifest via `SQLITE_MEMORY_PREMIUM_ARTIFACT_MANIFEST_URL`
- control policy via `SQLITE_MEMORY_PREMIUM_POLICY_URL`
- optional runtime fetch headers via `SQLITE_MEMORY_PREMIUM_REMOTE_HEADERS_JSON`

### Premium feature packs

The premium layer is not meant to be a vague "enterprise edition". It is structured as a set of **gated operational packs** that sit on top of the OSS memory core.

Entitlements can now be **modular**:

- choose `packs`
- choose explicit `features`
- combine both in one entitlement
- rely on dependency expansion so high-level premium surfaces pull in the lower-level capabilities they need

That means a customer can license one pack, one feature, or a hybrid bundle without forcing the whole private runtime scope on every deployment.

Commercially, numeric pricing is intentionally **not published** in this OSS README.
Serious paid prospects receive a scoped questionnaire first, then a customized offer
that is valid for **7 working days**.

#### 1. `access_governance`

- `acl_rbac`
- `governance_audit`

This is the control plane for customers that need scoped trust, explainable decisions, and audit-safe premium workflows.

#### 2. `communication_context`

- `multi_mailbox_ingestion`
- `cross_mailbox_context`

This pack turns memory into governed communication context instead of passive storage. It is where shared inboxes, thread memory, and client-scoped cross-mailbox views become first-class premium surfaces.

#### 3. `client_memory_twin`

- `client_memory_twin`
- `human_approved_notes`

Dependency expansion also brings in `memory_action_snapshots`, `client_history_notes`, `canonical_facts`, and provenance-aware context. The result is a live client twin built from trusted facts, approved notes, action checkpoints, and surrounding communication state.

#### 4. `briefing_suite`

- `instant_briefing`
- `team_digest`
- `chief_of_staff_queries`

This is the fastest-to-sell premium layer because it removes cold starts before calls, emails, or meetings. It combines ranking, query templates, partner/team digests, and scoped memory retrieval into concise operator briefings.

#### 5. `commitment_radar`

- `commitment_radar`
- `silence_drift_detection`

This pack is about not dropping the ball. It detects commitments, blockers, deadlines, stale threads, and drift before they become visible operational failures.

#### 6. `decision_ledger`

- `decision_ledger`
- `provenance_pointers`

This pack makes premium memory defensible. Important conclusions can be traced back to governance decisions, human-approved promotion, and source-linked provenance instead of vague AI summaries.

#### 7. `custom_design_surface`

- `custom_design_tab`

This is the premium operator UI layer. It lets an entitled user shape a live working view over premium rows, grouping, risk, mailbox/client focus, and custom search/sort surfaces without flattening everything back into the OSS task model.

#### 8. `protected_operator_surface`

- `password_protected_views`

This pack adds local password-gated premium views on top of the Custom Design surface for the highest-sensitivity operator slices, so a premium view can require an explicit per-session unlock before it renders its real rows.

#### Feature-level premium surfaces

On top of the pack structure, the private runtime now exposes concrete premium-only features for the most valuable operator workflows:

- `password_protected_views` for especially sensitive client, governance, or operator-specific surfaces inside the premium tray
- `instant_briefing` for fast pre-call or pre-mail context
- `commitment_radar` for open commitments, deadlines, blockers, and stale follow-ups
- `client_memory_twin` for a scoped memory profile per client
- `decision_ledger` for governance plus provenance-backed review trails
- `chief_of_staff_queries` for questions like `what depends on me`, `what is blocked`, `what changed recently`, and `who is risky`
- `team_digest` for internal handoff and management-style summaries
- `silence_drift_detection` for unanswered threads and slow-moving risk
- `cross_mailbox_context` for unified client context across multiple inboxes

#### High-control deployment surface

The commercial design still assumes that the local machine may be untrusted.

- explicit entitlements
- signed artifact manifests over the private runtime entrypoint
- signed control-plane policy with cached offline fallback
- remote issuer delivery for entitlements / manifests / policy over URL + runtime headers when desired
- local revocation
- owner approval for protected runtime loading
- host/runtime compatibility checks plus minimum protection phase enforcement
- installation fingerprinting for audit correlation
- password-protected premium views for the highest-sensitivity operator surfaces
- separate private runtime packaging
- optional extra service boundaries for the most sensitive premium logic

The point is not obfuscation theater. The point is to keep premium execution gated, auditable, and operationally controllable.

### Current premium runtime scope

The current private premium runtime is no longer limited to the first three pack families. The active bootstrap contract now supports and mounts concrete packs for:

- `acl_rbac`
- `governance_audit`
- `multi_mailbox_ingestion`
- `memory_action_snapshots`
- `client_history_notes`
- `canonical_facts`
- `provenance_pointers`
- `partner_digest`
- `team_digest`
- `advanced_ranking`
- `query_templates`
- `human_approved_notes`
- `task_signal_extraction`
- `instant_briefing`
- `commitment_radar`
- `client_memory_twin`
- `decision_ledger`
- `chief_of_staff_queries`
- `silence_drift_detection`
- `cross_mailbox_context`
- `custom_design_tab`
- `password_protected_views`

That runtime is intentionally separate from the OSS repo. The public repo ships the airlock, contract, tray loader hooks, schema hooks, and bootstrap template. The premium logic itself stays outside the OSS tree.

### Current premium tray/search surface

The current premium-facing UI surface is built around a gated `custom_design_tab` capability plus pack-aware entitlement selection.

- it activates only when the premium runtime is entitled and the private runtime exposes the tray extension builder
- premium rows can enter the same tray search index as OSS tasks and notes when the premium runtime is active
- premium-specific grouping and sorting modes can be injected into the tray at runtime without changing the OSS data model
- the `Custom Design` tab behaves like an operator-defined working view rather than a fixed canned tab
- protected premium views can be configured with a locally stored password hash and unlocked per session inside the premium tray
- tray loading now carries the resolved entitlement selection into the private runtime, so a customer can activate `packs`, explicit `features`, or both without changing the public host code

This remains premium-only functionality. The OSS repo contains the loader path and safe UI hooks, not the private business logic itself.

See:

- [`premium_contract.py`](premium_contract.py)
- [`docs/premium/entitlement.schema.json`](docs/premium/entitlement.schema.json)
- [`docs/premium/private_extension_contract.md`](docs/premium/private_extension_contract.md)
- [`templates/private_premium_repo/`](templates/private_premium_repo/)

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

## Installation

### Quick Start

```bash
# Clone
git clone https://github.com/rmanov/sqlite-memory-mcp.git
cd sqlite-memory-mcp

# Install from source
pip install -e .

# Optional extras
# pip install -e ".[gui,vector,speed]"

# Add the core drop-in server
claude mcp add sqlite_memory python /path/to/server.py

# Add companion servers for the full 56-tool OSS stack
claude mcp add sqlite_tasks python /path/to/task_server.py
claude mcp add sqlite_session python /path/to/session_server.py
claude mcp add sqlite_bridge python /path/to/bridge_server.py
claude mcp add sqlite_collab python /path/to/collab_server.py
claude mcp add sqlite_entity python /path/to/entity_server.py
claude mcp add sqlite_intel python /path/to/intel_server.py

# Optional: run the full stack as one all-in-one server instead
claude mcp add sqlite_unified python /path/to/unified_server.py
```

If you install the package instead of running from a checkout, the same servers are available as console scripts:

```bash
claude mcp add sqlite_memory sqlite-memory-core
claude mcp add sqlite_tasks sqlite-memory-tasks
claude mcp add sqlite_session sqlite-memory-session
claude mcp add sqlite_bridge sqlite-memory-bridge
claude mcp add sqlite_collab sqlite-memory-collab
claude mcp add sqlite_entity sqlite-memory-entity
claude mcp add sqlite_intel sqlite-memory-intel

# Optional all-in-one server
claude mcp add sqlite_unified sqlite-memory-unified
```

### Manual Configuration

Add these server/file pairs to your `~/.claude/settings.json` under `mcpServers`:

| MCP server name | Python entry file | Purpose |
|---|---|---|
| `sqlite_memory` | `server.py` | Core 9 drop-in memory tools |
| `sqlite_tasks` | `task_server.py` | Task CRUD, digest, archive, overdue bump |
| `sqlite_session` | `session_server.py` | Session recall, project search, health, resume |
| `sqlite_bridge` | `bridge_server.py` | Cross-machine bridge sync, sharing review |
| `sqlite_collab` | `collab_server.py` | Collaborator and public-knowledge workflows |
| `sqlite_entity` | `entity_server.py` | Task-entity linking and merge helpers |
| `sqlite_intel` | `intel_server.py` | Context assessment and enrichment tools |
| `sqlite_unified` | `unified_server.py` | Optional all-in-one server that mounts the full 56-tool OSS stack |

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
- `task_server.py`, `session_server.py`, `bridge_server.py`, `collab_server.py`, `entity_server.py`, and `intel_server.py` expose the remaining 41 tools.
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

The 56 OSS tools are grouped by MCP server:

| MCP server | Tool count | Tools |
|---|---:|---|
| `sqlite_memory` | 9 | `create_entities`, `add_observations`, `create_relations`, `delete_entities`, `delete_observations`, `delete_relations`, `read_graph`, `search_nodes`, `open_nodes` |
| `sqlite_session` | 5 | `session_save`, `session_recall`, `search_by_project`, `knowledge_health`, `resume_context` |
| `sqlite_tasks` | 7 | `create_task_or_note`, `update_task`, `query_tasks`, `find_by_title`, `task_digest`, `archive_done_tasks`, `bump_overdue_priority` |
| `sqlite_bridge` | 7 | `bridge_push`, `bridge_pull`, `bridge_status`, `bridge_doctor`, `assign_task`, `review_shared_tasks`, `process_recurring_tasks` |
| `sqlite_collab` | 9 | `manage_collaborators`, `share_knowledge`, `review_shared_knowledge`, `request_publish`, `cancel_publish`, `search_public_knowledge`, `rate_public_knowledge`, `get_knowledge_ratings`, `update_verification` |
| `sqlite_entity` | 7 | `link_task_entity`, `unlink_task_entity`, `get_task_links`, `get_entity_tasks`, `suggest_task_links`, `find_entity_overlaps`, `merge_entities` |
| `sqlite_intel` | 12 | `assess_context`, `queue_clarification`, `record_human_answer`, `extract_candidate_claims`, `promote_candidate`, `build_context_pack`, `explain_impact`, `audit_memory`, `replay_memory`, `govern_fact`, `list_memory_issues`, `enrich_context` |

## Bridge Sync (Cross-Machine)

Share knowledge graph entities between machines (e.g., personal laptop + work computer) via a private git repo.

### How it works

1. Tag entities for sharing by setting `project` to any value starting with `"shared"` (e.g., `"shared"`, `"shared:trading"`, `"shared:hooks"`)
2. `bridge_push()` first runs a bridge repo safety preflight, then exports shared data to `shared.json`, `shared.js`, `index.json`, `tasks/`, and `entities/`, and finally commits and pushes. The v2 payload also includes shared tasks.
3. `bridge_pull()` on the other machine also runs the same repo safety preflight, does `git pull`, and imports new entities/observations/relations. Task metadata comes from `index.json`, while `description` and `notes` are hydrated from per-task files before the LWW merge. Shared knowledge, public knowledge, and imported ratings are accepted only when they stay bound to a known collaborator identity.
4. `bridge_status()` shows what's in sync vs only-local vs only-remote

Auto-sync only overwrites bridge-generated artifacts (`shared.json`, `index.json`, `tasks/`, `entities/`, `public_knowledge/`, `shared.js`). If the bridge repo contains user-managed dirty files such as `index.html`, or if generated artifacts were replaced with symlinks/escaped paths, sync now blocks instead of discarding or following them.

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
- **Background bridge sync ownership at tray-app level** -- DB watchers, periodic pull, recurring maintenance, and purge no longer depend on opening the full window
- **Auto-refresh** every 30 seconds when visible
- **Window geometry** persisted via QSettings

```bash
# Install PyQt6 (one-time)
pip install PyQt6

# Run
python3 task_tray.py

# Bridge health / recovery smoke
python3 bin/bridge_ops.py doctor
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
