---
title: BUILD STEP 1 — Read-only Debate Tabs — S2 Receipt
date: 2026-07-19
branch: agent/tray-readonly-tabs
base: c236d2462ee108f0d002c1125a825aa53766883e (audited)
spec: BOARD-TO-NATIVE-TRAY-SPEC-2026-07-18.md sha 5c144fa29a702d8e25ca30007a40a7c9034cb95c12a6de81e42aa7486b2b4945
scope: read-only stages ONLY (S3 fence — zero close/write/CAS code)
status: local branch, NO push, NO merge, NO live-tray restart
---

# S2 Receipt — Read-only Debate Tabs (BUILD STEP 1)

Implements the read-only surface of BOARD-TO-NATIVE-TRAY-SPEC-2026-07-18.md:
`DebateReadDAO` (recent / waiting_section_a / topics / topic_thread /
board_search — faithful ports of `operator_board/board.py`), `DebateListWidget`
(§2.0 read-only isolation), three tabs (`recent`/`waiting`/`topics`) registered
with real `_build_tab_rows`/`_load_tab` branches, grouped per-source search
(verbatim board BM25, no recency band — M4), and the `debate:` defense-in-depth
guards in the three task-side handlers.

## Files (new / modified)

| File | Change | sha256 (post-build) |
|---|---|---|
| `debate_read_dao.py` | NEW — read-only DAO, injected clock, prod fence | `a2a92d7bb48be7ccdec2d05ceef483bbad7414ae5ab9fd55ea760e097e064009` |
| `debate_list_widget.py` | NEW — read-only widget (holds no db) | `b59b33c17f94004800ed72ab1fcad2cc4ed972d5fcc7f5e5bb07520aa7a8fe3a` |
| `task_tray.py` | MOD — tab registration + debate branches + guard | `355fb43ff445437db5e0dcf6c081daa23fca4512bba206e0179dc6e83b81f64e` |
| `tray_dialogs.py` | MOD — `debate:` guards in double-click + context-menu | `c33fb1caa3c91d6b70232c0be16d1d95bd8da8598a4a81ca7c993ffa6d345804` |
| `tests/test_tray_readonly_debate.py` | NEW — acceptance + negative tests | `86c0008797e7281f458a4ddcc41047bee391eebf56d02b8beb650b852f9291f0` |

## S2 point 1 — Zero-write surface (falsifiable)

AST + behavioral proof that the debate paths carry no mutation, including
subprocess/helper paths:

```
[debate_read_dao.py]  banned_imports=none  mutation_refs=none  os.system=False
[debate_list_widget.py] banned_imports=none mutation_refs=none os.system=False
[debate_read_dao]  mode=ro=True  query_only=True  self._conn_write_stmts=none
[task_tray debate helpers] mutation_calls=none  subprocess=False
RESULT: PASS (no debate write surface)
```

- `DebateReadDAO` imports neither `db_utils` nor `subprocess`; calls none of
  `apply_task_mutation` / `update_task` / `mark_done` / `delete_task`. Its only
  `INSERT`/`CREATE` statements target the ephemeral `:memory:` FTS mirror used
  for search — **never** `self._conn` (the fixture/prod connection).
- `DebateListWidget` holds **no** `db` attribute (structural), never wires
  `itemChanged`→mutation, and references no task-mutation name.
- The tray debate helper methods (`_build_debate_rows`, `_load_debate_tab`,
  `_load_debate_search`, `_on_debate_navigate`) call no mutation and no
  subprocess.
- Behavioral: `test_query_only_blocks_writes` proves `CREATE`/`INSERT` on the
  DAO connection raise `sqlite3.OperationalError` (`PRAGMA query_only=ON`).

## S2 point 2 — Harness DB access (fixtures only, fail-closed prod fence)

- Every DAO connection is `file:<path>?mode=ro` + `PRAGMA query_only=ON`.
- Fail-closed guard: `DebateReadDAO(path, forbid_path=PROD)` raises
  `PermissionError` when `realpath(path) == realpath(~/.claude/memory/memory.db)`
  — a **structural non-access** guarantee (O1 model). `test_prod_path_fail_closed`
  proves it (direct path and a `..`-relative form both refused).
- The whole test suite passes `forbid_path=PROD` and points only at the two
  frozen fixtures; it **never opens prod** (not even `mode=ro`).
- **Prod hash may drift** from other live actors (the running MCP servers write
  to `~/.claude/memory/memory.db` continuously). That drift is unrelated to this
  vehicle: this vehicle opened prod **zero** times from the harness, so the
  **ledger delta for this vehicle actor is empty by construction** (structural
  non-access, not an after-the-fact diff — the fence forbids opening prod to
  diff it).

## S2 point 3 — Frozen-clock reproduction (`as_of=2026-07-18T18:42:27Z`)

The harness injects `as_of` (no live `datetime.now` on the harness path). All
green (`pytest -q` → **15 passed** in the new suite; **103 passed** including the
existing tray/dialog suites — zero regressions):

| Gate | Fixture | Result |
|---|---|---|
| **T2** section-A parity | FX-A | `section_a == 0` (zero-parity) |
| **T1** recent (role-pin) | FX-B | `recent(1.0,'CODEX_FIXTURE',[DECISION,STATE,STATUS])` == the 10 targets `fxb-rec-01..10` in `ts DESC`; `role=None` == 24 (pin required) |
| **T3** waiting section-A | FX-B | `section_a == 10` == `{fxb-wait-01..10}` |
| **T6** topics | FX-B | 10 `fxb-topic-*` present; `topic_thread('fxb-topic-01').count == 3` |
| **T4** search per-source | FX-B | 15/15 nonces hit **only** their source; **per-source id list + order byte-equal to the board reference** on FX-B (exact-equality, `test_T4_fxb_search_exact_equality_vs_board`) |

Time-to-find **data precondition** (scripted; median of 5, under `as_of`):

```
T1 recent(role-pin): median=3.96ms   results=10  (zero-scroll: exactly 10 rows)
T3 waiting_section_a: median=15.90ms  results=10  (zero-scroll: exactly 10 rows)
T6 topics(targets):   median=3.17ms   results=10  (zero-scroll: exactly 10 rows)
```

*Interpretation:* the harness proves the **retrieval precondition** for the
spec's human-stopwatch T1/T3/T6 medians — the exact 10-target set is returned as
the entire first screen (zero scroll) in <16 ms. The human stopwatch medians
(≤10 s / ≤12 s) remain a UI-acceptance step for the adoption stage (they need a
person + the wired tray window); this receipt establishes that the data layer
cannot be the bottleneck and the target set is exact and unambiguous.

## S2 point 4 — Post-build artifact hash

See the file table above (five `sha256` values). Recompute:
`sha256sum debate_read_dao.py debate_list_widget.py task_tray.py tray_dialogs.py tests/test_tray_readonly_debate.py`.

## S2 point 5 — Committed receipt, py_compile + tests green

- `python3 -m py_compile debate_read_dao.py debate_list_widget.py task_tray.py tray_dialogs.py` → OK.
- `pytest tests/test_tray_readonly_debate.py -q` → 15 passed.
- `pytest` over the touched tray/dialog suites → **103 passed, 0 failed**
  (`test_daily_dashboard`, `test_tray_dialogs`, `test_task_db`,
  `test_task_tray_reminders`, `test_task_tray_memory_guard`, `test_tray_sync`,
  `test_tray_readonly_debate`).
- Fixture bytes unchanged across all runs (frozen hashes `50e4f458…` / `06a72ee5…`
  verified before and after).

## Fences honored

- Isolated worktree `/home/rmanov/sqlite-memory-tray-build` on branch
  `agent/tray-readonly-tabs` at the audited base `c236d24`. Live tray process and
  main checkout untouched.
- **Zero** close/write/CAS code (S3 fence). The only mutating verb in scope is
  absent; adoption/write stages are explicitly out of scope.
- No push (repo has `origin`; strictly local commits). No merge to main. No
  live-tray restart/deploy. Prod DB never opened by the harness.

## What remains for the ADOPTION step (out of scope here)

1. **Human time-to-find acceptance** (T1/T3/T6 stopwatch medians) on the wired
   tray window — the data precondition is proven; the felt-value run needs a person.
2. **Statefulness stage (stage 2)** — persist `recent.{hours,kinds,role}`,
   `waiting.*`, `topics.*` params + normalization (spec §3, T5). This build wires
   the tabs and reads persisted params if present, but full QSettings
   round-trip/normalization for the new keys is the next stage.
3. **`waiting` section B (tasks) + the grouped-search tasks group** — currently
   rendered read-only/inert; the write stage splits them into a `TaskListWidget`
   with the **gated close** (CAS in the canonical primitive, single-flight,
   transition matrix, undo — spec §6, T8). None of that is built here (S3 fence).
4. **ADVOCATE diff-gate** on this branch before any adoption; then a separate
   explicit operator GO for the write stage (spec §7 non-circular sequence).
