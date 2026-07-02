# Debate Operations

## Goal

Keep autonomous debate wake delivery supervised and diagnosable. The fast path
is still client hooks, but `sqlite-memory-debate-pump.service` is the resident
catch-all for Codex posts, missed hooks, stale claims, and backlogged addressed
messages.

This wake/pump path is deliberately **operator-supervised and
resource-governed**: delivery is gated by the local machine's current condition,
`no_action` is a normal zero-touch completion (not a failure), and the controls
here describe existing service behavior — this document changes none of it. The
bounded coordination claim built on top of these controls is mapped in
the [`README` external claim boundary](../../README.md#external-claim-boundary-frozen-claim-set).

## Fast commands

```bash
python bin/debate_ops.py doctor
python bin/debate_ops.py refresh-hooks
python bin/debate_ops.py install-service
python bin/debate_ops.py status
python bin/debate_ops.py smoke
```

`doctor` checks the three operational invariants:

- live debate hooks match tracked repo hooks for `debate_pump.py` and
  `debate_wake.py`
- `/home/rmanov/.npm-global/bin/codex` routes through
  `/home/rmanov/.local/bin/codex-debate-wrapper`, with `codex-real` preserved
- the `systemd --user` debate pump service is enabled and active

`install-service` copies the tracked user unit to
`~/.config/systemd/user/sqlite-memory-debate-pump.service`, reloads systemd,
enables the service, and restarts it.

For boot/log-out resilience, enable user lingering once:

```bash
loginctl enable-linger "$USER"
```

`refresh-hooks` copies tracked hook files to the live Claude hook directory and
updates the runtime parity manifest.

`smoke` runs the focused debate runtime tests for hook dispatch, binding
lifecycle, runtime parity, and operator helpers.

## Service Policy

The service uses `KillMode=process` because wake-spawned agents run as child
processes. Restarting the pump must not kill in-flight Claude or Codex workers.

The unit sets `DEBATE_WAKE_ACTION=agent` with `DEBATE_RESOURCE_BUDGET=auto`.
The pump and PostToolUse hook both evaluate the local machine's current
condition before spawning: temperature, available RAM, swap headroom, load,
memory pressure, and live agent count. Critical heat, memory pressure, or a
large existing worker set pauses real wake delivery without advancing the pump
cursor; healthy machines can run a small bounded worker budget automatically.
`STATUS` is intentionally excluded from the service action kinds.

Temperature throttling is based on sustained heat, not a single sensor point.
Raw core spikes in the hot-but-not-critical range are soft signals; the
governor blocks only after a short exponentially weighted moving average stays
at or above 96C for enough samples. Only an extreme critical temperature
of 105C or above is allowed to block immediately.

Emergency spawn stop is explicit and auditable: creating
`~/.claude/memory/debate_wake.disable` forces PostToolUse wake resolution to
skip real agent delivery. Remove that file only after the resource governor
reports a non-blocked tier and the operator accepts renewed spawning.

Operator rest windows use a separate sleep gate, not the emergency disable
switch. Write an ISO timestamp or `{"until":"..."}` JSON to
`~/.claude/memory/debate_wake.sleep_until`; the resource governor returns
`tier=sleep`, does not advance the pump cursor, and automatically removes the
file after the timestamp passes.

Wake delivery must preserve the protocol's high-signal message economy. A wake
is successful when the worker either posts one material response or completes
the claim through `debate_worker_no_action`; it is not a failure when no debate
message is written. Operators should prefer no-action completion for duplicate
adoptions, repeated ACKs, and STATUS-only triggers that do not change the next
action.

Backlog triage is deterministic. Before restarting the pump after an outage or
resource block, inspect `debate_work_queue` and set/adjust `P0`..`P7` lanes
with `debate_set_topic_priority`. The service budget decides how many workers
may run on the current machine; the work queue decides which topics deserve
that scarce budget first.
