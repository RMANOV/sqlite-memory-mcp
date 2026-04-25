# Release Confidence

Date: 2026-04-25
Release anchor: `v3.7.2` / `1efb17f`

This page documents the checks used to build confidence in the current release.
It is intentionally practical: every release-quality claim below should be
backed by a command, a test, or an explicitly named manual check.

## What Was Verified

The release was checked locally with:

- install doctor on a disposable database
- demo quickstart on the same disposable database
- focused tests for install/demo flow, tray dialogs, bridge operations, and
  runtime hook parity
- bridge hook refresh in dry-run mode
- bridge smoke tests for export/import, worker safety, and tray-side sync paths

The final local status was clean against `origin/main` after the documentation
update.

## Reproducible Checks

Use these commands when validating the release on another machine:

```bash
sqlite-memory-doctor --db /tmp/sqlite-memory-mcp-v372-confidence.db --check-gui --check-bridge --json
sqlite-memory-demo --db /tmp/sqlite-memory-mcp-v372-confidence.db --reset --json
pytest -q tests/test_install_demo_flow.py tests/test_tray_dialogs.py tests/test_bridge_ops.py tests/test_runtime_parity.py
python bin/bridge_ops.py refresh-hooks --dry-run
python bin/bridge_ops.py smoke
```

Expected high-level result:

- doctor returns `ok: true`
- demo creates a project, entity, task, and note
- focused tests pass
- hook refresh dry-run reports no drift
- bridge smoke passes

## Release-Quality Signals

### Install And Demo Path

`sqlite-memory-doctor` and `sqlite-memory-demo` are the first confidence gates.
They prove the package can initialize a real database, import required runtime
dependencies, check GUI availability when requested, and create demo data
without relying on an existing user database.

### Tray And Reminder Path

Focused tray dialog tests cover the UI-side reminder and recurring-task flows.
These tests are not a replacement for manual tray usage, but they catch the
highest-risk regressions around task creation and edit surfaces.

### Bridge Runtime Path

`bridge_ops refresh-hooks --dry-run` checks whether tracked bridge hooks match
the live runtime hook directory. `bridge_ops smoke` then exercises the bridge
export/import and worker-safety paths.

This matters because a green repository is not enough if the local runtime is
still using stale hook files.

### Runtime Boundary Path

Premium and private-runtime features are not treated as active by default. The
public host expects signed, machine-bound, policy-aware inputs before protected
runtime paths are allowed.

For release confidence, the important property is not that premium code exists
in this repository. It does not. The important property is that the public host
has explicit allow/deny gates and audit paths when those features are wired by
an operator.

## What Is Not A Release Gate

Static badges, broad scan summaries, and third-party safety labels are not
release-confidence evidence unless they scan the actual repository state and
produce reproducible output.

A badge can be useful as decoration. It should not replace local doctor, demo,
tests, bridge parity, or smoke checks.

## Known Manual Checks

These are still manual confidence gates and should be run before relying on the
system across machines:

- fresh-machine restore from bridge-only state
- cross-machine tray create/edit checks for reminders, recurring tasks, and
  attachments
- long-lived tray observation for hidden writes or sync churn
- generated-file conflict recovery with user-managed bridge files present
- context-pack false-positive checks before using retrieved context in executor
  workflows

## Scope Of The Claim

`v3.7.2` can be presented as a locally verified release with repeatable
install/demo/bridge confidence checks.

It should not be presented as proof of:

- perfect memory
- complete recovery coverage on every machine
- semantic merge correctness under every concurrent edit
- third-party scanner verification
- enterprise-grade deployment hardening

The near-term release-quality focus is install clarity, demo reliability, tray
stability, bridge reliability, and real user or pilot feedback.
