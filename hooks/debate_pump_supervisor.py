#!/usr/bin/env python3
"""Keep the user-level debate pump resident on Windows.

The pump remains the only process that scans SQLite and routes addressed
messages.  This process only starts it after an unexpected exit and exits
itself after a clean operator stop.  It is intended for HKCU Run, which is
available on managed machines where a user Task Scheduler registration is
denied.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from debate_ops_windows import PUMP_ARGS, PUMP_SCRIPT, _pythonw  # noqa: E402


MEMORY_DIR = Path(
    os.environ.get("SQLITE_MEMORY_DIR", os.path.expanduser("~/.claude/memory"))
)
HEARTBEAT_PATH = Path(
    os.environ.get(
        "DEBATE_PUMP_HEARTBEAT",
        str(MEMORY_DIR / "debate_pump_heartbeat.json"),
    )
)
PUMP_LOG_PATH = Path(
    os.environ.get("DEBATE_PUMP_LOG", str(MEMORY_DIR / "debate_pump.jsonl"))
)
SUPERVISOR_LOG_PATH = Path(
    os.environ.get(
        "DEBATE_PUMP_SUPERVISOR_LOG",
        str(MEMORY_DIR / "debate_pump_supervisor.jsonl"),
    )
)
SUPERVISOR_MUTEX_NAME = os.environ.get(
    "DEBATE_PUMP_SUPERVISOR_MUTEX",
    r"Local\SqliteMemoryDebatePumpSupervisorV1",
)
HEARTBEAT_FRESH_SECONDS = float(
    os.environ.get("DEBATE_PUMP_SUPERVISOR_HEARTBEAT_FRESH_SECONDS", "90")
)
CHECK_INTERVAL_SECONDS = float(
    os.environ.get("DEBATE_PUMP_SUPERVISOR_CHECK_INTERVAL_SECONDS", "5")
)
RESTART_BACKOFF_SECONDS = float(
    os.environ.get("DEBATE_PUMP_SUPERVISOR_RESTART_BACKOFF_SECONDS", "5")
)

STOP = False
_MUTEX_HANDLE: int | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _log(event: str, **fields: Any) -> None:
    try:
        SUPERVISOR_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with SUPERVISOR_LOG_PATH.open("a", encoding="utf-8") as stream:
            stream.write(
                json.dumps(
                    {"ts": _now(), "event": event, **fields},
                    ensure_ascii=False,
                    default=repr,
                )
                + "\n"
            )
    except Exception:
        # Diagnostics must never prevent a restart attempt.
        pass


def _read_heartbeat() -> dict[str, Any] | None:
    try:
        value = json.loads(HEARTBEAT_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None
    return value if isinstance(value, dict) else None


def _heartbeat_age(heartbeat: dict[str, Any]) -> float | None:
    raw = str(heartbeat.get("ts") or "")
    try:
        stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - stamp).total_seconds())


def _process_identity(heartbeat: dict[str, Any]) -> tuple[bool, bool]:
    """Return (process_exists_and_matches, process_is_fresh_enough)."""
    try:
        import psutil

        process = psutil.Process(int(heartbeat.get("pid") or 0))
        if process.status() == psutil.STATUS_ZOMBIE:
            return False, False
        expected_create_time = heartbeat.get("create_time")
        if expected_create_time is not None:
            if abs(process.create_time() - float(expected_create_time)) > 2.0:
                return False, False
        command_line = " ".join(process.cmdline() or []).lower()
        if "debate_pump" not in command_line:
            return False, False
        age = _heartbeat_age(heartbeat)
        return True, age is not None and age <= HEARTBEAT_FRESH_SECONDS
    except Exception:
        return False, False


def _pump_state() -> str:
    heartbeat = _read_heartbeat()
    if heartbeat is None:
        return "stopped"
    exists, fresh = _process_identity(heartbeat)
    if not exists:
        return "stale"
    return "running" if fresh else "stale"


def _pump_occupied() -> bool:
    heartbeat = _read_heartbeat()
    if heartbeat is None:
        return False
    exists, _fresh = _process_identity(heartbeat)
    return exists


def _last_pump_event() -> str:
    """Read only the tail; a clean pump_stop means an operator stop."""
    try:
        with PUMP_LOG_PATH.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            stream.seek(max(0, stream.tell() - 64 * 1024), os.SEEK_SET)
            tail = stream.read().decode("utf-8", errors="replace")
    except Exception:
        return ""
    for line in reversed(tail.splitlines()):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        event = str(value.get("event") or "")
        if event:
            return event
    return ""


def _clean_stop_observed(supervisor_started_at: float) -> bool:
    """Recognize a clean stop written after this supervisor started."""
    try:
        with PUMP_LOG_PATH.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            stream.seek(max(0, stream.tell() - 64 * 1024), os.SEEK_SET)
            tail = stream.read().decode("utf-8", errors="replace")
    except Exception:
        return False
    for line in reversed(tail.splitlines()):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        event = str(value.get("event") or "")
        if not event:
            continue
        if event != "pump_stop":
            return False
        raw_ts = str(value.get("ts") or "")
        try:
            stamp = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
            return stamp.timestamp() >= supervisor_started_at
        except ValueError:
            return False
    return False


def _acquire_mutex() -> bool:
    global _MUTEX_HANDLE
    if os.name != "nt":
        return True
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        handle = kernel32.CreateMutexW(None, False, SUPERVISOR_MUTEX_NAME)
        if not handle:
            return False
        kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        kernel32.WaitForSingleObject.restype = ctypes.c_uint
        result = kernel32.WaitForSingleObject(ctypes.c_void_p(handle), 0)
        if result not in (0x00000000, 0x00000080):
            kernel32.CloseHandle(ctypes.c_void_p(handle))
            return False
        _MUTEX_HANDLE = int(handle)
        return True
    except Exception as exc:
        _log("supervisor_mutex_failed", error=repr(exc))
        return False


def _release_mutex() -> None:
    global _MUTEX_HANDLE
    if _MUTEX_HANDLE and os.name == "nt":
        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            handle = ctypes.c_void_p(_MUTEX_HANDLE)
            kernel32.ReleaseMutex(handle)
            kernel32.CloseHandle(handle)
        except Exception:
            pass
    _MUTEX_HANDLE = None


def _spawn_pump() -> subprocess.Popen[Any]:
    flags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0)) | int(
        getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    )
    return subprocess.Popen(
        [str(_pythonw()), str(PUMP_SCRIPT), *PUMP_ARGS],
        cwd=str(ROOT),
        creationflags=flags,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _handle_signal(_signum: int, _frame: Any) -> None:
    global STOP
    STOP = True


def _wait(seconds: float) -> None:
    deadline = time.monotonic() + max(0.0, seconds)
    while not STOP:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(0.5, remaining))


def _check_payload() -> dict[str, Any]:
    heartbeat = _read_heartbeat()
    return {
        "supervisor": "debate_pump_supervisor",
        "supervisor_mutex": SUPERVISOR_MUTEX_NAME,
        "pump_state": _pump_state(),
        "pump_occupied": _pump_occupied(),
        "heartbeat": heartbeat,
        "last_pump_event": _last_pump_event(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="User-level debate pump supervisor")
    parser.add_argument(
        "--check",
        action="store_true",
        help="print supervisor/pump state without starting or changing anything",
    )
    args = parser.parse_args(argv)

    if args.check:
        payload = _check_payload()
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0 if payload["pump_state"] == "running" else 1

    if not _acquire_mutex():
        _log("supervisor_duplicate_exit")
        return 0

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    supervisor_started_at = time.time()
    _log("supervisor_start", pid=os.getpid())

    try:
        while not STOP:
            if _pump_occupied():
                _wait(CHECK_INTERVAL_SECONDS)
                continue

            if _clean_stop_observed(supervisor_started_at):
                _log("supervisor_clean_exit", reason="observed_operator_stop")
                return 0

            previous_event = _last_pump_event()
            try:
                pump = _spawn_pump()
                _log("pump_spawn", pid=pump.pid, previous_event=previous_event)
            except Exception as exc:
                _log("pump_spawn_failed", error=repr(exc))
                _wait(RESTART_BACKOFF_SECONDS)
                continue

            while pump.poll() is None and not STOP:
                _wait(CHECK_INTERVAL_SECONDS)

            if STOP:
                return 0

            return_code = pump.poll()
            event = _last_pump_event()
            _log("pump_exit", pid=pump.pid, return_code=return_code, event=event)

            # debate_ops stop sends the pump's named stop event. The pump
            # records pump_stop before removing its heartbeat; do not undo a
            # deliberate operator stop by immediately restarting it.
            if return_code == 0 and event == "pump_stop":
                _log("supervisor_clean_exit")
                return 0

            _wait(RESTART_BACKOFF_SECONDS)
    finally:
        _log("supervisor_stop", pid=os.getpid())
        _release_mutex()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
