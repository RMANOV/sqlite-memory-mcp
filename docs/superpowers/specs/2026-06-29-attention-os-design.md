# sqlite_memory Attention OS Design

Date: 2026-06-29
Status: design spec, not implementation
Owner: operator / sqlite-memory-mcp
Target review date: 2026-07-04

## 1. Thesis

`sqlite_memory` should stop treating enrichment as "make text smarter" and
become an attention operating system over provable ledgers.

Operational definition:

```text
sqlite_memory is provable coordination memory.
It turns raw events into actor-specific attention packets.
```

It is not a memory database. It is append-only ledgers plus a compiler plus an
attention router plus delivery surfaces. The core shift is from memory storage
to memory dispatch.

The product problem is not only that agents need memory. The deeper problem is
that a human operator and a team of agents have different bandwidth, different
roles, different surfaces, and different tolerance for repetition. The memory
system must therefore decide who should see which delta, at what resolution, on
which surface, and with what expected next action.

Canonical architecture:

```text
Raw Ledgers
  debate_messages
  memory_events
  task_field_versions
  source_events
        ↓
Memory Compiler
  typed units, evidence, contradictions, resolutions
        ↓
Attention Router
  actor delta + surface delta + timing decision
        ↓
Delivery
  terminal feed
  task tray dashboard tabs
  Claude/Codex context packs
```

The debate protocol remains a deterministic append-only coordination ledger.
The compiler and router are asynchronous projections over ledgers. They must be
rebuildable from raw history and must never become the source of truth.

Strict component boundary:

- Compiler: extract, normalize, and propose typed candidate units from raw
  ledgers.
- Router: route, surface, packetize, throttle, and coalesce units into
  actor/surface-specific packets.
- Governance: accept, reject, supersede, or confirm candidate units and packet
  actions.

The compiler does not decide delivery. The router does not mutate raw ledgers.
Governance does not erase history.

## 2. Market Research Summary

The market already has strong adjacent products:

- Mem0: universal memory layer for agents and apps, active open-source repo,
  Claude/Codex plugin surfaces, memory compression, read/write audit claims.
- Supermemory: context cloud, memory router, personal app, connectors, user
  profiles, graph memory, Codex/Claude plugins, and self-host/enterprise claims.
- Cognee: open-source agent memory platform with graph/vector/relational memory,
  MCP, Claude Code/Codex integrations, local/cloud deployment.
- Zep/Graphiti: temporal knowledge graph for agent memory, bi-temporal tracking,
  episodic/semantic/community graph structure.
- Letta/MemGPT: agent memory as context engineering with core memory blocks,
  recall memory, archival memory, and sleep-time memory agents.
- LangGraph/LangChain: durable execution, checkpointers, stores,
  human-in-the-loop interrupts, time travel, and resumable graph state.
- HITL/governance products such as Ledge, StackAI, Kiteworks, Galileo, and
  Streamkap cover approval workflows, decision traces, audit trails, delegation
  chains, and compliance evidence.

Conclusion: the broad "agent memory" category is already crowded. The sharper
unclaimed wedge is not another memory store. It is local, reviewable,
ledger-backed attention routing for a human-agent team: debate/work coordination
plus durable memory plus actor-specific deltas plus human delivery surfaces.

Positioning:

```text
Not: persistent memory for agents.
Better: attention OS for human-agent teams.
Best: verifiable delegation and context routing over local ledgers.
```

Research sources checked:

- Mem0: `https://mem0.ai/` and `https://github.com/mem0ai/mem0`.
- Supermemory: `https://supermemory.ai/` and
  `https://github.com/supermemoryai/supermemory`.
- Cognee: `https://www.cognee.ai/` and
  `https://github.com/topoteretes/cognee`.
- Zep/Graphiti: `https://github.com/getzep/graphiti` and
  `https://www.youtube.com/watch?v=NBZGieN8S6E`.
- Letta/MemGPT: `https://www.letta.com/blog/agent-memory/` and
  `https://docs.letta.com/guides/core-concepts/memory/archival-memory/`.
- LangGraph: `https://docs.langchain.com/oss/python/langgraph/persistence`
  and
  `https://aws.amazon.com/blogs/database/build-durable-ai-agents-with-langgraph-and-amazon-dynamodb/`.
- HITL/governance/audit trail references:
  `https://www.ledge.co/content/how-human-in-the-loop-oversight-works-in-ledge`,
  `https://www.stackai.com/insights/human-in-the-loop-ai-agents-how-to-design-approval-workflows-for-safe-and-scalable-automation`,
  `https://www.kiteworks.com/regulatory-compliance/human-in-the-loop-ai-compliance/`,
  `https://galileo.ai/blog/ai-agent-compliance-governance-audit-trails-risk-management`,
  `https://streamkap.com/resources-and-guides/decision-traces-ai-agents`.

## 3. Non-Goals

- Do not merge `debate_messages` into enrichment tables.
- Do not put LLM calls in the debate hot path.
- Do not make dashboard summaries authoritative without drill-back.
- Do not replace existing task, event, and debate ledgers.
- Do not start with GraphRAG or clustering as the core value.
- Do not ship a generic memory platform claim before the attention-routing
  distinction is working.

## 4. Core Invariants

Ledger invariants:

1. Raw ledgers are append-only.
2. Raw ledgers are never overwritten by compiler or router output.
3. Every derived memory unit has evidence refs.
4. Every context packet can be traced back to memory units and raw events.
5. Supersede never deletes history.
6. Compiler output is rebuildable from ledgers.
7. Router output is rebuildable from `memory_units` plus `actor_surface_state`.
8. No LLM call is required in the hot path.
9. Projections are read-only with respect to raw ledgers.
10. Wrong memory is corrected by supersession, not deletion.

Delivery invariants:

1. Terminal shows temporal deltas only.
2. Dashboard shows durable state.
3. Agents receive context packets, not raw memory dumps.
4. HUMAN receives decision prompts only when action is needed.
5. `seen`, `read`, `understood`, `accepted`, and `acted` are separate states.
6. The same memory unit can route differently to different actors.
7. Raw transcript appears only through explicit drill-back.

Governance invariants:

1. LLM-derived durable units start as `proposed` or `provisional`.
2. `policy_memory` and `decision_memory` require human confirmation unless
   sourced from an explicit human command.
3. `trap_memory` requires evidence.
4. Standing instructions are versioned.
5. Human correction supersedes affected future packets.

## 5. Existing Assets To Preserve

- `debate_messages`, `debate_watermarks`, debate lifecycle, compactions, and
  role-aware cursors.
- `memory_events` as mutation/audit history.
- `task_field_versions` as task-level evidence and replay source.
- `tasks.description` as long-form durable body.
- Hybrid retrieval and context packing where useful.
- Bridge sync behavior; DB changes should flow through existing sync mechanisms.
- Reviewable consolidation primitives: candidate extraction, promotion, fact
  governance, session save/recall.

Legacy `lazy_enrichment` should be demoted to lint/fallback. It should not be
the semantic authority.

## 6. Memory Unit Model

The compiler emits typed memory units. A unit is a projection, not raw truth.
Each unit must carry evidence and rebuild lineage.

Required unit types:

- `temporal_delta`: short-lived change that matters now.
- `working_state`: current task/topic owner, blocker, status, next action.
- `semantic_memory`: stable people, projects, terms, preferences, concepts.
- `episodic_memory`: what happened, when, and in what context.
- `procedural_memory`: verified workflow, runbook, command pattern.
- `prospective_memory`: future action, deadline, reminder, blocker.
- `policy_memory`: standing instruction, prohibition, preference, gate.
- `evidence_memory`: source, receipt, hash, raw reference, citation.
- `decision_memory`: decision, options considered, reason, effect.
- `trap_memory`: error, repeated failure, "do not do this again".
- `open_question`: unresolved question needing an actor.
- `receipt`: verifiable delegation/action record.

V1 required types:

- `temporal_delta`;
- `working_state`;
- `decision_memory`;
- `policy_memory`;
- `evidence_memory`;
- `prospective_memory`;
- `trap_memory`.

V1.5/V2 types:

- `semantic_memory`;
- `episodic_memory`;
- `procedural_memory`.

The V1 boundary is intentional. Semantic, episodic, and procedural extraction
can become a hallucination factory if it lands before governance, evidence, and
ack/supersede semantics are stable.

Promotion rules:

- `temporal_delta`, `working_state`, `evidence_memory`, and explicit-task
  `prospective_memory` may become active automatically when evidence is strong.
- `semantic_memory`, `episodic_memory`, `procedural_memory`, and `trap_memory`
  are provisional by default.
- `policy_memory`, `decision_memory`, standing instructions, high-risk
  `trap_memory`, people facts, and legal/financial/reputation claims require
  explicit human confirmation unless sourced from an explicit human command.

## 7. Proposed Tables

### `source_events`

Append-only ingestion ledger for non-task/non-debate inputs.

Fields:

- `source_event_id` primary key.
- `source_type`: `chat`, `gmail`, `file`, `web`, `calendar`, `clickup`,
  `manual`, `system`, `other`.
- `source_ref`: URI, file path, Gmail id, thread id, or external id.
- `event_kind`: `message`, `document`, `decision`, `instruction`,
  `observation`, `artifact`, `research_source`, `correction`.
- `actor`: originator or importer.
- `occurred_at`, `ingested_at`.
- `payload_hash`.
- `payload_json`.
- `raw_ref`.

### `memory_compile_runs`

Records compiler passes and makes projections replayable.

Fields:

- `run_id` primary key.
- `compiler_version`.
- `scope_kind`: `global`, `task`, `debate`, `project`, `actor`.
- `scope_id`.
- `started_at`, `finished_at`.
- `input_watermark_json`.
- `output_epoch`.
- `status`: `dry_run`, `completed`, `failed`, `reverted`.
- `stats_json`, `error_json`.

### `memory_units`

Typed durable projection.

Fields:

- `unit_id` primary key.
- `unit_type`.
- `status`: `candidate`, `active`, `superseded`, `rejected`, `expired`.
- `title`.
- `body_l1`: one-line summary.
- `body_l2`: short paragraph.
- `body_l3`: detailed summary.
- `body_l4_ref`: raw/long body ref when too large for row.
- `project`.
- `scope_kind`, `scope_id`.
- `valid_from`, `valid_to`.
- `importance`: 0-100.
- `confidence`: 0-1.
- `sensitivity`: `low`, `medium`, `high`, `private`.
- `created_by_run_id`.
- `supersedes_unit_id`.
- `created_at`, `updated_at`.

### `memory_unit_evidence`

Connects units to raw ledgers.

Fields:

- `evidence_id` primary key.
- `unit_id`.
- `evidence_kind`: `debate_msg`, `memory_event`, `task_field_version`,
  `source_event`, `file_span`, `email_msg`, `web_source`.
- `evidence_ref`.
- `quote_or_span`.
- `payload_hash`.
- `evidence_role`: `source`, `support`, `contradiction`, `supersession`,
  `approval`, `rejection`.
- `created_at`.

### `memory_edges`

Typed graph edges between units.

Fields:

- `edge_id` primary key.
- `from_unit_id`.
- `relation_type`: `supports`, `contradicts`, `supersedes`, `depends_on`,
  `blocks`, `implements`, `belongs_to`, `answers`, `asks`, `caused_by`,
  `same_as`, `derived_from`.
- `to_unit_id`.
- `valid_from`, `valid_to`.
- `confidence`.
- `evidence_id`.

### `actor_surface_state`

Tracks what each actor/surface has seen or acknowledged.

Fields:

- `state_id` primary key.
- `actor_id`: `HUMAN`, `CONDUCTOR`, `ADVOCATE`, `CODEX`, `CLAUDE`,
  `EXECUTOR_*`.
- `surface`: `terminal`, `tray_dashboard`, `agent_context`, `email_digest`,
  `mcp_tool`.
- `scope_kind`, `scope_id`.
- `last_seen_event_ref`.
- `last_seen_memory_epoch`.
- `last_delivered_packet_id`.
- `default_resolution`: `L1`, `L2`, `L3`, `L4`.
- `notification_threshold`.
- `updated_at`.

### `context_packets`

The atomic delivery object for human/agent surfaces.

Fields:

- `packet_id` primary key.
- `actor_id`.
- `surface`.
- `scope_kind`, `scope_id`.
- `priority`: `P0`..`P7` or `INFO`.
- `resolution`: `L1`..`L4`.
- `packet_kind`: `terminal_delta`, `dashboard_row`, `agent_context`,
  `decision_brief`, `digest`, `receipt`.
- `summary_l1`.
- `payload_json`.
- `action_required` boolean.
- `decision_needed` boolean.
- `ttl_until`.
- `created_by_run_id`.
- `created_at`, `delivered_at`.
- `ack_state`: `pending`, `seen`, `read`, `ack`, `acted`, `dismissed`,
  `expired`.

`ack_state` is the coarse packet state. Detailed acknowledgements live in the
append-only `ack_events` ledger.

### `context_packet_items`

Maps packets back to units/evidence.

Fields:

- `packet_id`.
- `unit_id`.
- `evidence_id`.
- `item_role`: `primary`, `context`, `risk`, `policy`, `next_action`,
  `drillback`.
- `sort_order`.

### `ack_events`

Explicit human/agent response to packets.

Fields:

- `ack_id` primary key.
- `packet_id`.
- `actor_id`.
- `ack_type`: `seen`, `read`, `understood`, `accepted`, `disagree`,
  `needs_later`, `corrected`, `delegated`, `acted`, `dismissed`, `snoozed`,
  `included_in_context`, `used_in_output`, `relied_upon`, `contradicted`,
  `supersede_request`.
- `ack_payload_json`.
- `created_at`.

Rules:

- `seen` is not `read`.
- `read` is not `understood`.
- `understood` is not `accepted`.
- `accepted` is not `acted`.
- Mechanical acknowledgement must not promote a policy, decision, or high-risk
  memory unit without evidence and the applicable promotion rule.

### `attention_rules`

Configurable routing rules.

Fields:

- `rule_id`.
- `enabled`.
- `trigger_kind`.
- `source_filter_json`.
- `target_actor`.
- `target_surface`.
- `min_priority`.
- `resolution`.
- `ttl_seconds`.
- `coalesce_key_template`.
- `created_at`, `updated_at`.

## 8. Tool Contracts

### `memory_capture_event`

Writes to `source_events`.

Input:

```json
{
  "source_type": "gmail|file|web|manual|...",
  "source_ref": "...",
  "event_kind": "instruction|document|correction|...",
  "actor": "HUMAN|CODEX|...",
  "payload": {},
  "raw_ref": ""
}
```

Output: `{ "source_event_id": "...", "payload_hash": "..." }`.

### `memory_compile_pending`

Runs compiler over new ledger tail.

Input:

```json
{
  "scope_kind": "global|task|debate|project|actor",
  "scope_id": "",
  "dry_run": true,
  "limit": 100
}
```

Output: run id, output epoch, candidate counts, active/superseded counts.

### `memory_review_candidates`

Returns compiler outputs requiring approval.

Input: scope, unit types, limit.

Output: candidate units with evidence and proposed action.

### `memory_decide_candidate`

Approve, reject, edit, or supersede a candidate.

Input:

```json
{
  "unit_id": "...",
  "decision": "approve|reject|edit|supersede",
  "edited_fields": {},
  "reason": "..."
}
```

### `attention_build_packet`

Builds one packet for one actor/surface.

Input:

```json
{
  "actor_id": "HUMAN",
  "surface": "terminal",
  "scope_kind": "debate",
  "scope_id": "...",
  "since": "actor_surface_state",
  "resolution": "L1"
}
```

Output: `context_packet`.

### `attention_next_terminal_delta`

Returns the next bounded terminal feed item.

Rules:

- Only current temporal deltas.
- No raw transcript.
- Default max 3 items per burst.
- Default line length cap: 120 characters per item summary.
- Must include `what_changed`, `why_it_matters`, `next_action`,
  `drillback_refs`.

### `attention_dashboard_state`

Returns durable Task Tray tab state.

Sections:

- active decisions;
- active blockers;
- policy/standing instructions;
- durable OODA summaries;
- receipts/evidence;
- stale/expired items;
- quiet important items.

### `agent_context_pack`

Builds role-specific context for Claude/Codex/ADVOCATE/EXECUTOR.

Input:

```json
{
  "actor_id": "CODEX",
  "role": "executor|advocate|conductor|planner",
  "task_id": "",
  "topic_id": "",
  "budget_tokens": 8000,
  "resolution": "L2"
}
```

Required sections:

- `TASK`;
- `CURRENT_STATE`;
- `RECENT_DELTA`;
- `APPLICABLE_POLICIES`;
- `PROCEDURES`;
- `EVIDENCE`;
- `RISKS_TRAPS`;
- `OPEN_QUESTIONS`;
- `STOP_CONDITIONS`;
- `NEXT_ACTION`.

### `attention_ack_packet`

Records read/ack/action state.

Input:

```json
{
  "packet_id": "...",
  "actor_id": "HUMAN",
  "ack_type": "seen|read|ack|acted|dismissed|correction",
  "payload": {}
}
```

### `memory_explain_unit`

Drill-back tool.

Input: `unit_id`, desired resolution.

Output: unit, provenance chain, source refs, supersession history.

### `memory_supersede_unit`

Human or compiler correction path.

Input: old unit id, replacement content, evidence/correction reason.

Output: old status `superseded`, new unit id, edge `supersedes`.

V1 public tool priority:

1. `attention_next_terminal_delta` / `build_human_terminal_delta`;
2. `attention_dashboard_state` / `build_task_tray_dashboard_delta`;
3. `agent_context_pack` / `build_agent_context_pack`;
4. `attention_ack_packet` / `ack_context_packet`;
5. `memory_explain_unit` / `drill_back`;
6. `memory_supersede_unit` / `supersede_memory_unit`.

Names may be normalized during implementation, but the contracts must preserve
these six capabilities before graph, embedding, or semantic-clustering work
enters the critical path.

## 9. Routing Rules

Default triggers:

- H/Q debate message addressed to HUMAN:
  - terminal immediate;
  - dashboard decision row;
  - requires ack.
- Low-value STATUS:
  - coalesce;
  - no terminal unless stale, high risk, or addressed to operator.
- COMPACTION:
  - dashboard durable OODA row;
  - agent bootstrap point.
- Task due/overdue/blocker:
  - terminal if due today or blocking active work;
  - dashboard otherwise.
- Standing instruction:
  - policy memory;
  - future agent context injection.
- Human correction:
  - supersede affected units;
  - push delta to impacted actors.
- New evidence/source:
  - evidence memory;
  - no terminal unless decision/risk changes.
- Closed debate/topic:
  - decision or OODA memory unit;
  - receipt.

V1 attention score:

```text
attention_score =
    novelty
  + urgency
  + risk
  + actor_relevance
  + decision_required
  + blocker_weight
  - already_seen_penalty
  - already_ack_penalty
  - surface_noise_penalty
  - stale_penalty
```

Signal meaning:

- `novelty`: new relative to actor/surface state.
- `urgency`: deadline or active-today pressure.
- `risk`: safety, legal, reputation, financial, or operational risk.
- `actor_relevance`: the actor owns, blocks, reviews, or must decide.
- `decision_required`: human or role choice required.
- `blocker_weight`: active blocker for a task/debate.
- `already_seen_penalty`: shown before.
- `already_ack_penalty`: accepted/acted before.
- `surface_noise_penalty`: useful elsewhere but wrong for this surface.
- `stale_penalty`: TTL value already decayed.

## 10. Surface Contracts

### Terminal

Purpose: immediate temporal orientation.

Contract:

- L1/L2 only by default.
- Default L1.
- Max 3 items per burst.
- Default line length cap: 120 characters per item summary.
- TTL required.
- Must not store durable truth.
- Must not show transcript dumps.
- Must show action/decision state first.
- If an item has no action, risk, blocker, decision, TTL, or temporal delta, it
  does not enter the terminal feed.

### Task Tray Dashboard

Purpose: durable operator control surface.

Tabs:

- `Now`: active decisions/blockers.
- `Memory`: durable units by type.
- `Policies`: standing instructions and traps.
- `Receipts`: action/delegation/evidence ledger.
- `Debate`: projections only, with drill-back.
- `Sync`: health and bridge status.

Rows must include evidence refs and packet/unit ids.

### Agent Context

Purpose: bounded role-specific pack.

Contract:

- Role-specific sections.
- No generic memory dump.
- Include only deltas since actor state plus durable facts needed for the task.
- Include stop conditions and policies.
- Include evidence refs when decisions or outbound actions are involved.

## 11. Rollout Plan

### Phase 0: Freeze the boundary

- Document that debate remains immutable/no-LLM hot path.
- Mark `lazy_enrichment` as legacy/fallback.
- Add no schema changes yet.

### Phase 1: Split enrichment into compiler and router

- Replace "enrichment" as the primary design word with compiler/router/packet
  language.
- Compiler reads ledger tails and proposes memory units.
- Router reads memory units and actor/surface state, then builds packets.
- Governance promotes, rejects, or supersedes candidates.

### Phase 2: Add read-only schema and dry-run compiler

- Add tables above behind feature flag.
- Compiler reads existing ledgers and emits candidate units.
- No delivery yet.
- Compare compiler output against current enrichment output.

### Phase 3: Human terminal delta

- Implement `attention_next_terminal_delta`.
- Source only active tasks/debates and recent memory units.
- Add ack state.
- Acceptance: no repeated giant summaries after ack.

### Phase 4: Task Tray dashboard tab

- Add durable tabs and dashboard projections.
- Show drill-back refs.
- Acceptance: terminal remains quiet while dashboard keeps durable state.

### Phase 5: Agent context packs

- Implement `agent_context_pack` for CODEX, CLAUDE, ADVOCATE, CONDUCTOR.
- Acceptance: same task yields different role packs.

### Phase 6: Supersession and correction

- Add correction flow and `memory_supersede_unit`.
- Acceptance: human correction propagates to future packs.

### Phase 7: Retire legacy enrichment authority

- Keep regex extraction as diagnostic/lint only.
- Stop auto-promoting heuristic claims.
- Make compiler/review queue the only semantic promotion path.

### Phase 8: Optional retrieval expansion

- Add embedding search, GraphRAG, LightRAG, Graphiti, community detection, or
  semantic entity graph only after packet routing, ack, supersede, and surface
  contracts pass.

V1 must not begin with embeddings or GraphRAG. The first product proof is that
30 messy debate messages can become 1 to 3 useful human terminal items, durable
dashboard rows, and different role-specific agent packets.

## 12. Acceptance Tests

1. HUMAN receives only temporal deltas in terminal, not raw debate history.
2. Durable conclusions appear in Task Tray dashboard and survive restart.
3. CODEX and ADVOCATE receive different context packs for the same topic.
4. A debate can be replayed without compiler tables.
5. Compiler projections can be rebuilt from raw ledgers.
6. A human correction supersedes old units without deleting history.
7. A standing policy appears in future agent context packs automatically.
8. Low-value STATUS messages are coalesced.
9. H/Q messages to HUMAN bypass coalescing.
10. Every dashboard row has drill-back evidence.
11. Packet ack prevents repeated delivery.
12. Legacy enrichment cannot auto-promote canonical truth.
13. Twenty low-value `STATUS` messages plus two `Q` messages, one blocker, and
    one correction produce at most three terminal items; `STATUS` is coalesced
    or hidden.
14. Same topic produces materially different HUMAN, CODEX, and ADVOCATE packets.
15. Deleting derived `memory_units` and `context_packets` while keeping raw
    ledgers allows compiler/router replay with preserved evidence refs.
16. Human correction "LightRAG first; Graphiti later" supersedes any prior
    "Graphiti in V1" unit and changes future Codex packets.
17. A delivered+seen but unacknowledged decision can be reminded later as a
    delta, not repeated as full context.
18. Raw transcript is available only through `memory_explain_unit` / drill-back,
    not in terminal or dashboard bodies.

## 13. Semantic Load Gate

The existing acceptance tests prove architectural safety, but they do not prove
semantic adequacy under real operator load. Before any runtime implementation
of the attention router is accepted, the project must pass a dedicated semantic
load gate.

Required fixture:

- At least 5 actors: `HUMAN`, `CONDUCTOR`, `CODEX`, `CLAUDE`, `ADVOCATE`, plus
  optional executor roles.
- 5 to 7 simulated days of dialogue.
- 500 to 1500 messages.
- Many-to-many conversation flow, not a clean single-thread transcript.
- Mixed message types: decisions, reversals, corrections, blockers, low-value
  status spam, stale waiting items, due dates, social nuance, implied
  commitments, and contradicting statements.
- At least 20 externally verifiable evidence refs.
- At least 10 human corrections or supersessions.
- At least 5 decisions that should become durable dashboard rows.
- At least 5 urgent temporal deltas that should reach terminal.
- At least 10 low-value deltas that must be suppressed or coalesced.

Golden outputs:

- `attention_next_terminal_delta` output for HUMAN at several checkpoints.
- `attention_dashboard_state` durable rows after each simulated day.
- `agent_context_pack` for CODEX, CLAUDE, ADVOCATE, and CONDUCTOR for the same
  topic.
- `memory_review_candidates` queue after compiler passes.
- Drill-back evidence for every promoted decision, policy, blocker, and
  receipt.

Passing criteria:

- Missed critical terminal items: zero.
- Repeated acknowledged packets: zero.
- Durable decision rows without evidence: zero.
- Context packs exceeding budget: zero.
- False urgent terminal items: <= 10%.
- Low-value status messages coalesced or suppressed: >= 90%.
- Human corrections supersede old units without deleting history: 100%.
- Actor-specific packs differ materially by role while sharing the same raw
  evidence base.
- HUMAN terminal feed stays bounded: default max 3 items and no raw transcript
  dumps.

The gate fails closed. If the deterministic router passes structural tests but
the semantic load gate fails, implementation must not proceed to dashboard or
agent-context rollout. The next action is compiler/router redesign, not tuning
the UI.

## 14. Optimal Orchestration Architecture

The attention system must not depend on a frontier LLM API in the real-time hot
path. Real-time LLM orchestration would be expensive, latency-sensitive,
non-deterministic, hard to replay, and fragile under offline/local-first
constraints.

The optimal split is:

```text
Hot path, deterministic:
  append ledgers
  update watermarks
  route addressed H/Q
  maintain actor_surface_state
  enforce TTL/priority/backpressure
  deliver already-compiled packets

Warm path, cheap/incremental:
  FTS/BM25 retrieval
  vector lookup if available
  entity and thread linking
  topic segmentation
  novelty/staleness/risk scoring
  packet coalescing

Cold path, semantic compiler:
  LLM extracts candidate units
  LLM labels ambiguous topic clusters
  LLM summarizes messy many-to-many dialogue
  LLM proposes decision/policy/trap/receipt units
  human or policy gate promotes candidates
```

The LLM is therefore a compiler, not the orchestrator. It converts messy
language into typed candidate memory units. The deterministic router decides
who sees what, when, at which resolution, and through which surface.

Algorithms that belong in the deterministic/warm layers:

- Event sourcing over append-only ledgers.
- Logical clocks and actor watermarks.
- Incremental materialized views.
- FTS5/BM25 for exact retrieval.
- Optional vector search for semantic recall.
- Temporal graph edges for depends-on, blocks, supersedes, supports, and
  contradicts.
- Priority queue with backpressure and TTL.
- Novelty scoring against actor seen/ack state.
- Staleness scoring for waiting/blocker/deadline items.
- Coalescing by scope, actor, topic, and evidence hash.
- Role-specific context packing with token budgets.
- Confidence calibration from evidence count, correction history, and source
  reliability.

Algorithms that should not be trusted alone:

- Pure graph clustering for semantic decisions.
- Embeddings-only similarity for importance.
- PageRank/centrality as a proxy for operator attention.
- Recency-only or frequency-only ranking.
- Regex extraction as semantic authority.

LLM use should be gated by value and uncertainty:

- No LLM call for simple addressed H/Q routing.
- No LLM call for ack, TTL, deadline, and stale-waiting mechanics.
- Small/local model or cached compiler output for routine summarization.
- Frontier model only for high-ambiguity, high-impact, or high-compression
  compiler passes.
- Every LLM output enters as `candidate`, not canonical truth.
- Every promoted unit must have evidence refs and replay path.

This architecture keeps the system economically and operationally viable:
the expensive semantic layer runs only when it can add value, while the
attention loop remains local, testable, replayable, and bounded.

## 15. Strategic Conclusion

The defensible product is not "sqlite_memory remembers things". Mem0,
Supermemory, Cognee, Letta, and Zep already fight that battle.

The defensible product is:

```text
Local, reviewable attention OS for human-agent teams:
proof ledgers + typed memory + per-actor delta + delivery routing.
```

More operationally:

```text
sqlite_memory = provable coordination memory
that turns raw events into actor-specific attention packets.
```

Shortest product line:

```text
Not memory storage.
Memory dispatch.
```

The next implementation plan should build the smallest useful vertical slice:

```text
debate/task/memory ledgers
→ dry-run compiler
→ actor_surface_state
→ terminal delta
→ ack
→ dashboard durable row
```

Only after that works should graph clustering, embeddings, or external memory
benchmarks enter the critical path.
