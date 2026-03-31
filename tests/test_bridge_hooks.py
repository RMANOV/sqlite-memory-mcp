import io
import json
import os
import subprocess
import sys
import types
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load_module(name: str, path: Path):
    spec = spec_from_file_location(name, path)
    module = module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_bridge_auto_sync_handles_invalid_json_input(tmp_path):
    env = os.environ.copy()
    env["HOME"] = str(tmp_path)
    env["USERPROFILE"] = str(tmp_path)
    proc = subprocess.run(
        [sys.executable, str(ROOT / "hooks" / "bridge_auto_sync.py")],
        input="{",
        text=True,
        capture_output=True,
        env=env,
        timeout=10,
    )

    assert proc.returncode == 0
    assert proc.stdout == ""
    assert proc.stderr == ""


def test_bridge_auto_sync_tracks_new_collab_writes_without_retriggering_bridge_push(
    tmp_path, monkeypatch
):
    module = _load_module(
        "bridge_auto_sync_test", ROOT / "hooks" / "bridge_auto_sync.py"
    )
    module.DIRTY_FLAG = str(tmp_path / ".bridge_dirty")
    module.LAST_SYNC = str(tmp_path / ".bridge_last_sync")
    module.NOTIFY_FILE = str(tmp_path / ".bridge_notification")
    module.WORKER_SCRIPT = str(tmp_path / "bridge_sync_worker.py")
    Path(module.WORKER_SCRIPT).write_text("print('worker')\n", encoding="utf-8")

    launches = []

    def fake_popen(args, **kwargs):
        launches.append(args)
        return types.SimpleNamespace(pid=1234)

    monkeypatch.setattr(module.subprocess, "Popen", fake_popen)

    out = io.StringIO()
    rc = module.main(
        stdin=io.StringIO(
            json.dumps({"tool_name": "mcp__sqlite_collab__request_publish"})
        ),
        stdout=out,
    )
    assert rc == 0
    assert Path(module.DIRTY_FLAG).exists()
    assert len(launches) == 1

    launches.clear()
    out = io.StringIO()
    rc = module.main(
        stdin=io.StringIO(json.dumps({"tool_name": "mcp__sqlite_bridge__bridge_push"})),
        stdout=out,
    )
    assert rc == 0
    assert launches == []


@pytest.mark.parametrize(
    "tool_name",
    [
        "mcp__sqlite_collab__request_publish",
        "mcp__sqlite_unified__create_task_or_note",
    ],
)
def test_bridge_auto_sync_tracks_unified_and_collab_writes(
    tmp_path, monkeypatch, tool_name
):
    module = _load_module(
        f"bridge_auto_sync_test_{tool_name}",
        ROOT / "hooks" / "bridge_auto_sync.py",
    )
    module.DIRTY_FLAG = str(tmp_path / ".bridge_dirty")
    module.LAST_SYNC = str(tmp_path / ".bridge_last_sync")
    module.NOTIFY_FILE = str(tmp_path / ".bridge_notification")
    module.WORKER_SCRIPT = str(tmp_path / "bridge_sync_worker.py")
    Path(module.WORKER_SCRIPT).write_text("print('worker')\n", encoding="utf-8")

    launches = []

    def fake_popen(args, **kwargs):
        launches.append(args)
        return types.SimpleNamespace(pid=1234)

    monkeypatch.setattr(module.subprocess, "Popen", fake_popen)

    out = io.StringIO()
    rc = module.main(
        stdin=io.StringIO(json.dumps({"tool_name": tool_name})),
        stdout=out,
    )

    assert rc == 0
    assert Path(module.DIRTY_FLAG).exists()
    assert len(launches) == 1


def test_hook_worker_treats_no_change_push_as_success(tmp_path, monkeypatch):
    module = _load_module(
        "bridge_hook_worker_test", ROOT / "hooks" / "bridge_sync_worker.py"
    )
    module.LOCK_FILE = str(tmp_path / ".lock")
    module.LAST_SYNC = str(tmp_path / ".last_sync")
    module.DIRTY_FLAG = str(tmp_path / ".dirty")
    module.NOTIFY_FILE = str(tmp_path / ".notify")
    module.FAIL_COUNTER = str(tmp_path / ".fail_count")
    module.SERVER_DIR = str(tmp_path)
    notifications = []
    fail_counts = []

    monkeypatch.setattr(module, "acquire_lock", lambda: True)
    monkeypatch.setattr(module, "release_lock", lambda: None)
    monkeypatch.setattr(module, "preflight_git_check", lambda: (True, None))
    monkeypatch.setattr(module, "_read_fail_count", lambda: 0)
    monkeypatch.setattr(module, "_write_fail_count", fail_counts.append)
    monkeypatch.setattr(
        module, "notify", lambda level, msg: notifications.append((level, msg))
    )
    monkeypatch.setattr(
        module,
        "fix_remote_ahead",
        lambda: (_ for _ in ()).throw(AssertionError("should not retry remote-ahead")),
    )

    class Tool:
        def __init__(self, fn):
            self.fn = fn

    fake_bridge_server = types.SimpleNamespace(
        bridge_pull=Tool(lambda: json.dumps({"new_tasks": 0})),
        bridge_push=Tool(
            lambda tag="shared": json.dumps(
                {"pushed": 0, "tasks": 3, "message": "No changes — already up to date"}
            )
        ),
    )
    monkeypatch.setitem(sys.modules, "bridge_server", fake_bridge_server)

    module.main()

    assert Path(module.LAST_SYNC).exists()
    assert fail_counts == [0]
    assert notifications[-1] == ("info", "BRIDGE: synced 3 tasks OK")


def test_bridge_auto_sync_prefers_repo_worker_path(tmp_path, monkeypatch):
    module = _load_module(
        "bridge_auto_sync_repo_worker_test", ROOT / "hooks" / "bridge_auto_sync.py"
    )
    repo_worker = tmp_path / "repo_bridge_sync_worker.py"
    legacy_worker = tmp_path / "legacy_bridge_sync_worker.py"
    repo_worker.write_text("print('repo worker')\n", encoding="utf-8")
    legacy_worker.write_text("print('legacy worker')\n", encoding="utf-8")

    module.WORKER_SCRIPT = str(repo_worker)
    module.WORKER_FALLBACK_SCRIPT = str(legacy_worker)

    launches = []

    def fake_popen(args, **kwargs):
        launches.append(args)
        return types.SimpleNamespace(pid=4321)

    monkeypatch.setattr(module.subprocess, "Popen", fake_popen)

    assert module._launch_worker() is None
    assert launches == [[sys.executable, str(repo_worker)]]


def test_hook_worker_retries_after_previous_failures(tmp_path, monkeypatch):
    module = _load_module(
        "bridge_hook_worker_retry_test", ROOT / "hooks" / "bridge_sync_worker.py"
    )
    module.LOCK_FILE = str(tmp_path / ".lock")
    module.LAST_SYNC = str(tmp_path / ".last_sync")
    module.DIRTY_FLAG = str(tmp_path / ".dirty")
    module.NOTIFY_FILE = str(tmp_path / ".notify")
    module.FAIL_COUNTER = str(tmp_path / ".fail_count")
    module.SERVER_DIR = str(tmp_path)

    calls = {"pull": 0, "push": 0}
    notifications = []
    fail_counts = []

    monkeypatch.setattr(module, "acquire_lock", lambda: True)
    monkeypatch.setattr(module, "release_lock", lambda: None)
    monkeypatch.setattr(module, "preflight_git_check", lambda: (True, None))
    monkeypatch.setattr(module, "_read_fail_count", lambda: module.MAX_FAILURES)
    monkeypatch.setattr(module, "_write_fail_count", fail_counts.append)
    monkeypatch.setattr(
        module, "notify", lambda level, msg: notifications.append((level, msg))
    )
    monkeypatch.setattr(module, "fix_remote_ahead", lambda: False)

    class Tool:
        def __init__(self, fn):
            self.fn = fn

    def fake_pull():
        calls["pull"] += 1
        return json.dumps({"new_tasks": 0})

    def fake_push(tag="shared"):
        calls["push"] += 1
        return json.dumps(
            {"pushed": 0, "tasks": 2, "message": "No changes — already up to date"}
        )

    fake_bridge_server = types.SimpleNamespace(
        bridge_pull=Tool(fake_pull),
        bridge_push=Tool(fake_push),
    )
    monkeypatch.setitem(sys.modules, "bridge_server", fake_bridge_server)

    module.main()

    assert calls == {"pull": 1, "push": 1}
    assert fail_counts == [0]
    assert notifications[-1] == ("info", "BRIDGE: synced 2 tasks OK")


def test_hook_worker_drains_writes_that_arrive_during_sync(tmp_path, monkeypatch):
    module = _load_module(
        "bridge_hook_worker_drain_test", ROOT / "hooks" / "bridge_sync_worker.py"
    )
    module.LOCK_FILE = str(tmp_path / ".lock")
    module.LAST_SYNC = str(tmp_path / ".last_sync")
    module.DIRTY_FLAG = str(tmp_path / ".dirty")
    module.NOTIFY_FILE = str(tmp_path / ".notify")
    module.FAIL_COUNTER = str(tmp_path / ".fail_count")
    module.SERVER_DIR = str(tmp_path)

    notifications = []
    fail_counts = []
    calls = {"pull": 0, "push": 0}
    clock = iter([100.0, 101.0, 102.0, 103.0])

    monkeypatch.setattr(module, "acquire_lock", lambda: True)
    monkeypatch.setattr(module, "release_lock", lambda: None)
    monkeypatch.setattr(module, "preflight_git_check", lambda: (True, None))
    monkeypatch.setattr(module, "_read_fail_count", lambda: 0)
    monkeypatch.setattr(module, "_write_fail_count", fail_counts.append)
    monkeypatch.setattr(
        module, "notify", lambda level, msg: notifications.append((level, msg))
    )
    monkeypatch.setattr(module, "fix_remote_ahead", lambda: False)
    monkeypatch.setattr(module.time, "time", lambda: next(clock))

    class Tool:
        def __init__(self, fn):
            self.fn = fn

    def fake_pull():
        calls["pull"] += 1
        return json.dumps({"new_tasks": 0})

    def fake_push(tag="shared"):
        calls["push"] += 1
        if calls["push"] == 1:
            Path(module.DIRTY_FLAG).write_text(
                str(module.time.time()), encoding="utf-8"
            )
        return json.dumps(
            {"pushed": 0, "tasks": 4, "message": "No changes — already up to date"}
        )

    fake_bridge_server = types.SimpleNamespace(
        bridge_pull=Tool(fake_pull),
        bridge_push=Tool(fake_push),
    )
    monkeypatch.setitem(sys.modules, "bridge_server", fake_bridge_server)

    module.main()

    assert calls == {"pull": 2, "push": 2}
    assert fail_counts == [0, 0]
    assert Path(module.LAST_SYNC).exists()
    assert not Path(module.DIRTY_FLAG).exists()
    assert notifications[-1] == ("info", "BRIDGE: synced 4 tasks OK")
