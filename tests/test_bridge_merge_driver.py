"""Regression tests: tombstone-safe shared.json / index.json merge driver.

DEFENSE IN DEPTH for the recurring "resurrected deleted tasks" incident class
(e.g. 2026-05-08 "12 tasks resurrected"). The hot sync path reconciles at the DB
layer (merge_import_tasks); this driver is the second line of defense for any
EXTERNAL git pull/merge of a tombstone-bearing bridge file.

Six required regressions, each exercising the NEW driver code directly (not the
already-green DB-layer merge):
  (a) remote-only row import (row-union keeps a task present on only one side)
  (b) remote-only tombstone import (a tombstone present on only one side survives)
  (c) active-after-delete does NOT resurrect (tombstone-union wins even when the
      active side has a strictly newer status timestamp)
  (d) missing / clock-skew archived_at handled (no crash; tombstone still wins)
  (e) stuck "UU" unmerged state WITHOUT MERGE_HEAD is auto-healed for generated
      files, but blocked when a user-managed file is also unmerged
  (f) idempotent second registration / merge (no-op, stable output)

Plus driver-wiring tests: .gitattributes + git config are actually installed,
the driver fires through real ``git merge``, and it FAILS CLOSED (writes nothing)
on unparsable input.
"""

import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import bridge_merge_driver as bmd  # noqa: E402
from db_utils import TASK_HIDDEN_STATUSES, json_dumps, json_loads  # noqa: E402


# ── fixtures / helpers ───────────────────────────────────────────────────────


def _fts(updated_at, updated_by="m", updated_order=0):
    return {"updated_at": updated_at, "updated_by": updated_by, "updated_order": updated_order}


def _task(tid, *, status="active", title="T", tombstone=False, status_fts=None, **extra):
    t = {"id": tid, "status": status, "title": title}
    if tombstone:
        t["_tombstone"] = True
    fts = {}
    if status_fts is not None:
        fts["status"] = status_fts
    if fts:
        t["_field_ts"] = fts
    t.update(extra)
    return t


def _collection(*tasks, pushed_at="2026-06-01T00:00:00+00:00"):
    return {"version": 4, "pushed_at": pushed_at, "tasks": list(tasks)}


@pytest.fixture()
def git_bridge(tmp_path):
    """An initialized bridge git repo with the merge driver installed."""
    repo = tmp_path / "bridge"
    repo.mkdir()

    def git(*args, check=True):
        cp = subprocess.run(
            ["git", *args], cwd=str(repo), capture_output=True, text=True
        )
        if check and cp.returncode != 0:
            raise AssertionError(f"git {args} failed: {cp.stderr or cp.stdout}")
        return cp

    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    git("config", "commit.gpgsign", "false")
    bmd.ensure_bridge_merge_protection(str(repo))
    return repo, git


def _write(repo, name, obj):
    (repo / name).write_text(json_dumps(obj), encoding="utf-8")


def _read(repo, name):
    return json_loads((repo / name).read_text(encoding="utf-8"))


def _by_id(collection):
    return {t["id"]: t for t in collection["tasks"]}


# ── (a) remote-only row import ───────────────────────────────────────────────


def test_a_remote_only_row_import():
    ours = _collection(_task("local-only"))
    theirs = _collection(_task("remote-only"))
    merged = bmd.reconcile_task_collection(ours, theirs)
    ids = _by_id(merged)
    assert set(ids) == {"local-only", "remote-only"}  # row-union keeps both


def test_a_remote_only_row_import_via_git(git_bridge):
    repo, git = git_bridge
    _write(repo, "index.json", _collection(_task("base")))
    git("add", "index.json", ".gitattributes")
    git("commit", "-qm", "base")
    git("checkout", "-q", "-b", "remote")
    _write(repo, "index.json", _collection(_task("base"), _task("remote-only")))
    git("commit", "-qam", "remote adds a task")
    git("checkout", "-q", "main")
    _write(repo, "index.json", _collection(_task("base"), _task("local-only")))
    git("commit", "-qam", "local adds a task")
    git("merge", "remote", "-m", "merge")
    ids = set(_by_id(_read(repo, "index.json")))
    assert ids == {"base", "remote-only", "local-only"}


# ── (b) remote-only tombstone import ─────────────────────────────────────────


def test_b_remote_only_tombstone_import():
    ours = _collection(_task("active-1"))
    theirs = _collection(_task("deleted-elsewhere", status="archived", tombstone=True))
    merged = bmd.reconcile_task_collection(ours, theirs)
    ids = _by_id(merged)
    assert set(ids) == {"active-1", "deleted-elsewhere"}
    tomb = ids["deleted-elsewhere"]
    assert tomb.get("_tombstone") is True
    assert tomb["status"] in TASK_HIDDEN_STATUSES  # never resurrected on import


# ── (c) active-after-delete does NOT resurrect (tombstone-union wins) ─────────


def test_c_active_after_delete_does_not_resurrect():
    # ours = active with a STRICTLY NEWER status timestamp/order than theirs.
    ours = _collection(
        _task("t1", status="in_progress", status_fts=_fts("2026-06-09T00:00:00+00:00", "win", 99))
    )
    # theirs = an older tombstone.
    theirs = _collection(
        _task("t1", status="archived", tombstone=True, status_fts=_fts("2026-06-01T00:00:00+00:00", "fed", 1))
    )
    merged = bmd.reconcile_task_collection(ours, theirs)
    t = _by_id(merged)["t1"]
    assert t["status"] in TASK_HIDDEN_STATUSES, "RESURRECTION: tombstone must win"
    assert t.get("_tombstone") is True


def test_c_resurrect_blocked_via_git(git_bridge):
    repo, git = git_bridge
    _write(repo, "index.json", _collection(_task("t1", status="active",
            status_fts=_fts("2026-06-01T00:00:00+00:00", "base", 1))))
    git("add", "index.json", ".gitattributes")
    git("commit", "-qm", "base")
    git("checkout", "-q", "-b", "win")
    _write(repo, "index.json", _collection(_task("t1", status="in_progress",
            status_fts=_fts("2026-06-09T00:00:00+00:00", "win", 99))))
    git("commit", "-qam", "win keeps active, newer ts")
    git("checkout", "-q", "main")
    _write(repo, "index.json", _collection(_task("t1", status="archived", tombstone=True,
            status_fts=_fts("2026-06-02T00:00:00+00:00", "fed", 5))))
    git("commit", "-qam", "fed deletes")
    git("merge", "win", "-m", "merge")
    t = _by_id(_read(repo, "index.json"))["t1"]
    assert t["status"] in TASK_HIDDEN_STATUSES, "RESURRECTION through real git merge"
    assert t.get("_tombstone") is True


def test_c_tombstone_only_on_ours_side_also_wins():
    # symmetry: tombstone on OUR side, active (newer) on theirs.
    ours = _collection(
        _task("t1", status="archived", tombstone=True, status_fts=_fts("2026-06-02T00:00:00+00:00", "a", 2))
    )
    theirs = _collection(
        _task("t1", status="active", status_fts=_fts("2026-06-09T00:00:00+00:00", "b", 99))
    )
    t = _by_id(bmd.reconcile_task_collection(ours, theirs))["t1"]
    assert t["status"] in TASK_HIDDEN_STATUSES
    assert t.get("_tombstone") is True


def test_c_status_only_tombstone_no_flag_still_wins():
    # A legacy peer expresses deletion via status alone (no _tombstone flag).
    ours = _collection(_task("t1", status="active", status_fts=_fts("2026-06-09T00:00:00+00:00", "win", 99)))
    theirs = _collection(_task("t1", status="cancelled", status_fts=_fts("2026-06-01T00:00:00+00:00", "fed", 1)))
    t = _by_id(bmd.reconcile_task_collection(ours, theirs))["t1"]
    assert t["status"] in TASK_HIDDEN_STATUSES
    assert t.get("_tombstone") is True


# ── (d) missing / clock-skew archived_at handled ─────────────────────────────


def test_d_missing_field_ts_handled():
    # Tombstone with NO _field_ts at all, active side with rich metadata.
    ours = _collection(_task("t1", status="active", status_fts=_fts("2026-06-09T00:00:00+00:00", "win", 99)))
    theirs = _collection({"id": "t1", "status": "archived", "_tombstone": True})  # no _field_ts, no updated_at
    t = _by_id(bmd.reconcile_task_collection(ours, theirs))["t1"]
    assert t["status"] in TASK_HIDDEN_STATUSES
    assert t.get("_tombstone") is True


def test_d_clock_skew_future_active_still_loses_to_tombstone():
    # Active side carries an absurd FAR-FUTURE timestamp (clock skew). The
    # tombstone-union ignores timestamps entirely, so it cannot be defeated.
    ours = _collection(_task("t1", status="active", status_fts=_fts("2099-01-01T00:00:00+00:00", "win", 10**15)))
    theirs = _collection(_task("t1", status="archived", tombstone=True, status_fts=_fts("2026-06-01T00:00:00+00:00", "fed", 1)))
    t = _by_id(bmd.reconcile_task_collection(ours, theirs))["t1"]
    assert t["status"] in TASK_HIDDEN_STATUSES
    assert t.get("_tombstone") is True


def test_d_both_tombstones_missing_archived_at_no_crash():
    ours = _collection({"id": "t1", "status": "archived", "_tombstone": True})
    theirs = _collection({"id": "t1", "status": "cancelled", "_tombstone": True})
    merged = bmd.reconcile_task_collection(ours, theirs)  # must not raise
    t = _by_id(merged)["t1"]
    assert t["status"] in TASK_HIDDEN_STATUSES and t.get("_tombstone") is True


# ── (e) stuck UU without MERGE_HEAD ──────────────────────────────────────────


def _make_stuck_uu(git_bridge, conflict_name, *, valid_theirs=False):
    """Force a UU on ``conflict_name`` via a merge whose theirs side is corrupt
    (driver fails closed) — then abort the merge so MERGE_HEAD is gone but the
    working-tree conflict remains, simulating the stuck-UU-without-MERGE_HEAD
    class. Returns (repo, git)."""
    repo, git = git_bridge
    _write(repo, conflict_name, _collection(_task("t1")))
    git("add", conflict_name, ".gitattributes")
    git("commit", "-qm", "base")
    git("checkout", "-q", "-b", "other")
    if valid_theirs:
        _write(repo, conflict_name, _collection(_task("t1", status="archived", tombstone=True)))
    else:
        (repo / conflict_name).write_text("{not valid json", encoding="utf-8")
    git("commit", "-qam", "other changes file")
    git("checkout", "-q", "main")
    _write(repo, conflict_name, _collection(_task("t1", status="in_progress"), _task("t2")))
    git("commit", "-qam", "main changes file")
    return repo, git


def test_e_find_unmerged_classifies_generated_vs_user(git_bridge):
    # index.json is generated; force a UU there via corrupt theirs.
    repo, git = _make_stuck_uu(git_bridge, "index.json")
    git("merge", "other", "-m", "merge", check=False)  # driver fails closed -> UU
    # Drop MERGE_HEAD to simulate the "stuck UU without MERGE_HEAD" state, but
    # keep the conflicted index in place (reset --mixed keeps working tree).
    (repo / ".git" / "MERGE_HEAD").unlink(missing_ok=True)
    gen, user = bmd.find_unmerged_generated_paths(str(repo))
    assert "index.json" in gen and user == []


def test_e_auto_heal_generated_only(git_bridge):
    repo, git = _make_stuck_uu(git_bridge, "index.json", valid_theirs=False)
    git("merge", "other", "-m", "merge", check=False)
    (repo / ".git" / "MERGE_HEAD").unlink(missing_ok=True)
    rec = bmd.auto_heal_unmerged_generated(str(repo))
    assert rec["healed"] == ["index.json"]
    gen_after, user_after = bmd.find_unmerged_generated_paths(str(repo))
    assert gen_after == [] and user_after == []  # conflict cleared


def test_e_auto_heal_blocked_when_user_file_also_unmerged(git_bridge):
    # Build a merge that conflicts BOTH a generated file and a user-managed file.
    repo, git = git_bridge
    _write(repo, "index.json", _collection(_task("t1")))
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    git("add", "index.json", "README.md", ".gitattributes")
    git("commit", "-qm", "base")
    git("checkout", "-q", "-b", "other")
    (repo / "index.json").write_text("{bad json", encoding="utf-8")
    (repo / "README.md").write_text("other side\n", encoding="utf-8")
    git("commit", "-qam", "other")
    git("checkout", "-q", "main")
    _write(repo, "index.json", _collection(_task("t1"), _task("t2")))
    (repo / "README.md").write_text("main side\n", encoding="utf-8")
    git("commit", "-qam", "main")
    git("merge", "other", "-m", "merge", check=False)
    (repo / ".git" / "MERGE_HEAD").unlink(missing_ok=True)
    rec = bmd.auto_heal_unmerged_generated(str(repo))
    assert rec["healed"] == []
    assert rec["skipped"] and "user-managed" in rec["skipped"]
    # The user file must remain unmerged (we never touched it).
    gen, user = bmd.find_unmerged_generated_paths(str(repo))
    assert "README.md" in user


def test_e_ensure_ready_heals_real_corrupt_uu(tmp_path):
    import db_utils

    repo = tmp_path / "bridge"
    repo.mkdir()

    def git(*args, check=True):
        cp = subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True)
        if check and cp.returncode != 0:
            raise AssertionError(f"git {args}: {cp.stderr or cp.stdout}")
        return cp

    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    git("config", "commit.gpgsign", "false")
    bmd.ensure_bridge_merge_protection(str(repo))
    _write(repo, "index.json", _collection(_task("t1")))
    git("add", "index.json", ".gitattributes")
    git("commit", "-qm", "base")
    git("checkout", "-q", "-b", "other")
    (repo / "index.json").write_text("{bad", encoding="utf-8")
    git("commit", "-qam", "corrupt")
    git("checkout", "-q", "main")
    _write(repo, "index.json", _collection(_task("t1"), _task("t2")))
    git("commit", "-qam", "main new")
    git("merge", "other", "-m", "merge", check=False)  # UU (fail-closed)
    (repo / ".git" / "MERGE_HEAD").unlink(missing_ok=True)
    assert subprocess.run(["git", "status", "--porcelain"], cwd=str(repo),
                          capture_output=True, text=True).stdout.startswith("UU")
    ok, msg = db_utils.ensure_bridge_repo_ready(str(repo))
    assert ok is True, f"expected auto-heal, got blocked: {msg}"
    status = subprocess.run(["git", "status", "--porcelain"], cwd=str(repo),
                            capture_output=True, text=True).stdout
    assert "UU" not in status  # conflict cleared


# ── (f) idempotent second push / registration / merge ────────────────────────


def test_f_registration_idempotent(git_bridge):
    repo, git = git_bridge
    assert bmd.is_merge_driver_registered(str(repo))
    rec1 = bmd.ensure_bridge_merge_protection(str(repo))
    rec2 = bmd.ensure_bridge_merge_protection(str(repo))
    assert rec1["ok"] and rec2["ok"]
    # .gitattributes content stable across repeated installs (no duplication).
    text = (repo / ".gitattributes").read_text(encoding="utf-8")
    assert text.count(bmd._GITATTRIBUTES_MANAGED_BLOCK_HEADER) == 1
    assert text.count("shared.json merge=bridge-reconcile") == 1


def test_f_idempotent_merge_no_op():
    # Merging identical sides yields an equivalent collection (no churn, no resurrection).
    coll = _collection(
        _task("a", status="active"),
        _task("b", status="archived", tombstone=True),
    )
    merged = bmd.reconcile_task_collection(coll, coll)
    ids = _by_id(merged)
    assert set(ids) == {"a", "b"}
    assert ids["a"]["status"] == "active"
    assert ids["b"]["status"] in TASK_HIDDEN_STATUSES and ids["b"].get("_tombstone")


def test_f_second_git_merge_after_first_is_noop(git_bridge):
    repo, git = git_bridge
    _write(repo, "index.json", _collection(_task("t1")))
    git("add", "index.json", ".gitattributes")
    git("commit", "-qm", "base")
    git("checkout", "-q", "-b", "other")
    _write(repo, "index.json", _collection(_task("t1"), _task("t2", status="archived", tombstone=True)))
    git("commit", "-qam", "other")
    git("checkout", "-q", "main")
    git("merge", "other", "-m", "merge1")
    first = _read(repo, "index.json")
    # Second merge of the same branch: already up to date (true no-op).
    cp = git("merge", "other", "-m", "merge2", check=False)
    assert "Already up to date" in (cp.stdout + cp.stderr)
    assert _by_id(_read(repo, "index.json")).keys() == _by_id(first).keys()


# ── driver wiring / fail-closed ──────────────────────────────────────────────


def test_gitattributes_and_config_installed(git_bridge):
    repo, git = git_bridge
    attrs = (repo / ".gitattributes").read_text(encoding="utf-8")
    assert "shared.json merge=bridge-reconcile" in attrs
    assert "index.json merge=bridge-reconcile" in attrs
    assert "tasks/*.json merge=bridge-reconcile" in attrs
    assert bmd.is_merge_driver_registered(str(repo))


def test_run_merge_driver_fail_closed_writes_nothing(tmp_path):
    base = tmp_path / "base.json"
    ours = tmp_path / "ours.json"
    theirs = tmp_path / "theirs.json"
    base.write_text("{}", encoding="utf-8")
    original_ours = json_dumps(_collection(_task("t1", status="active")))
    ours.write_text(original_ours, encoding="utf-8")
    theirs.write_text("{ this is not json", encoding="utf-8")
    rc = bmd.run_merge_driver(str(base), str(ours), str(theirs))
    assert rc == 1  # fail-closed
    assert ours.read_text(encoding="utf-8") == original_ours  # untouched


def test_run_merge_driver_single_task_tombstone(tmp_path):
    base = tmp_path / "b.json"
    ours = tmp_path / "o.json"
    theirs = tmp_path / "t.json"
    base.write_text("{}", encoding="utf-8")
    ours.write_text(json_dumps(_task("t1", status="active",
                    status_fts=_fts("2026-06-09T00:00:00+00:00", "win", 99))), encoding="utf-8")
    theirs.write_text(json_dumps(_task("t1", status="archived", tombstone=True,
                      status_fts=_fts("2026-06-01T00:00:00+00:00", "fed", 1))), encoding="utf-8")
    rc = bmd.run_merge_driver(str(base), str(ours), str(theirs), single_task=True)
    assert rc == 0
    result = json_loads(ours.read_text(encoding="utf-8"))
    assert result["status"] in TASK_HIDDEN_STATUSES and result.get("_tombstone") is True


def test_single_task_path_routing():
    assert bmd._is_single_task_path("tasks/abc-123.json") is True
    assert bmd._is_single_task_path("index.json") is False
    assert bmd._is_single_task_path("shared.json") is False
