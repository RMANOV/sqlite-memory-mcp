"""B3: the log bound must survive restarts, not just hold within one process.

``RotatingFileHandler`` bounds one file family. Making the sink per-process
(the fix for a shared file whose rotations ate other processes' records) made
the *number* of families unbounded: every restart of every server mints a new
PID, and nothing ever removed the old lineage. On a months-old install that is
the same unbounded-writer-beside-the-database hazard the bound was added to
remove, only reached by accumulation rather than by growth.

These tests pin the directory-level budget and, more importantly, the two ways
it could be worse than the problem: deleting a log a live server is writing, and
raising out of logger setup.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys

import pytest

import db_utils
from db_utils import setup_logger

BUDGET = 4096


@pytest.fixture()
def log_dir(tmp_path, monkeypatch):
    d = tmp_path / "logs"
    d.mkdir()
    monkeypatch.setenv("SQLITE_MEMORY_LOG_DIR", str(d))
    monkeypatch.setenv("SQLITE_MEMORY_LOG_TOTAL_BYTES", str(BUDGET))
    monkeypatch.setenv("SQLITE_MEMORY_LOG_PER_PROCESS", "1")
    yield d


def _dead_pid() -> int:
    """A PID that has certainly exited — the kernel confirms it for us."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def _plant(d, name: str, size: int) -> "os.PathLike":
    p = d / name
    p.write_bytes(b"x" * size)
    return p


def _fresh_logger(name: str, log_file: str = "server.log") -> logging.Logger:
    logger = logging.getLogger(name)
    for h in list(logger.handlers):
        logger.removeHandler(h)
        h.close()
    return setup_logger(name, log_file)


def test_dead_processes_logs_are_reclaimed_when_the_directory_is_over_budget(log_dir):
    dead = _dead_pid()
    _plant(log_dir, f"server.{dead}.log", BUDGET)
    _plant(log_dir, f"server.{dead}.log.1", BUDGET)

    _fresh_logger("budget-test-reclaim")

    assert not (log_dir / f"server.{dead}.log").exists(), (
        "a dead server's log family survived: the directory bound only ever "
        "applied within one process"
    )
    assert not (log_dir / f"server.{dead}.log.1").exists(), (
        "rotation backups were left behind, so the family was only half reclaimed"
    )


def test_budget_spans_different_named_logger_families(log_dir):
    """The budget is for the directory, not separately for every log stem."""
    dead = _dead_pid()
    victim = _plant(log_dir, f"task_server.{dead}.log", BUDGET * 2)

    # Trigger the sweep through a *different* named logger. The old
    # stem-scoped implementation inspected only ``server.*`` and left the
    # task-server family invisible, allowing every one of the eleven server
    # names to accumulate a separate 64 MiB allowance.
    _fresh_logger("budget-test-cross-stem", "server.log")

    assert not victim.exists(), (
        "a dead task_server family survived a server logger sweep — the "
        "supposed directory budget is still one budget per log name"
    )


def test_a_live_processes_log_is_never_deleted(log_dir):
    # The PID of a process that is definitely running: this one's parent, or
    # failing that this one. Both are alive for the whole test.
    live = os.getppid()
    victim = _plant(log_dir, f"server.{live}.log", BUDGET * 4)

    _fresh_logger("budget-test-live")

    assert victim.exists(), (
        "the sweep deleted a log belonging to a running process — worse than "
        "the unbounded growth it replaces, because those records are lost "
        "silently through a still-open fd"
    )


def test_this_processes_own_log_is_never_deleted(log_dir):
    mine = _plant(log_dir, f"server.{os.getpid()}.log", BUDGET * 4)

    _fresh_logger("budget-test-self")

    assert mine.exists(), "the sweep deleted the file it had just opened"


def test_an_under_budget_directory_is_left_completely_alone(log_dir):
    dead = _dead_pid()
    keeper = _plant(log_dir, f"server.{dead}.log", 16)

    _fresh_logger("budget-test-under")

    assert keeper.exists(), (
        "history was discarded while the directory was within budget — the "
        "sweep is a bound, not a retention policy"
    )


def test_the_oldest_dead_family_goes_first(log_dir):
    old_pid, new_pid = _dead_pid(), _dead_pid()
    old = _plant(log_dir, f"server.{old_pid}.log", BUDGET)
    new = _plant(log_dir, f"server.{new_pid}.log", BUDGET)
    os.utime(old, (1_600_000_000, 1_600_000_000))
    os.utime(new, (1_700_000_000, 1_700_000_000))

    _fresh_logger("budget-test-order")

    assert not old.exists() and new.exists(), (
        "the sweep discarded the newest diagnostics and kept the stalest: "
        f"old={old.exists()} new={new.exists()}"
    )


def test_unrelated_files_are_out_of_scope(log_dir):
    dead = _dead_pid()
    _plant(log_dir, f"server.{dead}.log", BUDGET * 2)
    bystanders = [
        _plant(log_dir, "task_server.log", 32),      # a different family
        _plant(log_dir, "memory_events.json", 32),   # not a log at all
        _plant(log_dir, "server.log", 32),           # the shared-sink legacy name
    ]

    _fresh_logger("budget-test-scope")

    for p in bystanders:
        assert p.exists(), f"the sweep reached outside its own family: {p.name}"


def test_a_broken_directory_does_not_take_the_logger_down(log_dir, monkeypatch):
    """Logger setup runs at import in nine modules; raising here stops a server."""
    def exploding_iterdir(self):
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(db_utils.Path, "iterdir", exploding_iterdir)

    logger = _fresh_logger("budget-test-resilient")

    assert logger.handlers, "logger setup failed because the sweep did"
    logger.info("still works")


def test_the_sweep_is_opt_out(log_dir, monkeypatch):
    monkeypatch.setenv("SQLITE_MEMORY_LOG_TOTAL_BYTES", "0")
    dead = _dead_pid()
    keeper = _plant(log_dir, f"server.{dead}.log", BUDGET * 10)

    _fresh_logger("budget-test-optout")

    assert keeper.exists(), "SQLITE_MEMORY_LOG_TOTAL_BYTES=0 must disable the sweep"
