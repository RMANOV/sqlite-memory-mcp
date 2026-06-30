# Core path vs advanced path — operator claim map

This is the canonical operator map for what is on the everyday **core path** of
`sqlite-memory-mcp`, what is on the **advanced / optional path**, and which
claims are in or out of bounds for external-facing material. It exists so that
documentation wording stays consistent and so that public claims about this
project stay matched to what is actually shipped, dogfooded, and test-backed.

This is a documentation / claim-hygiene map. It does **not** remove, deprecate,
disable, or set a sunset date for any surface. Every server, tool, and feature
named below remains present and supported. Labels here describe *posture and
emphasis*, not a product cut.

## Posture (one line)

Local-first, single-operator, civilian-dogfooded, test-backed; application-enforced
append-only governance. No external customers and no external deployment are claimed.

## Core path (load-bearing, start here)

These are the surfaces a first-time user and the everyday memory loop depend on:

- **`sqlite_memory`** — the 9 drop-in knowledge-graph tools (entities,
  observations, relations, `read_graph`, `search_nodes`, `open_nodes`).
- **`sqlite_tasks`** — task / note CRUD, typed queries, digests, archiving,
  overdue bumping, idempotent note upsert.
- **`sqlite_session`** — session save / recall, project search, resume context,
  knowledge health.
- **Provenance / knowledge-links** — memory mutations carry provenance, and
  candidate claims move to canonical facts through an approval-aware promotion
  gate instead of silent rewrites.
- **Bridge resilience** — cross-machine sync treated as the operational
  resilience spine: health / conflict / recovery discipline (see the bounded
  claim below).
- **Reflect / audit discipline** — `reflect_audit` is a scheduled,
  deterministic SQL audit loop in the provenance / audit spine; it surfaces
  consolidation candidates for per-row operator review.
- **Addressed debate routing** — when used, multi-agent coordination is bounded
  to addressed messages, role-aware cursors / watermarks, and `no_action`
  completion.
- **Entity hygiene** — de-duplication / merge with an audit trail.

## Advanced / optional path (opt in as needed)

These surfaces remain fully present and supported, but are not required for the
baseline memory loop and are not part of the first-time-user hero:

- **`sqlite_collab` / P2P** — *advanced / optional* shared-knowledge surface
  (publish requests, public-knowledge search, ratings, verification) for
  multi-user scenarios.
- **Premium / airlock** — an *optional operator / private-runtime boundary*.
  This is an **operator boundary, excluded from external-facing (DIANA) feature
  claims** unless a diligence question specifically asks about extension
  governance. See [`PREMIUM_BOUNDARY.md`](PREMIUM_BOUNDARY.md).
- **Vector semantic backend** — an *optional backend with FTS5 fallback*. With
  the `vector` extra, search fuses sqlite-vec results with BM25 via Reciprocal
  Rank Fusion; without it, search transparently falls back to pure FTS5 and
  nothing breaks. Vector search is not the product center and not a required
  baseline.
- **Advanced intel / debate operations** — the larger `sqlite_intel` surface
  (context assessment / enrichment, claim governance, the multi-agent debate
  tools) are power-user / operator features layered on top of the core memory
  loop.

## Design notes that are NOT change targets

- **The 7-microserver split is current MCP-visibility / ergonomics design.** It
  exists because some agent clients expose only a limited number of tools per
  MCP server. It is **not** a consolidation target under this documentation
  wave; no Tool Reference rows, server counts, or tool counts change here.
- **Memora-style retrieval handles are a future representation contract, not a
  current shipped external claim.** The project may describe the architecture as
  compatible with primary abstractions, cue anchors, and retrieval traces, but
  must not imply that this layer is already implemented or benchmarked until it
  ships and is verified.

## Bounded claim — bridge resilience

Bridge resilience is stated as **conflict / recovery discipline against
no-resurrect / no-data-loss failures**: covered conflict / recovery regressions,
health checks, and operator recovery drills. It is **not** an absolute,
impossible-to-lose-data guarantee.

## External-facing (DIANA) allowed claims

The following are the claims this project is willing to make in external /
diligence material:

- Local-first, governed cross-agent memory; WAL-backed shared SQLite memory that
  multiple MCP clients can read and write.
- Provenance / knowledge-links and approval-aware promotion from candidate
  claims to canonical facts.
- Audit gates: the deterministic `reflect_audit` findings loop.
- Addressed debate routing bounded to addressed messages, cursors, `no_action`,
  role watermarks, and audit logs.
- Hybrid retrieval with an FTS5 baseline and an optional vector backend
  (FTS5 fallback when the vector extra is absent).
- Entity hygiene / merge with an audit trail.
- Bridge cross-machine resilience as conflict / recovery discipline against
  no-resurrect / no-data-loss failures (bounded as above, not an absolute
  guarantee).

Framing for all of the above: **application-enforced append-only governance**,
local-first, civilian-dogfooded, single-operator, test-backed. Do not frame the
project as a generic long-horizon memory retriever competing with Microsoft
Memora; frame it as governed attention, evidence, provenance, and context
routing over local ledgers.

## External-facing (DIANA) forbidden claims

The following must never appear in external-facing material:

- No defence validation / accreditation / certification.
- No "immutable" / WORM / tamper-evident ledger claim (append-only is
  application-enforced only); no hash-chain claim until it actually ships.
- No shipped STRIX ↔ `sqlite_memory` integration (validation-ahead, not built).
- No edge-drone / on-hardware deployment claim.
- No premium / airlock as a named external (DIANA) feature.
- No absolute no-data-loss guarantee (bridge resilience is bounded as above).
- No vector search as the product center or a required baseline.
- No generic "better AI memory than Memora/Mem0/Zep" claim.
- No shipped primary-abstraction / cue-anchor / retrieval-trace layer claim
  until the corresponding schema, tools, tests, and documentation are actually
  implemented.
- No unbounded "production" claim — only production-quality, single-operator,
  with no external customers or deployment.

## D2 triggers (out of scope for this documentation wave)

Anything below is **not** a documentation change. It is a code / structural
change that requires a separate D2 inventory / RFC and explicit operator
approval; it must never ride along with a docs-only edit:

- Editing any server file or `@mcp.tool` registration, or changing tool /
  server counts, or consolidating the micro-servers.
- Changing `pyproject.toml` console scripts / extras / dependencies.
- Changing `schema.py`, `db_utils.py`, migrations, or any SQL schema.
- Disabling collab / P2P, premium, vector, debate, reflect, or bridge in code.
- Renaming / removing / moving any file, tool, server, public API symbol, or
  test.
- Changing `task_tray.py` or CLI runtime behavior.

## See also

- [`../../README.md`](../../README.md) — feature overview and the canonical
  Tool Reference.
- [`../USAGE.md`](../USAGE.md) — install, configure, and core vs advanced tool
  groups.
- [`BRIDGE_OPERATIONS.md`](BRIDGE_OPERATIONS.md) — bridge as resilience spine.
- [`../DEBATE_PROTOCOL.md`](../DEBATE_PROTOCOL.md) and
  [`DEBATE_OPERATIONS.md`](DEBATE_OPERATIONS.md) — governed multi-agent
  coordination.
- [`../REFLECT_AUDIT_DEMO.md`](../REFLECT_AUDIT_DEMO.md) — the scheduled audit
  loop.
- [`PREMIUM_BOUNDARY.md`](PREMIUM_BOUNDARY.md) — operator / private-runtime
  boundary.
