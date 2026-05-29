"""E1: sequence-marker auto-recovery in ensure_bridge_repo_ready.

Spec: auto-abort a left-behind rebase-merge / rebase-apply / MERGE_HEAD ONLY when
the working tree is clean or exclusively generated bridge artifacts; any
non-generated conflicted/dirty/staged/untracked path -> return blocked (preserve
user work). CHERRY_PICK_HEAD / REVERT_HEAD stay manual. Bounded git_run(timeout=5).
bridge_doctor surfaces the last attempt via get_last_bridge_auto_abort() (Option A,
contract-preserving — ensure_bridge_repo_ready keeps its (bool, str|None) return).
"""

import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import db_utils  # noqa: E402


def _cp(args, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(list(args), returncode, stdout, stderr)


# ── helper: _bridge_working_tree_safe_for_abort ──────────────────────────────


def test_safe_for_abort_clean_tree(monkeypatch):
    monkeypatch.setattr(db_utils, "git_run", lambda r, *a, **k: _cp(a, stdout=""))
    safe, reason = db_utils._bridge_working_tree_safe_for_abort("bridge")
    assert safe is True and reason is None


def test_safe_for_abort_generated_only(monkeypatch):
    monkeypatch.setattr(
        db_utils, "git_run", lambda r, *a, **k: _cp(a, stdout=" M shared.json\n")
    )
    safe, reason = db_utils._bridge_working_tree_safe_for_abort("bridge")
    assert safe is True and reason is None


def test_safe_for_abort_non_generated_unsafe(monkeypatch):
    monkeypatch.setattr(
        db_utils, "git_run", lambda r, *a, **k: _cp(a, stdout=" M user_code.py\n")
    )
    safe, reason = db_utils._bridge_working_tree_safe_for_abort("bridge")
    assert safe is False
    assert "user_code.py" in reason


# ── helper: _bridge_auto_abort_recover ───────────────────────────────────────


def test_auto_abort_recover_builds_commands_and_records(monkeypatch):
    seen = []

    def fake(repo, *args, timeout=30):
        seen.append((args, timeout))
        return _cp(args, returncode=0, stdout="ok")

    monkeypatch.setattr(db_utils, "git_run", fake)
    rec = db_utils._bridge_auto_abort_recover("bridge", ["rebase-merge", "MERGE_HEAD"])
    cmds = [a for a, _ in seen]
    assert ("rebase", "--abort") in cmds and ("merge", "--abort") in cmds
    assert all(t == 5 for _, t in seen)  # bounded timeout=5
    assert all(a["ok"] for a in rec["aborts"])


def test_auto_abort_recover_records_timeout(monkeypatch):
    def fake(repo, *args, timeout=30):
        raise subprocess.TimeoutExpired(cmd="git", timeout=timeout)

    monkeypatch.setattr(db_utils, "git_run", fake)
    rec = db_utils._bridge_auto_abort_recover("bridge", ["MERGE_HEAD"])
    assert rec["aborts"][0]["ok"] is False
    assert "timeout" in rec["aborts"][0]["detail"]


# ── integration: ensure_bridge_repo_ready ────────────────────────────────────


def test_rebase_merge_clean_tree_auto_recovers(tmp_path, monkeypatch):
    bridge = tmp_path / "bridge"
    seq = bridge / ".git" / "rebase-merge"
    seq.mkdir(parents=True)

    def fake(repo, *args, timeout=30):
        if args == ("status", "--porcelain"):
            return _cp(args, stdout="")  # clean
        if args == ("rebase", "--abort"):
            shutil.rmtree(seq)  # simulate the real abort clearing the marker
            return _cp(args, returncode=0)
        if args == ("rev-parse", "--abbrev-ref", "HEAD"):
            return _cp(args, stdout="main\n")
        raise AssertionError(f"Unexpected git call: {args}")

    monkeypatch.setattr(db_utils, "git_run", fake)
    db_utils._last_bridge_auto_abort = None
    ok, msg = db_utils.ensure_bridge_repo_ready(str(bridge))

    assert ok is True and msg is None
    rec = db_utils.get_last_bridge_auto_abort()
    assert rec and rec["aborts"][0]["cmd"] == "rebase --abort" and rec["aborts"][0]["ok"]


def test_merge_head_auto_recovers(tmp_path, monkeypatch):
    bridge = tmp_path / "bridge"
    (bridge / ".git").mkdir(parents=True)
    merge_head = bridge / ".git" / "MERGE_HEAD"
    merge_head.write_text("deadbeef\n")

    def fake(repo, *args, timeout=30):
        if args == ("status", "--porcelain"):
            return _cp(args, stdout=" M shared.json\n")  # generated-only = safe
        if args == ("merge", "--abort"):
            merge_head.unlink()
            return _cp(args, returncode=0)
        if args == ("rev-parse", "--abbrev-ref", "HEAD"):
            return _cp(args, stdout="main\n")
        # post-recovery readiness flow rebuilds generated artifacts (checkout/clean);
        # tolerate those — E1's contract here is: abort happened + sequence cleared.
        return _cp(args, returncode=0)

    monkeypatch.setattr(db_utils, "git_run", fake)
    db_utils._last_bridge_auto_abort = None
    ok, msg = db_utils.ensure_bridge_repo_ready(str(bridge))
    # MERGE_HEAD auto-abort fired + sequence cleared (not blocked on the marker).
    assert db_utils.get_last_bridge_auto_abort()["aborts"][0]["cmd"] == "merge --abort"
    assert db_utils._bridge_git_path(str(bridge), "MERGE_HEAD").exists() is False


def test_cherry_pick_stays_manual(tmp_path, monkeypatch):
    bridge = tmp_path / "bridge"
    (bridge / ".git").mkdir(parents=True)
    (bridge / ".git" / "CHERRY_PICK_HEAD").write_text("deadbeef\n")
    calls = []

    def fake(repo, *args, timeout=30):
        calls.append(args)
        raise AssertionError(f"Unexpected git call: {args}")

    monkeypatch.setattr(db_utils, "git_run", fake)
    ok, msg = db_utils.ensure_bridge_repo_ready(str(bridge))
    assert ok is False
    assert "CHERRY_PICK_HEAD" in msg and "manual recovery required" in msg
    assert calls == []  # no auto-abort, no git mutation


def test_rebase_abort_failure_returns_structured_blocked(tmp_path, monkeypatch):
    bridge = tmp_path / "bridge"
    seq = bridge / ".git" / "rebase-merge"
    seq.mkdir(parents=True)  # abort "fails": marker persists (fake never removes it)

    def fake(repo, *args, timeout=30):
        if args == ("status", "--porcelain"):
            return _cp(args, stdout="")
        if args == ("rebase", "--abort"):
            return _cp(args, returncode=1, stderr="fatal: no rebase in progress")
        raise AssertionError(f"Unexpected git call: {args}")

    monkeypatch.setattr(db_utils, "git_run", fake)
    ok, msg = db_utils.ensure_bridge_repo_ready(str(bridge))
    assert ok is False
    assert "auto-recovery failed" in msg
    assert "rebase --abort=fail" in msg
    assert "blocked_by_repo_state preserved" in msg
