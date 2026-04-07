# DOC-004 — Predictive System Checklist

**Intent:** най-краткият routing doc. Използва се преди промяна, за да предвиди кои generators ще се активират.

## 1. Change Routing

### Ако пипаш:

- `db_utils.py`, `schema.py`, `bin/task`
  - отвори `DOC-001`
- `bridge_sync_worker.py`, `bridge_server.py`, bridge artifacts, Pages workflow
  - отвори `DOC-002`
- `task_tray.py`, `tray_dialogs.py`, `tray_sync.py`, `task_server.py`, `context_packer.py`
  - отвори `DOC-003`

Ако пипаш 2 или повече от тези групи едновременно:

- [ ] change-ът е **cluster-risk**
- [ ] иска кратък written note кои generators активира

## 2. Generator Activation Table

| Change type | Activated generators |
|---|---|
| new write path | A, B |
| new field / attachment / artifact | C, F |
| new timer / startup hook / background helper | D, I |
| new generated file / temp file / staging artifact | E, J |
| new search or UI wording | G, H |
| new peer-machine rollout requirement | I |
| new site/deploy dependency | J |

## 3. Severity Rule

### TIER 1 — stop and review

- [ ] change can lose state
- [ ] change can publish stale or partial truth
- [ ] change can make peer-machine continuity depend on undocumented rollout
- [ ] change can hide an existing object from normal remembered-phrase lookup

### TIER 2 — merge only with targeted test

- [ ] change touches gravity well file
- [ ] change adds new export/import surface
- [ ] change alters tray startup / periodic behavior
- [ ] change changes retrieval ranking/gating

### TIER 3 — light path

- [ ] wording-only change
- [ ] read-only display change
- [ ] doc-only or comment-only change

## 4. Mandatory Outputs

Преди merge трябва да имаш поне едно от следните:

- [ ] targeted pytest command
- [ ] deterministic local smoke check
- [ ] explicit statement защо change-ът е read-only / non-authoritative

## 5. Stop Conditions

Ако едновременно са верни:

- [ ] gravity well file
- [ ] bridge/recovery relevance
- [ ] no targeted verification

това е **невалиден merge candidate**.

## 6. One-Line Predictive Summary

Попълни това преди risky change:

> “Тази промяна най-вероятно активира generators __________ и рискът е основно __________.”

Ако не можеш да го попълниш, значи още не си разбрал surface-а достатъчно.
