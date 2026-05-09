# reflect_audit demo — deterministic memory consolidation on real data

> **TL;DR.** `mcp__sqlite_intel__reflect_audit` runs a deterministic SQL audit
> against your local `memory.db` and surfaces consolidation candidates
> (duplicates, stale items, abandoned inbox, orphan parents, empty notes,
> bare entities) without an LLM call, without a network socket, and with
> reproducible output. This page walks one real run on the author's
> live store.

## What the audit does

`reflect_audit` (Phase 0.5, Tool 13 on the `sqlite-intel` MCP server) detects
six categories of consolidation candidates with pure SQL:

| Category | Detection rule |
|---|---|
| `exact_duplicate_titles` | same `LOWER(TRIM(title))` in same project, multiple active rows |
| `stale_overdue_tasks` | `status='not_started' AND due_date < today − stale_days` |
| `empty_description_notes` | `type='note'` with `description IS NULL OR LENGTH(TRIM(description))=0` |
| `orphan_parent_tasks` | `parent_id` references a row that no longer exists |
| `abandoned_inbox_items` | `section='inbox' AND status='not_started' AND updated_at < today − abandoned_inbox_days` |
| `entities_no_observations` | entity row with zero rows in `observations` |

Each candidate carries a `suggested_action` (e.g. `archive_or_reschedule`,
`merge_or_archive`, `clear_parent_or_relink`) plus the original evidence
fields, so a downstream reviewer can decide accept / reject / defer per
row without re-querying.

## Reproducible run

```bash
# Inside Claude Code or any MCP client that reaches sqlite-intel
mcp__sqlite_intel__reflect_audit                                   # default
mcp__sqlite_intel__reflect_audit  stale_days=15 abandoned_inbox_days=14
mcp__sqlite_intel__reflect_audit  project=sqlite-memory-mcp
mcp__sqlite_intel__reflect_audit  format=markdown                  # human report
```

Same input + same thresholds = bit-exact same candidate set every time.

## One real run on the author's local store

Snapshot taken 2026-05-09 from a live development workstation:

- **Store size:** 1,092 tasks (410 active), 58 entities.
- **Wall time:** 41 milliseconds per run.
- **Network sockets opened during the run:** 0.
- **API tokens consumed:** 0.

### Default thresholds (`stale_days=60`, `abandoned_inbox_days=30`)

```json
{
  "summary": {
    "total_candidates": 20,
    "by_category": {
      "exact_duplicate_titles": 0,
      "stale_overdue_tasks": 0,
      "empty_description_notes": 0,
      "orphan_parent_tasks": 0,
      "abandoned_inbox_items": 20,
      "entities_no_observations": 0
    },
    "applied_filters": {
      "project": null,
      "stale_days": 60,
      "abandoned_inbox_days": 30,
      "limit_per_category": 20
    }
  }
}
```

Conservative defaults flag inbox items untouched for thirty-plus days. The
operator can prune, promote, or schedule them in one pass.

### Loose thresholds (`stale_days=15`, `abandoned_inbox_days=14`)

```json
{
  "summary": {
    "total_candidates": 29,
    "by_category": {
      "exact_duplicate_titles": 0,
      "stale_overdue_tasks": 9,
      "empty_description_notes": 0,
      "orphan_parent_tasks": 0,
      "abandoned_inbox_items": 20,
      "entities_no_observations": 0
    },
    "applied_filters": {
      "project": null,
      "stale_days": 15,
      "abandoned_inbox_days": 14,
      "limit_per_category": 20
    }
  }
}
```

Aggressive thresholds catch nine additional stale-overdue tasks: due dates
in early April (now thirty-plus days past) where status never moved off
`not_started`. Same wall time, same code path, just looser cutoffs.

## What this proves on real data

**Speed.** Forty-one milliseconds against eleven hundred tasks on a single
developer laptop. A nightly cron job costs zero wall time.

**Determinism.** Re-run the same audit five times in a row and compare the
JSON. Bit-exact equality. Compare to LLM-based consolidation: each run drifts
because temperature is non-zero in production stacks.

**Offline.** The hot path opens zero network sockets. This is enforced by a
regression test — `tests/test_reflection_phase1_paranoid.py::test_reflect_start_works_with_socket_blocked`
monkey-patches `socket.socket` to raise `OSError` on every connection
attempt and verifies that `reflect_start` (the Phase 1 wrapper that runs
the same audit and persists candidates) still completes successfully. The
test passes today on `main`.

**Audit-ready.** Each candidate is materialized as a row in
`reflection_candidates` with full evidence and a suggested action. The
operator decides per row via `mcp__sqlite_intel__reflect_decide`. Decisions
are then applied via `mcp__sqlite_intel__reflect_apply`, which writes a
before / after JSON snapshot to `reflection_apply_snapshots` for every
mutation — never in-place. Discard a run via `reflect_discard` and the FK
cascade removes inputs, candidates, and snapshots in a single transaction.

## Cost comparison vs an LLM-based consolidation pass

| Metric | This audit run | Equivalent LLM-based pass at May 2026 list price |
|---|---|---|
| API calls | 0 | ≈ 1 per chunk |
| Tokens consumed | 0 | ≈ 200,000 input across 1,084 tasks at avg 200 tokens / task |
| Wall time | 0.04 s | minutes (network round-trip + inference) |
| Cost per run | $0.00 | ≈ $3.50 (claude-opus-4-7 input + small output) |
| Reproducibility | bit-exact | run-to-run drift due to sampling |
| Offline capable | yes (Raspberry Pi tier) | requires network to vendor |
| Vendor dependency | none | Anthropic / OpenAI / equivalent |

Multiplied by 365 nightly runs: **$0 vs ≈ $1,277 / year** for the
consolidation pipeline alone, before any per-page or per-session cost is
added. For a regulated environment that cannot send data to a vendor, the
LLM-based path is not cheaper — it is unavailable.

## When to choose the LLM pass anyway

Determinism is not always the right answer. If your consolidation goal is
free-form summarization across noisy session transcripts, an LLM pass adds
real value over rules. The Phase 2 roadmap (see entity
`MemoryReflection_Roadmap` in the local KG, plus
`MemoryReflection_LLMFreeArchitecture` for the durability thesis) keeps the
LLM-based extraction available as an opt-in module, with sentence-transformers
running locally — so even the LLM-augmented path stays vendor-neutral.

## Pointers

- Source module: [`reflection.py`](../reflection.py) (Phase 0.5 audit) +
  [`reflection_dao.py`](../reflection_dao.py) (Phase 1 DAO) +
  [`reflection_apply.py`](../reflection_apply.py) (apply orchestration).
- MCP server: [`intel_server.py`](../intel_server.py) Tools 13–22.
- Tests: `tests/test_reflection.py` (Phase 0.5 unit), `tests/test_reflection_phase1_*.py` (Phase 1 schema, DAO, tools, apply, paranoid).
- Strategy: `README.md` § *Convergent evolution: sqlite-memory-mcp vs GBrain*.
