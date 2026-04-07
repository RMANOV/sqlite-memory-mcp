# АНАЛИЗ НА ПАТТЕРНИТЕ В SQLITE MEMORY
## Sync, authority, retrieval и operational resilience — hardening epochs и повторяеми bug генератори

> **Дата на създаване:** 2026-04-07  
> **Обхват:** `sqlite_memory` repo, само вътрешният code/history corridor  
> **Източници:** git history, `docs/bug_hunt` pack-ове, първият реален run, последните hardening fix clusters

---

## Как да се ползва

1. Ако имаш нов incident: първо провери дали е проекция на известен generator от §2.
2. Ако пипаш gravity well файл от §4: мини regression чеклиста за съответния клъстер.
3. Ако нов change активира 2+ generators едновременно: това вече е архитектурен риск, не isolated bug.
4. Ако finding-ите се трупат в bridge / tray / retrieval едновременно: спри feature work и rerun short predictive pack `2026-04-07`.

---

## 1. HARDENING EPOCHS

### 1.1 Хронология на последните решаващи фиксове

| Epoch | Commit cluster | Фокус | Какво реално затвори |
|---|---|---|---|
| Hook corridor | `5f25af2`, `7cb8cd0` | unified writes + worker flow | write-и вече влизат в bridge path-а; worker-ът спира да се самоизключва при шум |
| Tray/search stabilization | `bd6cb72` | tray runtime, search races | WAL watcher, thread pressure, sync serialization, UI profile churn |
| Causal memory substrate | `af7fc76`, `7e1c051` | ledger, provenance, audit | memory state вече е replayable и governable, не само финален row set |
| Cross-machine merge correctness | `3daa33d`, `2276729`, `761667b`, `1235ec9` | ordering, status authority, tombstones, shrink auto-heal | най-опасният sync-loss corridor беше стеснен силно |
| Operator surface cleanup | `f36f0bb`, `b0e4503`, `a42f625`, `bcc4358`, `525bf6f` | attachments, startup writes, project aliases, search, notes/description clarity | human-facing “не го виждам / не го намирам / мести се само” класът отслабна |
| Phase-2 core hardening | `72955b9`, `ea149c4` | merge safety, migration race, FTS/audit details | втори pass върху authority, import precedence и regression seams |

### 1.2 Ключов извод

Тук bug-овете не идват главно от “една грешна функция”, а от **разминаване между authority surfaces**:

- DB row vs field/event history;
- local runtime copy vs repo source;
- bridge export artifact vs import truth;
- tray visible state vs actual state;
- retrieval confidence vs factual provenance;
- deploy publish surface vs real repo contents.

---

## 2. ROOT CAUSE ГЕНЕРАТОРИ

### 2.1 Generator Matrix

| ID | Generator | Статус | Типичен симптом |
|---|---|---|---|
| **A** | Authority bypass | ACTIVE WATCH | нов write path пише директно в row/state без event-backed mutation |
| **B** | Stale state wins over newer authority | PARTIAL, guarded | `tasks.status`, timestamps или raw rows бият field/event truth |
| **C** | Lifecycle coverage gap | ACTIVE WATCH | нов field/artifact не е вързан във всички surfaces |
| **D** | Background writer surprise | ACTIVE WATCH | startup / periodic path прави hidden writes, pushes или retries |
| **E** | Generated-artifact drift | ACTIVE WATCH | `.tmp`, lock, stale generated file или wrong publish surface чупи corridor |
| **F** | Recovery illusion | ACTIVE WATCH | bridge “изглежда пълен”, но fresh bootstrap губи част от state-а |
| **G** | Operator vocabulary divergence | PARTIAL, mitigated | project alias, status/type expectations, title-only search, UI wording |
| **H** | Retrieval surface mismatch | ACTIVE WATCH | user помни фраза, но tool/view/filter търси грешната повърхност |
| **I** | Runtime rollout drift | ACTIVE WATCH | repo fix е на място, но друга машина още върти stale hook/worker |
| **J** | Publish corridor overload | ACTIVE WATCH | deploy path включва data surface, която не е publish-safe |

### 2.2 Генераторни regression checklists

#### Generator A: Authority bypass

- [ ] Нов write path минава ли през authoritative mutation API?
- [ ] Създава ли `memory_events` и field history, а не само row update?
- [ ] Има ли raw SQL `UPDATE/INSERT` към task/fact/claim tables извън central path?
- [ ] Ако path-ът е legacy/CLI/hook — има ли същите invariants като tray/server path-а?

#### Generator B: Stale state beats newer authority

- [ ] При merge/import има ли по-силен authority source от raw row стойността?
- [ ] При task status / timestamps ползва ли се field/event precedence?
- [ ] Новата логика сравнява ли machine-local counters сякаш са global order?
- [ ] Ако редът е stale, има ли self-heal path, не само reject path?

#### Generator C: Lifecycle coverage gap

- [ ] Новото поле/artifact минава ли през create, edit, export, import, search, UI, bridge и recovery?
- [ ] Има ли тест поне за roundtrip export/import?
- [ ] Появява ли се в `shared.json` / per-file payload / search result по очаквания начин?
- [ ] Има ли operator-visible path за четене, а не само за запис?

#### Generator D: Background writer surprise

- [ ] Startup path-ът read-only ли е наистина?
- [ ] Periodic pull/poll path-ът случайно пушва ли?
- [ ] Retry loop-ът bounded ли е и различава ли pull-only от full sync?
- [ ] Background audit / maintenance path-ът променя ли state без user intent?

#### Generator E: Generated-artifact drift

- [ ] Temp/lock/generated файловете имат ли отделни имена и cleanup?
- [ ] Preflight различава ли user edits от generated debris?
- [ ] Bridge export може ли да остави stale `.tmp`/lock след crash?
- [ ] Publish path случайно deploy-ва ли generated internal artifacts?

#### Generator F: Recovery illusion

- [ ] Fresh DB from bridge възстановява ли active + archived + cancelled + attachments?
- [ ] Legacy/unsafe IDs имат ли deterministic safe transport?
- [ ] Tombstones materialize-ват ли се в празна база?
- [ ] Recovery smoke test има ли от последния hardening cluster?

#### Generator G: Operator vocabulary divergence

- [ ] Името на project-а нормализира ли се?
- [ ] UI labels ясно ли казват `description` vs `notes`?
- [ ] Search path-ът зависим ли е от type/status/project, когато user помни само фраза?
- [ ] Точното поведение на Today/Inbox/Done tabs съвпада ли с operator expectation?

#### Generator H: Retrieval surface mismatch

- [ ] Когато user помни част от title/phrase, има ли cross-surface lookup?
- [ ] Entity search се използва ли погрешно като task search?
- [ ] Preview/context gating скрива ли weak/noisy executor context?
- [ ] `matched_in` или друго обяснение казва ли защо резултатът е излязъл?

#### Generator I: Runtime rollout drift

- [ ] Repo fix-ът автоматично ли е предпочитан пред stale runtime copy?
- [ ] Друга машина има ли процедура за deploy на hooks/runtime files?
- [ ] Стар launcher/worker може ли да заобиколи новите гаранции?
- [ ] Incident notes документират ли изрично rollout step-а?

#### Generator J: Publish corridor overload

- [ ] Cloudflare/Pages publish dir включва ли само publish-safe files?
- [ ] Има ли файл над platform size limit?
- [ ] Internal memory layers (`extended_memory`, logs, tmp) изключени ли са от deploy surface?
- [ ] Пътят, който чете PWA/site, действително ли зависи само от staged files?

---

## 3. ПОВТАРЯЩИ СЕ КЛЪСТЕРИ

### 3.1 Authority and Merge Truth

**Генератори:** A, B  
**Симптоми:** status re-open, stale row winning, claims/facts imported by timestamp only, raw direct writers

| # | Проверка | [ ] | Бележки |
|---|---|---|---|
| 1 | Всеки task write създава ledger + field history | [ ] | |
| 2 | Import precedence предпочита event/field authority пред raw row | [ ] | |
| 3 | New CLI/hook/server path не bypass-ва mutation API | [ ] | |
| 4 | New merge ordering е cross-machine comparable | [ ] | |
| 5 | Divergence се journal-ва, не потъва silently | [ ] | |

### 3.2 Bridge Export / Import / Recovery

**Генератори:** C, E, F, J  
**Симптоми:** tombstone loss, unsafe IDs, stale temp files, Pages deploy failure, missing artifacts on fresh bootstrap

| # | Проверка | [ ] | Бележки |
|---|---|---|---|
| 1 | Нов artifact влиза ли и в export, и в import, и в recovery path | [ ] | |
| 2 | Generated `.tmp`/lock files не блокират sync | [ ] | |
| 3 | Fresh bootstrap възстановява historical statuses и attachments | [ ] | |
| 4 | Publish dir не съдържа internal oversized files | [ ] | |
| 5 | Bridge repo остава clean enough за safe commit/push cycle | [ ] | |

### 3.3 Tray and Background Automation

**Генератори:** D, G, I  
**Симптоми:** startup hidden writes, periodic pull pushing, due-date auto-promotion surprises, filter drift

| # | Проверка | [ ] | Бележки |
|---|---|---|---|
| 1 | Стартирането на tray е aggregate-diff clean | [ ] | |
| 2 | Periodic pull е pull-only, не full sync | [ ] | |
| 3 | Archived/cancelled tasks не се auto-promote-ват | [ ] | |
| 4 | Project aliases не се раздробяват в chips/views | [ ] | |
| 5 | Rollout към peer machine е документиран след tray/sync fix | [ ] | |

### 3.4 Retrieval, Search and Operator Semantics

**Генератори:** G, H  
**Симптоми:** “не я намирам”, entity search vs task search, wrong body field, weak context noise

| # | Проверка | [ ] | Бележки |
|---|---|---|---|
| 1 | Remembered phrase lookup намира task/note/entity независимо от surface | [ ] | |
| 2 | `description` е primary body field навсякъде в tooling wording | [ ] | |
| 3 | `notes` се виждат и четат през UI, но не крадат ролята на main body | [ ] | |
| 4 | Weak executor enrich previews се скриват | [ ] | |
| 5 | Result path казва къде е match-ът (`title`, `notes`, `project`, `observation`) | [ ] | |

---

## 4. GRAVITY WELLS

### 4.1 По размер

| Файл | LOC | Риск |
|---|---:|---|
| `db_utils.py` | 5184 | authority, merge, export/import, recovery, search helpers |
| `tray_dialogs.py` | 2535 | най-големият human-facing operator surface |
| `task_tray.py` | 1876 | startup semantics, direct UX write paths |
| `bridge_server.py` | 1591 | MCP-facing bridge corridor, shared-task mutations |
| `schema.py` | 1393 | една промяна тук може да счупи migration + storage assumptions |
| `memory_audit.py` | 1205 | epistemic integrity and repair loop |
| `context_packer.py` | 1139 | retrieval truth vs noise |
| `bridge_sync_worker.py` | 931 | machine-to-machine corridor |

### 4.2 По честота на hardening touch в последните 25 commits

| Файл | Touches | Смисъл |
|---|---:|---|
| `db_utils.py` | 18 | централният bug magnet и policy brain |
| `bridge_sync_worker.py` | 14 | operational corridor и export/import edge cases |
| `bridge_server.py` | 7 | shared bridge tooling и legacy write paths |
| `task_tray.py` | 5 | hidden writes, runtime UX semantics |
| `schema.py` | 5 | migration and storage coverage |
| `memory_audit.py` | 5 | repair/provenance truth |
| `tray_sync.py` | 4 | background cadence and drift |
| `task_server.py` | 4 | Claude-facing semantic surface |

### 4.3 Gravity-well rule

Промяна в gravity well файл без кратък 004-style risk pass е process smell.

---

## 5. OPEN STRATEGIC RISKS

| ID | Риск | Защо още е реален |
|---|---|---|
| R1 | Physical SQLite singleton | ledger помага за trust, не за disk death |
| R2 | Bridge repo as only propagation corridor | local truth оцелява, shared continuity не |
| R3 | Peer-machine rollout discipline | repo fix не е runtime fix на чужда машина |
| R4 | Retrieval false confidence | storage correctness не гарантира correct recall |
| R5 | Gravity-well change density | `db_utils.py` и bridge corridor още концентрират прекалено много от риска |

---

## 6. ПРЕПОРЪКИ

1. Използвай `2026-04-07` predictive pack преди всяка промяна по `db_utils.py`, `bridge_sync_worker.py`, `task_tray.py`, `context_packer.py`.
2. Пази `db_utils.py` като policy brain, не като dump zone за ad-hoc fixes.
3. Не добавяй нов artifact в bridge без explicit export/import/bootstrap/deploy decision.
4. Всеки “малък” tray convenience fix трябва да се третира като potential hidden-writer risk.
5. Search/retrieval fixes се оценяват по “може ли user да намери remembered phrase”, не по вътрешната elegance на query-то.

---

## 7. ВЕРИФИКАЦИЯ

| Раздел | Общо проверки | OK [x] | Проблем [!] | Не проверено [?] |
|---|---:|---:|---:|---:|
| §2 Generators | 40 | | | |
| §3 Clusters | 20 | | | |
| §4 Gravity wells | 8 | | | |
| §5 Strategic risks | 5 | | | |
| **Общо** | **73** | | | |

**Нови генератори, ако се открият след тази версия:**

1. _______________
2. _______________
3. _______________
