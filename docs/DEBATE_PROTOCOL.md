# Debate Protocol v2

> Productized inter-session coordination for AI agents. Single channel
> per debate, atomic message format, role-aware watermarks, explicit
> lifecycle state machine, escalation hooks, append-only log with
> compaction.
>
> Lives in `sqlite-memory-mcp` as part of the `sqlite-intel` MCP server.
> Seven tools: `debate_init`, `debate_post`, `debate_read`, `debate_state`,
> `debate_escalate`, `debate_compact`, `debate_advance_watermark`. Three
> tables: `debates`, `debate_messages`, `debate_watermarks`.

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
- **Compaction.** `kind=COMPACTION` snapshots are append-only OODA
  digests (regex-validated body). Future readers bootstrap from the
  latest compaction + incremental tail instead of full history.

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
session_id}` dicts, every role matches the role regex.

### `debate_post(topic_id, role, priority, kind, body, reply_to="")`

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
