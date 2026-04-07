# BUG HUNT ANALYSIS — GRAPHS
## SQLite Memory MCP — process metrics, dependency graph, predictive risk model

> **Дата:** 2026-04-07  
> **Цел:** извличане на максимален insight от текущите `sqlite_memory` bug-hunt данни и hardening history  
> **Данни:** 2 formal runs, 35 recent commits, 10 active generators, co-change graph на критичните файлове  
> **Важно:** това не е 27-wave dataset като `reports_generator`. Тук граф анализът е построен върху реалния hardening corridor на repo-то, а не върху огромен run registry.

---

## A) ПРОЦЕСНИ МЕТРИКИ

### A1. Hardening Throughput by Commit Theme

Последните 50 commits показват ясна концентрация в 6 теми:

| Theme | Count | Share | Meaning |
|---|---:|---:|---|
| `bridge` | 16 | 32% | operational corridor, export/import, runtime helper seams |
| `sync` | 6 | 12% | authority precedence, merge correctness, hook flows |
| `tray` | 6 | 12% | operator/runtime behavior |
| `memory` | 6 | 12% | ledger, audit, provenance, repair |
| `search` | 4 | 8% | retrieval and discoverability |
| `docs` | 2 | 4% | bug-hunt framework and predictive system |
| `other` | 10 | 20% | version bumps, task UX, attachments, misc core work |

### A2. Commit Mix Graph

```text
bridge  ████████████████ 16
sync    ██████            6
tray    ██████            6
memory  ██████            6
search  ████              4
docs    ██                2
other   ██████████       10
```

**Извод:** системата е все още dominated от corridor work, не от feature growth.  
Това е здравословно за текущия етап: означава, че repo-то още купува reliability, не измества риска с нов surface area.

### A3. Formal Run Registry Signal

| Run ID | Checkpoints | Code Verified | Open Risks | Manual Followups | Assessment |
|---|---:|---:|---:|---:|---|
| `RUN-2026-04-02_static-baseline` | 20 | 7 | 2 | 2 | `hardened_but_not_immortal` |
| `RUN-2026-04-02_first-real-sync-spof` | 18 | 8 | 3 | 2 | `mostly_green_with_one_write_enforcement_gap` |

### A4. What the Run Delta Means

- Checkpoints stayed almost flat: `20 -> 18`
- Verified code paths improved: `7 -> 8`
- Open risks did **not** collapse: `2 -> 3`

**Interpretation:** hardening progress има, но residual risk мигрира, не изчезва.  
Това е белег на system maturation: catastrophic bug classes намаляват, но на тяхно място идват topology / rollout / epistemic risks.

### A5. Practical ROI by Analysis Mode

| Method | What it found best | Weakness |
|---|---|---|
| commit-cluster review | where risk was historically concentrated | doesn’t prove runtime correctness |
| formal run docs | strategic open-risk inventory | still sparse sample size |
| co-change graph | reveals true coupling, not claimed architecture | insensitive to untouched dead zones |
| predictive questionnaires | high-yield pre-change filter | needs discipline, not automation |

**Process conclusion:** за `sqlite_memory`, кратки risk questions преди промяна имат по-висок practical ROI от големи retrospective passes след промяна.

---

## B) АРХИТЕКТУРЕН ГРАФ

### B1. Gravity Wells by Size

| File | LOC | Risk |
|---|---:|---|
| `db_utils.py` | 5748 | policy brain: write authority, merge, export/import, recovery |
| `tray_dialogs.py` | 2886 | largest operator-facing UI surface |
| `task_tray.py` | 2106 | startup semantics and direct runtime write paths |
| `bridge_server.py` | 1734 | bridge tool entry surface |
| `schema.py` | 1471 | migration and storage assumptions |
| `memory_audit.py` | 1280 | repair and epistemic integrity |
| `context_packer.py` | 1239 | retrieval truth vs noise |
| `bridge_sync_worker.py` | 1043 | machine-to-machine corridor |

### B2. Co-Change Graph (Last 35 Commits)

Top co-change edges:

| Edge | Count |
|---|---:|
| `bridge_sync_worker.py <-> db_utils.py` | 12 |
| `bridge_server.py <-> db_utils.py` | 9 |
| `db_utils.py <-> schema.py` | 8 |
| `bridge_server.py <-> bridge_sync_worker.py` | 7 |
| `db_utils.py <-> task_tray.py` | 6 |
| `db_utils.py <-> memory_audit.py` | 5 |
| `schema.py <-> task_tray.py` | 5 |

### B3. Dependency Graph

```text
                     schema.py
                         ^
                         | 8
                         |
bridge_server.py --9--> db_utils.py --12--> bridge_sync_worker.py
       | 7                 | 6                  | 3
       v                   v                    v
  shared bridge         task_tray.py         tray_sync.py
       |                   |
       |                   | 5
       |                   v
       |               memory_audit.py
       |                   ^
       | 2                 | 2
       +-------> task_server.py <---- context_packer.py
```

### B4. What the Graph Actually Says

1. `db_utils.py` is not just “important”; it is the **routing hub** for almost every hardening class.
2. `bridge_sync_worker.py` is the hottest corridor edge, not the biggest file.
3. `bridge_server.py` and `task_tray.py` are the two most dangerous side-entry points into the policy brain.
4. `context_packer.py` is less frequently touched, but it couples into the same authority layer through `db_utils.py`, `memory_audit.py` and `task_server.py`.

**Architectural prediction:** следващата critical regression по-вероятно ще дойде от new side-entry path към `db_utils.py`, отколкото от core algorithm bug inside `db_utils.py`.

---

## C) ПРЕДИКТИВЕН МОДЕЛ

### C1. Generator Activation Matrix

| If you change... | Most likely generators |
|---|---|
| write path / CLI / helper / hook | `A`, `B` |
| bridge artifact / export payload / import rule | `C`, `E`, `F`, `J` |
| startup / timer / background cadence | `D`, `I` |
| search / wording / result ranking / UI visibility | `G`, `H` |
| deploy surface / Pages workflow / staged output | `J` |
| new field / note/attachment/provenance artifact | `C`, `F` |

### C2. Next-Bug Probability Matrix

| Risk Class | Probability | Why |
|---|---|---|
| partial propagation bug | HIGH | new surface often lands in one layer first |
| hidden background write / push | HIGH | tray/runtime helpers are proven bug class |
| stale-runtime peer machine drift | HIGH | operational discipline remains external to code |
| remembered-phrase search miss | MED-HIGH | search UX is improving, but human recall remains fuzzy |
| deploy topology overload | MED-HIGH | data payload keeps growing while publish platform has hard limits |
| pure storage corruption from logic bug | MED | major sync-loss classes were already hardened |
| truly new ledger algorithm bug | LOW-MED | area is now heavily defended and tested |

### C3. Most Likely Next 5 Incident Shapes

1. **New field exists locally but not cross-machine.**
   Trigger: field/artifact added without full export/import/bootstrap path.

2. **Background helper does more than intended.**
   Trigger: “small convenience” timer or startup path ends up mutating state.

3. **User remembers phrase, not title, and still misses object.**
   Trigger: retrieval path privileges the wrong surface or filter context.

4. **Repo fix exists, peer machine still behaves old.**
   Trigger: runtime hook/worker rollout missed on Fedora/Windows peer.

5. **Deploy passes locally but fails on platform limits.**
   Trigger: publish surface includes files the site does not even read.

---

## D) РИСКОВИ ПРЕДСКАЗАНИЯ

### D1. Which Files Will Attract The Next Wave?

| File | Why it will attract the next wave |
|---|---|
| `db_utils.py` | every serious authority/recovery policy still routes through it |
| `bridge_sync_worker.py` | any cross-machine incident eventually resolves here |
| `task_tray.py` | startup/write semantics remain a recurring operator risk |
| `bridge_server.py` | legacy and shared-task entry points keep reopening corridor seams |
| `context_packer.py` | next class of bugs will increasingly be epistemic, not storage-only |

### D2. Highest-Risk Interaction Pairs

| Pair | Why dangerous |
|---|---|
| `db_utils.py + bridge_sync_worker.py` | authority + propagation in one change |
| `db_utils.py + task_tray.py` | policy change meets human-facing writer |
| `db_utils.py + schema.py` | new invariants plus migration risk |
| `bridge_server.py + bridge_sync_worker.py` | two different bridge entry surfaces diverge easily |
| `task_server.py + context_packer.py` | search semantics and retrieval confidence can drift apart |

### D3. Crash / Failure Scenarios to Expect First

| Scenario | Likelihood | Comment |
|---|---|---|
| sync blocks on generated debris / publish drift | MED | already recurring class historically |
| deploy fails from payload growth | MED-HIGH | already reproduced with `extended_memory/memory_events.json` |
| user cannot find object that exists | HIGH | not fatal, but frequent pain vector |
| wrong object shown with confident context | MED | harder to notice, more dangerous epistemically |
| hard DB loss | LOW frequency / HIGH severity | code mitigations don’t remove physical singleton risk |

---

## E) ДРУГИ ПОЛЕЗНИ ИЗВОДИ

### E1. The repo is transitioning from data-loss risk to trust-risk

Преди доминиращият страх беше:
- “ще го загубим ли между машините?”

Сега все по-често доминиращият страх става:
- “ще го намерим ли правилно?”
- “ще го покажем ли с правилния context?”
- “ще мислим ли, че системата е права, когато тя е само убедителна?”

Това е важен преход. Значи storage hardening работи, но следващият frontier е retrieval correctness.

### E2. The strongest predictive signal is not file size, but co-change centrality

`tray_dialogs.py` е по-голям от `bridge_sync_worker.py`, но не е по-опасен системно.  
Причината: опасността идва не само от LOC, а от това колко често файлът е в **same change set** с policy brain-а.

### E3. The next quality leap is procedural, not just technical

Най-голямата възвръщаемост вече идва от:

1. кратък predictive checklist преди промяна;
2. targeted verification веднага след нея;
3. периодичен recovery/rollout drill;
4. отделно мислене за retrieval truth, не само storage truth.

---

## F) ПРЕПОРЪЧАНО ИЗПОЛЗВАНЕ

Ползвай този doc така:

1. Преди risky change: отвори [DOC-004_PREDICTIVE_SYSTEM_CHECKLIST.md](C:/Users/rmanov/.claude/mcp_servers/sqlite_memory/docs/bug_hunt/questionnaire_packs/2026-04-07/DOC-004_PREDICTIVE_SYSTEM_CHECKLIST.md)
2. После мини към `DOC-001/002/003` според това кои generators се активират.
3. Ако change-ът е в `db_utils.py + bridge_sync_worker.py`, приемай го за TIER 1 по подразбиране.
4. Ако change-ът е само UI wording/search, гледай operator/retrieval risk, не се успокоявай от “малък diff”.

---

## G) ЕДНОИЗРЕЧЕНСКИ ИЗВОД

`sqlite_memory` вече не е repo, което най-вероятно ще загуби state поради една проста sync грешка; това е repo, което най-вероятно ще се провали в нова partial-surface, rollout или retrieval topology, ако не му се прилага кратък predictive gate преди промяна.
