# DOC-001 — Sync, Causal Ordering, Bridge Guarantees

**Intent:** да се провери дали системата не само пази локален state, а го пренася коректно между машини без silent overwrite.

**Primary anchors:** `db_utils.py:544`, `db_utils.py:2517`, `db_utils.py:2689`, `db_utils.py:3237`, `db_utils.py:5356`, `db_utils.py:1369`, `bridge_sync_worker.py:551`

---

## 1. Authority Chain

### 1.1 Canonical write paths

- [ ] `DOC-001/1.1/1` — Провери, че новите task writes минават през `create_task_with_ledger()` и seed-ват field/event history.
- [ ] `DOC-001/1.1/2` — Провери, че updates минават през `apply_task_mutation()` и не разчитат на raw SQL bypass.
- [ ] `DOC-001/1.1/3` — Провери, че `task_field_versions.source_event_id` и `memory_events` сочат към една и съща mutation линия.

### 1.2 Status authority

- [ ] `DOC-001/1.2/1` — Провери, че stale `tasks.status` не бие по-нов `_field_ts.status` / event history.
- [ ] `DOC-001/1.2/2` — При export/import path-а status normalization трябва да се случва преди merge decision, не след това.

---

## 2. Cross-Machine Merge

### 2.1 Ordering semantics

- [ ] `DOC-001/2.1/1` — Провери, че merge ordering не сравнява локални per-machine counters като глобална истина.
- [ ] `DOC-001/2.1/2` — При tie или divergence трябва да се записва `memory_conflicts`, а не просто да се губи loser branch-ът в мълчание.

### 2.2 Remote hydration

- [ ] `DOC-001/2.2/1` — `export_task_files()` трябва да емитира `_field_ts`, `_attachments`, `_links` и `_tombstone`, когато е приложимо.
- [ ] `DOC-001/2.2/2` — `import_remote_bridge_data()` трябва да хидратира tasks + supporting payloads преди task merge-а.
- [ ] `DOC-001/2.2/3` — Bridge import не трябва да разчита само на `shared.json`; per-task files трябва да са достатъчни за DR.

---

## 3. Bootstrap and Recovery

### 3.1 Tombstones and historical rows

- [ ] `DOC-001/3.1/1` — Fresh import от bridge трябва да materialize-ва archived/cancelled tasks, а не само active rows.
- [ ] `DOC-001/3.1/2` — Legacy/unsafe task ids не трябва да изчезват заради filename encoding.

### 3.2 Attachments parity

- [ ] `DOC-001/3.2/1` — Провери, че attachment metadata и attachment bytes roundtrip-ват заедно.
- [ ] `DOC-001/3.2/2` — Remote attachment import не трябва да оставя metadata pointing към липсващи bytes.

---

## 4. Incremental Sync Safety

### 4.1 Change detection

- [ ] `DOC-001/4.1/1` — `bridge_change_summary()` трябва да пропуска quiet runs, но да не skip-ва eventful runs.
- [ ] `DOC-001/4.1/2` — Audit activity не трябва да създава perpetual sync churn без реална причина.

### 4.2 Safety valve

- [ ] `DOC-001/4.2/1` — Suspicious shrink на `description`/`notes` трябва да auto-heal-ва локално, а не да overwite-ва bridge.
- [ ] `DOC-001/4.2/2` — Ако safety valve все пак блокира, блокът трябва да е explainable и recoverable.

---

## 5. Strategic Questions

1. Ако bridge repo изчезне, можеш ли да rebuild-неш вярно state от `memory.db`?
2. Ако `memory.db` изчезне, можеш ли да rebuild-неш достатъчно вярно state от bridge files?
3. Ако двете машини редактират едно и също поле почти едновременно, имаш ли traceable loser branch?
4. Ако attachment bytes се sync-нат, но metadata drift-не, ще разбереш ли навреме?

---

## 6. Time-Horizon Watchlist

- Всеки commit по `db_utils.py`, `bridge_sync_worker.py`, `bridge_server.py`, `schema.py` трябва автоматично да вкарва rerun на този документ.
- Ако се добави нов write path извън `apply_task_mutation()`, този документ става first-stop review.
