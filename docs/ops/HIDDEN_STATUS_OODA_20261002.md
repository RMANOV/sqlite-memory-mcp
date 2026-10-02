# Hidden task status preservation: OODA fix plan

Date: 2026-10-02
Base: `ea21e20`
Surface: task store, bridge import/export, index and Kanban projections.
Status: verified candidate; commit/tag/merge/push and runtime receipts follow.

## Observe

A production synchronization changed a locally archived task back to `done`.
An isolated copy reproduced the change without changing its row timestamp or
status field version. Its hidden row was newer than the last versioned `done`
write. The writer of the unversioned archive has not been identified. The
current auto-archive API does write a status event; attribution remains open.

Two paths reproduce the problem:

1. Import repairs the row from old status authority before checking the existing
   hidden-status guard. The guard then sees the repaired `done` projection.
2. Export canonicalization substitutes the same old `done` authority for the
   archived row. A hidden wire row paired with non-hidden field authority is
   also unsafe: peers resolve the embedded value before merging.

Private incident receipts and the immutable pre-sync backup remain outside the
source repository. Tests use synthetic records with the same inconsistent shape.

## Orient

`archived` and `cancelled` are hidden states; `done` is visible. An unversioned
hidden closure cannot be silently made visible by reconciliation. Row timestamps
are not authoring clocks. A merge/repair event must never become the source of
status authority.

Preserve these existing behaviors:

- stale non-hidden projections still repair from actual field/event authority;
- hidden-to-hidden changes retain their normal clock ordering;
- deliberate local reopening through the versioned task mutation API works;
- parent/link/attachment preservation and explicit-clear authority remain intact;
- imports and exports do not invent archival events or change provenance.

## Decide

Candidate minimum fix, subject to skeptical review:

1. Define one hidden-to-visible transition predicate and use it before any
   status projection repair or import write, including audit replay. Keep the
   original row status available for guards on scheduling fields. A genuine
   newer status authoring event may reopen a versioned hidden closure only when
   both closure and reopening events match the task, field, value and exact
   field/event versions. Subsequent visible edits retain the reopening witness;
   a later hidden authoring event requires a new matching reopening witness.
   A newer value-only clock is insufficient. An unrelated
   newer description edit must not disqualify a causal reopen.
2. Cover the tombstone path as well as ordinary index and full-content imports.
   Record rejected remote writes through the existing conflict ledger. Reject
   contradictory duplicate UUIDs and hidden payloads with visible authority
   before batch mutations; a tombstone must resolve to a hidden status.
3. Reject export with `TaskExportConflict` if a hidden row disagrees with
   non-hidden status authority without matching ordered reopening authorship.
   Validate the whole export batch before mutating
   generated payloads. Do not emit a forged clock, altered event value, or
   half-canonicalized snapshot. The actionable repair is an explicit versioned
   task update that confirms the intended status. Add narrow same-hidden-status
   reaffirmation for inconsistent authority through the existing API: author one
   real status event with the caller's provenance, invalidate the push stamp,
   then leave repeated consistent confirmations as ordinary no-ops.
4. Add synthetic regressions for both hidden states, legacy/equal/new clocks,
   imported event heads, malformed/contradictory tombstones, repeated pulls,
   full and incremental export, all generated projections and API reopening.
   Include audit replay with a newer unrelated title event and malformed event
   identity. Export preflight covers the union of task/index/public projections,
   including aged-out public hidden rows and supplied export overrides.

The export preservation claim starts after Git/import/migration: those earlier
phases can change downloaded files. A blocked export must author no generated
file/attachment changes and must not stage, commit or push a partial snapshot.

Adversarial review added these blockers: audit replay bypass; attachment writes
before late status validation; same-state API no-op; valid distributed reopening;
ambiguous event identity; and repeated UUIDs bypassing a cached row guard.

The final review added pending-reopen confirmation, projection membership and
tombstone consistency, optional legacy ledger columns, and reopening followed by
additional visible edits before a pull. Intermediate status authoring events
remain available to validate that latter case.

The grant uses existing status authoring events and their total clock order; it
does not add a causal-token protocol. A legacy archive written after a reopen
without changing any field/event token can be indistinguishable from an older
hidden projection. The verified protection covers the observed hidden-row vs
older visible-authority incident shape; all status writers should use the
versioned API. No claim of identifying the unknown legacy writer is made.

## Act

1. Two independent skeptics review semantic/provenance risks and test/runtime
   failure paths. Revise this plan with their findings before implementation.
2. Implement in an isolated worktree. First demonstrate failing regressions on
   the base revision, then make them pass. Re-run existing authority tests.
3. Run bridge smoke, preservation gate, lint and the full isolated pytest suite.
   Review failures against the base when needed; retain exact result receipts.
   Pin child-process imports to the candidate worktree, since the editable
   installation otherwise points to the old main checkout.
4. Obtain adversarial review of the final diff and resolve blocking findings.
5. Back up the live DB and both Git heads. Reproduce the original private fixture
   using the new code; verify no row/version loss and `PRAGMA quick_check`.
6. Commit, annotate `ws/BRIDGE-HIDDEN-STATUS-20261002`, merge the tested branch
   into local `main`, then push `main` and the tag. Verify live remote refs and
   ancestry without rewriting history. Keep rollback possible through the saved
   heads and DB backup.
7. Use a fresh private bridge worker for serial pull/status checks, hook doctor
   and an idempotent second pull. Compare task/note/entity counts and UUID sets.
   A Git receipt does not prove another machine has loaded the new code.
8. Only after the fix release completes, record the user's confirmed Callosum
   application in the job tracker, deduplicate by company and requisition URL,
   read it back, then synchronize the private bridge and verify the receipt.

## Acceptance and feedback

The incident fixture stays hidden under import, and inconsistent export blocks
before changing generated files. Correctly versioned closures and intentional
API reopening synchronize normally. Repeated reconciliation causes no domain
or field-version churn. All relevant automated gates pass, and source/tag/data
remote readbacks agree with the local commits.

If a counterexample violates one of these invariants, return to Observe with
the failing fixture, refine Decide, and repeat Act before publishing.

## Verification receipts

- Two independent read-only skeptics: semantic and runtime reviews PASS after
  their blocking findings were fixed and converted into synthetic regressions.
- Before implementation, the new incident regression failed on the base with
  an `archived` row becoming `done`.
- Full isolated suite: **1944 passed, 19 skipped**, two dependency/environment
  warnings, 466.44 seconds on Windows/Python 3.14.
- Required `python bin/bridge_ops.py smoke`: **115 passed**.
- `python -m ruff check .` and `git diff --check`: PASS.
- Child-process module readback resolved to the candidate worktree.
- The original private incident fixture stays `archived`, its status version
  stays unchanged, inconsistent export raises `TaskExportConflict`, and both
  first and repeat imports report zero new rows/field changes.
- Read-only export preflight on the live DB: PASS, 1913 parent overrides.
- Live DB backup: `quick_check=ok`; source and bridge rollback heads saved.

The full suite includes the bridge parent/bootstrap/link/entity preservation
gate. Skipped tests are not counted as passed. No second-machine runtime
acceptance is claimed by these local checks.
