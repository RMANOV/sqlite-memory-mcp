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

Exact per-file diff vs `c236d24` (this table is the single source of truth;
`git diff --numstat` — the receipt `.md` itself is documentation and is
deliberately **excluded** so its own churn can never make these numbers stale):

| File | Change | +add / −del | sha256 (post-build) |
|---|---|---|---|
| `debate_read_dao.py` | NEW — read-only DAO + **adoption-fix-1 live-await layer** | +575 / −0 | `ae786b19a129674a7f0402a258daa0000fd75109c824d0a72efab4e304cef0c9` |
| `debate_list_widget.py` | NEW — read-only widget (holds no db) | +114 / −0 | `b59b33c17f94004800ed72ab1fcad2cc4ed972d5fcc7f5e5bb07520aa7a8fe3a` |
| `task_tray.py` | MOD — tabs + branches + guard + BLOCKER-1 visibility fix + **adoption-fix-2 tab order** | +187 / −4 | `2ef18c2bc37c2c8c27d82670a9942ad57a1fa87187dc597362d36bc24d2820a5` |
| `tray_dialogs.py` | MOD — `debate:` guards in double-click + context-menu | +9 / −0 | `c33fb1caa3c91d6b70232c0be16d1d95bd8da8598a4a81ca7c993ffa6d345804` |
| `tests/test_tray_readonly_debate.py` | NEW — acceptance + negative + integration + adoption tests (20) | +588 / −0 | `c4d3405d5c74016d57ec9bb040100fef2a44b47ed9a344047aa82dad59a846cf` |
| **Total (code + test artifacts)** | | **+1473 / −4** | |

> **ADVOCATE pre-registered acceptance (dcd888d8c576) + re-audit (370313098246).**
> Every falsifiable point S2a–S2e is mapped explicitly below; a break in any is a
> NO-GO. This revision closes the two re-audit blockers: **BLOCKER 1** (debate tabs
> were hidden at startup/refresh) and **BLOCKER 2** (stale S2d diff count).

## BLOCKER-1 fix — debate tabs stay visible (functional)

Root cause: `FullWindow.__init__` calls `refresh()` (`task_tray.py:1397`), whose
tab-visibility check (`task_tray.py:2024–2033`) hides any tab whose `raw` bucket
is empty unless it is in `always_visible`. Debate tabs load from the read-only
DAO (not from `raw`), so their bucket is always empty → all three were hidden at
startup and on every periodic refresh, making the registered load paths
unreachable in the real window. **Fix:** add `*self._DEBATE_TABS` to
`always_visible` so `recent`/`waiting`/`topics` are always shown; their content
still loads lazily via `_load_debate_tab`. **Regression test**
`test_BLOCKER1_fullwindow_debate_tabs_visible_and_load` instantiates the **real
`FullWindow`** against a throwaway read-write copy of FX-B (frozen fixture
untouched; isolated QSettings; bridge restore stubbed) and asserts all three tabs
are `isTabVisible` after `__init__` **and** after a second `refresh()`, that each
renders through `DebateListWidget`, and that `waiting`/`topics` load seeded rows.

## S2a — Fail-closed refusal DEMONSTRATED, before any DB open

A **real refused attempt** (negative test `test_S2a_refusal_precedes_any_db_open`
+ live run), not grep-absence. The prod-path guard runs in `__init__` **before**
`sqlite3.connect`, so prod is never opened and no prod `-wal`/`-shm` is touched:

```
REFUSED: DebateReadDAO refused forbidden DB path:
         '/home/rmanov/.claude/memory/memory.db' resolves to the fenced path
         '/home/rmanov/.claude/memory/memory.db'
sqlite3.connect calls during refused attempt: []   => prod NEVER opened, no -wal/-shm touched
```

The test spies `sqlite3.connect` and asserts the call list is empty on refusal.
`test_prod_path_fail_closed` additionally proves a `..`-relative form resolving to
prod is refused too (realpath check).

## S2b — mode=ro / query_only on EVERY code path (incl. subprocess/helper)

- Every DAO connection: `file:<path>?mode=ro` **and** `PRAGMA query_only=ON`
  (behavioral proof `test_query_only_blocks_writes`: `CREATE`/`INSERT` on the DAO
  connection raise `sqlite3.OperationalError`).
- **The leak-class from `acb9b91c901f` (a helper without `--db` defaulting to
  PROD) cannot occur here: there is NO subprocess/helper anywhere on the debate
  surface.** Scan:
  ```
  grep -nE "subprocess|Popen|os.system|os.exec|check_output|run\(" \
       debate_read_dao.py debate_list_widget.py
    → debate modules: NO subprocess/exec of any kind (no helper can default to PROD)
  ```
  The tray debate helper methods likewise spawn nothing. So there is no code path
  — direct or spawned — that opens a DB outside the `mode=ro`+`query_only` DAO.

## S2c — Ledger delta for this vehicle actor = empty (entire build window)

- **Actor-attribution method.** Any prod write goes through
  `apply_task_mutation` → `task_field_versions.updated_by` (machine id) +
  `record_memory_event` rows carrying `actor_type`/`actor_id`/`tool_name`. A write
  by this vehicle would surface as a `task_field_set` event / field-version row
  tagged with the tray/DAO tool name. **This vehicle defines no such tool name and
  calls none of those functions** (see S2e).
- **Empty by construction, whole window.** The build window is `[worktree add at
  c236d24 … this receipt commit]`. Across it, the only prod-capable code is
  `DebateReadDAO` opened `mode=ro`+`query_only` (cannot write), and the harness
  never opened prod at all (S2a/S2b). The tray was never launched. Therefore the
  vehicle's ledger delta on prod is **∅** — established structurally, **not** by an
  after-the-fact diff (deliberately: opening prod to diff it would violate the
  S2a zero-touch fence). Prod-file drift observed during the window
  (`memory.db` mtime advancing) is attributable to the **other live actors** (the
  running MCP servers) via the same `actor_id`/`tool_name` columns — not this
  vehicle.

## S2d — Frozen-clock reproduction of ALL manifest targets + hashes + clean worktree

Harness injects `as_of=2026-07-18T18:42:27Z` (no live `datetime.now`). `pytest -q`
→ **20 passed** (new suite, incl. the FullWindow visibility regression + the two
adoption-fix-1 tests); **109 passed** across the full touched 9-file tray/dialog
set, **0 regressions**.

Reproducible collection command (the exact 9 files that this change touches or
integrates with):

```
QT_QPA_PLATFORM=offscreen python3 -m pytest \
  tests/test_tray_readonly_debate.py \
  tests/test_tray_sync.py \
  tests/test_tray_purge_bridge_visible.py \
  tests/test_tray_filters.py \
  tests/test_tray_dialogs.py \
  tests/test_task_tray_reminders.py \
  tests/test_task_tray_memory_guard.py \
  tests/test_premium_task_tray.py \
  tests/test_task_db.py -q
  → 109 passed
```

| Gate | Fixture | Result |
|---|---|---|
| **T2** section-A parity | FX-A | `section_a == 0` (zero-parity) |
| **T1** recent (role-pin) | FX-B | `recent(1.0,'CODEX_FIXTURE',[DECISION,STATE,STATUS])` == the 10 targets `fxb-rec-01..10` in `ts DESC`; `role=None` == 24 (pin required) |
| **T3** waiting section-A | FX-B | `section_a == 10` == `{fxb-wait-01..10}` |
| **T6** topics | FX-B | 10 `fxb-topic-*` present; `topic_thread('fxb-topic-01').count == 3` |
| **T4** search per-source | FX-B | 15/15 nonces hit **only** their source; **per-source id list + order byte-equal to the board reference** on FX-B (`test_T4_fxb_search_exact_equality_vs_board`) |

Time-to-find **data precondition** (median of 5, under `as_of`):

```
T1 recent(role-pin): median=3.96ms   results=10  (zero-scroll: exactly 10 rows)
T3 waiting_section_a: median=15.90ms  results=10  (zero-scroll: exactly 10 rows)
T6 topics(targets):   median=3.17ms   results=10  (zero-scroll: exactly 10 rows)
```

*(The human-stopwatch T1/T3/T6 medians — ≤10 s / ≤12 s — remain a UI-acceptance
step for adoption; this receipt proves the data layer returns the exact 10-target
first screen in <16 ms, so it cannot be the bottleneck.)*

- **Post-build artifact hashes:** the five `sha256` in the file table above
  (recompute with `sha256sum …`).
- **Worktree receipt, artifact-scoped clean:** built from `c236d24`. The exact
  per-file `git diff --numstat c236d24` for the code + test artifacts is in the
  file table — **+1309 / −4** total across the 5 files, nothing else. **BLOCKER-2
  fix:** the earlier "1382 / 3" (and a `task_tray +182/−3` figure) were a stale
  total that folded in the receipt `.md`'s own churn; this receipt now reports
  **per-file numstat excluding the receipt document**, so its own edits can never
  desync the numbers. Fixture bytes unchanged across all runs (frozen
  `50e4f458…` / `06a72ee5…` verified before and after).
- **Production wiring is deliberately unfenced (adoption-gate pre-registered).**
  The live tray opens the DAO at `task_tray.py:1126` as
  `DebateReadDAO(self.db.db_path)` **without** `forbid_path` — correct: in
  production the tray legitimately reads its own DB, and the read-only guarantee
  there is **structural** (`mode=ro` + `PRAGMA query_only=ON`), not the harness
  fence. `forbid_path` is a **harness-only** fence so tests are structurally
  unable to touch prod. This split is intentional and recorded here for the
  adoption gate.

## S2e — Static S3-fence proof: no close/write/CAS capability

`test_S2e_no_cas_or_dml_or_mutation_in_new_code` (grep + AST) asserts across the
new debate surface:
- **no** `apply_task_mutation` / `apply_task_mutation_cas` / `update_task` /
  `mark_done` / `delete_task` call;
- **no** import of `db_utils` / `subprocess` / `close_task`;
- **no** CAS tokens (`expected_status` / `expected_version` / `expected_order` /
  `expected_event_id` / `BEGIN IMMEDIATE` / `ConflictError`);
- **no** raw DML (`INSERT/UPDATE/DELETE/CREATE/REPLACE`) on the read-only
  `self._conn` (only the ephemeral `:memory:` search mirror is written, on a
  separate connection);
- the tray debate helpers carry no mutation / subprocess / CAS token.

There is no mutating handler in the new code: the only write-shaped verb is the
`:memory:` FTS build, which never touches a persistent DB.

### py_compile + committed
- `python3 -m py_compile debate_read_dao.py debate_list_widget.py task_tray.py tray_dialogs.py` → OK.
- Receipt committed on `agent/tray-readonly-tabs` (this file).

## Fences honored

- Isolated worktree `/home/rmanov/sqlite-memory-tray-build` on branch
  `agent/tray-readonly-tabs` at the audited base `c236d24`. Live tray process and
  main checkout untouched.
- **Zero** close/write/CAS code (S3 fence). The only mutating verb in scope is
  absent; adoption/write stages are explicitly out of scope.
- No push (repo has `origin`; strictly local commits). No merge to main. No
  live-tray restart/deploy. Prod DB never opened by the harness.

## Adoption fix 1 + 2 (live UI-acceptance MODIFY — main @ `585fd28`)

Two live findings from operator UI-acceptance, fixed on `agent/tray-readonly-tabs`
(merge-ready; still read-only — no close/write/CAS added):

### Fix 1 — «Какво чака мен» was empty on live data

**Root cause (known from the manifest):** prod carries **zero `human-` recipients**,
so board `_section_a` runs in fallback-regex mode; but the real operator-await
asks address **roles** (ADVOCATE/CONDUCTOR/EXECUTOR…) and carry the await in the
**body**, which the narrow fallback marker never matches → the tab showed 0 while
half the ledger genuinely waits on the operator.

**Fix:** keep verbatim `_section_a` as **layer 1** (board parity untouched) and add
a **layer 2** in `waiting_section_a(live_await=True)` (default). Layer 2 admits a
message iff ALL hold: `kind ∈ {Q, DECISION, PING, STATUS}`, age `≤ 21 d`, author
not operator, **body matches an operator-await marker**, the body is not itself an
already-given/recorded GO (`_A_TAKEN` / `_A_REF` near the marker), the thread is
unresolved (no operator reply, no later `_A_TAKEN` descendant), and the msg is not
already in layer 1 (**dedup**). Output is `ts DESC`, `stale>5d` badged, layer-2
rows tagged `fwd="live-await: <phrase>"`.

**Exact marker set** (`_A_OPERATOR_AWAIT_LIVE`, calibrated against the live ledger):
`операторск\w*\s+ръка` · `операторск\w*\s+GO` · `операторск\w*\s+решени` ·
`deploy\s+решени` · `тво(я|ята)\s+ръка` · `кажи\s` · `при\s+оператора` ·
`\bGO\s+за` · `чака\w*\s+оператор` · `ратифицира` (case-insensitive). Calibration
on FX-A @ `as_of` (counts only, no bodies): 314 eligible → **77 raw marker
matches** across roles ADVOCATE/ADVOCATE_CODEX/CONDUCTOR/EXECUTOR3.

**FX-A / T2 status (honest):**
- **Layer 1 (`live_await=False`) on FX-A @ as_of = 0** → the **T2 board-parity
  contract is fully preserved** (`test_T2_fxa_section_a_zero` still green; the
  parity test explicitly uses `live_await=False`).
- **Live combined (`live_await=True`) on FX-A @ as_of = 52** genuine operator-await
  asks now surfaced (77 raw − already-given − resolved − dedup). This is the new,
  honestly-documented value the fix produces on the real snapshot; it is proven by
  `test_ADOPTIONFIX1_fxa_live_await_surfaces_real_asks`. On FX-B the layer-1 10
  seeded targets stay exactly 10 (`live_await=False`, T3 preserved) and are a
  subset of the 62-row combined view.
- Unit proof `test_ADOPTIONFIX1_live_await_surfaces_role_addressed` seeds synthetic
  role-addressed marker messages and asserts layer 2 surfaces them while excluding
  already-given / >21 d / resolved and deduping the human- recipient row.

### Fix 2 — tab order

`waiting` («Какво чака мен») now leads the debate tabs in `_tab_keys`
(`waiting, recent, topics`) — North-Star pain request #1. Existing task-tab
indices are unchanged (debate tabs remain appended after `done`).

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
