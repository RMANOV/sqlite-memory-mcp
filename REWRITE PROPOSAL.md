# README Rewrite Proposal — Final Approved Positioning

**Status:** Operator-approved (signed off). Implemented in `README.md` on branch `docs/readme-positioning-rewrite`.
**Supersedes:** the unfinished 2026-05-29 draft.
**Scope:** README hero + structure only. No code, schema, or runtime changes. Every claim verified against the codebase (see "Verification" below).

---

## 1. Approved positioning (verbatim)

### Headline
> Governed cross-agent memory for coding agents

### Support line
> Claude and Codex can share one provenance-rich knowledge graph with approval-aware promotion workflows.

### Three proof bullets (exactly three)
1. **Hybrid retrieval** — BM25/FTS5 keyword search fused with optional semantic (sqlite-vec) results via Reciprocal Rank Fusion.
2. **Provenance + reviewable / approval-aware promotion** — mutations carry provenance; candidate claims move to canonical facts through an approval-aware promotion gate, not silent rewrites.
3. **Cross-agent MCP memory with bridge sync** — one local SQLite knowledge graph any MCP client can read/write, with bridge tools that sync shared entities across machines.

---

## 2. Messaging guardrails (honesty constraints that gate launch articles)

- **No overclaim on promotion.** Use "reviewable / approval-aware promotion." Do **not** write "nothing becomes durable memory without review." The code path is not fully test-gated: `promote_candidate` supports `human_confirmed` (explicit approval), `multi_evidence` (policy-gated auto-promotion), and `imported` (bulk). Only sensitive scopes force `human_confirmed`. "Approval-aware" is accurate; "always gated" is not.
- **Beads.** "Can sit beside Beads" — NOT "integrates with." No adapter is shipped. The `ready_context` tool is the cross-project/cross-machine analog of `bd ready` / `bd prime`; that is coexistence, not integration. Repo referenced: `github.com/steveyegge/beads`.
- **Codex Memories.** Neutral comparison framing — category/mindshare risk, different point in the design space (local-first, multi-agent, provenance-governed, MCP-shared). NOT a 1:1 replacement claim.
- **Cursor.** Kept verbatim in the approved support line. In the body, cross-agent install is shown explicitly only for Claude Code (`claude mcp add`) and Codex (`codex mcp add`). Cursor support rests on generic "any MCP client" compatibility — there is no Cursor-specific install block or test. This is the one residual factual gap; it is honest as written but flagged for the advocate.

---

## 3. Structural changes (demote, don't delete)

| Element | Before | After |
|---|---|---|
| Hero | "Technical deep-dives" article links led the page; SQLite-centric pitch | Headline + support line + 3 proof bullets lead; deep-dive links demoted to a `### Technical deep-dives` subsection under the intro |
| Premium / Enterprise Boundary | ~265 lines (8 feature packs, runtime scope, tray surface) inline, immediately after the FAQ | Collapsed into a concise subsection under **## Advanced & operator topics**, with links to canonical docs (`docs/ops/PREMIUM_BOUNDARY.md`, `docs/ops/RELEASE_CONFIDENCE.md`, `premium_contract.py`, `docs/premium/*`, `templates/private_premium_repo/`) |
| Intelligence v2 | scattered in Features + premium prose | Dedicated subsection under **## Advanced & operator topics**, links to `docs/REFLECT_AUDIT_DEMO.md` |
| Debate / multi-agent protocol | implied via tool list | Dedicated subsection under **## Advanced & operator topics**, links to `docs/DEBATE_PROTOCOL.md` + `docs/ops/DEBATE_OPERATIONS.md` |
| Features list | included a verbose "Premium runtime boundary" bullet | Replaced with a "Provenance + approval-aware promotion" bullet that points to the Advanced section |
| Tool counts | stale: hero/Features/Tool-Reference claimed "57 (9 + 48)"; intel listed as 12, tasks as 8 | Re-measured against `@mcp.tool()` decorators: **92 total** (9 + 5 + 9 + 7 + 9 + 7 + 46). Hero/Features/Architecture now point to the Tool Reference instead of repeating a grand total; the Tool Reference shows exact per-server counts plus a defined, reproducible total. AMEND 2026-05-31 (Codex advocate blocker). |
| Competitor Comparison | retained (table) | Retained verbatim + new "Where this sits in the ecosystem" note (Beads / Codex Memories framing) |
| Convergent evolution vs GBrain | retained | Retained unchanged |

Net effect: README dropped from 931 to ~711 lines; the hero answers "what is this and who is it for" in the first screen; operator-only material (premium, intelligence, debate) is one click down.

---

## 4. Verification (claims checked against code)

- **Hybrid retrieval:** `vec_search.py` → `rrf_merge()` implements Reciprocal Rank Fusion of FTS5 + vector results. ✔
- **Provenance + promotion:** `intel_server.py` exposes `extract_candidate_claims`, `promote_candidate`, `govern_fact`, `queue_clarification`, `record_human_answer`. `db_utils.py` has `provenance_links`, `memory_events`, `record_memory_event()`, `add_provenance_link()`. `promote_candidate` modes confirm the "approval-aware, not always-gated" wording. ✔
- **Cross-agent + bridge sync:** explicit `claude mcp add` and `codex mcp add` blocks in README; bridge tools (`bridge_push`/`bridge_pull`/`bridge_status`) in `sqlite_bridge`. Cursor = generic MCP only (flagged). ✔ / ⚠ Cursor
- **Tool count (re-measured 2026-05-31):** exact `@mcp.tool()` decorator counts — sqlite_memory 9, sqlite_session 5, sqlite_tasks 9, sqlite_bridge 7, sqlite_collab 9, sqlite_entity 7, sqlite_intel 46 (14 intelligence/governance + 32 reflect/debate). **Total = 92.** The earlier "57 (9 + 48)" was stale (intel and tasks had grown). README counts now match the code and the Tool Reference defines the total explicitly. ✔
- **Linked docs exist:** all six Advanced-section targets verified present on disk. ✔

---

## 5. What the Codex advocate should verify

1. **Factual accuracy vs codebase** — RRF in `vec_search.py`; promotion/provenance tools in `intel_server.py` + `db_utils.py`; bridge sync in `bridge_server.py`.
2. **No-overclaim** — confirm the README nowhere asserts "nothing becomes durable without review" or that every write is gated; "approval-aware / reviewable" only.
3. **Beads framing** — "can sit beside," not "integrates with"; no claimed adapter.
4. **Codex Memories framing** — neutral category/mindshare comparison, not a 1:1 replacement claim.
5. **Cursor gap** — support line keeps "Cursor" (approved verbatim), but body only ships Claude/Codex install; Cursor = generic MCP. Decide whether to add a Cursor install note or leave the "any MCP client" framing.
6. **Exactly three proof bullets** — confirm the hero has precisely the three approved bullets, no more.
7. **Tool-count consistency** — verified 92 total (9 + 5 + 9 + 7 + 9 + 7 + 46) via `@mcp.tool()` decorators. No grand total is repeated in the hero/Features/Architecture (they link to the Tool Reference); only the Tool Reference states the defined, reproducible total. Confirm no stray "57" / "48" / "9 + 48" remains except the external Dev.to article title ("54-Tool…"), which is a real published title, not a repo claim.

> **CURSOR — RESOLVED 2026-05-31:** Operator decided to REMOVE Cursor from positioning. README support line is now "Claude and Codex"; the comparison-table parenthetical dropped Cursor; no Cursor-specific claim remains in README. The "Cursor gap" notes above are superseded.
