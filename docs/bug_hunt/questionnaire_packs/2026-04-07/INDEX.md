# SQLite Memory Predictive Pack — 2026-04-07

**Pack ID:** `BH-PACK-2026-04-07-SQLITE-MEMORY-PREDICTIVE`

Това е краткият pack за pre-change triage. Той не замества базовия pack от `2026-04-02`, а го допълва с `DOC-004`-style въпроси.

## Документи

1. `DOC-001` — write authority risk questions
2. `DOC-002` — bridge, deploy and recovery risk questions
3. `DOC-003` — operator and retrieval risk questions
4. `DOC-004` — predictive system checklist

## Recommended Order

1. `DOC-004` — избира кои други docs изобщо са нужни.
2. `DOC-001` — ако change-ът пише state.
3. `DOC-002` — ако change-ът пипа bridge/export/import/deploy/recovery.
4. `DOC-003` — ако change-ът пипа tray/search/retrieval/operator semantics.

## When To Use This Pack

- преди промяна по `db_utils.py`, `bridge_sync_worker.py`, `bridge_server.py`, `schema.py`;
- преди нов tray/runtime helper, timer, watcher, background task;
- преди нов field/artifact/export/import surface;
- преди промяна по search/retrieval wording или result ranking;
- преди промяна по bridge publish topology.
