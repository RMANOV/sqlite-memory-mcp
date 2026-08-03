# Changelog

All notable changes to `sqlite-memory-mcp` are recorded here. This file
follows the spirit of [Keep a Changelog](https://keepachangelog.com/) and the
project uses semantic-ish versioning on the `3.x` line.

## v3.13.4

### Fixed

- **Bookkeeping events are no longer export authority.** `merge` and `repair`
  events record *that* a reconciliation happened; they do not author a value.
  The status-authority resolver nevertheless ranked them alongside real writes,
  so a bookkeeping event carrying a fresh local clock outranked the write it was
  describing and the export claimed authorship this machine never had — silently
  reverting a peer's edit on the next sync. They are now excluded while the
  event head is chosen (both the local and the remote builder) and again in the
  resolver, which also covers lookups by `source_event_id`. Excluding them only
  in the resolver would let a bookkeeping event win head selection and take the
  authoring event ranked below it down with it.
- **Withdrawn: stamping merge events with absorbed authority (v3.13.3).** That
  approach wrote a foreign `(machine_id, logical_clock)` pair, which collides
  with the peer's own event under the unique index on
  `memory_events(machine_id, logical_clock)` — reachable in production because a
  pull imports peer events before merging tasks. Reproduced for `notes`, `title`
  and `priority`; `status` was immune only because a peer status event in the
  ledger makes the field a materialization repair, so no merge event is written.
  Bookkeeping events go back to fresh local clocks, which is now harmless.

## v3.13.3

### Fixed

- **Merge events no longer claim local authorship.** Absorbing a peer's field
  write recorded an audit event stamped with a fresh local logical clock. That
  synthetic event then outranked the field version it came from, so the export
  advertised the local machine as the author of a value it never wrote and the
  peer's next genuine edit was silently reverted on the next sync. The event is
  now stamped with the authority it absorbed, and carries
  `payload.synthetic_authority` so a peer receiving an event that claims its own
  authorship can tell it apart from the original. Peers that send no packed
  clock keep the previous behaviour unchanged. The effect was status-only,
  because only `status` is canonicalised on export.
- **Future field-version clocks are clamped at startup.** A future-dated packed
  logical clock in `task_field_versions.updated_order` outranks every later
  write permanently; the existing clamp applied only in memory for the duration
  of a single merge, so the row stayed poisoned on disk, warned on every sync,
  and was re-exported to every peer. Startup now pulls such rows back while
  preserving their counter, using the same tolerance as the runtime clamp.

## v3.13.2

### Fixed

- **Orphan task field-version cleanup.** Startup now removes
  `task_field_versions` rows whose parent task no longer exists. The cleanup is
  idempotent and preserves every version row with a live parent; configured
  production connections continue to enforce the existing `ON DELETE CASCADE`
  foreign key so new hard deletes cannot recreate the orphan set.

## v3.13.1

### Fixed

- **Legacy debate role/session migration.** Databases created before the
  one-active-role-per-session invariant can contain multiple active roles for
  the same `(topic_id, session_id)`. Startup now keeps the most recently
  updated binding, retires older duplicates with an audit reason, and only then
  creates the unique index. This prevents `sqlite_bridge` and `sqlite_intel`
  startup paths from failing while preserving the complete binding history.
- **Foreign-key baseline during debate schema rebuilds.** The v1 envelope
  migration now rejects only foreign-key violations introduced by its own
  rebuild, instead of aborting on unrelated legacy violations that already
  existed elsewhere in the database.

## v3.12.5

### Fixed

- **Bridge sync no longer blocks on the `kanban_payload.json` artifact.** v3.12.4
  wired `kanban_payload.json` into `surface_contract` and the merge driver but
  missed `db_utils.is_generated_bridge_path()` / the `generated_paths` restore
  list. Because the export regenerates the file each run and leaves it
  uncommitted, the pre-sync readiness check (`_path_allowed_dirty`) treated it as
  a user-managed edit and failed closed with *"commit or stash bridge repo edits
  before sync: kanban_payload.json"* — silently freezing the mirror (and any
  downstream restore that relies on it). The file is now recognized as a
  regenerable generated artifact: allowed-dirty through the readiness gate and
  restorable from DB state alongside `shared.json`/`index.json`. This restores
  the v3.12.4 "sync stays ON" guarantee. Regression test added
  (`test_kanban_payload_is_recognized_as_generated_bridge_path`).

## v3.12.4

### Added

- **Render-only `kanban_payload.json` bridge artifact.** The Kanban PWA was
  loading the full `shared.json` (~18 MB, with single notes up to ~540 KB),
  which hung the browser render. Exports now also emit a separate
  `kanban_payload.json` that mirrors the payload but truncates task
  descriptions: non-active notes (done/archived/someday) collapse broadly,
  active notes over 20 KB truncate, small active notes pass through full. Each
  truncated copy carries `_mirror_preview` / `_full_len` / `_full_hash`
  (sha256). Generated on both export paths (`bridge_sync_worker` and
  `bridge_server.bridge_push`) before git staging.

### Guarantees

- **Transport is never truncated.** `shared.json`, `index.json`, and
  `tasks/*.json` keep full bodies, so a fresh pull / restore recovers the
  complete description. The new artifact is `pull=False` in the surface contract
  (never an import source) and `merge=union` + self-heal, so a corrupt
  union-merged copy is rebuilt from the DB on the next export. Write failures are
  non-fatal to push.

### Notes

- This closes the export/artifact-generation side only. The Kanban PWA consumer
  repoint (reading `kanban_payload.json`) is deferred to a separate change
  because `pwa/app.js` is read-write and a naive preview-read could write
  truncated bodies back to `shared.json`.

## v3.12.3

### Fixed

- **Task Tray full-window launch when Dashboard is empty.** The large Task
  Manager window now starts on Today instead of forcing an empty curated
  Dashboard tab, hides Dashboard when no curated rows exist, and restores the
  window through an explicit Win32-visible path when another tray instance sends
  `SHOW`.
- **Task Tray enrich-cache worker pileup.** Periodic and manual refresh paths now
  use a single-flight guard so long-running enrich-cache refreshes cannot spawn
  overlapping background workers every 60 seconds.

## v3.12.2

### Fixed

- **Bridge sync duplicate-redirect marker precision.** Archived duplicate
  redirects now require the explicit `ARCHIVED DUPLICATE` marker plus `DO NOT
  USE` or `SUPERSEDED`, preventing ordinary archived notes about
  deduplication/canonicalization from bypassing bridge shrink guards.

## v3.12.1

### Fixed

- **Bridge sync archived-duplicate redirect preservation.** The bridge safety
  and export paths now recognize archived duplicate redirect tasks as
  intentional canonical pointers, so content-aware shrink guards do not restore
  stale full task bodies over a short `DO NOT USE` redirect stub.

## v3.12.0

> Release notes for the `v3.11.x` line — summarizing the work landed on `main`
> after the `v3.11.19` tag.

### Added

- **`debate_add_role` — flexible debate roster.** Roles can now be added to a
  debate topic *after* `debate_init`, instead of being fixed at topic creation.
  This lets a running multi-agent debate grow its participant set (for example,
  binding a new EXECUTOR or ADVOCATE mid-flight) without recreating the topic.
  Role addition goes through the same validation and mutation ledger as the rest
  of the debate lifecycle.

### Fixed

- **Push-aware tombstone retention in tray purge sync.** When the Task Tray
  purges a task and that deletion is synced across machines, the tombstone is
  now retained in a push-aware way so a peer that has not yet seen the deletion
  cannot resurrect the task on the next pull. This closes a class of
  "deleted task comes back" regressions in the cross-machine bridge sync path.

### Builds on (established `3.x` capabilities, unchanged this release)

These are not new in this delta; they are part of the shipped `3.x` foundation
that the above work extends, and are listed for launch-context clarity:

- **Debate Protocol v2** — the schema, validators, and lifecycle state machine
  behind multi-agent debate (addressed messages, role bindings, watermarks,
  topic state transitions). See `docs/DEBATE_PROTOCOL.md`.
- **Hybrid BM25 + semantic search (RRF).** FTS5 BM25 ranking fused with
  optional sqlite-vec vector results via Reciprocal Rank Fusion
  (`vec_search.py::rrf_merge`). The vector path is opt-in via the `vector`
  extra; without it, search falls back to pure FTS5 BM25.

### Notes

- Existing databases migrate forward automatically on first `init_db()`; no
  manual migration step is required for this delta.
- No grand tool-count total is asserted here. Tool counts per server are
  documented in the README/Tool Reference, which is the canonical surface;
  this changelog tracks behavioral deltas, not a headline number.

---

For releases up to and including `v3.11.x`, see the historical GitHub release
descriptions.
