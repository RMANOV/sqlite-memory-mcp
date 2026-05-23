# Debate Operations

## Goal

Keep autonomous debate wake delivery supervised and diagnosable. The fast path
is still client hooks, but `sqlite-memory-debate-pump.service` is the resident
catch-all for Codex posts, missed hooks, stale claims, and backlogged addressed
messages.

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

The unit sets `DEBATE_WAKE_ACTION=agent`, includes `STATUS` in action kinds, and
limits burst launch pressure through the pump's worker throttles.
