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
Retrieval Representation
  primary abstractions, cue anchors, retrieval traces
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

Memora amendment, 2026-06-30: the compiler/router split needs a formal
retrieval representation layer between typed units and delivery routing. This
layer decouples rich stored memory value from lightweight retrieval handles.
It is a derived projection layer, not a new source of truth.

## 2. Market Research Summary

The market already has strong adjacent products:

- Microsoft Research Memora: harmonic memory representation that separates
  rich memory values from lightweight primary abstractions and cue anchors,
  with policy-guided retrieval. It validates the abstraction-specificity
  bottleneck and makes generic long-horizon memory retrieval a major-player
  research/product front.
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
Memora sharpens this conclusion: do not compete as a generic memory retriever.
Use Memora-like retrieval handles under a verifiable attention/evidence OS.

Positioning:

```text
Not: persistent memory for agents.
Better: attention OS for human-agent teams.
Best: verifiable delegation and context routing over local ledgers.
```

Research sources checked:

- Microsoft Research Memora:
  `https://www.microsoft.com/en-us/research/blog/memora-a-harmonic-memory-representation-balancing-abstraction-and-specificity/`,
  `https://arxiv.org/abs/2602.03315`, and
  `https://github.com/microsoft/Memora`.
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
- Do not treat Memora-like retrieval handles as canonical truth or as a
  substitute for evidence, promotion, and ack/supersede governance.
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
7. Router output is rebuildable from `memory_units`, `actor_surface_state`, and
   materialized `actor_outbox` rows.
8. No LLM call is required in the hot path.
9. Projections are read-only with respect to raw ledgers.
10. Wrong memory is corrected by supersession, not deletion.
11. Retrieval handles are derived projections, not canonical truth.
12. Memory value is preserved separately from retrieval abstraction.
13. Cue anchors help recall but never replace evidence refs.

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
6. Retrieval relevance is not attention priority and is not permission to act.

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

### Retrieval representation layer

Memora-style retrieval is a representation layer over memory units, not a
replacement for evidence or routing.

Definitions:

- `primary_abstraction`: compact canonical retrieval handle, typically 6 to 12
  words, describing what the unit is fundamentally about.
- `memory_value`: the rich stored content of the unit. In this spec it is the
  existing L1/L2/L3/L4 body and raw reference structure. It must not be
  collapsed into the retrieval handle.
- `cue_anchor`: alternate access path to the same unit, generated from the
  memory value and evidence. Anchors are useful for multi-hop recall and
  non-local context, but they are not facts.
- `retrieval_trace`: audit/debug record explaining which abstractions and
  anchors were followed, which units were selected, which were rejected, and
  why retrieval stopped.

Hard boundary:

```text
body_l1/body_l2/body_l3/body_l4_ref = value and display resolution.
primary_abstraction + cue_anchors = retrieval representation.
actor_outbox + context_packets = attention delivery.
```

Retrieval handles may be LLM-generated, model-generated, or deterministic, but
they always enter as derived/provisional projection data. They can be reviewed,
superseded, rejected, and rebuilt from the raw ledgers plus compiler version.

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
- `primary_abstraction`: compact canonical retrieval handle.
- `abstraction_status`: `candidate`, `active`, `superseded`, `rejected`.
- `abstraction_version`: compiler/model/prompt version that generated the
  handle.
- `abstraction_embedding_ref`: optional external/vector row reference for the
  primary abstraction. The rich memory value is not the default embedding
  target.
- `retrieval_value_policy`: `handle_only`, `handle_plus_anchors`,
  `value_allowed_for_explicit_search`, or `no_embedding`.
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

### `memory_unit_cue_anchors`

Alternate retrieval handles for a memory unit. Cue anchors are derived,
reviewable, and supersedable. They are not canonical facts and never replace
evidence refs.

Fields:

- `anchor_id` primary key.
- `unit_id`.
- `anchor_text`.
- `anchor_kind`: `person`, `project`, `topic`, `decision`, `deadline`,
  `source`, `risk`, `procedure`, `other`.
- `anchor_status`: `candidate`, `active`, `superseded`, `rejected`.
- `confidence`: 0-1.
- `source_evidence_id`.
- `created_by_run_id`.
- `supersedes_anchor_id`.
- `created_at`.

Rules:

- A unit may have many active anchors.
- Anchors can be added, rejected, or superseded without changing the memory
  value.
- Anchor text must be short enough for retrieval and display diagnostics.
- Anchor-derived recall still returns the memory unit plus evidence refs, not
  anchor text alone.

### `memory_retrieval_traces`

Replayable trace of a retrieval pass. This table is for audit, debugging,
evaluation, and explaining why a context packet saw a unit.

Fields:

- `trace_id` primary key.
- `query_text`.
- `actor_id`.
- `surface`.
- `scope_kind`, `scope_id`.
- `initial_hits_json`.
- `cue_expansions_json`.
- `selected_units_json`.
- `rejected_units_json`.
- `stop_reason`: `budget_exhausted`, `enough_evidence`, `low_confidence`,
  `no_more_anchors`, `permission_filter`, `manual_stop`, `other`.
- `created_at`.

Rules:

- Retrieval traces are not source of truth.
- Traces must include selected and rejected unit ids when available.
- A packet built from retrieval-expanded context should preserve the
  `trace_id` in `payload_json` or `context_packet_items`.

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

### `actor_outbox`

Incremental queue of attention-worthy delivery items per actor/surface. This is
the speed boundary between routing and rendering: terminal/dashboard calls read
from the outbox instead of scanning `memory_units`.

Fields:

- `actor_id`.
- `surface`.
- `outbox_seq`.
- `scope_kind`, `scope_id`.
- `coalesce_key`.
- `item_kind`: `temporal_delta`, `decision_required`, `blocker`,
  `risk_changed`, `policy_warning`, `receipt`, `agent_context_seed`.
- `priority`.
- `severity`.
- `summary_l1`.
- `body_l2`.
- `unit_id`.
- `evidence_id`.
- `event_ref`.
- `status`: `queued`, `delivered`, `read`, `ack`, `acted`, `expired`,
  `dismissed`, `coalesced`.
- `ttl_until`.
- `created_at`.

Rules:

- Router writes `actor_outbox` incrementally.
- Terminal reads only queued outbox rows, bounded by actor/surface policy.
- `context_packets` bundle outbox items; they are not the only delivery queue.
- Repeated low-value items are coalesced at the outbox layer, not by dropping
  raw human input.

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

### `memory_generate_retrieval_handles`

Compiler-facing tool/DAO contract that proposes primary abstractions and cue
anchors for existing memory units. This is not a promotion path.

Input:

```json
{
  "scope_kind": "global|task|debate|project|actor",
  "scope_id": "",
  "unit_ids": [],
  "dry_run": true,
  "limit": 100
}
```

Output: run id, proposed primary abstractions, proposed cue anchors, evidence
refs, and confidence.

Rules:

- Generated handles start as `candidate` unless the unit was explicitly created
  from a human command and the handle is deterministic.
- Handle generation must not change packet delivery directly.
- Handle generation must not promote `semantic_memory`, `policy_memory`,
  `decision_memory`, or `trap_memory`.

### `memory_review_anchors`

Returns cue anchors and primary abstractions requiring review.

Input: scope, anchor status, unit types, limit.

Output: candidate anchors with unit preview, evidence, confidence, and proposed
action.

### `memory_retrieval_trace`

Returns the trace behind a retrieval-expanded context packet or an exploratory
query.

Input:

```json
{
  "trace_id": "...",
  "resolution": "L1|L2|L3"
}
```

Output: query, initial abstraction hits, cue-anchor expansions, selected units,
rejected units, stop reason, and drill-back refs.

### `attention_build_packet`

Builds one packet for one actor/surface.

Packet build must select from materialized `actor_outbox` rows and attach
referenced units/evidence. It must not scan all `memory_units` on the request
path.

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

- Reads queued `actor_outbox` items for the HUMAN terminal surface.
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

Retrieval-handle contracts (`memory_generate_retrieval_handles`,
`memory_review_anchors`, and `memory_retrieval_trace`) are compiler/evaluation
contracts before they are public tools. They may be implemented earlier than
Phase 8 because they are part of the memory representation contract, not a
GraphRAG/vector expansion.

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

Memora-inspired retrieval pipeline:

```text
1. Determine actor/surface/task intent.
2. Search primary_abstraction handles for candidate units.
3. Expand through active cue_anchors for related non-local context.
4. Filter by unit status, validity, actor permissions, sensitivity, evidence
   confidence, and actor ack state.
5. Score novelty, urgency, risk, decision_required, and blocker relevance.
6. Build a context_packet at the requested L1/L2/L3/L4 resolution.
7. Preserve retrieval trace and evidence refs.
```

Hard rule:

```text
retrieval relevance != attention priority != permission to act
```

Retrieval finds candidates. The router decides delivery. Governance decides
promotion, correction, and permission to treat a unit as durable truth.

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

### Phase 2.5: Retrieval representation handles

- Add `primary_abstraction` fields to `memory_units`.
- Add `memory_unit_cue_anchors`.
- Add `memory_retrieval_traces`.
- Compiler emits retrieval handles as candidate/provisional projection data.
- Anchors are reviewable and supersedable independently from memory value.
- Acceptance: a rich unit can be reached through multiple cue anchors while
  preserving the original evidence and full value.

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

- Add full embedding search, GraphRAG, LightRAG, Graphiti, community detection,
  or semantic entity graph only after packet routing, ack, supersede, surface
  contracts, and retrieval-handle gates pass.

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
15. Deleting derived `memory_units`, `actor_outbox`, and `context_packets`
    while keeping raw ledgers allows compiler/router replay with preserved
    evidence refs.
16. Human correction "LightRAG first; Graphiti later" supersedes any prior
    "Graphiti in V1" unit and changes future Codex packets.
17. A delivered+seen but unacknowledged decision can be reminded later as a
    delta, not repeated as full context.
18. Raw transcript is available only through `memory_explain_unit` / drill-back,
    not in terminal or dashboard bodies.
19. A memory unit preserves rich L2/L3/L4 value while retrieval uses
    `primary_abstraction` and cue anchors.
20. Cue-anchor retrieval returns units with evidence refs, not anchor text as
    standalone truth.
21. Superseded anchors stop influencing future packet builds.
22. Two distinct decisions with similar wording are not falsely merged under one
    abstraction.
23. `memory_retrieval_trace` explains selected and rejected units for an
    agent-context packet.

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
- `memory_review_anchors` queue for generated primary abstractions and cue
  anchors.
- `memory_retrieval_trace` for packets that used cue-anchor expansion.
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
- Rich detail is preserved in memory value while retrieval uses compact
  abstractions and cue anchors.
- Same source can be reached through multiple cue anchors without duplicating
  the underlying unit.
- False merge of distinct decisions under one abstraction: zero for the golden
  decision fixtures.
- Retrieval traces exist for every cue-expanded context packet.

The gate fails closed. If the deterministic router passes structural tests but
the semantic load gate fails, implementation must not proceed to dashboard or
agent-context rollout. The next action is compiler/router redesign, not tuning
the UI.

### Abstraction-specificity gate

Memora makes this an explicit sub-gate. The system must prove that it can keep
specific details while retrieving through compact handles.

The gate passes only when:

- rich details remain in `body_l2`, `body_l3`, or `body_l4_ref`;
- primary abstractions are short retrieval handles, not lossy replacements for
  value;
- cue anchors improve recall without creating canonical facts;
- contradicted or superseded anchors stop affecting future packets;
- every retrieved unit has drill-back evidence;
- retrieval traces make selected/rejected unit choices inspectable;
- actor-specific packet differences remain after retrieval filtering.

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
  primary_abstraction lookup
  cue_anchor expansion
  vector lookup if available
  entity and thread linking
  topic segmentation
  novelty/staleness/risk scoring
  packet coalescing

Cold path, semantic compiler:
  LLM extracts candidate units
  LLM proposes primary abstractions and cue anchors
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
- Primary abstraction lookup for compact recall.
- Cue-anchor expansion for related-but-not-similar context.
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
- Cue-anchor match as a proxy for truth or delivery urgency.
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
- Every LLM-generated retrieval handle enters as candidate/provisional
  projection data until reviewed or safely auto-accepted by policy.
- Every promoted unit must have evidence refs and replay path.

This architecture keeps the system economically and operationally viable:
the expensive semantic layer runs only when it can add value, while the
attention loop remains local, testable, replayable, and bounded.

## 15. Runtime Profiles

The specification must be profile-aware. "Optimal" is relative to the
bottleneck. STRIX and desktop human-agent workflows share the same kernel, but
they do not optimize the same resources.

```text
STRIX = high-volume / low-latency / structured trace events.
Desktop = lower-volume / high-semantic-density / messy human context.
```

The correct product structure is:

```text
sqlite_memory_core
  + desktop_human_agents profile
  + strix_trace profile
```

Same kernel, different optimization policy.

### Common Kernel

These are non-negotiable across profiles:

- Raw append-only ledgers.
- Derived memory units only with evidence refs.
- Retrieval handles separated from rich memory value.
- Cue anchors are reviewable/supersedable projection data.
- Compiler separated from router.
- `actor_surface_state`.
- `actor_outbox`.
- `context_packets`.
- `ack_log` / `ack_events`.
- Supersede semantics instead of silent overwrite.
- Drill-back to raw evidence.
- Replayable compiler/router rebuild.
- Terminal as temporal delta only.
- Dashboard as durable control surface.
- Agent gets context packet, not memory dump.
- `seen`, `read`, `ack`, and `acted` remain separate states.

### Desktop Human-Agent Profile

The desktop profile optimizes human cognition, semantic richness, low capture
friction, layered output, and correct context for Claude/Codex/ADVOCATE.

Allowed:

- Rich SQLite persistence.
- Markdown and JSON payloads.
- Voice transcripts, chat imports, email/file/web captures.
- SQLite FTS and later sqlite-vec/embedding search.
- Primary abstractions and cue anchors for retrieval without dumping raw
  memory values into context.
- User-triggered retrieval trace inspection.
- Async LLM compiler passes.
- User-triggered synchronous LLM work when the human explicitly asks for it.
- Rich `memory_units` with L1/L2/L3/L4 resolution.
- Ad-hoc semantic search and exploratory recall.
- Human review queue for policy/decision/trap promotion.

Forbidden:

- Raw transcript spam.
- AI-derived canonical truth without evidence and promotion rule.
- Treating cue anchors as facts or permissions.
- Treating `seen` as `ack`.
- Unbounded terminal bursts.
- Unbounded agent context dumps.
- Dropping raw human input as a storage policy.

The desktop loss rule is:

```text
Never lose memory; reduce attention.
Coalesce delivery, not raw human input.
```

### STRIX Trace Profile

The STRIX profile is not runtime memory. It is a bounded, deterministic,
append-only, replayable attention sidecar over STRIX traces.

The STRIX path must be:

```text
HOT PATH
Rust tick emits tiny typed events. No SQLite. No JSON. No LLM. No MCP.
No graph retrieval. No blocking.

WARM PATH
Async writer batches events into WAL/control ledgers and projections.

COLD PATH
Compiler, summaries, primary abstractions, cue anchors, GraphRAG/embeddings,
human digest, and agent context packs.
```

Do not integrate as:

```text
STRIX tick -> sqlite_memory write -> compiler -> router -> packet
```

Integrate as:

```text
STRIX tick/replay/scenario
  -> bounded event sink
  -> ring buffer or trace file
  -> async batch writer
  -> SQLite control ledger
  -> projections
  -> attention router
  -> terminal/dashboard/agent packets
```

STRIX emits trace events, not memory. Memory is derived later.

Example trace event:

```text
event_kind = SAFETY_CLAMP_APPLIED
tick = 142
severity = medium
source = strix-swarm
payload_ref = ...
```

The compiler/router may later derive:

- `temporal_delta`.
- `evidence_memory`.
- `trap_memory`.
- primary abstractions and cue anchors for post-run recall.
- dashboard row.
- post-run Codex/Claude context packet.

Memora-like retrieval handles are allowed only after trace capture, indexing,
and projection. They are for post-run/cold-path analysis: scenario summaries,
failure-pattern recall, human review dashboards, and agent development packs.
They are never part of STRIX tick authority.

### STRIX Hot/Warm/Cold Gates

Hot path forbidden operations:

- SQLite.
- fsync.
- JSON serialization.
- LLM.
- MCP.
- Network client.
- Blocking I/O.
- Unbounded queue.
- Router scan.
- Graph/embedding retrieval.
- Primary-abstraction or cue-anchor retrieval.

Warm path responsibilities:

- Async writer.
- Batched inserts.
- WAL mode.
- Prepared statements.
- Incremental projections.
- Coalescing.
- `actor_outbox` materialization.

Cold path responsibilities:

- Semantic compiler.
- LLM summaries.
- GraphRAG/embeddings.
- Primary-abstraction generation.
- Cue-anchor generation and expansion.
- Cross-run failure-pattern search.
- Human digest.
- Agent context packs.

### STRIX Storage Split

SQLite is the control ledger, not the telemetry warehouse.

SQLite stores:

- run metadata;
- event index;
- decision trace refs;
- packet queues;
- ack state;
- memory units;
- evidence refs;
- actor/surface watermarks;
- projection state.

SQLite must not store bulk per-agent/per-tick telemetry as ordinary rows.
Bulk scenario telemetry belongs in Parquet/Arrow/compressed trace blobs, with
SQLite storing hash, offset, tick span, event kind, severity, scenario/run id,
summary, and drill-back refs.

### STRIX Data Shapes

STRIX-mode events should use a small typed envelope:

```rust
pub struct AttentionEvent {
    pub run_id: u128,
    pub seq: u64,
    pub tick: u64,
    pub sim_time_us: u64,
    pub kind: EventKind,
    pub source: EventSource,
    pub actor_id: u32,
    pub severity: Severity,
    pub priority: u8,
    pub flags: u32,
    pub payload_ref: Option<u64>,
    pub hash: [u8; 32],
}
```

Principle:

```text
small, typed, bounded envelope
large payload as blob/ref
semantic interpretation later
```

V1 STRIX event taxonomy should stay small:

- `RUN_STARTED`
- `RUN_ENDED`
- `SCENARIO_LOADED`
- `TICK_SUMMARY`
- `REGIME_CHANGED`
- `STATE_ESTIMATE_DEGRADED`
- `ANOMALY_DETECTED`
- `ASSIGNMENT_CHANGED`
- `TASK_BLOCKED`
- `TASK_COMPLETED`
- `SAFETY_CLAMP_APPLIED`
- `POLICY_DENY`
- `CONSTRAINT_VIOLATION_PREVENTED`
- `MESH_PARTITION_DETECTED`
- `MESH_REJOINED`
- `COMMS_DEGRADED`
- `TRACE_CREATED`
- `BATTLE_REPORT_CREATED`
- `HUMAN_DECISION_REQUIRED`
- `HUMAN_CORRECTION`
- `SINK_DEGRADED`

Do not capture everything as semantic memory. Capture decision-relevant trace
events, tick summaries, exception events, periodic snapshots, and drill-back
refs.

### STRIX Write Amplification Rule

Unacceptable:

```text
1 tick
  -> 500 agent events
  -> 500 source events
  -> 500 memory units
  -> 500 evidence rows
  -> 500 outbox rows
```

Acceptable:

```text
1 tick
  -> 1 tick summary
  -> 0..N exception events
  -> projections updated
  -> outbox only if attention-worthy
```

The router must route only attention-worthy events:

- state change;
- risk change;
- decision required;
- blocker;
- human correction;
- policy deny/violation;
- trace close;
- summary close.

Not every event becomes attention.

### STRIX Backpressure Policy

Hot path emit result must be explicit:

```rust
pub enum EmitResult {
    Accepted,
    Coalesced,
    DroppedNonCritical,
    SinkDegraded,
}
```

Never drop:

- `RUN_STARTED`
- `RUN_ENDED`
- `SAFETY_CLAMP_APPLIED`
- `POLICY_DENY`
- `HUMAN_DECISION_REQUIRED`
- `TRACE_CREATED`
- `BATTLE_REPORT_CREATED`

May coalesce:

- low-value status;
- repeated `COMMS_DEGRADED`;
- repeated mesh heartbeat;
- repeated unchanged tick summaries.

May sample:

- per-agent state snapshots;
- high-frequency telemetry.

Queue full behavior:

1. Coalesce low-value status.
2. Drop non-critical telemetry.
3. Keep counters.
4. Emit `SINK_DEGRADED` once.
5. Never block the tick.

### STRIX Budget Gates

These are benchmark targets, not public performance claims:

| Operation | Target |
| --- | ---: |
| `NoopAttentionSink.emit()` | p99 < 1 us |
| `RingBufferAttentionSink.emit()` | p99 < 5 us |
| Hot path allocations | 0 |
| Blocking calls | 0 |
| LLM/MCP calls in tick | 0 |
| SQLite calls in tick | 0 |
| Terminal packet read | O(k), k <= 3 |
| Agent pack build excluding LLM | < 100 ms target |
| Full replay rebuild | deterministic and benchmarked |

Any README/public benchmark or external capability claim must be refreshed from
the exact commit before publication. The spec may use such numbers as
benchmark targets only, not as current truth.

### STRIX Integration Modes

Rollout order:

1. Offline replay post-processor:
   `replay trace -> sqlite_memory ingest -> run_event_index -> dashboard`.
   Zero runtime overhead and no STRIX code changes.
2. Passive simulation sidecar:
   `AttentionSink -> async writer -> live terminal/dashboard`.
   If the sink fails, simulation continues.
3. Agent development packs:
   failing scenario/range -> Codex/Claude packet with trace refs, affected
   surfaces, capability boundary, and stop conditions.
4. Human review dashboard:
   post-run OODA summary, decision rows, evidence refs, caveats.
5. Live autonomy authority:
   explicitly out of V1 scope.

Capability-boundary guard:

- Do not claim field/on-hardware deployment from software replay alone.
- Do not claim delivered external memory integration until it is shipped.
- Do not claim edge-LLM autonomous decision authority as core autonomy.
- Do not infer sensor/RF/field readiness from software-only scenarios.
- Public wording that crosses those boundaries requires human review.

### Profile Config Sketch

`desktop_human_agents.toml`:

```toml
[profile]
name = "desktop_human_agents"
primary_bottleneck = "human_attention"

[capture]
never_drop_raw_human_input = true
allow_markdown = true
allow_json_payloads = true
allow_voice_transcripts = true
allow_chat_imports = true

[compiler]
llm_allowed_async = true
llm_allowed_user_triggered_sync = true
durable_units_require_evidence = true
policy_memory_requires_human_confirm = true
decision_memory_requires_human_confirm = true
retrieval_handles_enabled = true
cue_anchors_enabled = true
retrieval_traces_enabled = true

[router]
use_actor_outbox = true
terminal_max_items = 3
terminal_default_resolution = "L1"
dashboard_default_resolution = "L2"
coalesce_delivery_only = true
retrieval_relevance_is_not_priority = true

[loss_policy]
drop_raw_human_input = false
drop_low_value_delivery_items = true
```

`strix_trace.toml`:

```toml
[profile]
name = "strix_trace"
primary_bottleneck = "runtime_determinism"

[hot_path]
sqlite_allowed = false
llm_allowed = false
mcp_allowed = false
blocking_io_allowed = false
unbounded_queue_allowed = false

[capture]
typed_event_envelope = true
batch_writer = true
ring_buffer = true
payload_blobs = true

[router]
projection_first = true
actor_outbox = true
terminal_max_items = 3
route_only_attention_worthy_events = true
cold_path_retrieval_handles_only = true

[loss_policy]
drop_raw_human_input = false
drop_noncritical_telemetry = true
coalesce_status = true
never_drop_safety_events = true
```

### STRIX Profile Acceptance Tests

The STRIX profile is not accepted until these pass:

1. Same scenario with sink disabled, `NoopAttentionSink`, and
   `RingBufferAttentionSink` has unchanged deterministic replay result.
2. Tick path has no imports or calls to SQLite, MCP, LLM, network clients, or
   blocking I/O.
3. 1000 low-value status events plus safety/decision events produce at most
   three terminal items and no raw telemetry dump.
4. Deleting projections/outbox/packets while keeping event index and payload
   blobs allows replay rebuild with same evidence refs and packet hashes, or an
   explainable version difference.
5. Human correction supersedes future agent packets while preserving old
   history.
6. Public-claim boundary guard blocks field-ready, hardware-validated,
   external-memory-shipped, or edge-LLM-authority claims unless explicitly
   confirmed.

Canonical product line:

```text
Desktop sqlite_memory optimizes cognition.
STRIX sqlite_memory profile optimizes non-interference.
Shared core, different runtime profiles.
```

Design comment: any implementation plan must name its runtime profile before
choosing storage, packet format, latency budget, compiler behavior, router data
structures, and loss policy. A design that is "optimal" without a profile is
underspecified.

## 16. Strategic Conclusion

The defensible product is not "sqlite_memory remembers things". Microsoft
Memora, Mem0, Supermemory, Cognee, Letta, and Zep already fight the generic
agent-memory battle.

Memora changes the claim boundary. The project should not claim to beat
Microsoft-scale long-horizon memory retrieval. It should use Memora-like
retrieval handles where useful, while owning the layer Memora does not fully
define here: proof ledgers, provenance, actor-specific attention, human gates,
ack/supersede, and replayable workpapers.

The defensible product is:

```text
Local, reviewable attention OS for human-agent teams:
proof ledgers + typed memory + retrieval handles + per-actor delta +
delivery routing.
```

More operationally:

```text
sqlite_memory = provable coordination memory
that turns raw events into actor-specific attention packets.
```

Shortest product line:

```text
Not memory storage.
Not generic memory retrieval.
Memory dispatch with proof.
```

The next implementation plan should build the smallest useful vertical slice:

```text
debate/task/memory ledgers
→ dry-run compiler
→ primary abstractions and cue anchors
→ actor_surface_state
→ terminal delta
→ ack
→ dashboard durable row
```

Only after that works should full graph clustering, GraphRAG, embeddings, or
external memory benchmarks enter the critical path. Primary abstractions and
cue anchors are no longer optional extras after Memora; they are part of the
minimum retrieval representation contract.
