# Bug Pattern Graph — SQLite Memory

> Discovery topology for `sqlite_memory`: authority, sync, recovery, retrieval и operator seams.  
> **Generated:** 2026-04-07  
> **Method:** git-history corridor + bug-hunt pack synthesis + recent incident review

---

## 1. Generator Genealogy

Всеки hardening layer разкри следващия. Проблемът не беше “една голяма грешка”, а onion stack от различни seams.

```text
Layer 1 — CORRIDOR (late Mar):
  unified hook writes, worker retries, bridge import crash
  commits: 5f25af2 → 7cb8cd0 → bd6cb72
      |
      v  fix corridor, authority drift becomes visible...
Layer 2 — AUTHORITY (Apr 1-2):
  causal ledger, merge ordering, status precedence
  commits: af7fc76 → 3daa33d → 2276729 → 7e1c051
      |
      v  fix authority, bootstrap/recovery gaps surface...
Layer 3 — RECOVERY (Apr 2):
  tombstones, legacy ids, shrink auto-heal, write enforcement
  commits: 761667b → 1235ec9 → 5a2cd57
      |
      v  fix recovery, operator drift and hidden-writer bugs surface...
Layer 4 — OPERATOR/RETRIEVAL (Apr 2-7):
  attachments, startup writes, alias normalization, notes/description, phrase lookup
  commits: f36f0bb → b0e4503 → a42f625 → bcc4358 → 525bf6f
      |
      v  fix operator drift, publish/deploy topology surfaces...
Layer 5 — PUBLISH/TOPOLOGY (Apr 7):
  extended_memory extraction + Pages publish-surface overload
  commit: 19244d1 in repo, then workflow staging fix in bridge repo
```

**Pattern:** each fix removed one illusion:

- “write happened” != “bridge saw it”
- “row value exists” != “newest authority”
- “bridge repo has data” != “fresh bootstrap can restore it”
- “task exists” != “operator can find it”
- “deploy job exists” != “publish surface is platform-safe”

---

## 2. Spatial Clustering

### 2.1 Gravity wells by size and touch frequency

| File | LOC | Touches in last 25 commits | Risk class |
|---|---:|---:|---|
| `db_utils.py` | 5184 | 18 | absolute center of authority / merge / export / recovery |
| `bridge_sync_worker.py` | 931 | 14 | machine-to-machine corridor |
| `bridge_server.py` | 1591 | 7 | bridge tool surface + legacy mutation seams |
| `task_tray.py` | 1876 | 5 | human-facing runtime writer |
| `schema.py` | 1393 | 5 | migration correctness |
| `memory_audit.py` | 1205 | 5 | epistemic correctness |
| `tray_sync.py` | 251 | 4 | cadence / retries / hidden pushes |
| `task_server.py` | 725 | 4 | Claude-facing semantic surface |

### 2.2 Implication

`sqlite_memory` bug density не е “навсякъде”, а е концентрирана в:

1. one policy brain (`db_utils.py`);
2. one sync corridor (`bridge_sync_worker.py` + `bridge_server.py`);
3. one operator shell (`task_tray.py` + `tray_dialogs.py`);
4. one retrieval truth layer (`context_packer.py` + `memory_audit.py`).

---

## 3. Root Cause Tree

```text
                 ┌──────────────────────────────┐
                 │  WHY DO THESE BUGS RECUR?    │
                 └──────────────┬───────────────┘
           ┌──────────────┬─────┼──────────┬──────────────┐
           v              v     v          v              v
      ┌──────────┐   ┌────────┐ ┌────────┐ ┌──────────┐ ┌──────────┐
      │ RC-1     │   │ RC-2   │ │ RC-3   │ │ RC-4     │ │ RC-5     │
      │ Shared   │   │ One    │ │ Back-  │ │ Coverage │ │ Human/   │
      │ authority│   │ repo = │ │ ground │ │ across   │ │ tool     │
      │ brain    │   │ one    │ │ auto-  │ │ surfaces │ │ mismatch │
      │          │   │ corridor││ action │ │          │ │          │
      └────┬─────┘   └────┬───┘ └────┬───┘ └────┬─────┘ └────┬─────┘
           │              │          │          │             │
         A,B,C          E,F,J      D,I        C,F           G,H
```

### RC-1: Shared authority brain

Прекалено много истина се решава в един модул. Ако нов path не спази invariants на `db_utils.py`, регресията е structural, не local.

### RC-2: One repo = one corridor

Bridge repo-то е не просто storage artifact, а operational transport. Промяна в generated payload, temp file policy или publish surface може да счупи continuity.

### RC-3: Background automation is real writer

Tray startup, periodic pull, audit loop, hooks и retries не са “невидима инфраструктура”; те са real mutation/push actors.

### RC-4: Coverage across surfaces

Най-честият механизъм за regression е partial rollout:

- поле има DB support, но липсва UI;
- export има artifact, import не го възстановява;
- search знае title, но не notes/project;
- deploy качва вътрешни data layers без нужда.

### RC-5: Human/tool mismatch

Operator мисли във фрази, status expectations и project aliases. Tool-овете дълго време мислеха в type-specific exact queries.

---

## 4. Predictive Model

### 4.1 If you change X, expect Y

| If you add / change... | Expect bug class | Generator | Confidence |
|---|---|---|---|
| new task/note field | partial propagation across UI/export/import/search | C | HIGH |
| new write path / CLI / tool | ledger bypass or stale row write | A | HIGH |
| new merge rule | newer authority losing silently | B | HIGH |
| new background timer / watcher | hidden writes or surprise pushes | D | HIGH |
| new bridge artifact | bootstrap gap or Pages/deploy overload | C / F / J | HIGH |
| new temp/generated file | sync preflight false conflict | E | HIGH |
| new project / filter semantics | exact-match operator drift | G | MED |
| new retrieval surface | search false negative or noisy false positive | H | HIGH |
| new machine rollout step | stale runtime copy on peer host | I | MED |
| new published site dependency | Pages size or path mismatch | J | HIGH |

### 4.2 Most likely next bug families

1. **Coverage bug:** нов field/artifact е записан, но липсва в bridge/PWA/search/recovery.
2. **Automation bug:** нов periodic/helper path прави write/push side effect, който не е мислен като writer.
3. **Retrieval bug:** result technically exists, но user не може да го намери с remembered phrase.
4. **Operational drift:** fix е в repo, но една peer машина още работи със stale runtime surface.
5. **Deploy topology bug:** публикува се повече, отколкото реално сайтът чете.

---

## 5. Dead-Zone Map

Тук dead zone не значи “непроверени файлове”, а **непроверени interaction classes**:

| Dead zone | Защо е опасен |
|---|---|
| multi-machine rollout after repo fix | кодът е верен, runtime reality може да не е |
| fresh-machine restore after weeks of drift | bootstrap correctness без drill е само обещание |
| background cadence under long-lived tray session | кратките тестове не виждат slow drift |
| retrieval under noisy real notes | synthetic tests не симулират human remembered-phrase behavior |
| publish/deploy path after data growth | platform limits удрят късно, не при малки payload-и |

---

## 6. Recommended Next Waves

| Wave | Method | Target | Expected ROI |
|---|---|---|---|
| q-pred-1 | short risk questionnaire | all changes touching `db_utils.py` / `bridge_sync_worker.py` | HIGH |
| q-pred-2 | recovery drill | empty DB from bridge + peer-machine rollout | HIGH |
| q-pred-3 | remembered-phrase retrieval pass | noisy tasks/notes/entities with mixed project aliases | MED-HIGH |
| q-pred-4 | long-lived tray observation | hidden writes, background jobs, periodic pull semantics | MED |
| q-pred-5 | publish-surface audit | PWA/site inputs vs deploy outputs | MED |

---

## 7. Process Rule

За `sqlite_memory` най-ефективният predictive filter е:

1. кратък 004-style risk pass преди промяна;
2. targeted regression test след промяна;
3. run-level documentation само ако промяната е в sync/authority/recovery corridor.

С други думи: не всяка промяна иска нов голям run, но всяка рискова промяна иска кратка предиктивна проверка.
