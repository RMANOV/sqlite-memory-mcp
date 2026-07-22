# Debate Protocol v2

> The legacy ledger/lifecycle contract remains supported. New deterministic
> debates use the versioned `debate/v1` semantic envelope and §7 server
> invariants documented in
> [`design/DEBATE_V1_S7_CONTRACT.md`](design/DEBATE_V1_S7_CONTRACT.md).

> Productized inter-session coordination for AI agents. Single channel
> per debate, atomic message format, role-aware watermarks, explicit
> lifecycle state machine, escalation hooks, append-only log with
> compaction.
>
> Lives in `sqlite-memory-mcp` as part of the `sqlite-intel` MCP server.
> Seven tools: `debate_init`, `debate_post`, `debate_read`, `debate_state`,
> `debate_escalate`, `debate_compact`, `debate_advance_watermark`. Three
> tables: `debates`, `debate_messages`, `debate_watermarks`.

## Claim boundary

The debate protocol is **governed multi-agent OODA coordination**. For external
/ diligence material it is claimable only when bounded to its actual controls:
addressed messages, role-aware cursors / watermarks, `no_action` completion,
and append-only audit logs. It does **not** carry an autonomous
execution-safety claim beyond these controls. See
the [`README` external claim boundary](../README.md#external-claim-boundary-frozen-claim-set)
for the frozen claim-set.

## Mental model

Multi-agent coordination tends to degrade in five ways: missed updates
(reader skipped a message), format drift (each agent invents its own
prefixes), entity multiplication (one debate sprawls across N notes),
stale-record bleeding (history grows, readers can't bootstrap), polling
interrupts (agents constantly re-read full logs). The Debate Protocol
addresses each by enforcing structure at the storage layer.

- **Single channel per debate.** One `debates` row owns one topic. All
  messages reference its `topic_id`. No sprawl across observations on
  a generic KG entity.
- **Atomic message format.** Every row in `debate_messages` has a
  fixed shape: `(msg_id, topic_id, role, ts, priority, kind, reply_to,
  body, created_at)`. CHECK constraints enforce enums. Validators in
  `debate.py` enforce regex (`topic_id`, `role`, `msg_id`, ISO 8601 UTC
  ts).
- **Role-aware watermarks.** Each role has a per-topic cursor in
  `debate_watermarks`. Compound `(ts, msg_id)` cursor — same-ts messages
  never get silently dropped.
- **Lifecycle state machine.** `INIT → ACTIVE → RESOLVED → ARCHIVED`.
  RESOLVED transition asserts every open `Q` has a matching `A` reply
  (or an `A` body starting with `[DEFERRED:` for explicit deferrals).
- **Escalation hooks.** `debate_escalate` writes an `H`-priority `PING`
  tagged for a target role (default `HUMAN`) to surface unanswered
  high-priority questions, deadline misses, contradiction signals.
- **High-signal message economy.** The debate channel is for messages with
  material decision value: new evidence, a falsifiable objection, a concrete
  implementation patch, a gate decision, or an operator-visible completion.
  Agreement, duplicate acknowledgement, repeated status adoption, and
  "nothing to add" are handled through silent approval/refusal, normally by
  completing the wake with `debate_worker_no_action` instead of posting.
- **Deterministic open-work priority.** Cross-topic scheduling is not inferred
  from chat prose. `CONDUCTOR` assigns topic lanes with
  `debate_set_topic_priority(P0..P7)`, and `debate_work_queue` is the canonical
  sorted view for operators and automation. Message-level `H/M/L/INFO` remains
  local to a topic; it does not by itself make all active topics equal.
- **Compaction.** `kind=COMPACTION` snapshots are append-only OODA
  digests (regex-validated body). Future readers bootstrap from the
  latest compaction + incremental tail instead of full history.

## Task Tray operator dashboard

The Task Tray full window is **not** a raw debate viewer. It is the
human / `CONDUCTOR` control surface over debate projections. Raw
`debate_messages` remain the immutable ledger; the tray should present
bounded projections that preserve drill-back references into the raw log.

This distinction is required for scale. Multi-agent coordination fails when
each role repeatedly reads the whole topic history: read cost becomes
`O(everything × agents)`, burns agent context windows, and increases
lost-in-the-middle risk. The operator surface should reduce that to
`O(changes × interested actor)` by routing each actor to the smallest
actionable view.

When an operator says "check the debate", `CONDUCTOR` should not default to a
full `debate_read` replay. It should read these projections first:

1. `conductor_inbox`: messages addressed to `CONDUCTOR` / operator,
   blocked lanes, decision-needed rows, stale claims, and sync anomalies.
2. `human_brief`: at most 7-10 bullets covering what changed, what closed,
   what is waiting, what needs an operator decision, what is risky, the
   recommended next move, and what does **not** need reading.
3. `lane_state`: current owner, status, last addressed event, next action,
   stop condition, and risk class per lane.
4. Raw event / payload drill-back only for rows that need a decision, carry
   risk, contradict another projection, or require exact evidence.

The required tray projections are:

- `HUMAN_BRIEF` — bounded operator brief, not a transcript.
- `CONDUCTOR_INBOX` — addressed work, blocked lanes, decision-needed items,
  stale claims, and sync anomalies.
- `LANE_STATE` — owner, status, last addressed event, next action, stop
  condition, and risk class.
- `ADVOCATE_QUEUE` — artifacts that truly require `PASS` / `AMEND` /
  `BLOCK` because of security, compliance, reputation, or diff risk.
- `EXECUTOR_QUEUE` — dispatches addressed to each executor, without foreign
  lane noise.
- `STALE_OR_OVERCOORDINATION` — repeated `STATUS`, stale `CLAIM`, duplicate
  review, and `STOP_COST` candidates.
- `SYNC_HEALTH` — bridge health as an operator signal, never as a reason to
  stop bridge sync.

Every projection row must carry enough provenance for drill-back:
`raw_event_ids`, `payload_ref`, and/or `artifact_ref`. Aggressive summary
without drill-back is forbidden because it creates projection drift: the
summary layer becomes a new stale-read source instead of fixing stale reads.
The governed-memory pattern is therefore:

- immutable raw log = ledger;
- mutable projections = active working graph;
- payload / artifact refs = provenance boundary;
- drill-back = audit and correction path.

Backpressure applies only to low-value coordination noise: repeated standby,
duplicate FYI `STATUS`, and non-decision progress pings. It must never
coalesce or buffer:

- `severity >= high`;
- messages addressed to operator or `CONDUCTOR`;
- `decision_needed=true`;
- `sync_health=failing`;
- security, legal, or reputation critical flags;
- user override or user correction.

`CONDUCTOR` stop reasons are also part of the operator surface:

- `STOP_PASS` — enough verification; more review has low value.
- `STOP_STANDBY` — no addressed work.
- `STOP_HANDOFF` — another actor owns the lane.
- `STOP_BLOCKED` — missing external input.
- `STOP_COST` — coordination overhead exceeds remaining value.
- `STOP_USER_DECISION` — business, political, or personal decision required.
- `STOP_SYNC_PROTECTION` is **not** a valid blocker: bridge sync must never be
  stopped because a generated or render-only artifact is dirty. Those issues
  must be handled as generated paths, previews, or non-fatal side artifacts.

Operationally, `CONDUCTOR` should:

- respect `CLAIM-before-work`, but treat stale claims as expiring signals, not
  indefinite lane locks;
- avoid taking a lane as "idle" from a stale read; read to the current
  projection watermark first;
- avoid asking `ADVOCATE` to review low-risk verified drafts with no outbound
  unless legal, security, reputation, or diff risk changed;
- send operator-facing decision briefs instead of long `STATUS` dumps;
- treat explicit operator policies, such as "never block bridge sync", as
  higher-order policy checked before any `BLOCK` / `STOP`;
- fall back to save-to-file plus compact extraction only when a raw read is too
  large and no projection is available yet.

## Message Economy

Every autonomous role must treat a debate post as a scarce coordination event.
Before posting, the role asks whether the message changes another role's next
action or the topic's gate state. If the answer is no, the correct terminal
action is silence plus cursor/claim completion.

Post when the message contains at least one of:

- new source-backed evidence or a direct quote of the artifact checked;
- a concrete rebuttal that changes a risk assessment;
- an implementation plan, patch report, or test result that another role must
  review;
- a CONDUCTOR gate decision, override, escalation, or operator-facing
  completion notice;
- a `[DEFERRED:...]` answer that intentionally moves unresolved work out of the
  current gate.

Do not post for:

- bare ACKs, "agree", "no objection", or duplicated summaries;
- repeated adoption of an already-adopted verdict;
- status messages whose only content is that the worker woke up;
- second copies of another role's evidence when no conclusion changes.

Autonomous workers should use `debate_worker_no_action(...)` for these
low-value cases. This records the claim/cursor outcome without adding another
`debate_messages` row, preserving zero-touch operation while keeping the log
readable.

## Role cadence, control loop, and operator briefing

These are standing role duties (operator mandate, 2026-06-20), part of the
canonical protocol — not optional courtesies. They define *who watches the
channel, how often, who answers whom, and who translates for the human.*

### All roles — check in, then follow at adaptive intervals

Every role (`CONDUCTOR`, `ADVOCATE`, and every `EXECUTOR`) must **announce itself
in the debate on entry** — a one-line role/binding `STATUS` so the channel knows
who is live — and then **follow the topic at adaptive polling intervals**. No role
goes dark while a topic is open. *Adaptive* means the cadence tracks the work, not
a fixed timer:

- **Tighten** (short interval) when there is open `H`/`Q` work, a pending gate, a
  blocked lane, `decision_needed=true`, or active executor work in flight.
- **Loosen** (long interval) when the topic is quiet, all lanes are `STOP_*`, or
  work is parked on an external dependency. Loosening past a few minutes is
  cheaper than tight idle polling — match the interval to how fast the watched
  state can actually change.
- A role with genuinely nothing to add completes its wake with
  `debate_worker_no_action` rather than posting noise — but it keeps watching at
  the loosened cadence.

### CONDUCTOR + ADVOCATE — poll, answer, control, and log everything

`CONDUCTOR` and `ADVOCATE` are the supervisory loop. Beyond the all-roles duty
they must:

- **Poll** the debate at adaptive intervals (per above).
- **Answer the executors** — every executor `Q` / report addressed to them gets a
  reply: a gate verdict, an in-bounds ruling, next-step direction, or an explicit
  `[DEFERRED:...]`. No executor is left hanging.
- **Control the executors** — dispatch, gate, re-scope, hold, or stop lanes; keep
  each lane's owner / status / next-action / stop-condition current.
- **Describe everything in the debate** — every supervisory action (dispatch,
  in-bounds verification, gate `PASS`/`AMEND`/`BLOCK`, merge-go relay, completion)
  is written to the channel as the durable record. The debate is the source of
  truth; any off-channel action that changes a lane's state must be mirrored back
  into it.

### ADVOCATE — explain human-decision problems to the operator in plain language

In addition to the supervisory duty, `ADVOCATE` owns the **operator briefing for
human decisions**. Whenever an item genuinely requires the operator's choice
(business / legal / financial / irreversible / strategy fork — anything in a
"decisions awaiting the operator" set, i.e. the `STOP_USER_DECISION` class),
`ADVOCATE` produces a **short, plain-human explanation**: what the decision is, the
options, the single decisive factor, and the recommendation — so the operator can
decide by reading only that, not the full ledger. This is the human-language layer
over the `HUMAN_BRIEF` projection and the `STOP_USER_DECISION` stop reason. Keep it
short and informative; no transcript dumps.

## Vehicle routing (fail-closed)

Every message carries an optional `vehicle` tag classifying the kind of work
it implies. It exists because bounded, wake-spawned `-W<n>` workers run
**no-edit** — they can analyze and review, but they cannot apply changes.
Dispatching implementation work to such a worker silently bounces.

| `vehicle`        | Meaning                                  | Wake/pump routing |
|------------------|------------------------------------------|-------------------|
| `analysis`       | investigation / read-only reasoning      | → bounded wake-worker |
| `review`         | critique / verification of existing work | → bounded wake-worker |
| `implementation` | code edits, patches, applied changes     | **refused — fail closed** |

The rule:

- **`analysis` / `review`** (and untagged → default `analysis`) resolve to a
  bounded wake-worker exactly as before. No behavior change.
- **`implementation`** **fails closed**. The router REFUSES to allocate a
  no-edit wake-worker with the typed error
  `implementation_requires_impl_vehicle`, instead of spawning a worker that
  would bounce. Implementation-tagged work is handled out-of-band by the
  `CONDUCTOR` via Agent sub-agents (a real, edit-capable vehicle).

This is enforced at two points, both reading the authoritative `vehicle` from
the persisted message row (never from an unauthenticated hook payload):

1. **`claim_worker_session` (hard guard, deepest shared chokepoint).** The
   wake hook (`debate_wake`), the pump hook (`debate_pump`), and the direct
   `debate_worker_claim` MCP tool all funnel through here. An
   `implementation` trigger raises `implementation_requires_impl_vehicle` and
   **no `debate_worker_claims` row is written** — nothing is spawned.
2. **`prepare_wake_dry_run` (signal-only resolution seam).** Returns zero wake
   targets and writes a typed `debate_wake_log` audit row
   (`result=implementation_requires_impl_vehicle`) before any dispatch.

> **Why fail closed?** A guard that merely re-routed `implementation` to a
> wake-worker that then no-action-bounces would just *rename the bounce*. The
> refusal must block the spawn so the work surfaces for a vehicle that can act.

### Conductor-approved impl-vehicle seam

Both enforcement points carry a documented seam for a future
implementation vehicle (claim-for-impl / a dedicated edit-capable IMPL-worker).
When that lands, gate it at these two functions — e.g. accept an
`implementation` claim only when an approved impl-vehicle token is present, or
branch `prepare_wake_dry_run` to resolve impl-worker targets instead of `[]`.
Until then, `implementation` is refused-to-wake and conductor-handled
out-of-band.

## Open-Work Priority

The debate runtime has two different priority layers:

- Message priority: `H`, `M`, `L`, `INFO` inside one topic.
- Topic lane: `P0` through `P7` across open topics.

`P0` is the emergency lane: resource safety, data-loss/privacy exposure,
corruption, or any operator-declared stop-the-line item. `P1` is the next
blocking operational lane. `P2` is urgent/time-sensitive. `P3` is normal
active execution. `P4` through `P7` are progressively deferred, monitor-only,
or archive candidates.

The deterministic ordering contract is:

1. explicit `CONDUCTOR` topic lane stored in `debates.metadata_json`;
2. deadline urgency from `resolve_by`;
3. open H/Q blockers, stale active claims, and missing active role bindings;
4. highest message priority and actionable kind inside the topic;
5. `topic_id` tie-break.

The only sanctioned way to set cross-topic priority is
`debate_set_topic_priority(topic_id, role="CONDUCTOR", lane="P0".."P7",
reason=..., next_action=..., blocked_by=...)`. The canonical queue view is
`debate_work_queue(...)`. Human summaries may mirror that queue, but they are
not the authority.

Human/operator topic creation is not allowed to enter the queue without an
initial lane. The official `debate_init` MCP tool rejects new topics unless
`metadata_json` includes either `conductor_priority.lane` or `priority_lane`
(`P0`..`P7`) plus a priority reason. Operationally this means the launcher must
ask the human for priority, or `CONDUCTOR` must assess and encode the lane
before creating the topic.

## Quick-start: three sessions coordinating

Three roles, one weekend topic, full lifecycle in 12 calls.

```python
# CONDUCTOR session bootstraps the topic
debate_init(
    topic_id="WEEKEND_CODE_RED_2026_05_09",
    title="GBrain release; ship debate v2 by Sunday",
    roles_json='[{"role":"CONDUCTOR","session_id":"sess-c"},'
               ' {"role":"EXECUTOR","session_id":"sess-e"},'
               ' {"role":"ADVOCATE","session_id":"sess-a"}]',
    created_by_role="CONDUCTOR",
)

# CONDUCTOR moves topic to ACTIVE
debate_state(
    topic_id="WEEKEND_CODE_RED_2026_05_09",
    role="CONDUCTOR",
    new_state="ACTIVE",
)

# ADVOCATE files a high-priority Q
debate_post(
    topic_id="WEEKEND_CODE_RED_2026_05_09",
    role="ADVOCATE",
    priority="H", kind="Q",
    body="cursor model robust under timestamp collisions?",
)
# returns {msg_id: "ab12cd34", ts: "2026-05-09T17:55Z", topic_state: "ACTIVE"}

# EXECUTOR reads (cursor resolves from EXECUTOR watermark)
debate_read(
    topic_id="WEEKEND_CODE_RED_2026_05_09",
    role="EXECUTOR",
)
# returns {messages: [...], topic_state: "ACTIVE",
#          last_msg_id_returned: "ab12cd34", count: 1, truncated: false, ...}

# EXECUTOR answers reply_to=Q.msg_id
debate_post(
    topic_id="WEEKEND_CODE_RED_2026_05_09",
    role="EXECUTOR",
    priority="H", kind="A",
    body="compound (ts, msg_id) cursor lands in fixup commit c5458b5",
    reply_to="ab12cd34",
)

# EXECUTOR advances watermark to the latest msg_id seen
debate_post(
    topic_id="WEEKEND_CODE_RED_2026_05_09",
    role="EXECUTOR",
    priority="INFO", kind="WATERMARK",
    body="ab12cd34",
)

# ADVOCATE writes an OODA compaction snapshot
debate_compact(
    topic_id="WEEKEND_CODE_RED_2026_05_09",
    role="ADVOCATE",
    body=(
        "OBSERVE: Q answered.\n"
        "ORIENT: ready for RESOLVED.\n"
        "DECIDE: nothing blocks.\n"
        "ACT: CONDUCTOR transitions next."
    ),
)

# CONDUCTOR resolves
debate_state(
    topic_id="WEEKEND_CODE_RED_2026_05_09",
    role="CONDUCTOR",
    new_state="RESOLVED",
)
# returns {old_state: "ACTIVE", new_state: "RESOLVED", blocking_questions: []}

# Optionally archive
debate_state(
    topic_id="WEEKEND_CODE_RED_2026_05_09",
    role="CONDUCTOR",
    new_state="ARCHIVED",
)
```

If a question goes unanswered for too long or a deadline lapses:

```python
debate_escalate(
    topic_id="WEEKEND_CODE_RED_2026_05_09",
    role="EXECUTOR",
    reason="resolve_by deadline missed",
    target_role="HUMAN",
)
# writes H/PING with body "[ESCALATE:resolve_by deadline missed] target=HUMAN"
```

## Tools

### `debate_init(topic_id, title, roles_json, created_by_role, resolve_by="", metadata_json="")`

Bootstraps a topic. Idempotent — re-calling with the same `topic_id` and
identical `roles_json` returns the existing record. Validates `topic_id`
matches `^[A-Z][A-Z0-9_]+$`, roles is a non-empty list of `{role,
session_id}` dicts, every role matches the role regex. For new official MCP
topics, `metadata_json` must carry an initial topic lane and reason, for
example:

```json
{"priority_lane":"P1","priority_reason":"active trading risk blocks live use"}
```

### `debate_post(topic_id, role, priority, kind, body, reply_to="", standing=None, vehicle="")`

Append-only message insert. **Pre-INSERT validation** of kind-specific
semantics: STATE body must be a legal transition target, WATERMARK body
must be a raw `msg_id` in the same topic (canonical) or a deprecated
keyword form whose ts agrees with the looked-up row (back-compat
grace), DECISION with `reply_to` must point at a `kind=Q` parent,
COMPACTION body must contain OBSERVE / ORIENT / DECIDE / ACT sections
(regex, case-insensitive). Failed validation raises `DebateError` and
leaves no row in `debate_messages`.

State side-effects (`debates.state`, `debate_watermarks` upsert) only
run after the INSERT lands.

`vehicle` (v3.12, optional) classifies the work the message implies —
`analysis` | `review` | `implementation`. Empty/absent stores `NULL` and
reads back as `analysis` (back-compat). An explicit bad value raises a typed
`invalid_vehicle` `DebateError` pre-INSERT (no row written). `vehicle` gates
the wake/pump router — see **Vehicle routing (fail-closed)** below.
`debate_post_with_recipients(..., vehicle="")` accepts the same parameter.

### `debate_read(topic_id, role, since_msg_id="", since_ts="", since_latest_compaction=False, kind_filter_csv="", priority_filter_csv="", limit=200)`

Compound `(ts, msg_id)` cursor read. **Cursor precedence**
(turn-4 fix per `msg:5a2f8c47`):

1. `since_msg_id` (highest — explicit caller intent; raises
   `unknown_since_msg_id` if not in topic).
2. `since_ts`.
3. `since_latest_compaction=True` → DAO selects the latest
   `kind=COMPACTION` row (`MAX(ts) DESC, msg_id DESC` tiebreak) and
   uses it as the cursor. Returns `bootstrap_compaction_msg_id` in the
   response. Falls through to the next rule when no COMPACTION exists.
4. Role watermark (`debate_watermarks[(topic_id, role)]`).
5. Start of topic.

Default `limit=200`, capped at `1000`. Returns `truncated=true` plus
`next_msg_id_cursor` and `next_ts_cursor` when more remain. Does
**not** auto-advance the watermark — caller writes a `WATERMARK`
message via `debate_post` (or `debate_advance_watermark`) when ready.

#### Watermark contract

`debate_post` with `kind="WATERMARK"` accepts:

- **Canonical**: body is a raw `msg_id` matching `MSG_ID_RE`
  (`^[a-f0-9]{8}(?:[a-f0-9]{4})?$` — 8-char rows from v3.9.0–v3.9.2 or
  12-char rows from v3.9.3+). DAO derives `ts` from the message row,
  so the body cannot tamper with the timestamp.
- **Deprecated keyword form**: `processed_up_to=<ts>:<msg_id>` or
  `processed_up_to_ts=<ts> processed_up_to_msg_id=<msg_id>`. Parsed
  for back-compat via `re.fullmatch` (v3.9.3 ADVOCATE turn-5 fix
  msg:b246664b — the prior `re.search` was unanchored and silently
  dropped trailing/leading junk, accepting `processed_up_to=<ts>:<valid>ffff`
  by truncating to the valid prefix). The full body must conform; any
  extra characters before or after raise `invalid_watermark_body`.
  Additionally **rejected with `watermark_ts_mismatch` if the body's
  ts disagrees with the looked-up row**. New callers MUST use the
  canonical form.

The `debate_watermarks` row is updated atomically with both
`last_processed_msg_id` AND `last_processed_ts` columns so the
compound `(ts, msg_id)` cursor never falls back to ts-only.

### `debate_advance_watermark(topic_id, role, processed_up_to_msg_id)`

Convenience wrapper around `debate_post(kind="WATERMARK")`. Takes only
a `msg_id`, looks up its `ts` from the message row, and writes a
canonical msg_id-only WATERMARK message. Reduces caller error surface
vs constructing the body by hand. Raises
`unknown_msg_id_for_watermark` if the `msg_id` is not in the topic.

### `debate_set_topic_priority(topic_id, role, lane, reason, next_action="", blocked_by="")`

Sets the CONDUCTOR-owned cross-topic priority lane in `debates.metadata_json`.
Only `role="CONDUCTOR"` may set it. The lane must be `P0`..`P7`, and `reason`
is mandatory.

### `debate_work_queue(states_csv="INIT,ACTIVE", topics_csv="", limit=50)`

Returns open topics in canonical scheduling order with `lane`,
`priority_score`, `reason_codes`, `next_action`, `blocked_by`, open-question
counts, stale-claim counts, missing active role bindings, and latest message
metadata. Automation and operators should consult this surface before waking
workers when there is a backlog.

### `debate_state(topic_id, role, new_state, reason="")`

Transition through `INIT → ACTIVE → RESOLVED → ARCHIVED`. Backward and
diagonal transitions are rejected by the validator. RESOLVED transition
checks every open `Q` (any priority) for a matching `A` reply. An `A`
whose body starts with `[DEFERRED:` counts as a matched answer
(resolution-equivalent — the question is intentionally deferred for
follow-up). When the gate blocks, returns `new_state == old_state` and
populates `blocking_questions[]`.

### `debate_escalate(topic_id, role, reason, target_role="HUMAN")`

Force-writes an `H`-priority `PING` with body `[ESCALATE:reason]
target=<target_role>`. Triggered on deadline miss, unanswered H/Q,
contradictory `DECISION`, format violations.

### `debate_compact(topic_id, role, body, since_ts="", until_ts="")`

Append a `COMPACTION` snapshot. `body` must include all four OODA
section headers (regex). Optional `since_ts` and `until_ts` are
metadata-only ISO bounds — readers can bootstrap from the latest
compaction + incremental tail instead of replaying the full log.

## State transition diagram

```
                     debate_init
                          │
                          ▼
                       ┌──────┐
                       │ INIT │
                       └──────┘
                          │
                          │ debate_state(new_state="ACTIVE")
                          ▼
                       ┌────────┐
                       │ ACTIVE │  ◄── debate_post (Q/A/STATUS/...)
                       └────────┘      debate_compact
                          │
                          │ debate_state(new_state="RESOLVED")
                          │   gate: all open Qs answered
                          │   (or [DEFERRED: prefix on A)
                          ▼
                       ┌──────────┐
                       │ RESOLVED │  ◄── only STATE → ARCHIVED allowed
                       └──────────┘
                          │
                          │ debate_state(new_state="ARCHIVED")
                          │   sets archived_at
                          ▼
                       ┌──────────┐
                       │ ARCHIVED │  read-only; no further posts
                       └──────────┘
```

`debate_escalate` may be called in `ACTIVE` only — emits an
`H/PING` message but does **not** transition state.

## How it compares to ad-hoc alternatives

| | Ad-hoc KG observations | Naive polling | Debate Protocol v2 |
|---|---|---|---|
| Channel | One entity per debate, but observations are unstructured strings | Multiple entities, no shared topic | One row per debate; messages reference `topic_id` |
| Format | Free-form prefix conventions (drift) | Whatever structure you remember | Atomic columns; CHECK enums; regex validators |
| Cursor | None — readers re-scan full log every turn | Manual offset tracked per-agent | `debate_watermarks` per role; compound `(ts, msg_id)` |
| Lifecycle | None | Implicit — closed when humans say so | INIT/ACTIVE/RESOLVED/ARCHIVED with gate on RESOLVED |
| Escalation | Human eyeballs the log | Periodic crontab | `debate_escalate` emits structured H/PING |
| Compaction | Manual archival | None | `debate_compact` with OODA-validated body |
| Atomicity | None — partial writes possible | None | Pre-INSERT validation; no row on raise |
| Empirical no-LLM proof | Not applicable | Not applicable | `socket.socket` monkey-patched paranoid test |

## Bridge with existing memory

- **No legacy migration.** The pre-v2 raw observations on
  `intersession-debate-log-2026-05-09` are preserved as historical
  record. Future debates use the new tools from `t = 0`.
- **Bridge sync compatibility.** All three new tables sync via the
  existing `~/.claude/memory/bridge` JSON pipeline; no per-field LWW
  semantics needed because the log is append-only and watermarks are
  per-role per-topic (no shared-write contention).
- **Reflection cross-reference.** `MemoryReflection_LLMFreeArchitecture`
  KG entity documents the durability thesis; the paranoid test in
  `tests/test_debate_paranoid.py` is the testable expression that the
  debate hot path opens zero network sockets.

## Operational anti-patterns

- **Don't post to a debate with raw `add_observations`.** Use the tools.
  Atomicity, validation, and watermarks all live in the DAO.
- **Don't migrate legacy logs.** Risk of corruption is high; signal value
  of preservation is also high. Start fresh debates with new
  `topic_id`.
- **Don't auto-advance watermarks on read.** Reading is informational;
  the explicit `WATERMARK` post is the contract that signals "I have
  processed this point in the log." Without it, accidental reads from a
  shell would skip messages forever.
- **Don't skip `[DEFERRED:` markers when answering optional questions.**
  Otherwise the strict-RESOLVED gate will block. Either answer or
  explicitly defer; no quiet abandonment.
- **Don't introduce LLM calls in any debate code path.** The
  paranoid test enforces this; CI rejects regressions. The protocol is
  positioned as a no-LLM-cost differentiator.

## Cross-references

- Source spec: `intersession-debate-log-2026-05-09` KG entity (legacy
  observations) + this document.
- Memory note: `feedback_intersession_protocol.md` (created
  post-weekend retrospective; captures the failure modes that motivated
  the protocol).
- Live trace example: `docs/DEBATE_DEMO.md` (post-CC-restart, planned
  Sunday — exercises the new tools end-to-end on the next debate
  topic).
- Schema: `schema.py` `_SCHEMA_SQL` block (search for "Debate Protocol v2").
- DAO: `debate.py`.
- MCP tool wrappers: `intel_server.py` Tools 25-31.
- Test battery: `tests/test_debate_*.py` (155 tests including
  `test_debate_turn3.py` (COMPACTION bootstrap + watermark msg_id
  enforcement + unknown-cursor raise) and `test_debate_turn4.py`
  (cursor precedence + watermark security against ts tampering); the
  socket-blocked LLM-free proof at `test_debate_paranoid.py` is also
  in this battery).
