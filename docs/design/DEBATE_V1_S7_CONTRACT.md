# Debate Protocol `debate/v1`: §7 deterministic server contract

Status: implemented contract, 2026-07-22
Canonical management note: `9cc692e5-d011-4ba4-8d4a-66b2f7404196`

## North Star

The debate runs as fast as the local OS and machine safely allow. Addressed
committed work wakes automatically; no human copies, pastes, polls, or wakes an
agent. The scheduler is event-first and uses deterministic 1/2/5/10/30-second
retry/backoff plus a 30-second durable crash-replay sweep. Responses are typed
and structured unless a message explicitly declares `body_mode=live_text`.
Missing ownership is repaired to the same semantic role. Human intervention is
represented only by a typed `ESCALATE` packet and consumed through Task Tray's
`Waiting on Me`, `Recent Decisions`, and `Debate by Topic` tabs.

Correctness never depends on an agent obeying a prompt. Prompts help a worker
produce a valid proposal; SQLite constraints and the transactional DAO decide
whether it exists.

## Authoritative envelope and state

`debate_messages` remains the append-only source ledger and adds:

- `protocol_version = debate/v1`
- server-assigned `round_no`
- `body_mode = structured | live_text`
- kind-specific canonical `payload_json`

Semantic kinds are `CLAIM`, `CHALLENGE`, `EVIDENCE`, `REBUT`, `CONCEDE`,
`VERIFY`, `DISSENT`, and `ESCALATE`. Legacy conversational kinds
`Q/A/STATUS/DECISION` are rejected on configured `debate/v1` topics; control
kinds such as `STATE`, `PING`, and `WATERMARK` remain available.

The topic lifecycle (`INIT/ACTIVE/RESOLVED/ARCHIVED`) remains separate from the
protocol micro-state:

`BLIND_CLAIM -> DEBATE -> ADJUDICATE -> STOPPED`

Any bounded-round, timeout, or AB/BA disagreement transition goes to
`STALEMATE`; a single idempotent human packet moves it to `ESCALATED`.

## Transactional invariants

All rules execute through the shared `post_message` choke point under the
caller's `BEGIN IMMEDIATE` transaction, before the message insert:

1. Validate protocol version, kind-specific payload, body mode, role ownership,
   phase, server round, and same-topic `reply_to` target.
2. Reject `CHALLENGE/EVIDENCE/REBUT/CONCEDE/VERIFY/DISSENT` without a valid,
   compatible target; payload target must exactly equal `reply_to`.
3. Reject a debate turn with `STALE_READ` while the author has an unread,
   addressed H-priority message beyond its compound `(ts,msg_id)` cursor.
4. Hide the first two independent claims until both are committed. The same
   visibility predicate protects thread reads, signal reads, cursor operations,
   LIKE search, hybrid FTS retrieval, wake dispatch, and Task Tray.
5. Cap debate at three rounds. A contested round-three `VERIFY` creates durable
   `STALEMATE`; only one `DISSENT` per semantic role and one `ESCALATE` packet
   per protocol generation are accepted afterward.
6. `ESCALATE` requires a declared human/operator recipient and a structured
   decision question, options, decisive evidence, consequences, unresolved
   point, and exact requested human action.
7. Reject an exact typed-act replay with `DUPLICATE_ACT`; a retry cannot insert
   a second message or advance the round. Unresolved challenges remain blockers
   across round boundaries until a `REBUT` or `CONCEDE` targets them.
8. `VERIFY` must come from a role outside the two opposing blind roles.

Any validation failure is zero mutation: no message, recipient, state,
visibility, wake, or audit row is committed.

## Adjudication, recovery, and scheduling

The judge receives two server-generated normalized projections, AB and BA.
Their source positions must come from the two configured opposing roles. A
verdict role must be declared, active, non-human, and outside those opposing
roles. Matching immutable verdicts stop the protocol; disagreement creates
`STALEMATE`. Replaying an identical verdict is a state-version no-op. The
source messages are not reordered or rewritten.

The pump runs phase-timeout and role-ownership sweeps. A missing active binding
is replaced by a new generation of the same role; quiet bindings are not
declared dead without objective evidence. Existing PID/create-time worker reap
and durable trigger replay recover proven-dead hidden workers without advancing
the cursor or changing semantic role.

Each `debate/v1` derived worker is scoped to exactly its claimed trigger. Its
signal read replays that trigger even when the parent cursor has already moved
past it; it cannot read or advance through a sibling worker's later trigger.
Only a delivered-and-advanced claimed trigger can authorize its one semantic
response.

Post-commit Windows events are the immediate path. With no event, the scheduler
uses these deterministic projections:

- eligible backlog and free capacity: `0s`;
- active lease/capacity wait: `1s`;
- failed/throttled retry: `1,2,5,10,30s`, capped at `30s`;
- idle durable replay sweep: `30s`;
- resource block: the governor's explicit interval.

Decision reason and interval are logged; changed scheduler projections are also
persisted in `debate_scheduler_decisions`.

## Task Tray contract

- `Waiting on Me`: open typed `ESCALATE` packets are authoritative; legacy
  heuristics remain only for pre-v1 topics.
- `Recent Decisions`: legacy decisions plus typed `VERIFY`, `CONCEDE`,
  `DISSENT`, and `ESCALATE` outcomes.
- `Debate by Topic`: topic lifecycle plus protocol phase, round/max rounds,
  stalemate reason, typed envelope, and reply topology.

No tab can display an unreleased blind claim.

## Specification hierarchy

When sources disagree, use this order:

1. Canonical note `9cc692e5-d011-4ba4-8d4a-66b2f7404196`.
2. Implementation task `0d806934-f5d3-4df0-bf21-15b987dbb4a3` and zero-paste
   delivery note `78fb459e-e6f6-4e57-b710-1f6bf75462a7`.
3. This executable contract, schema, DAO, pump, and tests on the hardened main
   lineage from `c1e4998`.
4. Research artifact
   `C:\Users\rmanov\debate_protocol_research_2026-07-21\artifact.json`.
5. Historical protocol note `b4623ded` only as a gap-filling fallback.

## Definition of Done

Done means fresh and legacy schema migration is lossless; validation and
concurrency tests prove zero partial writes; blind content cannot leak through
any projection; stale reads, round cap, timeout, DISSENT/ESCALATE uniqueness,
AB/BA adjudication, same-role recovery, adaptive scheduling, crash replay, and
the three tray tabs pass focused and full regression; Windows live smoke proves
post-commit auto-wake and exact `reply_to`; and deployed `doctor` reports one
healthy pump. No acceptance criterion is phrased as “the agent should remember.”
