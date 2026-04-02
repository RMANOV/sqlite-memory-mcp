# SQLite Memory Baseline Run — Static Architecture, Sync and SPOF Review

**Run ID:** `RUN-2026-04-02_static-baseline`  
**Pack:** `BH-PACK-2026-04-02-SQLITE-MEMORY`  
**Date:** 2026-04-02  
**Operator:** Codex static architecture analysis  
**Method:** git history + code path tracing + live DB state review

## Results

| Metric | Value |
|---|---:|
| Checkpoints checked | 20 |
| Code verified | 7 |
| Recently hardened | 9 |
| Open risks | 2 |
| Manual followups | 2 |
| SPOFs reviewed | 7 |

## Strategic Reading

Репото е осезаемо по-харднато в сравнение с pre-`5f25af2` / pre-`7cb8cd0` периода. Най-важният сигнал е, че hardening-ът не е само в една нишка, а по целия loss corridor:

- hook path за unified writes;
- auto-sync worker flow;
- causal ledger + audit;
- cross-machine merge ordering;
- event-authoritative task status;
- tombstone/bootstrap recovery;
- shrink auto-heal;
- attachments;
- tray startup guard;
- project alias normalization.

Това е правилният тип прогрес: не "още features", а затваряне на memory-loss surfaces.

## Hardening Corridor

| Commit | Theme | Why it matters |
|---|---|---|
| `5f25af2` | unified hook writes | bridge не пропуска новия MCP write path |
| `7cb8cd0` | worker flow | sync worker спира да е крехък operational link |
| `af7fc76` | causal ledger | state става replayable и auditable |
| `3daa33d` | merge ordering | cross-machine writes имат по-защитен winner logic |
| `2276729` | status authority | stale row вече не reopen-ва задача сам |
| `761667b` | tombstones + legacy ids | fresh bootstrap става по-надежден |
| `1235ec9` | shrink auto-heal | дълги note/task bodies не чупят sync-а |
| `f36f0bb` | attachments | binary payload-ите стават first-class |
| `b0e4503` | tray startup semantics | hidden writes при startup са ограничени |
| `a42f625` | project aliases | filter invisibility bug class е затворен |

## Residual Risks

### 1. Physical DB file remains a real SPOF

SQLite + WAL пазят от много класове corruption, но не от disk loss, sync client interference извън SQLite, или човешко изтриване. Това не е "bug", а hard operational boundary.

### 2. Bridge repo/auth remains the shared-truth corridor

Локалната истина оцелява без bridge, но shared continuity между машини не оцелява. Това е operational singleton и трябва да се третира като такъв.

## Manual Followups

1. Периодичен fresh-machine recovery drill от bridge only.
2. Attachment open/remove smoke на втора машина след реален pull.

## Bottom Line

Текущото състояние е: **hardened but not immortal**.

Това repo вече има силен защитен коридор срещу най-страшния клас инциденти: silent overwrite, stale status resurrection, bootstrap loss, shrink-induced sync failure и project-filter invisibility.

Онова, което остава, е по-малко "скрит кодов бъг" и повече operational discipline:

- backup / recovery drills;
- bridge corridor health;
- peer-machine rollout hygiene;
- periodic rerun на SPOF документа, когато sync layer-ът се променя.
