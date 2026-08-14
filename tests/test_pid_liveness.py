"""``_pid_is_alive`` decides whether a log file may be deleted.

It used to ask with ``os.kill(pid, 0)``. On POSIX that is a probe. On Windows
CPython implements ``os.kill`` with ``TerminateProcess`` and does not
special-case signal 0, so the "probe" was an attempt to kill the process being
asked about; and because a gone PID raises a bare ``OSError`` there rather than
``ProcessLookupError``, every dead PID read as alive and the directory budget
never applied on Windows at all.

Both halves are pinned here: the answers must be right, and asking must be
harmless. The harmlessness test is only safe to run because the fix is in --
against the old implementation it would have killed its own fixture, which is
precisely why it is the regression guard that matters.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from db_utils import _pid_is_alive  # noqa: E402

IS_WINDOWS = sys.platform == "win32"
windows_only = pytest.mark.skipif(not IS_WINDOWS, reason="Windows-specific path")


def test_this_process_is_alive():
    assert _pid_is_alive(os.getpid()) is True


def test_a_finished_process_is_dead():
    """The whole point: without this the sweep can never reclaim anything."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    pid = proc.pid
    del proc  # release the handle, so the PID is not merely a zombie
    assert _pid_is_alive(pid) is False


def test_a_pid_that_cannot_exist_is_dead():
    """Out of range on every platform this runs on."""
    assert _pid_is_alive(999_999_999) is False


def test_a_nonsense_pid_is_never_deletable():
    """0 and negatives are not answerable, so they must resolve to 'alive'."""
    assert _pid_is_alive(0) is True
    assert _pid_is_alive(-1) is True


@windows_only
def test_an_inaccessible_process_counts_as_alive():
    """PID 4 is the Windows System process: it exists and we cannot open it.

    'Access denied' must never be read as 'gone' -- that is the direction that
    deletes a running server's log out from under its open handle.
    """
    assert _pid_is_alive(4) is True


def test_asking_does_not_terminate_the_process():
    """The defect, stated as a property.

    ``os.kill(pid, 0)`` on Windows calls ``TerminateProcess(handle, 0)``. This
    test asserts the check is a question, not an action.
    """
    victim = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        time.sleep(0.5)
        assert victim.poll() is None, "fixture process did not start"

        for _ in range(5):
            assert _pid_is_alive(victim.pid) is True

        time.sleep(0.5)
        assert victim.poll() is None, (
            "the liveness check TERMINATED the process it was asked about "
            f"(exit code {victim.poll()}) -- os.kill(pid, 0) is back"
        )
    finally:
        victim.kill()
        victim.wait()


@windows_only
def test_the_process_handle_is_not_leaked():
    """Called once per log file per server start; a leak here is unbounded."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.GetProcessHandleCount.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.GetProcessHandleCount.restype = wintypes.BOOL

    def handle_count() -> int:
        count = wintypes.DWORD()
        ok = kernel32.GetProcessHandleCount(
            kernel32.GetCurrentProcess(), ctypes.byref(count)
        )
        assert ok, "GetProcessHandleCount failed"
        return count.value

    me = os.getpid()
    for _ in range(50):  # warm up any one-off allocations first
        _pid_is_alive(me)

    before = handle_count()
    for _ in range(500):
        _pid_is_alive(me)
    after = handle_count()

    assert after - before < 50, (
        f"handle count grew {before} -> {after} over 500 calls: the process "
        "handle is not being closed"
    )
