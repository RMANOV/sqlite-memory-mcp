#!/usr/bin/env python3
"""Operator helpers for debate wake runtime health."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
SERVICE_NAME = "sqlite-memory-debate-pump.service"
SERVICE_SRC = ROOT / "systemd" / "user" / SERVICE_NAME
SERVICE_DST = Path.home() / ".config/systemd/user" / SERVICE_NAME
SMOKE_TESTS = [
    "tests/test_debate_hooks.py",
    "tests/test_debate_priority.py",
    "tests/test_debate_v3_10_lifecycle.py",
    "tests/test_runtime_parity.py",
    "tests/test_debate_ops.py",
]


def _run(cmd: list[str], *, capture: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=ROOT,
        check=False,
        capture_output=capture,
        text=True,
    )


IS_WINDOWS = sys.platform == "win32"


def cmd_doctor(_: argparse.Namespace) -> int:
    if IS_WINDOWS:
        import debate_ops_windows

        return debate_ops_windows.cmd_doctor()
    import install_doctor

    payload = install_doctor.run_doctor(check_debate_runtime=True)
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0 if payload.get("ok") else 1


def cmd_refresh_hooks(args: argparse.Namespace) -> int:
    from runtime_parity import sync_runtime_hooks

    payload = sync_runtime_hooks(dry_run=args.dry_run)
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 1 if payload.get("missing_repo") else 0


def _systemctl(args: list[str]) -> dict[str, object]:
    cmd = ["systemctl", "--user", *args]
    result = _run(cmd, capture=True)
    return {
        "cmd": cmd,
        "returncode": result.returncode,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
    }


def cmd_install_service(args: argparse.Namespace) -> int:
    if IS_WINDOWS:
        import debate_ops_windows

        if args.dry_run:
            print(
                json.dumps(
                    {
                        "task": debate_ops_windows.TASK_NAME,
                        "dry_run": True,
                        "pythonw": str(debate_ops_windows._pythonw()),
                        "pump_args": debate_ops_windows.PUMP_ARGS,
                    },
                    indent=2,
                )
            )
            return 0
        return debate_ops_windows.cmd_install(start=not args.no_start)
    if not SERVICE_SRC.is_file():
        print(f"missing service template: {SERVICE_SRC}", file=sys.stderr)
        return 1

    payload: dict[str, object] = {
        "service": SERVICE_NAME,
        "source": str(SERVICE_SRC),
        "target": str(SERVICE_DST),
        "dry_run": args.dry_run,
        "actions": [],
    }
    if args.dry_run:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0

    SERVICE_DST.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(SERVICE_SRC, SERVICE_DST)
    actions = payload["actions"]
    assert isinstance(actions, list)
    actions.append(_systemctl(["daemon-reload"]))
    if not args.no_enable:
        actions.append(_systemctl(["enable", SERVICE_NAME]))
    if not args.no_start:
        actions.append(_systemctl(["restart", SERVICE_NAME]))
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 1 if any(item.get("returncode") for item in actions) else 0


def cmd_status(_: argparse.Namespace) -> int:
    if IS_WINDOWS:
        import debate_ops_windows

        return debate_ops_windows.cmd_status()
    return _run(
        ["systemctl", "--user", "status", SERVICE_NAME, "--no-pager"]
    ).returncode


def cmd_start(_: argparse.Namespace) -> int:
    if IS_WINDOWS:
        import debate_ops_windows

        return debate_ops_windows.cmd_start()
    return _run(["systemctl", "--user", "start", SERVICE_NAME]).returncode


def cmd_stop(_: argparse.Namespace) -> int:
    if IS_WINDOWS:
        import debate_ops_windows

        return debate_ops_windows.cmd_stop()
    return _run(["systemctl", "--user", "stop", SERVICE_NAME]).returncode


def cmd_uninstall(_: argparse.Namespace) -> int:
    if IS_WINDOWS:
        import debate_ops_windows

        return debate_ops_windows.cmd_uninstall()
    result = _run(["systemctl", "--user", "disable", "--now", SERVICE_NAME])
    SERVICE_DST.unlink(missing_ok=True)
    _run(["systemctl", "--user", "daemon-reload"])
    return result.returncode


def cmd_smoke(args: argparse.Namespace) -> int:
    cmd = [sys.executable, "-m", "pytest", *SMOKE_TESTS]
    if not args.verbose:
        cmd.append("-q")
    return _run(cmd).returncode


def cmd_work_queue(args: argparse.Namespace) -> int:
    from db_utils import get_conn
    from debate import list_open_debate_work

    states = [s.strip() for s in args.states.split(",") if s.strip()]
    topics = [t.strip() for t in args.topics.split(",") if t.strip()]
    with get_conn() as conn:
        payload = list_open_debate_work(
            conn,
            states=states or None,
            topics=topics or None,
            limit=args.limit,
        )
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


def cmd_set_priority(args: argparse.Namespace) -> int:
    from db_utils import get_conn_immediate
    from debate import set_topic_priority

    with get_conn_immediate() as conn:
        payload = set_topic_priority(
            conn,
            topic_id=args.topic_id,
            role="CONDUCTOR",
            lane=args.lane,
            reason=args.reason,
            next_action=args.next_action,
            blocked_by=args.blocked_by,
        )
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Local debate wake pump and runtime hardening helpers."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="Print debate runtime doctor JSON.")
    doctor.set_defaults(func=cmd_doctor)

    refresh = sub.add_parser(
        "refresh-hooks",
        help="Copy tracked repo hooks into the live Claude runtime hook dir.",
    )
    refresh.add_argument("--dry-run", action="store_true")
    refresh.set_defaults(func=cmd_refresh_hooks)

    install = sub.add_parser(
        "install-service",
        help="Install and start the systemd user debate pump service.",
    )
    install.add_argument("--dry-run", action="store_true")
    install.add_argument("--no-enable", action="store_true")
    install.add_argument("--no-start", action="store_true")
    install.set_defaults(func=cmd_install_service)

    status = sub.add_parser(
        "status",
        help="Show pump service/task status (systemd on Linux, Scheduled Task + heartbeat on Windows).",
    )
    status.set_defaults(func=cmd_status)

    start = sub.add_parser("start", help="Start the resident debate pump.")
    start.set_defaults(func=cmd_start)

    stop = sub.add_parser(
        "stop",
        help="Stop the resident pump gracefully (in-flight workers survive).",
    )
    stop.set_defaults(func=cmd_stop)

    uninstall = sub.add_parser(
        "uninstall", help="Stop and remove the pump service/Scheduled Task."
    )
    uninstall.set_defaults(func=cmd_uninstall)

    smoke = sub.add_parser("smoke", help="Run focused debate runtime smoke tests.")
    smoke.add_argument("--verbose", action="store_true")
    smoke.set_defaults(func=cmd_smoke)

    queue = sub.add_parser(
        "work-queue",
        help="Print deterministic open debate work priority queue.",
    )
    queue.add_argument("--states", default="INIT,ACTIVE")
    queue.add_argument("--topics", default="")
    queue.add_argument("--limit", type=int, default=50)
    queue.set_defaults(func=cmd_work_queue)

    priority = sub.add_parser(
        "set-priority",
        help="Set CONDUCTOR topic priority lane P0..P7.",
    )
    priority.add_argument("topic_id")
    priority.add_argument("lane")
    priority.add_argument("reason")
    priority.add_argument("--next-action", default="")
    priority.add_argument("--blocked-by", default="")
    priority.set_defaults(func=cmd_set_priority)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
