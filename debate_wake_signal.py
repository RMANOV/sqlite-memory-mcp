#!/usr/bin/env python3
"""Windows named-event wake signaling for the debate pump.

Latency hint only: the durable outbox is the addressed message + recipient
rows + wake/claim state in SQLite. A lost or early-consumed event is always
recovered by the pump's bounded timeout sweep and startup backlog sweep.

Contract:
- ``signal_wake()`` is called strictly AFTER a successful DB commit and must
  never raise into the caller (a failed signal must not fail the post).
- The pump waits with ``wait_for_wake_or_stop()``; ``signal_stop()`` asks a
  resident pump to exit gracefully without killing in-flight workers.
- Auto-reset events; single resident waiter. A SetEvent that fires while the
  pump is scanning leaves the event signaled, so the next wait returns
  immediately — at worst one extra scan, never a lost trigger.

Non-Windows platforms: every call is a cheap no-op ("unsupported").
"""

from __future__ import annotations

import os
import sys


def _wake_event_name() -> str:
    """Resolved per call so tests can isolate themselves from a live pump
    sharing the default name (auto-reset events release only one waiter)."""
    return os.environ.get("DEBATE_WAKE_EVENT_NAME", r"Local\SqliteMemoryDebateWakeV1")


def _stop_event_name() -> str:
    return os.environ.get(
        "DEBATE_PUMP_STOP_EVENT_NAME", r"Local\SqliteMemoryDebatePumpStopV1"
    )


_WAIT_OBJECT_0 = 0x00000000
_WAIT_ABANDONED_0 = 0x00000080
_WAIT_TIMEOUT = 0x00000102
_HANDLES: dict[str, int] = {}


def is_supported() -> bool:
    return sys.platform == "win32"


def _kernel32():
    import ctypes

    return ctypes.windll.kernel32  # type: ignore[attr-defined]


def _open_or_create(name: str) -> int | None:
    """CreateEventW opens the existing named event or creates it (auto-reset)."""
    if not is_supported():
        return None
    handle = _HANDLES.get(name)
    if handle:
        return handle
    try:
        handle = _kernel32().CreateEventW(None, False, False, name)
    except Exception:
        return None
    if not handle:
        return None
    _HANDLES[name] = handle
    return handle


def _signal(name: str) -> bool:
    handle = _open_or_create(name)
    if not handle:
        return False
    try:
        return bool(_kernel32().SetEvent(handle))
    except Exception:
        return False


def signal_wake() -> bool:
    """Post-commit latency hint. Never raises."""
    return _signal(_wake_event_name())


def signal_stop() -> bool:
    """Ask the resident pump to exit gracefully. Never raises."""
    return _signal(_stop_event_name())


def wait_for_wake_or_stop(timeout_seconds: float) -> str:
    """Block until wake/stop event or timeout.

    Returns "wake", "stop", "timeout" or "unsupported". Stop has priority
    when both are signaled (listed first in WaitForMultipleObjects).
    """
    if not is_supported():
        return "unsupported"
    stop_handle = _open_or_create(_stop_event_name())
    wake_handle = _open_or_create(_wake_event_name())
    if not stop_handle or not wake_handle:
        return "unsupported"
    import ctypes

    handles = (ctypes.c_void_p * 2)(stop_handle, wake_handle)
    timeout_ms = max(0, int(timeout_seconds * 1000))
    try:
        result = _kernel32().WaitForMultipleObjects(2, handles, False, timeout_ms)
    except Exception:
        return "unsupported"
    if result == _WAIT_OBJECT_0:
        return "stop"
    if result == _WAIT_OBJECT_0 + 1:
        return "wake"
    if result == _WAIT_TIMEOUT:
        return "timeout"
    return "timeout"


def close_handles() -> None:
    """Release cached handles (tests / process teardown)."""
    if not is_supported():
        _HANDLES.clear()
        return
    for handle in _HANDLES.values():
        try:
            _kernel32().CloseHandle(handle)
        except Exception:
            pass
    _HANDLES.clear()


# ── Pump singleton mutex ─────────────────────────────────────────────────

_SINGLETON_HANDLE: int | None = None


def _singleton_mutex_name() -> str:
    return os.environ.get(
        "DEBATE_PUMP_SINGLETON_MUTEX", r"Local\SqliteMemoryDebatePumpSingletonV1"
    )


def acquire_pump_singleton() -> bool:
    """Atomic OS-level pump singleton (advocate BLOCK high-risk #1).

    The heartbeat-file guard is advisory (read-check-act race); a named
    mutex is atomic. The handle is held for the process lifetime — the OS
    releases it on ANY exit path, so a crashed pump never wedges the slot.
    Returns True when this process owns the singleton. Non-Windows: True
    (systemd already enforces single instance there).
    """
    global _SINGLETON_HANDLE
    if not is_supported():
        return True
    if _SINGLETON_HANDLE:
        return True
    import ctypes

    try:
        # Dedicated use_last_error binding: GetLastError via plain
        # ctypes.windll is unreliable (interpreter may issue intervening
        # Win32 calls between CreateMutexW and the read).
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        handle = kernel32.CreateMutexW(None, False, _singleton_mutex_name())
        if not handle:
            return False
        kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        kernel32.WaitForSingleObject.restype = ctypes.c_uint
        wait_result = kernel32.WaitForSingleObject(ctypes.c_void_p(handle), 0)
        if wait_result not in (_WAIT_OBJECT_0, _WAIT_ABANDONED_0):
            kernel32.CloseHandle(ctypes.c_void_p(handle))
            return False
        _SINGLETON_HANDLE = handle
        return True
    except Exception:
        return False


def release_pump_singleton() -> None:
    """Explicit release (tests). Production relies on process exit."""
    global _SINGLETON_HANDLE
    if _SINGLETON_HANDLE and is_supported():
        import ctypes

        try:
            handle = ctypes.c_void_p(_SINGLETON_HANDLE)
            _kernel32().ReleaseMutex(handle)
            _kernel32().CloseHandle(handle)
        except Exception:
            pass
    _SINGLETON_HANDLE = None
