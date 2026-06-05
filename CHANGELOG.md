# Changelog

All notable changes to `sqlite-memory-mcp` are recorded here. This file
follows the spirit of [Keep a Changelog](https://keepachangelog.com/) and the
project uses semantic-ish versioning on the `3.x` line.

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
