# Bridge Operations

## Goal

Keep bridge health, tray-side sync behavior, and recovery confidence visible without relying only on ad-hoc memory.

Bridge sync is the **governance / resilience spine** of the cross-machine path. The claim it backs is bounded: **health / conflict / recovery discipline against no-resurrect / no-data-loss failures**, plus operator recovery drills — *not* an absolute, impossible-to-lose-data guarantee. See the [`README` external claim boundary](../../README.md#external-claim-boundary-frozen-claim-set) for how this fits the public claim map.

## Fast commands

```bash
python bin/bridge_ops.py doctor
python bin/bridge_ops.py refresh-hooks
python bin/bridge_ops.py smoke
```

`doctor` prints the current `bridge_doctor` JSON snapshot from the local repo/runtime.

`refresh-hooks` copies the tracked repo bridge hooks into the live Claude runtime
hook directory and rewrites the runtime parity manifest.

`smoke` runs the highest-signal automated checks for:
- bridge export/import
- bridge worker safety and recovery
- tray-side sync ownership and initiators

## Recommended cadence

- After any change in `task_tray.py`, `tray_sync.py`, `bridge_sync_worker.py`, `bridge_server.py`, or `db_utils.py`:
  - run `python bin/bridge_ops.py smoke`
- Before or after a machine-to-machine rollout:
  - run `python bin/bridge_ops.py doctor`
- When `doctor` reports runtime hook drift:
  - run `python bin/bridge_ops.py refresh-hooks`
- Weekly or before high-risk travel / machine changes:
  - run one manual fresh-machine recovery drill from bridge only

## Export and attachment contract

Task export eligibility and attachment retention are separate contracts:

- `task_attachments.status = 'active'` with a valid `stored_relpath` keeps the
  blob eligible for retention even when its parent task is outside the current
  tombstone/export window.
- A full export builds the active-attachment keep-set before the empty-task
  return. Therefore a full export with zero task JSON files still retains an
  active attachment blob.
- Full-export cleanup may remove generated task JSON that is no longer
  export-eligible and attachment blobs with no valid active metadata.
- Incremental export is task-scoped and must not run global stale-file cleanup;
  otherwise an unchanged task or attachment can be deleted by a partial
  keep-set.
- Marking an attachment inactive/removed permits cleanup on a later full
  export; it must not make unrelated active attachments removable.

The regression gate is:

```bash
python -m pytest -q tests/test_bridge_export.py tests/test_memory_bridge_import.py tests/test_tray_purge_bridge_visible.py
```

The focused cases cover an aged-out parent with an active attachment, zero
exportable tasks with an active attachment, and removed-versus-active cleanup.
The operational smoke wrapper remains:

```bash
python bin/bridge_ops.py smoke
```

The GitHub workflow (`.github/workflows/ci.yml`) runs `ruff check .` and the
full `pytest -q` suite, so the focused bridge regression tests are included in
CI. The registry smoke job is a separate core-only MCP check. CI runs on Linux;
Windows-specific byte-preservation and operator recovery checks remain manual.

## Windows recovery note

The bridge preflight may rebuild generated paths, including `tasks/` and
`attachments/`. An untracked recovered copy under the bridge directory is not
durable by itself: restore the canonical local attachment source before export,
or use an already verified bridge commit, then read back the result.

When exact attachment bytes matter on Windows, do not rely on an ordinary Git
checkout/restore without checking attributes and line-ending behavior. Use a
byte-preserving restore from a verified object/source and compare SHA-256
against a manifest before and after the canonical worker run.

## Still manual

These remain operator checks and are not closed by unit tests:
- fresh-machine recovery from bridge only
- attachment byte/open/remove parity on a second machine
- long-lived tray session observation for hidden churn
- safe merge conflict recovery with user-managed bridge files present
