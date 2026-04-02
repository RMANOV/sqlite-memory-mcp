# DOC-004 — SPOF and Operational Resilience

**Intent:** да се назоват местата, където системата още зависи от едно нещо, един corridor или един навик. Това не са непременно бъгове, но са местата, от които идват най-тежките инциденти.

**Primary anchors:** `db_utils.py:544`, `db_utils.py:1812`, `db_utils.py:2689`, `db_utils.py:1481`, `bridge_sync_worker.py:551`, `bridge_sync_worker.py:798`, `task_tray.py:2092`, `memory_audit.py:1183`

---

## 1. SPOF Matrix

| ID | Singleton | Failure mode | Current mitigation | Residual risk |
|---|---|---|---|---|
| SPOF-01 | `memory.db` | disk loss, OneDrive interference, human deletion | WAL, atomic tx, event history | physical file remains single artifact |
| SPOF-02 | `db_utils.py` write/merge policy | one bad bypass path can corrupt authority model | event-backed mutation, tests, field history | future raw SQL path |
| SPOF-03 | bridge git repo | propagation stops between machines | safety valve, auto-heal, conflict reset | auth / remote / repo corruption |
| SPOF-04 | deployed hooks on each machine | runtime code lags repo code | repo-preferred launcher, recent hardening | stale peer host copy |
| SPOF-05 | tray process | hidden writes or UI-state drift | mutation API, periodic pull, audit hooks | operator confusion, long-lived session drift |
| SPOF-06 | retrieval contract | wrong context returned with high confidence | `memory_contract_v2`, preview gating | semantic false positives |
| SPOF-07 | recovery discipline | restore exists on paper, not in practice | bridge export/import hardening | no drill, false confidence |

---

## 2. What Is Already No Longer a Catastrophic SPOF

- [ ] `DOC-004/2.1/1` — Unified writes missing from hook path е historical incident, не current core gap.
- [ ] `DOC-004/2.1/2` — Status overwrite от stale row е historical incident, не current intended behavior.
- [ ] `DOC-004/2.1/3` — Tombstone/bootstrap loss за archived/cancelled tasks е hardened, не baseline weakness.
- [ ] `DOC-004/2.1/4` — Project alias duplication вече е mitigated в create/update/filter/profile restore paths.

---

## 3. Residual SPOFs That Still Matter

### 3.1 Physical and operational

- [ ] `DOC-004/3.1/1` — Single SQLite file still means no automatic replica if disk dies.
- [ ] `DOC-004/3.1/2` — Bridge repo outage does not lose local truth, but does break shared truth continuity.
- [ ] `DOC-004/3.1/3` — Hook rollout across machines still needs discipline; repo fix alone is not magic on a stale peer.

### 3.2 Epistemic

- [ ] `DOC-004/3.2/1` — Retrieval errors can create “I remember” illusion even when storage is correct.
- [ ] `DOC-004/3.2/2` — If no one reruns DR drills, the system can look hardened while recovery muscle decays.

---

## 4. Detection and Recovery Drills

### 4.1 Detection

- [ ] `DOC-004/4.1/1` — Confirm which logs are your first-stop signals: bridge notifications, bridge conflicts, tray log, audit issues.
- [ ] `DOC-004/4.1/2` — Confirm which counters matter after a run: task counts by status, attachment counts, memory event counts, open audit issues.

### 4.2 Recovery drills

- [ ] `DOC-004/4.2/1` — Rebuild empty DB from bridge only.
- [ ] `DOC-004/4.2/2` — Force generated-file bridge conflict and confirm auto-recovery path.
- [ ] `DOC-004/4.2/3` — Validate large note shrink auto-heal on a real note, not a toy example.

---

## 5. Strategic Questions

1. Кое е по-страшно за този repo: data loss, stale truth, или false confidence?
2. Ако bridge propagation спре за 48 часа, колко бързо ще го видиш?
3. Ако retrieval сгреши, имаш ли user-visible indicator, че output-ът е epistemically weak?
4. Ако трябва да възстановиш системата на чиста машина след 30 дни, знаеш ли точната процедура или само вярваш, че я има?

---

## 6. Decision Rule

Ако `DOC-004` не е зелен в стратегически смисъл, release confidence трябва да пада, дори всички unit tests да са зелени.
