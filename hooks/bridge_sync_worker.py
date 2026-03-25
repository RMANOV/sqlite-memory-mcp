#!/usr/bin/env python3
"""Background worker: runs bridge_pull + bridge_push with intelligent error handling.

Spawned by bridge_auto_sync.py hook. Runs detached from Claude session.
Pull imports remote tasks into local DB, then push exports merged state.
Handles: remote-ahead (auto-rebase), conflicts, network errors.
Writes notifications for the hook to pick up on next tool call.
"""

import json
import os
import subprocess
import sys
import time
import asyncio
import inspect
import logging

LOCK_FILE = os.path.expanduser("~/.claude/memory/.bridge_sync.lock")
LAST_SYNC = os.path.expanduser("~/.claude/memory/.bridge_last_sync")
DIRTY_FLAG = os.path.expanduser("~/.claude/memory/.bridge_dirty")
NOTIFY_FILE = os.path.expanduser("~/.claude/memory/.bridge_notification")
LOG_FILE = os.path.expanduser("~/.claude/memory/bridge_sync.log")
SERVER_DIR = os.path.expanduser("~/.claude/mcp_servers/sqlite_memory")
BRIDGE_REPO = os.path.expanduser("~/.claude/memory/bridge")
FAIL_COUNTER = os.path.expanduser("~/.claude/memory/.bridge_fail_count")
MAX_FAILURES = 2

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)


def notify(level, message):
    """Write notification for hook to pick up."""
    try:
        payload = {
            "level": level,  # info, warning, error
            "message": message,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        with open(NOTIFY_FILE, "w") as f:
            json.dump(payload, f, ensure_ascii=False)
        logging.log(
            {"info": logging.INFO, "warning": logging.WARNING, "error": logging.ERROR}[
                level
            ],
            message,
        )
    except OSError:
        pass


def git_run(*args, cwd=None):
    """Run git command, return (success, output)."""
    try:
        kwargs = dict(
            cwd=cwd or BRIDGE_REPO,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        r = subprocess.run(["git"] + list(args), **kwargs)
        return r.returncode == 0, r.stdout.strip() + r.stderr.strip()
    except (subprocess.TimeoutExpired, OSError) as e:
        return False, str(e)


def _pid_alive(pid):
    """Check if process is alive (Windows-safe).

    On Windows, os.kill(pid, 0) calls TerminateProcess — it KILLS instead of probing.
    Use OpenProcess with PROCESS_QUERY_LIMITED_INFORMATION instead.
    """
    if sys.platform == "win32":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


def fix_remote_ahead():
    """Handle remote-ahead: pull --rebase, then push again."""
    ok, out = git_run("pull", "--rebase", "--autostash", "origin", "main")
    if not ok:
        if "CONFLICT" in out:
            git_run("rebase", "--abort")
            notify(
                "error",
                f"BRIDGE CONFLICT: auto-rebase failed. Manual resolve needed.\n{out[:200]}",
            )
            return False
        notify("warning", f"BRIDGE: pull --rebase failed: {out[:200]}")
        return False

    ok, out = git_run("push", "origin", "main")
    if ok:
        notify("info", "BRIDGE: auto-resolved remote-ahead (rebase + push)")
        return True
    notify("error", f"BRIDGE: push failed after rebase: {out[:200]}")
    return False


def preflight_git_check():
    """Ensure bridge repo is in a clean, syncable state."""
    # 1. Detached HEAD → checkout main
    ok, branch = git_run("rev-parse", "--abbrev-ref", "HEAD")
    if ok and branch.strip() == "HEAD":
        git_run("checkout", "main")
        logging.warning("Preflight: fixed detached HEAD → main")

    # 2. Single git status check for conflicts AND dirty state
    ok, status = git_run("status", "--porcelain")
    if not ok:
        return

    lines = [ln for ln in status.strip().split("\n") if ln]
    has_conflicts = any(ln[:2] in ("UU", "AA", "DD", "DU", "UD") for ln in lines)

    if has_conflicts:
        git_run("rebase", "--abort")
        git_run("merge", "--abort")
        git_run("reset", "--hard", "HEAD")
        logging.warning("Preflight: cleared merge/rebase conflicts")
        # Re-check only after conflict resolution
        ok, status = git_run("status", "--porcelain")
        if not ok:
            return
        lines = [ln for ln in status.strip().split("\n") if ln]

    # 3. Dirty working tree → discard (all files in bridge repo are generated from DB)
    if lines:
        git_run("checkout", "--", ".")
        logging.info("Preflight: discarded dirty working tree")


def acquire_lock():
    """File lock with PID liveness check. Max 1 retry after clearing stale lock."""
    for _ in range(2):
        try:
            fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return True
        except FileExistsError:
            try:
                with open(LOCK_FILE) as f:
                    pid = int(f.read().strip())
                if _pid_alive(pid):
                    # Process alive — check age as fallback (180s max)
                    age = time.time() - os.path.getmtime(LOCK_FILE)
                    if age > 180:
                        os.unlink(LOCK_FILE)
                        continue  # retry
                    return False  # process alive and lock fresh
                else:
                    os.unlink(LOCK_FILE)
                    continue  # retry — PID dead
            except (ValueError, OSError):
                try:
                    os.unlink(LOCK_FILE)
                except OSError:
                    return False
                continue
    return False


def release_lock():
    try:
        os.unlink(LOCK_FILE)
    except OSError:
        pass


def _read_fail_count():
    """Read failure counter from file."""
    try:
        if os.path.exists(FAIL_COUNTER):
            with open(FAIL_COUNTER) as f:
                return int(f.read().strip())
    except (OSError, ValueError):
        pass
    return 0


def _write_fail_count(count):
    """Write failure counter to file."""
    try:
        with open(FAIL_COUNTER, "w") as f:
            f.write(str(count))
    except OSError:
        pass


def main(progress_callback=None):
    """Run bridge pull + push cycle.

    Args:
        progress_callback: Optional callable(pct: int, label: str) for UI progress.
    """

    def _progress(pct, label):
        if progress_callback:
            try:
                progress_callback(pct, label)
            except Exception:
                pass  # UI callback failure must not break sync

    # Failure counter — stop auto-sync after MAX_FAILURES consecutive failures
    fail_count = _read_fail_count()
    if fail_count >= MAX_FAILURES:
        logging.info(
            "Auto-sync disabled — %d consecutive failures. Manual sync needed.",
            fail_count,
        )
        return

    if not acquire_lock():
        logging.info("Skipped — another sync in progress")
        return

    try:
        # Pre-flight: ensure bridge git repo is in clean state
        _progress(2, "Preflight check...")
        preflight_git_check()

        sys.path.insert(0, SERVER_DIR)
        from bridge_server import bridge_pull, bridge_push

        # Step 1: Pull remote changes into local DB
        _progress(5, "git pull...")
        pull_fn = bridge_pull.fn
        if inspect.iscoroutinefunction(pull_fn):
            pull_result = asyncio.run(pull_fn())
        else:
            pull_result = pull_fn()
        logging.info("bridge_pull result: %s", pull_result)
        _progress(35, "Preparing push...")

        # Step 2: Push local (now merged) DB to remote
        fn = bridge_push.fn
        _progress(40, "Pushing...")

        if inspect.iscoroutinefunction(fn):
            result_str = asyncio.run(fn(tag="shared"))
        else:
            result_str = fn(tag="shared")

        logging.info("bridge_push result: %s", result_str)
        _progress(55, "Writing shared.js...")

        # Generate shared.js wrapper for file:// compatibility
        shared_json = os.path.join(BRIDGE_REPO, "shared.json")
        shared_js = os.path.join(BRIDGE_REPO, "shared.js")
        try:
            with open(shared_json, encoding="utf-8") as f:
                raw = f.read()
            with open(shared_js, "w", encoding="utf-8") as f:
                f.write("window.__BRIDGE_DATA__ = ")
                f.write(raw)
                f.write(";")
        except OSError as e:
            logging.warning("shared.js generation failed: %s", e)

        # Commit shared.js locally — no separate push (fix_remote_ahead carries it)
        git_run("add", "shared.js")
        ok, porcelain = git_run("status", "--porcelain")
        if ok and porcelain.strip():
            git_run("commit", "-m", "chore: update shared.js wrapper")

        # Parse result to check pushed_to_remote
        try:
            result = (
                json.loads(result_str) if isinstance(result_str, str) else result_str
            )
        except (json.JSONDecodeError, TypeError):
            result = {}

        pushed = (
            result.get("pushed_to_remote", False) if isinstance(result, dict) else False
        )
        tasks_count = result.get("tasks", 0) if isinstance(result, dict) else 0

        _progress(70, "Verifying push...")
        if not pushed:
            # Remote might be ahead — try auto-resolve
            logging.warning("pushed_to_remote=false, attempting auto-resolve")
            if fix_remote_ahead():
                pushed = True
            else:
                # Check if it was just "nothing to commit"
                ok, status = git_run("status", "--porcelain")
                if ok and not status:
                    # Working tree clean — maybe shared.json didn't change
                    ok, log = git_run("log", "--oneline", "origin/main..HEAD")
                    if ok and not log:
                        notify("info", f"BRIDGE: already in sync ({tasks_count} tasks)")
                        pushed = True

        _progress(90, "Finalizing...")
        if pushed:
            with open(LAST_SYNC, "w") as f:
                f.write(str(time.time()))
            try:
                os.unlink(DIRTY_FLAG)
            except OSError:
                pass
            _write_fail_count(0)
            notify("info", f"BRIDGE: synced {tasks_count} tasks OK")
        else:
            _write_fail_count(fail_count + 1)
            notify(
                "warning",
                f"BRIDGE: sync incomplete — {tasks_count} tasks exported but push failed. Check bridge_sync.log",
            )

    except Exception as e:
        _write_fail_count(fail_count + 1)
        notify("error", f"BRIDGE ERROR: {e}")
        logging.exception("bridge_push failed")
    finally:
        release_lock()


if __name__ == "__main__":
    main()
