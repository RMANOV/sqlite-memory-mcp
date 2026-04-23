# Bridge Operations

## Goal

Keep bridge health, tray-side sync behavior, and recovery confidence visible without relying only on ad-hoc memory.

## Fast commands

```bash
python bin/bridge_ops.py doctor
python bin/bridge_ops.py smoke
```

`doctor` prints the current `bridge_doctor` JSON snapshot from the local repo/runtime.

`smoke` runs the highest-signal automated checks for:
- bridge export/import
- bridge worker safety and recovery
- tray-side sync ownership and initiators

## Recommended cadence

- After any change in `task_tray.py`, `tray_sync.py`, `bridge_sync_worker.py`, `bridge_server.py`, or `db_utils.py`:
  - run `python bin/bridge_ops.py smoke`
- Before or after a machine-to-machine rollout:
  - run `python bin/bridge_ops.py doctor`
- Weekly or before high-risk travel / machine changes:
  - run one manual fresh-machine recovery drill from bridge only

## Still manual

These remain operator checks and are not closed by unit tests:
- fresh-machine recovery from bridge only
- attachment open/remove parity on a second machine
- long-lived tray session observation for hidden churn
- safe merge conflict recovery with user-managed bridge files present
