# Release Value Policy — sqlite-memory-mcp

> Gate for future releases. Derived from the 2026-06-27 user-value release audit + verified prune-list (audit-the-audit: 18 confirmed / 1 refuted / 6 nuanced). **Principle: freeze > delete** — nothing is hard-deleted; dead-but-wired code stays on disk and simply stops receiving release attention.

### (B) RELEASE VALUE POLICY (operator-endorsed) — enforceable criteria

**ГЛАВНА ПОРТА (7-day felt-value gate):**
> **"Може ли операторът да УСЕТИ това в ежедневния си workflow в рамките на 7 дни?"**
> Ако **НЕ** → промяната е `spec-only` / `experimental` / `deprecated-parked`. **НЕ е major release** и НЕ получава titled release tag.

Операционализация на портата (release reviewer проверява всичко по-долу преди да присвои версия):
1. **Felt-surface тест** — посочи КОНКРЕТНАТА повърхност, която операторът докосва (tray, `bin/task`, recall, debate inbox, bridge). Ако промяната не достига нито една от тези пет повърхности за ≤7 дни → не е release.
2. **Live-evidence тест** — приложи live DB/лог доказателство, че кодовият път реално се изпълнява (row count расте, лог показва не-no-op изпълнения). Нула изпълнения → spec-only.
3. **No-empty-mass тест** — ако фичърът защитава/обработва маса, която е `COUNT = 0` в live DB (collaborators, sharing_rules, knowledge_ratings, public entities, candidate_claims) → автоматично FREEZE, не release.

**ПРАВИЛО 1 — Core/user-visible priority.**
Само промени, които достигат петте load-bearing повърхности (memory/recall, task store + tray, bridge sync, debate orchestration, и техните root-fix-ове), получават release-приоритет и titled major/minor tag. Enforce: всеки release PR декларира коя от петте повърхности докосва; ако нито една → не получава release tag.

**ПРАВИЛО 2 — Plumbing само срещу доказан инцидент.**
Plumbing/инфраструктурна промяна получава release tag САМО ако затваря документиран production инцидент (с repro! линк/лог/ticket). Enforce: release notes за plumbing ЗАДЪЛЖИТЕЛНО цитират инцидента, който затварят (напр. "12 tasks resurrected" → v3.11.10/v3.11.18). Без цитиран инцидент → merge без version bump (internal commit, не release). Това директно блокира version-inflation (v3.8.1, v3.10.2, v3.11.1, v3.11.6, v3.12.2).

**ПРАВИЛО 3 — Театър/dead-surface = FROZEN до втори реален потребител.**
Всяка подсистема с live `COUNT = 0` за централната ѝ маса е FROZEN: остава на диска, остава wired, но НЕ получава нови фичъри, нови release tag-ове, нито рефактор-инвестиция. Enforce: unlock_condition = **"a second real user/team actually exercises the surface"** (≥1 не-нулева редица генерирана от реален втори участник). До тогава — нула release-внимание.

**ПРАВИЛО 4 — Collab/premium/public-mesh: hands-off.**
Collaboration, premium/monetization, public knowledge mesh НЕ се пипат (нито фичъри, нито "подобрения", нито hardening) докато няма реален втори потребител/екип. Enforce: PR, който модифицира `collab_server.py` / `premium_runtime.py` / public-mesh пътищата без приложено доказателство за реален втори участник → автоматично отхвърлен. Bug-fix за сигурност (не-фичър) е единственото изключение и пак минава без release tag.

---

### (C) PRUNE-LIST — per театър/dead subsystem

> **NEVER hard-delete.** Всеки ред остава на диска и wired. ACTION ∈ {KEEP-CORE, KEEP-NO-INVEST, FREEZE, DEPRECATE-DOC}. `do_not_delete` = какво ТРЯБВА да остане, за да не се счупи нищо.

| Subsystem (релийзи) | Класификация | ACTION | Unlock condition | DO NOT DELETE |
|---|---|---|---|---|
| **Core memory/recall** (v0.1.0): entities/observations/relations + FTS5 BM25 + WAL | LOAD-BEARING ядро, докосвано ежедневно | **KEEP-CORE** | n/a — никога не замразявай | `server.py:564 search_nodes`, `schema.py:249 memory_fts`, session save/recall |
| **Task store + Tray GUI + CLI** (v0.3.0/v0.4.0/v0.7.0): 1920 live tasks | Главната дневна повърхност на оператора | **KEEP-CORE** | n/a | `task_tray.py`, `bin/task`, tasks schema, recurring respawn |
| **Cross-machine bridge sync** (v0.2.0/v0.5.1): shared.json 22.6MB live | Реален cross-machine живот | **KEEP-CORE** (но стабилизирай hotfix-веригата чрез ПРАВИЛО 2) | n/a | `bridge/shared.json`, bridge_push/pull/status, tombstone merge driver (v3.12.3) |
| **Debate Protocol v2 — durable log + addressed signaling** (v3.9.0/.1/.2): 1329 msgs, 65 debates | Жив orchestration substrate | **KEEP-CORE** | n/a | `debate_messages`, `debates`, watermarks, role_bindings, recipient rows, worker_claims |
| **Debate wake-layer — autonomous impl-vehicle seam** (v3.10.0): 97% no-op, но signal-routing реален | Signal routing = ядро; autonomous "impl_vehicle" = непостроен seam | **KEEP-NO-INVEST** (routing); seam замразен | Реален impl-vehicle backend, който превръща no-op в real_spawn | `debate_wake_log` (audit trail), wake dispatch код. **Plus: затвори runaway `DAILY_20260612`/`f16945be0d08` topic** — източникът на 60,740-те no-op |
| **Resource governor** (v3.11.14, 569 lines) | Кърпи self-inflicted wake-thrash | **KEEP-NO-INVEST** | Премахва се натиска чак когато impl-vehicle seam се построи или runaway loop се изкорени | `hooks/debate_resource_budget.py` — активно throttle-ва; премахването връща thrash |
| **Causal/event ledger** (v3.4.0): 451,768 rows, ~417MB | Раздува базата; вика се само от maintenance | **KEEP-NO-INVEST** | Когато replay/explain_impact станат user-facing — иначе остава cold | `memory_events` + replay_memory/explain_impact пътищата (изтриване чупи causal queries) |
| **Intelligence v2 — 4-tier Knowledge Sovereignty** (v3.0.0): canonical_facts=12, candidate_claims=0 | Cathedral с празен pipeline | **FREEZE** | Реален claim-extraction поток (candidate_claims расте от реална употреба) | `canonical_facts` (12 реални), govern_fact/promote_candidate schema |
| **P2P knowledge collab** (v0.6.0): collaborators=0 | Multiplayer machinery за single-player | **FREEZE** | Втори реален потребител/екип | `collab_server.py`, sharing_rules schema, share_knowledge tools |
| **Public knowledge mesh** (v0.8.0): 0 public entities | Mesh с един участник | **FREEZE** | Втори реален участник, който публикува | public_knowledge schema, search_public_knowledge tool |
| **TruthScore anti-gaming ratings** (v0.9.0): knowledge_ratings=0 | Криптографска броня върху празен трезор | **FREEZE** | Реални ratings от втори потребител | rating schema, anti-gaming/anomaly код |
| **Premium Airlock** (v3.5.0): gate default-OFF, fail-open | Монетизационно scaffolding; механизъм реален, не блокира по подразбиране | **FREEZE** | Реален плащащ втори потребител | `premium_runtime.py` (default-off!), `evaluate_debate_protocol_creation_gate`, kill-switch env vars — изтриване може да счупи debate_init gate-path |
| **GBrain interop** (v3.8.0/v3.8.1): реален тестван one-way bridge, без жив target | Self-contained, тестван, но без жива интеграция | **KEEP-NO-INVEST** | Жив GBrain instance за двупосочен sync | `tools/gbrain_bridge/{export,import}.py`, MCP tools 23/24, `tests/test_gbrain_bridge.py` — работещ код, тестван |
| **v3.12.6 projection spec** (7 имена, 0 code hits) | Documentation-only, имплементира НИЩО | **DEPRECATE-DOC** | Реална имплементация на ≥1 projection, която операторът вижда в tray | `docs/DEBATE_PROTOCOL.md:91-104` — остава като spec/доку, но маркиран "UNIMPLEMENTED SPEC", НЕ се брои за release |
| **Version-inflation залежи** (v3.8.1, v3.10.2, v3.11.1, v3.11.3, v3.11.6, v3.12.2) | Минали 1-line/metadata/regex "релийзи" | **DEPRECATE-DOC** (само като release-история; кодът остава) | n/a — ретроактивно не се пипа | Самите кодови промени остават; само бъдещи такива губят release tag по ПРАВИЛО 2 |

**Обобщение на действията:** KEEP-CORE = 4 ядрени повърхности (+ routing на wake-layer). KEEP-NO-INVEST = 4 (wake routing, governor, ledger, GBrain). FREEZE = 5 (Intelligence v2, collab, mesh, TruthScore, premium). DEPRECATE-DOC = 2 (v3.12.6 spec, version-inflation история). **Нула hard-delete.**


---
