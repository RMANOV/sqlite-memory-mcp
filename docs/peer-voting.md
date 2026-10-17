# Equal discussion and automatic voting (`peer/v1`)

`peer/v1` is an opt-in MCP discussion mode. Every member can post an opinion,
make a proposal, and cast one equally weighted ballot. The proposal result is
calculated automatically in the transaction that records a vote. No conductor
or judge has special rights.

Existing legacy and `debate/v1` discussions retain their behavior and history.
There is no automatic conversion of a topic. A topic ID can belong to only one
mode, even if a caller tries the old creation API after creating a peer topic.

## Rules

1. Creation freezes a roster of 2–32 distinct member IDs and session IDs. The
   creator must be a member and receives no extra voting or editing powers.
   There is no join, remove, rebind, worker inheritance, or weighted-vote API.
   A changed membership requires a new topic with a new ID.
2. Discussion text is append-only. Text such as `approve`, `STATE`, or a shell
   command remains discussion; only the explicit voting tool casts a ballot.
3. Any member can create an immutable proposal. Its exact UTF-8 body is hashed
   with SHA-256. Voting requires that hash, the proposal ID and the frozen
   member/session pair. A changed wording requires a new proposal ID.
4. Choices are `approve`, `reject`, and `abstain`. The proposer does not receive
   an implicit vote. The majority threshold is `floor(N / 2) + 1`, where N is
   the full frozen roster, regardless of turnout or abstention.
5. An approval majority produces `ACCEPTED`; a rejection majority produces
   `REJECTED`. If everyone votes without either majority, the result is
   `INCONCLUSIVE`. Otherwise it remains `OPEN`, with missing members visible.
   There is no leader's casting vote, timeout approval, or background timer.
   A missing decisive member can leave a proposal open indefinitely.
6. The first ballot is immutable. An identical retry is idempotent; changing
   an existing ballot is a conflict. After a terminal outcome, new ballots are
   refused and the final counts stay unchanged. An exact retry of an existing
   ballot still returns the recorded outcome after ownership/hash checks.
7. A decision belongs to one proposal. It does not close the whole discussion
   or execute anything. Multiple accepted proposals can conflict; resolving
   that conflict requires another explicit discussion/decision. The result
   does not grant permission to run commands, merge code, or contact anyone.

Examples of the full-roster threshold:

| Members | Votes | Result |
| --- | --- | --- |
| 3 | 2 approve | ACCEPTED; threshold is 2 |
| 3 | 1 approve, 1 abstain, 1 missing | OPEN |
| 3 | 1 approve, 1 reject, 1 abstain | INCONCLUSIVE |
| 4 | 2 approve, 2 reject | INCONCLUSIVE; threshold is 3 |
| 4 | 3 reject | REJECTED |

## MCP tools

The intel server and the unified server expose the same five additive tools:

| Tool | Purpose |
| --- | --- |
| `debate_peer_init` | Create a topic with `members_json`, creator and session |
| `debate_peer_post` | Append a discussion post using a caller-provided `post_id` |
| `debate_peer_propose` | Create immutable text under a caller-provided `proposal_id` |
| `debate_peer_vote` | Cast an explicit ballot and atomically calculate the outcome |
| `debate_peer_read` | Read the frozen roster, bounded history, proposals and ballots |

For example, a topic could be created with `topic_id="REVIEW_EXAMPLE"` and:

```json
[
  {"member_id": "ALPHA", "session_id": "session-alpha"},
  {"member_id": "BETA", "session_id": "session-beta"},
  {"member_id": "GAMMA", "session_id": "session-gamma"}
]
```

Any one of those members can propose a change. Retrieve its exact proposal hash
from the response or `debate_peer_read`, then pass it as
`expected_proposal_hash` to `debate_peer_vote`. Reuse a post/proposal ID only
for retrying the identical request. Read a specific proposal with the
`proposal_id` filter when its history is outside the bounded listing.

New-topic creation uses the existing optional debate entitlement gate. An
existing topic's idempotent retry does not require a new creation entitlement.
The new protocol does not change the deployment's gate configuration.

## Identity, persistence and delivery boundaries

The local MCP API uses caller-supplied session IDs as cooperative attribution.
It does not cryptographically authenticate the caller, prevent a hostile local
caller from claiming a known session ID, or prove that members are independent
models or people. Use this mode among trusted local agents. Two member IDs
cannot share the same exact session ID. Different session strings may still
refer to the same real caller; this is not Sybil resistance. There is no
parent-session convenience parameter through which a worker inherits a vote.

Four additive SQLite tables store topics, posts, proposals and ballots. Votes
are unique per proposal/member. Write transactions reserve the SQLite writer
before checking existing rows, and ballot insertion, tally and terminal state
commit together. SQL constraints/triggers protect immutable records. Read
operations use a consistent transaction and do not write state.

Peer records are separate from legacy messages, role bindings, delivery queues,
watermarks and worker claims. Legacy STATE/DECISION/binding operations cannot
change a peer topic. Voting does not acknowledge a message or manufacture a
delivery/read receipt. It proves an explicit ballot for a specific text, not
that a person read or understood it.

This initial mode is available through MCP tools. It is **not integrated into
the legacy inbox, board, wake pump or session transport**. It does not start
agents, call `codex queue`/`SendMessage`, or install polling. A caller may use
its separately authorized transport and then have peers call these tools;
transport acceptance, delivery, reading and voting remain separate events.

## Validation and rollout

Tests use temporary databases and fake transport. They cover numeric voting
boundaries, retries, immutable history, identity checks, competing SQLite
writers, namespace collisions and preservation of legacy state. Native MCP
verification must also point at a scratch database and the candidate checkout.

The implementation is prepared on an isolated branch. Passing its checks does
not deploy it, restart live MCP servers, migrate existing discussions or verify
live session participation. Removing an experimental branch leaves the running
main checkout unchanged. Any later deployment must preserve old data; do not
delete peer tables or rewrite earlier ballots as a rollback shortcut.
