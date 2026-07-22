# Debate Operations

## Goal

Keep autonomous debate wake delivery supervised and diagnosable. The primary
path is a post-commit kernel event into the resident pump; durable
`debate_delivery_queue` rows identify the exact addressed work. Client hooks and
adaptive replay sweeps cover mixed-version clients, missed events, stale claims,
and crash recovery.

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
python bin/debate_ops.py start
python bin/debate_ops.py stop
python bin/debate_ops.py uninstall
python bin/debate_ops.py smoke
```

## Windows runtime (REV 2.2 zero-paste delivery)

On Windows the same commands manage a resident user-level pump. `install-service`
first tries a **user-level Scheduled Task** (`SqliteMemoryDebatePump`); on a
managed machine where IT policy denies `schtasks /Create` (observed on this
host: `ERROR: Access is denied.`) it automatically falls back to an **HKCU
`...\CurrentVersion\Run` entry** — user-writable, starts at logon, no admin.
`status`/`doctor` report which mechanism is active (`autostart_mechanism`).
Trade-off: the Run-key path has **no automatic restart-on-failure** (only the
Scheduled Task does); a crashed pump is restarted at next logon or via
`debate_ops.py start`. `MultipleInstances=IgnoreNew` is enforced regardless by
an atomic named-mutex singleton guard inside the pump. `doctor` treats a
running pump + a present autostart mechanism as **mandatory** checks. No admin
required either way:

- runs hidden at user logon via `pythonw.exe`, working directory = repo,
  `MultipleInstances=IgnoreNew`, automatic restart on failure;
- initial machine cap `--max-concurrent-workers 2` (the resource governor may
  lower it; backlog waits and is never lost) and
  `--mcp-prefix mcp__sqlite_unified__` for spawned claude workers;
- **post-commit wake**: `debate_post` / `debate_post_with_recipients` fire the
  named kernel event `Local\SqliteMemoryDebateWakeV1` strictly AFTER the DB
  commit. The pump blocks on that event (`WaitForMultipleObjects`), so ordinary
  delivery does not wait for a timer. An adaptive 30s → 60s → 120s → 240s →
  300s timeout sweep replays a crash between commit and signal; the sweep is a
  recovery watchdog, never the normal delivery mechanism;
- **target isolation**: every new official executor role is a numbered address
  (`EXECUTOR_1`, `EXECUTOR_2`, ...); the DB permits exactly one active owner per
  `(topic, role)` and exactly one active role per `(topic, session_id)`;
- **graceful stop**: `debate_ops.py stop` sets
  `Local\SqliteMemoryDebatePumpStopV1`; the pump exits after the current scan
  without killing in-flight workers (`schtasks /End` only as fallback);
- **worker identity** is `pid + create_time` (recorded in the spawn receipt) —
  a reused PID is never accepted as the old worker, and a pump restart never
  retires a live Windows worker;
- workers spawn hidden (`CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP`) with a
  `shutil.which`-resolved executable and a bounded spawn-log directory
  (`DEBATE_WAKE_AGENT_LOG_KEEP`, default 50);
- `status`/`doctor` read `~/.claude/memory/debate_pump_heartbeat.json`
  (pid + create_time + ts) and distinguish running / stale / stopped;
- resource governor: memory comes from psutil (Win32 fallback); zero total
  memory is a loud adapter error, never `mem_available_low_0mib`; unknown
  temperature lands in the guarded-concurrency tier, never a permanent block.

The `debate_wake.disable` and `debate_wake.sleep_until` kill-switch files
below remain authoritative on Windows exactly as on Linux.

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
