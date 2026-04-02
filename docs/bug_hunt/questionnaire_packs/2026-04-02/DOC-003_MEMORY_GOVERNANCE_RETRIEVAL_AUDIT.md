# DOC-003 — Memory Governance, Retrieval, Audit

**Intent:** да се провери дали системата не само пази state, а пази проверим и полезен state. Перфектната памет не е "всичко е записано", а "правилното нещо излиза, с правилния статут и доказуем произход".

**Primary anchors:** `context_packer.py:50`, `context_packer.py:1046`, `db_utils.py:4749`, `db_utils.py:4914`, `memory_audit.py:605`, `memory_audit.py:730`, `memory_audit.py:1183`

---

## 1. Provenance and Evidence

### 1.1 Facts / claims / questions

- [ ] `DOC-003/1.1/1` — `candidate_claims` и `canonical_facts` трябва да пазят достатъчно provenance за повторна проверка.
- [ ] `DOC-003/1.1/2` — `context_questions` и enrichable / awaiting_human chunks не трябва да се преструват на canonical truth.

### 1.2 Replayability

- [ ] `DOC-003/1.2/1` — `replay_memory_events()` трябва да дава usable audit slice, а не декоративен log.
- [ ] `DOC-003/1.2/2` — Когато issue е resolved, историята трябва да остава traceable, не да се загубва в последно състояние.

---

## 2. Retrieval Contract

### 2.1 Precision over noise

- [ ] `DOC-003/2.1/1` — `memory_contract_v2` трябва да отделя previewable/high-confidence context от weak fragment noise.
- [ ] `DOC-003/2.1/2` — Contradicted or weakly grounded facts не трябва да се показват като равностойни на canonical memory.

### 2.2 Executor vs planner behavior

- [ ] `DOC-003/2.2/1` — `executor` path не трябва да действа като loose semantic autocomplete.
- [ ] `DOC-003/2.2/2` — Ако retrieval policy се промени, това трябва да се version-ва и pack-ът да се rerun-не.

---

## 3. Audit and Repair

### 3.1 Due-aware scheduler

- [ ] `DOC-003/3.1/1` — `maybe_run_memory_audit()` трябва да пази cadence state в DB и да не run-ва безкрайно често.
- [ ] `DOC-003/3.1/2` — Background audit от tray и bridge worker не трябва да води до sync churn без semantic delta.

### 3.2 Issue model

- [ ] `DOC-003/3.2/1` — `memory_audit_issues` трябва да различава open vs resolved, не просто да трупа шум.
- [ ] `DOC-003/3.2/2` — Missing provenance, contradiction counts и stale pack-ове трябва да оставят operationally useful signal.

---

## 4. Strategic Questions

1. Ако storage state е коректен, но retrieval подаде грешен context, как ще разбереш?
2. Ако audit loop намира проблеми, но няма кой да ги затвори operationally, помага ли реално?
3. Ако факт е technically import-нат, но provenance му е тънка, трябва ли изобщо да е user-facing?

---

## 5. Re-Run Triggers

- промени по `context_packer.py`, `memory_audit.py`, `claim_graph.py`, `intel_server.py`;
- нови memory artifacts / summary / decision paths;
- regressions от типа “контекст енрич е шумен/безполезен”;
- нови contradiction / provenance механизми.
