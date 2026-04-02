# SQLite Memory First Real Run — Sync and SPOF

**Run ID:** `RUN-2026-04-02_first-real-sync-spof`  
**Pack:** `BH-PACK-2026-04-02-SQLITE-MEMORY`  
**Date:** 2026-04-02  
**Operator:** Codex targeted sync/SPOF run  
**Method:** doc-guided code review + targeted pytest

## Results

| Metric | Value |
|---|---:|
| Checkpoints checked | 18 |
| Code verified | 8 |
| Recently hardened | 5 |
| Open risks | 3 |
| Manual followups | 2 |
| Relevant tests passed | 52 |

## What This Run Confirms

Sync hardening is real, not cosmetic. The authority chain around task status, bridge hydration, conflict journaling, tombstone recovery, attachment parity and shrink auto-heal is materially stronger than the pre-hardening corridor.

The strongest verified path in this run is:

- task creation seeds field/event history;
- bridge import resolves authoritative status before merge;
- divergence writes `memory_conflicts` instead of going silent;
- recovery does not depend only on `shared.json`;
- incremental sync sees bridge-relevant movement, not only task rows;
- shrink safety auto-heals before blocking.

## New First-Run Finding

The repo is still not at **absolute write enforcement**.

`bin/task done` updates `tasks` directly with raw SQL and does not go through the authoritative mutation/ledger path. That means the dominant write corridor is hardened, but not universal. `bridge_server.py` also still contains mirrored raw-SQL task updates, although those do at least compensate with `_upsert_field_versions()`.

This is the most important code-level gap surfaced by the first real run. It is smaller than the old sync-loss bugs, but it is still exactly the kind of future regression seam that can reopen authority drift.

## Residual SPOFs

### 1. Physical DB remains real

`memory.db` is still a physical singleton. WAL and ledger improve integrity and auditability, but they do not create an automatic second live replica.

### 2. Bridge corridor remains real

The bridge repo plus git auth is still the only machine-to-machine propagation corridor. Local truth survives a bridge outage; shared continuity does not.

### 3. Peer rollout remains operational

Repo fixes help only after the peer machine actually runs them. Hook/runtime rollout is still an operations discipline problem, not a solved code problem.

## Verification Performed

Targeted tests:

```text
pytest tests/test_bridge_export.py tests/test_memory_bridge_import.py tests/test_bridge_sync_worker.py -q
pytest tests/test_memory_audit.py tests/test_context_packer.py -q
```

Result: `52 passed`

## Bottom Line

This first real run is **mostly green**, but not “everything is immortal”.

The old catastrophic sync-loss class is materially narrowed. The remaining meaningful risks are:

1. one real code seam around universal write enforcement;
2. the physical SQLite file;
3. the bridge/auth corridor;
4. stale peer-machine rollout;
5. recovery discipline drifting if drills stop.
