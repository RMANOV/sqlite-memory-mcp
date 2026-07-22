"""Windows adapter tests for REV 2.2 zero-paste delivery (task 0d806934).

Covers the Windows-specific mandate: real resource snapshot, unknown
temperature policy, pid+create_time identity (PID reuse), pump restart not
retiring live workers, hidden spawn flags, wake-event signal semantics, and
graceful-stop priority. Server-side protocol invariants (round cap, blind
gate, STALE_READ, stalemate) live in the existing regression suites.

All tests run against temp paths / mocks — never against production memory.db.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
HOOKS = REPO / "hooks"
for path in (str(REPO), str(HOOKS)):
    if path not in sys.path:
        sys.path.insert(0, path)

IS_WINDOWS = sys.platform == "win32"
windows_only = pytest.mark.skipif(not IS_WINDOWS, reason="Windows adapter behavior")


@pytest.fixture()
def isolated_wake_events(monkeypatch):
    """Unique kernel event names per test run: the production pump waits on
    the default names and an auto-reset event releases only ONE waiter, so
    sharing names with a live pump makes signal tests racy."""
    import debate_wake_signal as dws

    monkeypatch.setenv("DEBATE_WAKE_EVENT_NAME", rf"Local\DebateWakeTest{os.getpid()}")
    monkeypatch.setenv(
        "DEBATE_PUMP_STOP_EVENT_NAME", rf"Local\DebateStopTest{os.getpid()}"
    )
    dws.close_handles()
    yield dws
    dws.close_handles()


# --- Implementation A: resource snapshot -----------------------------------


@windows_only
def test_windows_snapshot_reports_real_memory():
    import debate_resource_budget as drb

    snapshot = drb.read_resource_snapshot()
    assert snapshot.mem_total_mib > 1024, "physical RAM must be real, not 0"
    assert snapshot.mem_available_mib > 0
    assert snapshot.max_temp_c is None  # unknown on this platform by design


@windows_only
def test_windows_zero_memory_is_adapter_error_not_low_mem(monkeypatch):
    import debate_resource_budget as drb

    fake_psutil_missing = pytest.raises(
        RuntimeError, match="windows_memory_adapter_error"
    )
    monkeypatch.setattr(
        drb, "_windows_memory_fallback_mib", lambda: (0, 0), raising=True
    )

    real_import = __import__

    def _no_psutil(name, *args, **kwargs):
        if name == "psutil":
            raise ImportError("simulated missing psutil")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", _no_psutil)
    with fake_psutil_missing:
        drb._read_windows_snapshot()


def test_unknown_temperature_lands_in_guarded_tier_not_block():
    import debate_resource_budget as drb

    snapshot = drb.ResourceSnapshot(
        mem_total_mib=32000,
        mem_available_mib=16000,
        swap_total_mib=8192,
        swap_free_mib=8192,
        cpu_count=8,
        load1=0.5,
        memory_full_avg10=0.0,
        max_temp_c=None,  # unknown temperature
        live_agent_count=0,
    )
    budget = drb.compute_debate_resource_budget(snapshot)
    assert budget.allow_agent is True, "unknown temp must not block"
    assert budget.max_concurrent_workers >= 1
    assert budget.tier == "guarded"
    assert budget.max_concurrent_workers == 2


# --- Pump identity: pid + create_time (PID reuse protection) ----------------


@windows_only
def test_pid_reuse_is_not_accepted_as_old_worker(monkeypatch):
    import psutil

    import debate_pump

    me = psutil.Process(os.getpid())
    # Same live PID, but recorded create_time from a "previous life": the
    # identity check must refuse it even though the PID exists and is python.
    stale_create_time = me.create_time() - 3600.0
    assert (
        debate_pump._windows_pid_is_live_agent(os.getpid(), stale_create_time) is False
    )


@windows_only
def test_live_agent_with_matching_identity_is_accepted(monkeypatch):
    import psutil

    import debate_pump

    me = psutil.Process(os.getpid())

    class _FakeProc:
        def status(self):
            return psutil.STATUS_RUNNING

        def create_time(self):
            return me.create_time()

        def cmdline(self):
            return ["claude", "-p"]

        def name(self):
            return "claude.exe"

    monkeypatch.setattr(psutil, "Process", lambda pid: _FakeProc())
    assert debate_pump._windows_pid_is_live_agent(4242, me.create_time()) is True


@windows_only
def test_pump_restart_does_not_retire_live_worker(tmp_path, monkeypatch):
    """_live_worker_session_ids must keep a claim whose pid+create_time match."""
    import sqlite3

    import debate_pump

    db = tmp_path / "wake.db"
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE debate_worker_claims (
            topic_id TEXT, worker_session_id TEXT, trigger_msg_id TEXT, state TEXT
        );
        CREATE TABLE debate_wake_log (
            wake_id TEXT, trigger_msg_id TEXT, topic_id TEXT, recipient TEXT,
            target_role TEXT, target_session_id TEXT, target_runtime TEXT,
            binding_generation INTEGER, action TEXT, result TEXT,
            schema_version TEXT, details_json TEXT, created_at TEXT
        );
        """
    )
    import psutil

    me = psutil.Process(os.getpid())
    details = json.dumps({"pid": os.getpid(), "create_time": me.create_time()})
    con.execute(
        "INSERT INTO debate_worker_claims VALUES ('T1','cc-x-W1','m1','active')"
    )
    con.execute(
        "INSERT INTO debate_wake_log VALUES "
        "('w1','m1','T1','EXEC','EXEC','cc-x-W1','cc',NULL,"
        "'external_agent_spawn','real_spawn','v1',?, '2026-07-21T00:00:00Z')",
        (details,),
    )
    con.commit()
    con.close()

    monkeypatch.setattr(debate_pump, "DB_PATH", db)
    # Current process cmdline is python, not claude — patch the agent check
    # to isolate the identity logic from the needle match.
    monkeypatch.setattr(
        debate_pump,
        "_windows_pid_is_live_agent",
        lambda pid, ct=None: (
            pid == os.getpid() and ct is not None and abs(ct - me.create_time()) <= 2.0
        ),
    )
    live = debate_pump._live_worker_session_ids("T1")
    assert live == {"cc-x-W1"}, "restarted pump must recognize its live worker"


# --- Implementation B: wake event semantics ---------------------------------


@windows_only
def test_wake_event_roundtrip_and_stop_priority(isolated_wake_events):
    dws = isolated_wake_events

    outcome: list[str] = []
    t = threading.Thread(target=lambda: outcome.append(dws.wait_for_wake_or_stop(5)))
    t.start()
    time.sleep(0.2)
    assert dws.signal_wake() is True
    t.join(timeout=5)
    assert outcome == ["wake"]

    # Two signals, one waiter: second signal leaves the event set → the next
    # wait returns immediately (at-least-once, never lost, never duplicated
    # into two logical results — dedupe is the claim layer's job).
    assert dws.signal_wake() is True
    assert dws.signal_wake() is True
    assert dws.wait_for_wake_or_stop(1) == "wake"

    # Stop wins over wake when both are signaled.
    assert dws.signal_wake() is True
    assert dws.signal_stop() is True
    assert dws.wait_for_wake_or_stop(1) == "stop"
    # Drain the still-set wake event so later tests start clean.
    assert dws.wait_for_wake_or_stop(1) in {"wake", "timeout"}
    dws.close_handles()


@windows_only
def test_wake_event_timeout_is_bounded_sweep(isolated_wake_events):
    dws = isolated_wake_events

    t0 = time.perf_counter()
    assert dws.wait_for_wake_or_stop(0.3) == "timeout"
    elapsed = time.perf_counter() - t0
    assert 0.2 <= elapsed < 2.0
    dws.close_handles()


def test_signal_wake_never_raises_off_windows_or_on_failure(monkeypatch):
    import debate_wake_signal as dws

    if IS_WINDOWS:
        monkeypatch.setattr(dws, "_open_or_create", lambda name: None)
        assert dws.signal_wake() is False
    else:
        assert dws.signal_wake() is False
        assert dws.wait_for_wake_or_stop(0.1) == "unsupported"


# --- Implementation C: hidden bounded spawn ---------------------------------


@windows_only
def test_launch_agent_uses_hidden_flags_and_resolved_executable(tmp_path, monkeypatch):
    import debate_wake

    captured: dict[str, object] = {}

    class _FakeProc:
        pid = 43210
        stdin = None

    def _fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return _FakeProc()

    monkeypatch.setattr(debate_wake.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(debate_wake, "AGENT_LOG_DIR", tmp_path)
    monkeypatch.setattr(
        debate_wake, "_record_real_spawn", lambda **kw: {"result": "real_spawn"}
    )
    monkeypatch.setattr(debate_wake, "_record_receipt_event", lambda event: None)
    monkeypatch.setattr(debate_wake, "_receipt_event", lambda *a: {"event": "x"})
    monkeypatch.setattr(debate_wake, "_receipt_line", lambda e: "RECEIVED test")
    monkeypatch.setattr(debate_wake, "_claim_worker_target", lambda t, m, tp: t)

    target = {
        "target_runtime": "cc",
        "target_role": "EXEC",
        "target_session_id": "cc-t-W1",
        "recipient": "EXEC",
    }
    out = debate_wake._launch_agent(target, "m1", "T1")
    assert out["launched"] is True
    flags = captured["kwargs"].get("creationflags", 0)
    assert flags & subprocess.CREATE_NO_WINDOW, "worker must not open a console"
    assert flags & subprocess.CREATE_NEW_PROCESS_GROUP
    assert "start_new_session" not in captured["kwargs"]
    cmd = captured["cmd"]
    assert Path(cmd[0]).is_absolute(), "executable must be shutil.which-resolved"


def test_launch_agent_missing_executable_is_typed_refusal(monkeypatch, tmp_path):
    import debate_wake

    monkeypatch.setattr(debate_wake, "_claim_worker_target", lambda t, m, tp: t)
    monkeypatch.setattr(debate_wake.shutil, "which", lambda name: None)
    target = {
        "target_runtime": "cc",
        "target_role": "EXEC",
        "target_session_id": "cc-t-W1",
        "recipient": "EXEC",
    }
    out = debate_wake._launch_agent(target, "m1", "T1")
    assert out == {
        "launched": False,
        "reason": "executable_not_found",
        "executable": "claude",
    }


def test_agent_command_mcp_prefix_is_configurable(monkeypatch):
    import debate_wake

    monkeypatch.setenv("DEBATE_WAKE_MCP_PREFIX", "mcp__sqlite_unified__")
    cmd = debate_wake._agent_command({"target_runtime": "cc"}, "m1", "T1")
    allowed = cmd[cmd.index("--allowedTools") + 1]
    assert "mcp__sqlite_unified__debate_worker_claim" in allowed
    assert "mcp__sqlite_intel__" not in allowed


@windows_only
def test_codex_route_disabled_by_default_on_windows(monkeypatch):
    """Advocate BLOCK high-risk #3: the auto-spawned codex route runs with
    --dangerously-bypass-approvals-and-sandbox. On Windows it must be a typed
    refusal unless explicitly enabled — never a silent automatic bypass."""
    import debate_wake

    monkeypatch.delenv("DEBATE_WAKE_CODEX_ENABLED", raising=False)
    assert debate_wake._agent_command({"target_runtime": "codex"}, "m1", "T1") is None
    monkeypatch.setenv("DEBATE_WAKE_CODEX_ENABLED", "1")
    cmd = debate_wake._agent_command({"target_runtime": "codex"}, "m1", "T1")
    assert cmd is not None and "--ephemeral" in cmd


@windows_only
def test_pump_singleton_mutex_is_atomic_cross_process():
    """Advocate BLOCK high-risk #1: the singleton must be an atomic OS mutex,
    not a read-check-act heartbeat race. A second acquirer in a fresh process
    (no shared in-process handle) must lose while the first holds it."""
    import subprocess

    # Unique mutex name so the test does not collide with a live production
    # pump that holds the default singleton mutex.
    env = dict(os.environ)
    env["DEBATE_PUMP_SINGLETON_MUTEX"] = rf"Local\DebateSingletonTest{os.getpid()}"
    code = (
        "import sys; sys.path.insert(0, r'"
        + str(REPO)
        + "'); from debate_wake_signal import acquire_pump_singleton; "
    )
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            code
            + "import time; print(acquire_pump_singleton(), flush=True); time.sleep(4)",
        ],
        stdout=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        assert holder.stdout.readline().strip() == "True"
        contender = subprocess.run(
            [sys.executable, "-c", code + "print(acquire_pump_singleton())"],
            capture_output=True,
            text=True,
            timeout=15,
            env=env,
        )
        assert contender.stdout.strip() == "False"
    finally:
        holder.wait(timeout=15)


@windows_only
def test_pump_singleton_claims_existing_unowned_mutex():
    """An existing kernel object is not proof that another pump owns it."""
    import subprocess

    env = dict(os.environ)
    env["DEBATE_PUMP_SINGLETON_MUTEX"] = rf"Local\DebateUnownedTest{os.getpid()}"
    repo_bootstrap = "import sys; sys.path.insert(0, r'" + str(REPO) + "'); "
    observer = subprocess.Popen(
        [
            sys.executable,
            "-c",
            repo_bootstrap
            + "import ctypes, os, time; "
            + "k=ctypes.WinDLL('kernel32', use_last_error=True); "
            + "k.CreateMutexW.restype=ctypes.c_void_p; "
            + "h=k.CreateMutexW(None, False, os.environ['DEBATE_PUMP_SINGLETON_MUTEX']); "
            + "print(bool(h), flush=True); time.sleep(4)",
        ],
        stdout=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        assert observer.stdout.readline().strip() == "True"
        contender = subprocess.run(
            [
                sys.executable,
                "-c",
                repo_bootstrap
                + "from debate_wake_signal import acquire_pump_singleton; "
                + "print(acquire_pump_singleton())",
            ],
            capture_output=True,
            text=True,
            timeout=15,
            env=env,
        )
        assert contender.stdout.strip() == "True"
    finally:
        observer.wait(timeout=15)


@windows_only
def test_pump_singleton_explicit_release_precedes_process_exit():
    """A clean pump can hand off its lease before interpreter teardown."""
    import subprocess

    env = dict(os.environ)
    env["DEBATE_PUMP_SINGLETON_MUTEX"] = rf"Local\DebateReleaseTest{os.getpid()}"
    repo_bootstrap = "import sys; sys.path.insert(0, r'" + str(REPO) + "'); "
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            repo_bootstrap
            + "from debate_wake_signal import acquire_pump_singleton, "
            + "release_pump_singleton; import time; "
            + "print(acquire_pump_singleton(), flush=True); "
            + "release_pump_singleton(); print('released', flush=True); time.sleep(4)",
        ],
        stdout=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        assert holder.stdout.readline().strip() == "True"
        assert holder.stdout.readline().strip() == "released"
        contender = subprocess.run(
            [
                sys.executable,
                "-c",
                repo_bootstrap
                + "from debate_wake_signal import acquire_pump_singleton; "
                + "print(acquire_pump_singleton())",
            ],
            capture_output=True,
            text=True,
            timeout=15,
            env=env,
        )
        assert contender.stdout.strip() == "True"
    finally:
        holder.wait(timeout=15)


def test_agent_log_dir_stays_bounded(tmp_path, monkeypatch):
    import debate_wake

    monkeypatch.setattr(debate_wake, "AGENT_LOG_DIR", tmp_path)
    monkeypatch.setenv("DEBATE_WAKE_AGENT_LOG_KEEP", "5")
    for index in range(9):
        log = tmp_path / f"{index:02d}.log"
        log.write_text("x", encoding="utf-8")
        os.utime(log, (1000000 + index, 1000000 + index))
    debate_wake._prune_agent_logs()
    assert len(list(tmp_path.glob("*.log"))) == 5
    assert (tmp_path / "08.log").exists(), "newest logs are kept"
    assert not (tmp_path / "00.log").exists(), "oldest logs are pruned"


# --- Implementation D: lifecycle heartbeat ----------------------------------


@windows_only
def test_pump_state_running_stale_stopped(tmp_path, monkeypatch):
    import debate_ops_windows as dow

    hb = tmp_path / "heartbeat.json"
    monkeypatch.setattr(dow, "HEARTBEAT_PATH", hb)

    assert dow.pump_state()["state"] == "stopped"

    import psutil
    from datetime import datetime, timezone

    me = psutil.Process(os.getpid())
    hb.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "create_time": me.create_time(),
                "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            }
        ),
        encoding="utf-8",
    )
    assert dow.pump_state()["state"] == "running"

    hb.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "create_time": me.create_time() - 3600.0,  # PID reuse → not our pump
                "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            }
        ),
        encoding="utf-8",
    )
    state = dow.pump_state()
    assert state["state"] == "stale"
    assert state["pid_live"] is False


def test_stop_waits_for_captured_process_after_heartbeat_disappears(
    monkeypatch, capsys
):
    import debate_ops_windows as dow
    import debate_wake_signal

    heartbeat = {"pid": 1234, "create_time": 5678.0}
    liveness = iter([True, True, False])
    monkeypatch.setattr(dow, "_read_heartbeat", lambda: heartbeat)
    monkeypatch.setattr(dow, "_heartbeat_pid_live", lambda _hb: next(liveness))
    monkeypatch.setattr(
        dow, "pump_state", lambda: {"state": "stopped", "heartbeat": None}
    )
    monkeypatch.setattr(debate_wake_signal, "signal_stop", lambda: True)
    monkeypatch.setattr(dow.time, "sleep", lambda _seconds: None)

    assert dow.cmd_stop(timeout_seconds=5.0) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["stopped"] == "graceful"


def test_start_requires_observed_running_heartbeat(monkeypatch, capsys):
    import debate_ops_windows as dow

    states = iter(
        [
            {"state": "stopped", "heartbeat": None},
            {"state": "stopped", "heartbeat": None},
        ]
    )
    monkeypatch.setattr(dow, "pump_state", lambda: next(states))
    monkeypatch.setattr(
        dow,
        "_schtasks",
        lambda _args: {"returncode": 1, "stdout": "", "stderr": "missing"},
    )
    monkeypatch.setattr(dow, "_spawn_pump_now", lambda: {"spawned_pid": 9876})
    monkeypatch.setattr(
        dow,
        "_wait_for_pump_running",
        lambda: {"state": "stopped", "heartbeat": None},
    )

    assert dow.cmd_start() == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["fallback"]["spawned_pid"] == 9876
    assert payload["pump_state"]["state"] == "stopped"


def test_stop_when_already_stopped_does_not_leave_stop_event_signaled(
    monkeypatch, capsys
):
    import debate_ops_windows as dow
    import debate_wake_signal

    signaled = False

    def signal_stop():
        nonlocal signaled
        signaled = True
        return True

    monkeypatch.setattr(dow, "_read_heartbeat", lambda: None)
    monkeypatch.setattr(
        dow, "pump_state", lambda: {"state": "stopped", "heartbeat": None}
    )
    monkeypatch.setattr(debate_wake_signal, "signal_stop", signal_stop)

    assert dow.cmd_stop() == 0
    assert signaled is False
    payload = json.loads(capsys.readouterr().out)
    assert payload["stopped"] == "already_stopped"


@windows_only
def test_task_xml_encodes_spec_constraints():
    import debate_ops_windows as dow

    xml = dow._task_xml()
    assert "<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>" in xml
    assert "<Hidden>true</Hidden>" in xml
    assert "<RunLevel>LeastPrivilege</RunLevel>" in xml
    assert "<RestartOnFailure>" in xml
    assert "pythonw.exe" in xml
    assert "--max-concurrent-workers 2" in xml.replace('"', "")
    assert str(dow.ROOT) in xml  # working directory is the repo


def test_task_xml_escapes_windows_paths_and_arguments(monkeypatch):
    import xml.etree.ElementTree as ET

    import debate_ops_windows as dow

    monkeypatch.setattr(dow, "ROOT", Path(r"C:\repo&ops<one>"))
    monkeypatch.setattr(dow, "PUMP_SCRIPT", Path(r"C:\repo&ops<one>\pump.py"))
    monkeypatch.setattr(dow, "PUMP_ARGS", ["--topic", "A&B<C>"])
    monkeypatch.setattr(dow, "_pythonw", lambda: Path(r"C:\py&thon\pythonw.exe"))

    xml = dow._task_xml()
    root = ET.fromstring(xml)
    namespace = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}

    assert root.findtext(".//t:Command", namespaces=namespace) == str(dow._pythonw())
    assert root.findtext(".//t:WorkingDirectory", namespaces=namespace) == str(dow.ROOT)
    assert "A&B<C>" in root.findtext(".//t:Arguments", namespaces=namespace)


# --- Pump wait integration ---------------------------------------------------


@windows_only
def test_pump_wait_or_stop_returns_early_on_wake_signal(
    monkeypatch, isolated_wake_events
):
    import debate_pump

    dws = isolated_wake_events
    monkeypatch.setattr(debate_pump, "STOP", False, raising=False)
    debate_pump.STOP_EVENT.clear()
    timer = threading.Timer(0.2, dws.signal_wake)
    timer.start()
    t0 = time.perf_counter()
    stopped = debate_pump._wait_or_stop(10.0)
    elapsed = time.perf_counter() - t0
    timer.join()
    assert stopped is False
    assert elapsed < 5.0, "wake event must cut the sleep short"
    dws.close_handles()


@windows_only
def test_pump_wait_or_stop_honors_stop_event(monkeypatch, isolated_wake_events):
    import debate_pump

    dws = isolated_wake_events
    monkeypatch.setattr(debate_pump, "STOP", False, raising=False)
    debate_pump.STOP_EVENT.clear()
    timer = threading.Timer(0.2, dws.signal_stop)
    timer.start()
    stopped = debate_pump._wait_or_stop(10.0)
    timer.join()
    assert stopped is True
    assert debate_pump.STOP is True
    debate_pump.STOP_EVENT.clear()
    debate_pump.STOP = False
    dws.close_handles()
