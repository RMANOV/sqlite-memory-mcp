# SQLite Memory Bug Hunts — Strategic Manual Checks

> **Repo snapshot:** `a42f625`  
> **Method:** static architecture review + git history corridor + selective live-state verification  
> **Intent:** не просто да се намерят бъгове, а да се пази memory stack-ът от регресии, които водят до state drift, sync loss или фалшиво доверие.

---

## A. RECENTLY_HARDENED (10) — коридорът е осезаемо по-силен от преди

- [x] `5f25af2` — unified sqlite writes вече стигат до bridge hooks, вместо да остават локални.
- [x] `7cb8cd0` — auto-sync worker flow е стабилизиран и вече не се самоубива при кратък operational шум.
- [x] `bd6cb72` — tray/search path-овете са харднати срещу скрити sync/search races.
- [x] `af7fc76` — causal ledger + audit loop създават replayable memory substrate, а не само final rows.
- [x] `3daa33d` — cross-machine merge ordering спира да разчита на локални scalar counters като глобална истина.
- [x] `2276729` — task status authority предпочита field/event history пред stale `tasks.status`.
- [x] `7e1c051` — event-backed repair guarantees и audit scheduler state затварят част от “memory rot” риска.
- [x] `761667b` — bridge bootstrap вече пази tombstones и legacy task ids.
- [x] `1235ec9` — shrink safety valve auto-heal-ва дълги `description`/`notes`, вместо да убива sync-а.
- [x] `f36f0bb`, `b0e4503`, `a42f625` — attachments, tray startup guard и project alias normalization затварят три реални operator-facing regression канала.

---

## B. OPEN STRATEGIC RISKS (5) — не са счупвания, но остават системни зависимости

- [ ] **Физическият SQLite файл остава SPOF за disk-level failure.** ACID и WAL пазят от corruption, но не от повреден диск, sync client race извън SQLite или човешко изтриване.
- [ ] **Bridge repo + git auth остава machine-to-machine chokepoint.** Ако push/pull/auth пропадне, локалната истина оцелява, но propagation спира.
- [ ] **Runtime rollout към други машини остава operational риск.** Repo кодът е правилният source, но стара runtime copy на hook/worker на друга машина може да остане назад.
- [ ] **Fresh-machine recovery трябва да се smoke-ва периодично, не само да се предполага.** Теорията е добра, но DR без drill е wishful thinking.
- [ ] **Retrieval correctness не е същото като storage correctness.** `memory_contract_v2` е голям напредък, но wrong-context bugs са отделен клас риск.

---

## C. TRULY_MANUAL / OPERATOR CHECKS (7) — това не може да се затвори само със static review

- [ ] Прикачи файл на машина A, sync-ни, отвори го на машина B от reader dialog-а.
- [ ] Редактирай note с голям `description`, после го смали локално и потвърди, че bridge auto-heal-ва правилно без data loss.
- [ ] Създай нова DB от bridge only и провери: active, archived, cancelled, attachments, `memory_events`, `memory_artifacts`.
- [ ] Пусни tray на Windows и Fedora с различни tab views и провери, че `project` filter-ите се нормализират еднакво.
- [ ] Форсирай safe merge conflict в generated bridge payload и провери, че auto-recovery не пипа user-managed files.
- [ ] Остави tray-а да работи достатъчно дълго и потвърди, че periodic pull + background audit не причиняват hidden writes без операторски смисъл.
- [ ] Пусни cross-machine note/task create/edit wave с attachments и verify-вай per-run registry, не само визуалния резултат.

---

## D. SPOF Quick Reference

| ID | Singleton | Защо е singleton | Текуща защита | Residual risk |
|---|---|---|---|---|
| S1 | `memory.db` | един физически state store | WAL, busy timeout, atomic transactions | disk / file loss |
| S2 | `db_utils.py` write policy | централен mutation/merge brain | event-backed mutation, tests, field history | нов bypass path |
| S3 | bridge git repo | един corridor между машини | safety valve, auto-heal, conflict reset | auth / remote failure |
| S4 | deployed hooks per machine | runtime copy все още е machine-local | repo-preferred launcher, recent fixes | stale rollout on peer host |
| S5 | tray GUI process | мощен direct writer + operator surface | mutation API, periodic pull, audit hooks | hidden UX regression |
| S6 | retrieval contract | ако върне грешен context, memory звучи вярно, но подвежда | `memory_contract_v2`, preview gating | semantic mis-prioritization |
| S7 | recovery discipline | ако няма drill, restore promise е теоретично | bridge export/import hardening | false confidence |

---

## E. What To Watch Next

Следващият полезен wave не е “още функции”, а:

1. recovery drills по график;
2. attachment parity cross-machine;
3. long-lived tray session monitoring;
4. retrieval false-positive analysis под реални notes;
5. периодичен SPOF rerun при всяка sync-related промяна.
