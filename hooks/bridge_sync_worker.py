#!/usr/bin/env python3
"""Background worker: runs bridge_pull + bridge_push with intelligent error handling.

Spawned by bridge_auto_sync.py hook. Runs detached from Claude session.
Pull imports remote tasks into local DB, then push exports merged state.
Handles: remote-ahead (auto-rebase), conflicts, network errors.
Writes notifications for the hook to pick up on next tool call.

ARCHITECTURE NOTE — NOT a duplicate of ../bridge_sync_worker.py (authoritative sync engine):
  - THIS file: orchestration wrapper — locking, failure counter, notifications,
    calls bridge_server.bridge_pull / bridge_push (MCP tools) via asyncio
  - ../bridge_sync_worker.py: low-level sync engine — called by task_tray.py's Sync
    button, imports db_utils directly, no MCP server dependency

These two files serve different layers and must NOT be merged or replaced with
a thin delegate. Fix bugs in each independently; keep this comment updated.
bridge_auto_sync.py now prefers this repo copy and only falls back to the
legacy deployed copy under ~/.claude/hooks if needed.
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
MAX_FAILURES = 2  # warning threshold only; auto-sync keeps retrying
MAX_DRAIN_CYCLES = 3

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
    """Handle remote-ahead: pull --rebase, then push again.

    On conflict: fail closed. Recovery must be explicit and backed up.
    """
    ok, out = git_run("pull", "--rebase", "--autostash", "origin", "main")
    if not ok:
        if "CONFLICT" in out or "unmerged" in out:
            notify(
                "error",
                "BRIDGE: pull conflict; sync blocked pending explicit recovery",
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
    """Ensure bridge repo is safe to sync without discarding user-managed files."""
    if SERVER_DIR not in sys.path:
        sys.path.insert(0, SERVER_DIR)
    try:
        from db_utils import ensure_bridge_repo_ready
    except Exception as exc:
        msg = f"cannot load bridge preflight helper: {exc}"
        logging.warning("Preflight blocked: %s", msg)
        return False, msg

    ok, msg = ensure_bridge_repo_ready(BRIDGE_REPO)
    if ok:
        return True, None

    logging.warning("Preflight blocked: %s", msg)
    return False, msg


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


def _read_dirty_timestamp():
    """Return the latest known dirty timestamp, if any."""
    try:
        if os.path.exists(DIRTY_FLAG):
            with open(DIRTY_FLAG, encoding="utf-8") as f:
                return float(f.read().strip())
    except (OSError, ValueError):
        pass
    return None


def _call_tool(tool, **kwargs):
    fn = tool.fn
    if inspect.iscoroutinefunction(fn):
        return asyncio.run(fn(**kwargs))
    return fn(**kwargs)


def _parse_result(result):
    try:
        return json.loads(result) if isinstance(result, str) else result
    except (json.JSONDecodeError, TypeError):
        return {}


def _sync_block_message(result, default):
    if not isinstance(result, dict):
        return None
    blocked = (
        result.get("blocked_by_repo_state")
        or result.get("git_pull_failed")
        or result.get("blocked_by_merge_failure")
        or result.get("blocked_by_safety")
        or result.get("git_add_failed")
        or result.get("git_commit_failed")
    )
    if not blocked:
        return None
    return str(result.get("error") or result.get("message") or default)


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

    fail_count = _read_fail_count()
    if fail_count >= MAX_FAILURES:
        logging.warning(
            "Auto-sync saw %d consecutive failures; retrying instead of disabling.",
            fail_count,
        )

    if not acquire_lock():
        logging.info("Skipped — another sync in progress")
        return

    try:
        for cycle in range(1, MAX_DRAIN_CYCLES + 1):
            cycle_started_at = time.time()

            # Pre-flight: ensure bridge git repo is in clean state
            _progress(2, "Preflight check...")
            ok, msg = preflight_git_check()
            if not ok:
                notify("warning", f"BRIDGE: sync blocked — {msg}")
                return

            sys.path.insert(0, SERVER_DIR)
            from bridge_server import bridge_pull, bridge_push

            # Step 1: Pull remote changes into local DB
            _progress(5, "git pull...")
            pull_result_raw = _call_tool(bridge_pull)
            logging.info("bridge_pull result: %s", pull_result_raw)
            pull_result = _parse_result(pull_result_raw)
            block_msg = _sync_block_message(pull_result, "bridge pull blocked")
            if block_msg:
                notify("warning", f"BRIDGE: sync blocked — {block_msg}")
                return
            _progress(35, "Preparing push...")

            # Step 2: Push local (now merged) DB to remote
            _progress(40, "Pushing...")

            result_str = _call_tool(bridge_push, tag="shared")
            logging.info("bridge_push result: %s", result_str)

            # Parse result to check pushed_to_remote
            result = _parse_result(result_str)

            pushed = False
            tasks_count = result.get("tasks", 0) if isinstance(result, dict) else 0
            if isinstance(result, dict):
                pushed = bool(result.get("pushed_to_remote", False))
                block_msg = _sync_block_message(result, "bridge push blocked")
                if block_msg:
                    notify("warning", f"BRIDGE: sync blocked — {block_msg}")
                    return
                message = str(result.get("message", ""))
                if (
                    not pushed
                    and result.get("pushed") == 0
                    and message.startswith("No changes")
                ):
                    pushed = True

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
                            notify(
                                "info", f"BRIDGE: already in sync ({tasks_count} tasks)"
                            )
                            pushed = True

            _progress(90, "Finalizing...")
            if not pushed:
                fail_count += 1
                _write_fail_count(fail_count)
                notify(
                    "warning",
                    f"BRIDGE: sync incomplete — {tasks_count} tasks exported but push failed. Check bridge_sync.log",
                )
                break

            fail_count = 0
            _write_fail_count(0)
            dirty_after = _read_dirty_timestamp()
            if dirty_after is not None and dirty_after > cycle_started_at:
                logging.info(
                    "New writes arrived during sync; draining cycle %d/%d "
                    "(dirty %.3f > start %.3f)",
                    cycle,
                    MAX_DRAIN_CYCLES,
                    dirty_after,
                    cycle_started_at,
                )
                if cycle < MAX_DRAIN_CYCLES:
                    continue
                notify(
                    "warning",
                    "BRIDGE: new writes kept arriving during sync; pending changes remain dirty for the next trigger",
                )
                break

            with open(LAST_SYNC, "w") as f:
                f.write(str(time.time()))
            try:
                os.unlink(DIRTY_FLAG)
            except OSError:
                pass
            notify("info", f"BRIDGE: synced {tasks_count} tasks OK")
            break

    except Exception as e:
        _write_fail_count(fail_count + 1)
        notify("error", f"BRIDGE ERROR: {e}")
        logging.exception("bridge_push failed")
    finally:
        release_lock()


if __name__ == "__main__":
    main()
