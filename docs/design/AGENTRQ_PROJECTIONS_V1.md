# AgentRQ Control-Plane Projections v1 — Design Spec

> **Wave-2 item B2.** Status: **DESIGN ONLY.** This document defines seven
> read-projections over the *existing* debate substrate. It introduces **no
> code, no schema, no tables, no tools, no migrations.** Every projection
> named here is post-submit-implementable in a later vehicle; this item ships
> the contract, the substrate map, the invariant naming, and the naming-collision
> guard — nothing executable. See §7 (Scope Fence) for the binding constraint.

---

## 0. Purpose and framing

"AgentRQ" is the control-plane *read* surface that an operator (and the
`CONDUCTOR` role acting on the operator's behalf) consults instead of replaying
the full debate transcript. `DEBATE_PROTOCOL.md:78-89` already establishes the
intent: when an operator says "check the debate", `CONDUCTOR` should **not**
default to a full `debate_read` replay — it should read bounded projections
first, and drill back to raw events only for rows that need a decision, carry
risk, contradict another projection, or require exact evidence
(`DEBATE_PROTOCOL.md:88-89`).

This spec takes that informal intent and pins down **seven** named
read-projections as a versioned control-plane contract (`AgentRQ v1`). The
governing principle is **reuse over invention**: the debate substrate already
records every fact each projection needs. A projection is a *deterministic read
shape* over existing tables/tools — never a new write path, never a new source
of truth.

ADVOCATE control reference for this item: `9c57dafca031`.

### 0.1 Definition of "read-projection"

A read-projection is:

1. **Derived** — computed entirely from existing substrate rows; it holds no
   authoritative state of its own.
2. **Deterministic** — same substrate + same parameters ⇒ same output ordering
   and content (so two readers agree, and a reader and a test agree).
3. **Bounded** — returns the *smallest actionable view* for one actor, not a
   transcript (`DEBATE_PROTOCOL.md:74-76`).
4. **Side-effect-free at read time** — reading a projection MUST NOT mutate the
   substrate. (Cursor-advancing operations like `debate_signal_advance` are
   explicitly *not* projections; they are write paths invoked separately.)

---

## 1. Substrate inventory (the existing source of truth)

Every projection in §2 reads from this fixed set of already-shipped tables and
tools. No row below is created by this item.

| Substrate object | Defined at | Role in control plane |
| --- | --- | --- |
| `debates` | `schema.py:696` | Topic record + lifecycle state (`INIT→ACTIVE→RESOLVED→ARCHIVED`). |
| `debate_messages` | `schema.py:713` | Append-only event log: `role`, `priority`, `kind`, `vehicle`, `reply_to`, `body`. |
| `debate_message_recipients` | `schema.py:764` | WHO an event is addressed to (normalized; replaced the CSV `addressed_to` anti-pattern). |
| `debate_signal_state` | `schema.py:774` | Per-`(session_id, role, topic_id)` read cursor. |
| `debate_watermarks` | `schema.py:743` | Per-`(topic_id, role)` processed cursor. |
| `debate_role_bindings` | `schema.py:792` | Runtime authority: which concrete `session_id` owns/diagnoses a role now (one `active` per `(topic_id, role)` via `idx_drb_one_active`, `schema.py:808`). |
| `debate_worker_claims` | `schema.py:848` | Derived `-W<n>` worker claim per trigger; states `active/completed/retired`. |
| `debate_message_claims` | `schema.py:873` | Per-`(msg_id, role)` claim ownership; states `active/done`. |
| `debate_wake_log` | `schema.py:816` | Idempotent wake audit (`idx_dwl_once`, `schema.py:835`). |
| `daily_dashboard` | `db_utils.py:1415` | Machine-local operator dashboard rows: `(day, task_id, kind, slot, body, priority, src_msg_id, author, updated_at)`. Single-writer (see §3). |
| `debate_work_queue` (tool) | `intel_server.py:1966` → `_debate_list_open_work_dao` | Deterministic open-work priority view over `debates`. |
| `debate_worker_claim` (tool) | `intel_server.py:1835` | Idempotent worker allocation over `debate_worker_claims`. |
| `assert_dashboard_conductor_writer` (guard) | `db_utils.py:1449` | Fail-closed single-writer guard for `daily_dashboard` (see §3). |

### 1.1 Dashboard kind/priority vocabulary (existing)

`daily_dashboard.kind` is constrained to the existing enum
(`db_utils.py:118-125`): `result, option, decision, difficulty,
misunderstanding, advice`. Priority is `H, M, L` (`db_utils.py:126`) with a
per-kind cap of `DASHBOARD_KIND_CAP = 8` (`db_utils.py:127`). **No projection in
this spec adds a kind, a priority level, or a column** — the dashboard-rows
projection (§2.7) reads this vocabulary as-is.

---

## 2. The seven read-projections

Each projection below carries: **(a)** an intent one-liner, **(b)** a
classification — `EXISTING-SUBSTRATE-REUSE` or `NET-NEW` — **(c)** the cited
substrate table/tool it reads, and **(d)** the read shape (parameters → output).
`NET-NEW` here means "no single existing tool returns this exact shape today, so
a later vehicle must implement a read-only DAO/tool over the cited substrate" —
it never means new state.

### 2.1 `conductor_inbox`

- **Intent:** Messages addressed to `CONDUCTOR`/operator, blocked lanes,
  decision-needed rows, stale claims, and sync anomalies — the "what needs me"
  view (`DEBATE_PROTOCOL.md:81-82`, `94-95`).
- **Classification:** `EXISTING-SUBSTRATE-REUSE` (composition; no net-new
  source of truth). A net-new *read DAO* may compose it, but every input row
  already exists.
- **Substrate read:**
  - addressed events → `debate_message_recipients` (`schema.py:764`) JOIN
    `debate_messages` (`schema.py:713`) WHERE `recipient='CONDUCTOR'` (or the
    operator recipient token).
  - decision-needed → `debate_messages.kind IN ('Q','DECISION')`
    (`schema.py:721`).
  - stale claims → `debate_message_claims.state='active'` past heartbeat
    (`schema.py:873`) and `debate_worker_claims.state='active'` past heartbeat
    (`schema.py:848`).
  - blocked lanes → CONDUCTOR priority-lane metadata set via
    `debate_set_topic_priority` (`intel_server.py:1965` region; `blocked_by`
    param at `intel_server.py:1943`).
- **Read shape:** `(topic_id?, recipient='CONDUCTOR', limit)` →
  ordered list of `{msg_id, kind, priority, lane, reason, age}` rows. Bounded;
  not a transcript.

### 2.2 `human_brief`

- **Intent:** At most 7-10 bullets — what changed, what closed, what is waiting,
  what needs an operator decision, what is risky, the recommended next move, and
  what does **not** need reading (`DEBATE_PROTOCOL.md:83-85`, `93`).
- **Classification:** `EXISTING-SUBSTRATE-REUSE` (a bounded *summary read* over
  the same substrate as `conductor_inbox`; the brief is a presentation of
  existing events, never a stored artifact).
- **Substrate read:**
  - "what closed" → `debates.state` transitions to `RESOLVED/ARCHIVED`
    (`schema.py:699-700`, `debates.archived_at` `schema.py:704`).
  - "what changed / what is waiting" → recent `debate_messages` filtered by
    `kind`/`priority` (`schema.py:718-722`).
  - "what needs a decision" → same `Q`/`DECISION` filter as §2.1.
  - "recommended next move" → CONDUCTOR `next_action` lane metadata
    (`intel_server.py:1942`).
- **Read shape:** `(topic_id?, since_ts?, max_bullets=10)` → ordered bullet list
  capped at 10. The cap is part of the contract: a `human_brief` that exceeds 10
  bullets is non-conforming and MUST be re-bounded by the implementer, not
  by widening the projection.

### 2.3 `permission_request`

- **Intent:** Surface the subset of addressed events that ask the operator to
  *grant or deny* something (a gated action the agent will not self-authorize) —
  the human-in-the-loop approval queue.
- **Classification:** `NET-NEW` (read shape) over **existing** substrate. No
  current tool returns "events awaiting a human grant/deny" as a distinct
  shape; a later vehicle adds a read-only DAO. **No new table is needed** — the
  request and its answer are both `debate_messages` rows.
- **Substrate read:**
  - request events → `debate_messages.kind='Q'` (`schema.py:721`) addressed to
    the operator recipient in `debate_message_recipients` (`schema.py:764`),
    optionally tagged via existing `body`/`metadata` convention.
  - pending vs. answered → an answering `debate_messages.kind='A'`
    (`schema.py:721`) linked by `reply_to` (`schema.py:728`); absence of a
    reply ⇒ pending.
- **Read shape:** `(topic_id?, recipient=operator, status='pending')` →
  list of `{request_msg_id, asked_by_role, body, age, reply_to?}`.
- **Naming note:** named `permission_request` (a *debate* event view). It is
  **distinct from and MUST NOT be confused with** the PREMIUM RBAC table
  `premium_control_plane_cache` (`schema.py:636`) — see §4. This projection
  reads debate events; it is not an ACL/policy store and grants nothing.

### 2.4 `work_artifact_manifest`

- **Intent:** For a unit of dispatched work (a worker claim / trigger), list the
  artifacts that work produced or references, so an operator/ADVOCATE can see
  "what did this dispatch actually yield" without a transcript walk.
- **Classification:** `NET-NEW` (read shape) over **existing** substrate.
- **Substrate read:**
  - the dispatch → `debate_worker_claims` (`schema.py:848`), keyed by
    `(topic_id, role, parent_session_id, trigger_msg_id)` with
    `details_json` (`schema.py:862`) and `ack_msg_id` (`schema.py:861`).
  - produced events → `debate_messages` authored by the worker session,
    correlated via `reply_to`/`ack_msg_id` and `vehicle`
    (`schema.py:725-728`).
- **Read shape:** `(topic_id, trigger_msg_id | worker_session_id)` →
  `{claim_state, claimed_at, completed_at, ack_msg_id, referenced_msg_ids[]}`.
- **MANDATORY naming guard (see §4):** this projection is named
  `work_artifact_manifest`, **deliberately scoped** to avoid the PREMIUM
  code-signing table `premium_artifact_manifests` (`schema.py:617`). The
  `work_` qualifier is load-bearing. This projection is a *debate work* view
  (which messages a dispatch produced); it has **nothing to do** with
  entrypoint SHA-256 signing, `contract_version`, or `protection_phase`
  (`schema.py:621-625`). Reusing the bare term `artifact_manifest` is
  **prohibited** by this spec.

### 2.5 `single-writer-invariant`

- **Intent:** The named, documented guarantee that `daily_dashboard` has exactly
  one authorized writer per day — the active `CONDUCTOR` session — enforced
  fail-closed.
- **Classification:** `EXISTING-SUBSTRATE-REUSE` (this is **already enforced** by
  `assert_dashboard_conductor_writer`, `db_utils.py:1449`). This spec *names and
  documents* the invariant; it changes nothing. See §3 for the formal
  statement.
- **Substrate read (verification shape):**
  - active writer → `debate_role_bindings` WHERE
    `role='CONDUCTOR' AND state='active'` ORDER BY `generation DESC LIMIT 1`
    (`db_utils.py:1476-1480`; uniqueness backed by `idx_drb_one_active`,
    `schema.py:808`).
  - audit stamp → `daily_dashboard.author` / `updated_at`
    (`db_utils.py:1423-1424`).
- **Read shape:** `(day?)` → `{topic_id, active_conductor_session_id,
  is_held: bool}`. This is a *read of the invariant's current binding*, not the
  enforcement (enforcement is the guard at write time).

### 2.6 `get_next_work`

- **Intent:** "Give me the next actionable unit of open work in deterministic
  CONDUCTOR priority order" — the executor/worker pull view.
- **Classification:** `EXISTING-SUBSTRATE-REUSE`. The deterministic open-work
  ordering **already exists** as the `debate_work_queue` tool
  (`intel_server.py:1966` → `_debate_list_open_work_dao`, imported at
  `intel_server.py:84`). `get_next_work` is the *singular projection* of that
  list: "head of the queue, optionally filtered to my role/lane."
- **Substrate read:**
  - open topics in priority order → `debate_work_queue`
    (`intel_server.py:1966`), parameters `states_csv` (default
    `"INIT,ACTIVE"`, `intel_server.py:1968`), `topics_csv`, `limit`.
  - "is this already taken" → `debate_worker_claims.state`
    (`schema.py:854`) and `debate_message_claims.state` (`schema.py:877`)
    so the head skips work already claimed.
- **Read shape:** `(role?, lane?, exclude_claimed=true)` →
  zero-or-one `{topic_id, trigger_msg_id, priority, lane}`. Returning at most
  one item is the contract distinction from `debate_work_queue` (which returns
  the full ordered list). `get_next_work` does **not** claim — claiming is the
  separate `debate_worker_claim` write path (`intel_server.py:1835`). Read and
  claim stay decoupled so the projection remains side-effect-free (§0.1.4).

### 2.7 `dashboard-rows`

- **Intent:** The operator's machine-local dashboard for a day — the bounded set
  of `result/option/decision/difficulty/misunderstanding/advice` rows.
- **Classification:** `EXISTING-SUBSTRATE-REUSE`. Reads `daily_dashboard`
  (`db_utils.py:1415`) directly; the table, its kind/priority enums, and its
  per-kind cap already exist.
- **Substrate read:**
  - rows → `daily_dashboard` for `(day, task_id?)` ordered by `updated_at DESC`,
    then priority rank (`_priority_rank_sql`, `db_utils.py:1493`), then `slot`
    (mirroring the existing prune ordering at `db_utils.py:1513-1514`).
  - cap → `DASHBOARD_KIND_CAP = 8` per kind (`db_utils.py:127`), already
    enforced by `_prune_dashboard_rows` (`db_utils.py:1497`).
- **Read shape:** `(day?, task_id?, kind?)` → ordered rows
  `{kind, slot, body, priority, src_msg_id, author, updated_at}`. Read-only:
  this projection never writes; writes go through the single-writer path (§3).

### 2.8 Classification summary

| Projection | Classification | Primary substrate cite |
| --- | --- | --- |
| `conductor_inbox` (§2.1) | EXISTING-SUBSTRATE-REUSE | `debate_message_recipients` `schema.py:764` + `debate_messages` `schema.py:713` |
| `human_brief` (§2.2) | EXISTING-SUBSTRATE-REUSE | `debates` `schema.py:696` + `debate_messages` `schema.py:713` |
| `permission_request` (§2.3) | NET-NEW (read shape) | `debate_messages` `Q`/`A` `schema.py:721` + `debate_message_recipients` `schema.py:764` |
| `work_artifact_manifest` (§2.4) | NET-NEW (read shape) | `debate_worker_claims` `schema.py:848` + `debate_messages` `schema.py:713` |
| `single-writer-invariant` (§2.5) | EXISTING-SUBSTRATE-REUSE | `assert_dashboard_conductor_writer` `db_utils.py:1449` + `debate_role_bindings` `schema.py:792` |
| `get_next_work` (§2.6) | EXISTING-SUBSTRATE-REUSE | `debate_work_queue` `intel_server.py:1966` |
| `dashboard-rows` (§2.7) | EXISTING-SUBSTRATE-REUSE | `daily_dashboard` `db_utils.py:1415` |

> Two of seven are `NET-NEW` *read shapes* only; **zero** are net-new state.
> Every `NET-NEW` projection is implementable as a read-only DAO over already
> existing rows in a later vehicle.

---

## 3. The single-writer invariant (formally named)

This spec formally names the guarantee enforced by
`assert_dashboard_conductor_writer` (`db_utils.py:1449-1490`):

> **INVARIANT `AGENTRQ-SW-1` (Dashboard Single-Writer).** For any given `day`,
> the only session permitted to write `daily_dashboard` rows is the session that
> holds the **active** `CONDUCTOR` role binding for that day's topic. A write
> attempt by any other session — or with no `writer_session` supplied, or when
> no active `CONDUCTOR` binding exists — is **denied fail-closed** with a
> `PermissionError`.

**Enforcement mechanics (existing, unchanged):**

1. The guard resolves the day's topic via `dash_topic_id(day)`
   (`db_utils.py:1469`).
2. It requires a non-empty `writer_session`; empty ⇒ `PermissionError`
   (`db_utils.py:1470-1474`).
3. It looks up the single active `CONDUCTOR` binding ordered by `generation
   DESC` (`db_utils.py:1475-1480`); absence ⇒ `PermissionError`
   (`db_utils.py:1481-1484`).
4. It compares the active binding's `session_id` to `writer_session`; mismatch
   ⇒ `PermissionError` (`db_utils.py:1486-1490`).
5. A test-only override (`SQLITE_MEMORY_DASH_TEST_OVERRIDE` /
   `allow_test_override`) bypasses the check for tests
   (`db_utils.py:1439-1446`, `1467-1468`) — production code never sets it.

**Stated honestly (per the guard's own docstring, `db_utils.py:1456-1465`):**
`AGENTRQ-SW-1` is a **cooperative session-binding guard, not an identity proof
or security boundary.** `writer_session` is caller-supplied, so anything that
knows the active `CONDUCTOR` `session_id` can pass it. The real protection is
(a) deployment shape — executors have no CLI/MCP path to the dashboard writer —
plus (b) the `author='conductor'` / `updated_at` audit stamp on every row
(`db_utils.py:1423-1424`). **This spec does not upgrade the invariant into an
auth mechanism**; doing so would be a security-relevant change and is explicitly
out of scope (recorded in blockers as the safer choice). The `single-writer-invariant`
read-projection (§2.5) merely *exposes which session currently holds the writer
right*; it neither enforces nor strengthens it.

The uniqueness backing `AGENTRQ-SW-1` (at most one `active` `CONDUCTOR` per
`(topic_id, role)`) is the partial unique index `idx_drb_one_active`
(`schema.py:808-810`).

---

## 4. MANDATORY naming-collision guard

Two PREMIUM tables in `schema.py` carry names dangerously close to two
control-plane concepts in this spec. **This section is a hard constraint, not a
suggestion.**

| PREMIUM table (do NOT overload) | Defined at | What it actually is | Spec name that MUST stay scoped |
| --- | --- | --- | --- |
| `premium_artifact_manifests` | `schema.py:617` | PREMIUM **code-signing** manifest: `entrypoint_sha256`, `contract_version`, `protection_phase`, host-version bounds (`schema.py:621-627`). | `work_artifact_manifest` (§2.4) — the `work_` qualifier is mandatory. |
| `premium_control_plane_cache` | `schema.py:636` | PREMIUM **ACL/RBAC** policy cache: `policy_id`, `scope_key`, `payload_json` (`schema.py:637-643`). | `permission_request` (§2.3) — a debate-event view, NOT a policy/ACL store. |

**Binding rules:**

1. **No projection, DAO, tool, or table introduced by any later AgentRQ vehicle
   may be named `artifact_manifest` or `control_plane`** (bare, unqualified).
   The bare terms are reserved for the PREMIUM tables above.
2. The work-product view MUST use the scoped name `work_artifact_manifest` (or a
   more explicit qualifier such as `debate_work_artifacts`). It MUST NOT read,
   write, or semantically alias `premium_artifact_manifests`.
3. The approval-queue view MUST use `permission_request` (or
   `debate_permission_request`). It MUST NOT read, write, or semantically alias
   `premium_control_plane_cache`, and it grants nothing — it only *surfaces*
   events that ask a human to decide.
4. Code-signing semantics (SHA-256, signing, `protection_phase`) and RBAC/policy
   semantics live **exclusively** in the PREMIUM tables. AgentRQ projections are
   **read views over the debate substrate** and must never absorb those
   semantics.

Rationale: overloading either bare name would let a free-tier debate projection
shadow a PREMIUM security primitive — a correctness *and* monetization hazard.
The scoped names make the two domains non-aliasable by construction.

---

## 5. Determinism and ordering contract

So two readers (and a reader vs. a test) never disagree:

- **List projections** (`conductor_inbox`, `dashboard-rows`, the list behind
  `get_next_work`) order by the **existing** deterministic keys already used in
  the substrate — e.g. `daily_dashboard` rows follow `updated_at DESC`, then the
  existing `_priority_rank_sql` rank (`db_utils.py:1493`), then `slot`
  (`db_utils.py:1513-1514`); open work follows the deterministic CONDUCTOR
  order already produced by `debate_work_queue` (`intel_server.py:1966`).
- **Bounded projections** (`human_brief`) carry an explicit cap (≤10 bullets,
  `DEBATE_PROTOCOL.md:83`) as part of the contract; exceeding the cap is
  non-conforming.
- **Singular projections** (`get_next_work`) return zero-or-one row; ties are
  broken by the same deterministic order as the underlying list, so the "next"
  item is stable.

No projection introduces a new ordering key; each reuses what the substrate
already guarantees.

---

## 6. What this item explicitly does NOT do (anti-scope)

- It does **not** create any table, column, index, trigger, or enum value.
- It does **not** add or modify any MCP tool, DAO, or CLI command.
- It does **not** write a migration (no `schema.py` version bump).
- It does **not** change `assert_dashboard_conductor_writer` or upgrade
  `AGENTRQ-SW-1` into an authentication/identity mechanism (deliberately the
  safer choice; recorded in blockers).
- It does **not** touch the PREMIUM tables (`schema.py:617`, `schema.py:636`) or
  alias their names.
- It does **not** implement the two `NET-NEW` read shapes; it only specifies
  them for a later vehicle.

---

## 7. Scope fence (binding)

**DESIGN ONLY. POST-SUBMIT-IMPLEMENTABLE.** This document is the v1 contract for
AgentRQ control-plane projections. Implementation — read-only DAOs/tools for the
two `NET-NEW` shapes, and projection wrappers for the five reuse shapes — is a
separate, later Wave item. **No code, schema, table, tool, or migration is part
of this B2 item.** Any future implementer MUST honor §4 (naming guard), §3
(`AGENTRQ-SW-1` stays cooperative, not an auth boundary, unless a dedicated
security item revisits it), and §0.1 (projections stay read-only/side-effect-free).

---

## 8. Source citations (grounding)

- `DEBATE_PROTOCOL.md:74-104` — projection intent, `conductor_inbox` +
  `human_brief` + required tray projections, "smallest actionable view".
- `intel_server.py:1965-1988` — `debate_work_queue` tool (deterministic open-work
  priority view) → `_debate_list_open_work_dao` (import `intel_server.py:84`).
- `intel_server.py:1835-1859` — `debate_worker_claim` tool (idempotent worker
  allocation; the separate claim write path, decoupled from `get_next_work`).
- `db_utils.py:1415-1426` — `daily_dashboard` table definition.
- `db_utils.py:118-127` — `DASHBOARD_KINDS`, `DASHBOARD_PRIORITIES`,
  `DASHBOARD_KIND_CAP`.
- `db_utils.py:1449-1490` — `assert_dashboard_conductor_writer` (single-writer
  guard); docstring `1456-1465` (cooperative, not a security boundary).
- `db_utils.py:1493-1514` — `_priority_rank_sql` / `_prune_dashboard_rows`
  ordering and per-kind cap.
- `schema.py:617-634` — `premium_artifact_manifests` (PREMIUM code-signing) — DO
  NOT overload.
- `schema.py:636-646` — `premium_control_plane_cache` (PREMIUM ACL/RBAC) — DO
  NOT overload.
- `schema.py:713-741` — `debate_messages` (kind/priority/vehicle/reply_to).
- `schema.py:743-750` — `debate_watermarks`.
- `schema.py:764-786` — `debate_message_recipients`, `debate_signal_state`.
- `schema.py:792-814` — `debate_role_bindings` + `idx_drb_one_active`.
- `schema.py:848-871` — `debate_worker_claims`.
- `schema.py:873-888` — `debate_message_claims`.
