"""A sync cycle commits only when DB data changed, and then only that data.

Real git throughout: a bridge clone whose origin is a local bare repository, so
staging, commit and push are the production calls. Network-only steps (peer
payloads, public release) are disabled.
"""

import json
import subprocess
import sys
import types
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest

import bridge_sync_worker as worker
import db_utils as db
import memory_audit
from schema import init_db

ROOT = Path(__file__).resolve().parent.parent
PARENT, CHILD, OTHER = "task-parent", "task-child", "task-other"
AUDIT_FILES = (
    "extended_memory/memory_audit_issues.json",
    "extended_memory/memory_audit_state.json",
)


def _git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def env(tmp_path, monkeypatch):
    db_path = str(tmp_path / "memory.db")
    init_db(db_path)
    now = db.now_iso()
    with db.get_conn(db_path) as conn:
        db.create_task_with_ledger(conn, PARENT, "Parent", now)
        db.create_task_with_ledger(conn, CHILD, "Child", now, notes="first note")
        db.create_task_with_ledger(conn, OTHER, "Other", now)
        db.apply_task_mutation(conn, CHILD, {"parent_id": PARENT}, tool_name="test")

    remote = tmp_path / "remote.git"
    bridge = tmp_path / "bridge"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(remote))
    _git(tmp_path, "init", "-q", "-b", "main", str(bridge))
    for key, value in (
        ("user.name", "test"),
        ("user.email", "test@example.invalid"),
        ("core.autocrlf", "false"),
    ):
        _git(bridge, "config", key, value)
    # Same ignore rules as the live bridge: the ledger file is never committed.
    (bridge / ".gitignore").write_text(
        "shared.js\nindex.html\nextended_memory/memory_events.json\n", encoding="utf-8"
    )
    _git(bridge, "add", ".gitignore")
    _git(bridge, "commit", "-q", "-m", "init")
    _git(bridge, "remote", "add", "origin", str(remote))
    _git(bridge, "push", "-q", "-u", "origin", "main")

    monkeypatch.setattr(worker, "ensure_bridge_repo_ready", lambda repo: (True, None))
    monkeypatch.setattr(
        worker, "ensure_bridge_git_identity", lambda repo: {"changed": False}
    )
    monkeypatch.setattr(worker, "publish_peer_payloads", lambda *a, **k: {})
    monkeypatch.setattr(worker, "create_public_release", lambda *a, **k: None)
    return types.SimpleNamespace(db=db_path, bridge=bridge, remote=remote)


def _cycle(env):
    return worker.main(db_path=env.db, bridge_repo=str(env.bridge), force=False)


def _remote_head(env):
    return _git(env.remote, "rev-parse", "main")


def _non_item_activity(env):
    """A ledger write that is not a task/entity change, like the audit's events.

    It trips the time-based skip check, so the cycle really exports.
    """
    with db.get_conn(env.db) as conn:
        db.record_memory_event(
            conn,
            event_type="memory_audit_run",
            aggregate_kind="audit",
            aggregate_id="memory",
            tool_name="test",
        )


def _blocked_keys(result):
    return [
        key
        for key, value in result.items()
        if value is True and (key.startswith("blocked_by_") or key.endswith("_failed"))
    ]


def _open_audit_issue(env):
    """A claim without evidence: every audit run reports it open and re-stamps it."""
    now = db.now_iso()
    with db.get_conn(env.db) as conn:
        conn.execute(
            "INSERT INTO context_chunks (chunk_id, source_type, source_ref, "
            "source_hash, title, body, state, enrich_policy, created_at, updated_at) "
            "VALUES ('chunk-a', 'observation', 'source-a', 'hash-a', 'Chunk', "
            "'Service uses Redis', 'enrichable', 'manual', ?, ?)",
            (now, now),
        )
        conn.execute(
            "INSERT INTO candidate_claims (claim_id, chunk_id, subject, predicate, "
            "object_text, claim_scope, confidence, created_at, updated_at) "
            "VALUES ('claim-a', 'chunk-a', 'Service', 'uses', 'Redis', 'memory', "
            "0.9, ?, ?)",
            (now, now),
        )


def _audit_due(monkeypatch):
    """The next cycle finds the hourly audit due, as the top of the hour does.

    The schedule cannot be rewound in the DB: each cycle imports the bridge copy
    of memory_audit_state over it. So the next scheduler call is forced instead.
    """
    real = memory_audit.maybe_run_memory_audit

    def due_once(conn, **kwargs):
        monkeypatch.setattr(memory_audit, "maybe_run_memory_audit", real)
        return real(conn, **kwargs, force=True)

    monkeypatch.setattr(memory_audit, "maybe_run_memory_audit", due_once)


def _audit_schedule(env):
    with db.get_conn(env.db) as conn:
        row = conn.execute(
            "SELECT last_finished_at, next_run_after FROM memory_audit_state "
            "WHERE runner_name = 'bridge_sync'"
        ).fetchone()
    return tuple(row)


def _staged_audit_only(env):
    return _git(env.bridge, "status", "--porcelain").splitlines() == [
        f"M  {path}" for path in AUDIT_FILES
    ]


def _commit_files(env, sha):
    return set(
        _git(
            env.remote, "diff-tree", "--no-commit-id", "--name-only", "-r", sha
        ).split()
    )


def test_field_ts_key_order_is_canonical_whatever_the_merge_order():
    clock = ["2026-10-06T06:00:00+00:00", "peer", 0, None]
    task = {"id": CHILD, "_field_ts": {"title": clock, "assignee": clock}}
    overrides = {CHILD: {"parent_id": None, "field_versions": {"parent_id": clock}}}

    db.apply_task_export_overrides([task], overrides)

    assert list(task["_field_ts"]) == ["assignee", "parent_id", "title"]


def test_first_and_later_export_of_a_task_are_byte_identical(tmp_path):
    """Before: no transport file -> SQL key order; file exists -> parent_id first."""
    db_path = str(tmp_path / "memory.db")
    init_db(db_path)
    bridge = tmp_path / "bridge"
    bridge.mkdir()
    now = db.now_iso()
    with db.get_conn(db_path) as conn:
        db.create_task_with_ledger(conn, PARENT, "Parent", now)
        db.create_task_with_ledger(conn, CHILD, "Child", now)
        db.apply_task_mutation(conn, CHILD, {"parent_id": PARENT}, tool_name="test")

    def export():
        with db.get_conn(db_path) as conn:
            overrides = db.prepare_task_export_overrides(conn, str(bridge))
            shared = worker._export_tasks(conn, export_overrides=overrides)
            db.export_task_files(conn, str(bridge), export_overrides=overrides)
            db.export_index_json(conn, str(bridge), export_overrides=overrides)
        task = (bridge / "tasks" / f"{CHILD}.json").read_text(encoding="utf-8")
        index = json.loads((bridge / "index.json").read_text(encoding="utf-8"))
        return task, index, shared

    first_task, first_index, first_shared = export()
    second_task, second_index, second_shared = export()

    assert second_task == first_task
    assert second_index == first_index
    assert second_shared == first_shared
    field_ts = json.loads(first_task)["_field_ts"]
    assert "parent_id" in field_ts
    assert list(field_ts) == sorted(field_ts)


def test_export_leaves_a_semantically_equal_peer_file_untouched(tmp_path):
    """A peer's serializer or key order is not a change, so its bytes stay."""
    db_path = str(tmp_path / "memory.db")
    init_db(db_path)
    bridge = tmp_path / "bridge"
    bridge.mkdir()
    with db.get_conn(db_path) as conn:
        db.create_task_with_ledger(conn, CHILD, "Child", db.now_iso())
        db.export_task_files(conn, str(bridge))
    path = bridge / "tasks" / f"{CHILD}.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    record["_field_ts"] = dict(reversed(list(record["_field_ts"].items())))
    peer_bytes = json.dumps(record, separators=(",", ":"))  # orjson-style, compact
    path.write_text(peer_bytes, encoding="utf-8")

    with db.get_conn(db_path) as conn:
        db.export_task_files(conn, str(bridge))

    assert path.read_text(encoding="utf-8") == peer_bytes


def test_no_change_cycle_creates_no_commit_and_is_a_clean_success(env):
    first = _cycle(env)
    assert first["pushed"] is True
    head = _remote_head(env)
    with db.get_conn(env.db) as conn:
        watermark_before = conn.execute(
            "SELECT value FROM bridge_meta WHERE key='last_push_at'"
        ).fetchone()[0]

    _non_item_activity(env)
    second = _cycle(env)

    # It exported (not the early time-based skip) and found nothing to publish.
    assert second.get("no_changes") is True
    assert second.get("skipped") is True
    assert second["pushed"] is False
    assert _blocked_keys(second) == []
    assert _remote_head(env) == head
    assert _git(env.bridge, "rev-parse", "HEAD") == head
    assert _git(env.bridge, "status", "--porcelain") == ""
    with db.get_conn(env.db) as conn:
        watermark_after = conn.execute(
            "SELECT value FROM bridge_meta WHERE key='last_push_at'"
        ).fetchone()[0]
    # The remote holds this export, so the watermark advances like a push.
    assert watermark_after > watermark_before

    third = _cycle(env)
    assert third.get("skipped") is True
    assert _remote_head(env) == head


def test_unpushed_local_commit_is_still_pushed_when_export_is_unchanged(env):
    """An earlier push that failed left a local commit: "no changes" must not strand it."""
    assert _cycle(env)["pushed"] is True
    with db.get_conn(env.db) as conn:
        db.apply_task_mutation(conn, CHILD, {"notes": "offline edit"}, tool_name="test")
    real_retry = worker.git_retry

    def push_fails_once(repo_dir, *args, **kwargs):
        if args and args[0] == "push":
            return subprocess.CompletedProcess(["git", *args], 1, "", "network down")
        return real_retry(repo_dir, *args, **kwargs)

    worker.git_retry = push_fails_once
    try:
        failed = _cycle(env)
    finally:
        worker.git_retry = real_retry
        worker._push_backoff_until = 0.0
        worker._push_failure_count = 0
    assert failed["pushed"] is False
    local = _git(env.bridge, "rev-parse", "HEAD")
    assert _remote_head(env) != local

    _non_item_activity(env)
    retried = _cycle(env)

    assert retried["pushed"] is True
    assert retried.get("no_changes") is not True
    assert _remote_head(env) == local


def test_one_task_change_commits_only_that_task(env):
    assert _cycle(env)["pushed"] is True
    _non_item_activity(env)
    assert _cycle(env).get("no_changes") is True
    head = _remote_head(env)

    with db.get_conn(env.db) as conn:
        db.apply_task_mutation(conn, CHILD, {"notes": "second note"}, tool_name="test")
    result = _cycle(env)

    assert result["pushed"] is True
    commits = _git(env.remote, "rev-list", f"{head}..main").split()
    assert len(commits) == 1
    files = set(
        _git(
            env.remote, "diff-tree", "--no-commit-id", "--name-only", "-r", commits[0]
        ).split()
    )
    # The task's own file, the three aggregates that embed this task, and the
    # conflict ledger, where the import records "local notes outranked the
    # bridge copy" for this task. Nothing about any other task or entity.
    assert files == {
        f"tasks/{CHILD}.json",
        "index.json",
        "shared.json",
        "kanban_payload.json",
        "extended_memory/memory_conflicts.json",
    }
    conflicts = json.loads(
        _git(env.remote, "show", "main:extended_memory/memory_conflicts.json")
    )
    assert {(c["aggregate_id"], c["field_name"]) for c in conflicts} == {
        (CHILD, "notes")
    }
    shared = json.loads((env.bridge / "shared.json").read_text(encoding="utf-8"))
    assert (
        next(t for t in shared["tasks"] if t["id"] == CHILD)["notes"] == "second note"
    )
    for other in (PARENT, OTHER):
        assert (
            _git(env.remote, "rev-list", f"{head}..main", "--", f"tasks/{other}.json")
            == ""
        )


def test_hook_worker_treats_the_engine_no_change_result_as_synced(
    env, tmp_path, monkeypatch
):
    _cycle(env)
    _non_item_activity(env)
    stats = _cycle(env)
    assert stats.get("no_changes") is True

    hook = spec_from_file_location(
        "bridge_hook_worker_no_change", ROOT / "hooks" / "bridge_sync_worker.py"
    )
    module = module_from_spec(hook)
    hook.loader.exec_module(module)
    state = tmp_path / "hook_state"
    state.mkdir()
    module.LOCK_FILE = str(state / ".lock")
    module.LAST_SYNC = str(state / ".last_sync")
    module.DIRTY_FLAG = str(state / ".dirty")
    module.NOTIFY_FILE = str(state / ".notify")
    module.FAIL_COUNTER = str(state / ".fail_count")
    module.SERVER_DIR = str(tmp_path)
    Path(module.DIRTY_FLAG).write_text("1", encoding="utf-8")
    monkeypatch.setattr(module, "preflight_git_check", lambda: (True, None))
    monkeypatch.setattr(
        module,
        "fix_remote_ahead",
        lambda: (_ for _ in ()).throw(AssertionError("no push for a no-op cycle")),
    )

    # bridge_server.bridge_push's envelope around the engine result.
    envelope = dict(stats, pushed_to_remote=bool(stats.get("pushed")))
    fake_server = types.SimpleNamespace(
        bridge_push=types.SimpleNamespace(fn=lambda tag="shared": json.dumps(envelope))
    )
    monkeypatch.setitem(sys.modules, "bridge_server", fake_server)

    module.main()

    assert not Path(module.DIRTY_FLAG).exists()
    assert Path(module.LAST_SYNC).exists()
    assert Path(module.FAIL_COUNTER).read_text(encoding="utf-8") == "0"
    notice = json.loads(Path(module.NOTIFY_FILE).read_text(encoding="utf-8"))
    assert notice["level"] == "info"


def test_idle_cycle_that_runs_the_audit_commits_nothing(env, monkeypatch):
    """The hourly audit re-stamps its own bookkeeping; alone that is no commit."""
    # Real preflight: what the next cycle reads back decides the audit cadence.
    monkeypatch.setattr(worker, "ensure_bridge_repo_ready", db.ensure_bridge_repo_ready)
    _open_audit_issue(env)
    assert _cycle(env)["pushed"] is True
    head = _remote_head(env)
    before = _audit_schedule(env)
    _audit_due(monkeypatch)

    result = _cycle(env)

    schedule = _audit_schedule(env)
    assert schedule != before  # the audit ran ...
    assert result.get("no_changes") is True  # ... the export ran, found nothing
    assert result["pushed"] is False
    assert _blocked_keys(result) == []
    assert _remote_head(env) == head
    assert _git(env.bridge, "rev-parse", "HEAD") == head
    # Only the audit files changed; they stay staged for the next real change.
    assert _staged_audit_only(env)

    _non_item_activity(env)
    again = _cycle(env)

    # The import read back the staged copy, so the audit is not due again.
    assert again.get("no_changes") is True
    assert _audit_schedule(env) == schedule
    assert _remote_head(env) == head


def test_tray_syncs_without_item_change_commit_nothing(env):
    """tray_sync._build_ui_profile stamps updated_at=now; that stamp is no change."""

    def tray_sync(stamp):
        profile = {"theme": "dark", "font_size": 11, "bold": False}
        return worker.main(
            db_path=env.db,
            bridge_repo=str(env.bridge),
            ui_profile={**profile, "tab_views": {}, "updated_at": stamp},
        )

    assert tray_sync("2026-10-06T08:00:00+00:00")["pushed"] is True
    head = _remote_head(env)

    for stamp in ("2026-10-06T08:30:00+00:00", "2026-10-06T09:00:00+00:00"):
        _non_item_activity(env)
        result = tray_sync(stamp)
        assert result.get("no_changes") is True
        assert result["pushed"] is False

    assert _remote_head(env) == head
    assert _git(env.bridge, "status", "--porcelain") == ""


def test_audit_bookkeeping_rides_along_with_the_next_item_change(env, monkeypatch):
    monkeypatch.setattr(worker, "ensure_bridge_repo_ready", db.ensure_bridge_repo_ready)
    _open_audit_issue(env)
    assert _cycle(env)["pushed"] is True
    _audit_due(monkeypatch)
    assert _cycle(env).get("no_changes") is True
    head = _remote_head(env)

    with db.get_conn(env.db) as conn:
        db.apply_task_mutation(
            conn, OTHER, {"title": "Other, renamed"}, tool_name="test"
        )
    result = _cycle(env)

    assert result["pushed"] is True
    commits = _git(env.remote, "rev-list", f"{head}..main").split()
    assert len(commits) == 1
    # The task's file, the aggregates that embed it, the conflict ledger the
    # import writes for it, and the audit bookkeeping the idle cycle left staged.
    assert _commit_files(env, commits[0]) == {
        f"tasks/{OTHER}.json",
        "index.json",
        "shared.json",
        "kanban_payload.json",
        "extended_memory/memory_conflicts.json",
        *AUDIT_FILES,
    }
    state = json.loads(_git(env.remote, "show", f"main:{AUDIT_FILES[1]}"))
    assert [row["next_run_after"] for row in state] == [_audit_schedule(env)[1]]
    assert _git(env.bridge, "status", "--porcelain") == ""


def test_staged_audit_bookkeeping_yields_to_a_peer_commit_on_the_same_file(
    env, tmp_path, monkeypatch
):
    """Left staged, the bookkeeping must not stop the fast-forward to a peer's audit."""
    _open_audit_issue(env)
    assert _cycle(env)["pushed"] is True
    _audit_due(monkeypatch)
    assert _cycle(env).get("no_changes") is True
    assert _staged_audit_only(env)

    peer = tmp_path / "peer"
    _git(tmp_path, "clone", "-q", str(env.remote), str(peer))
    issues_path = peer / AUDIT_FILES[0]
    issues = json.loads(issues_path.read_text(encoding="utf-8"))
    for issue in issues:
        issue["last_detected_at"] = db.now_iso()  # the peer's own hourly audit
    issues_path.write_text(json.dumps(issues), encoding="utf-8")
    identity = ("-c", "user.name=peer", "-c", "user.email=peer@example.invalid")
    _git(peer, *identity, "commit", "-q", "-am", "peer audit")
    _git(peer, "push", "-q", "origin", "main")
    peer_head = _git(peer, "rev-parse", "HEAD")

    result = _cycle(env)

    assert _blocked_keys(result) == []
    assert result.get("no_changes") is True
    assert _git(env.bridge, "rev-parse", "HEAD") == peer_head
    assert _remote_head(env) == peer_head


def test_remote_check_answers_false_when_git_times_out(tmp_path, monkeypatch):
    def timed_out(repo_dir, *args, timeout=30):
        raise subprocess.TimeoutExpired(["git", *args], timeout)

    monkeypatch.setattr(worker, "git_run", timed_out)

    assert worker._remote_already_has_export(str(tmp_path)) is False
