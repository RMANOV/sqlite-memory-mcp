#!/usr/bin/env python3
"""Windows user-level lifecycle for the resident debate pump.

Mirrors the systemd user service on Linux with a Task Scheduler task:
- runs at user logon, hidden, via pythonw.exe (no console window);
- least privilege (no admin), correct working directory;
- MultipleInstances=IgnoreNew, automatic restart on failure;
- stop is graceful first (named stop event → pump exits after the current
  scan without killing in-flight workers), ``schtasks /End`` only as a
  fallback;
- heartbeat file distinguishes running / stale / stopped.

The disable and sleep kill-switch files stay authoritative regardless of
task state — the pump honors them through the resource governor.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from xml.sax.saxutils import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TASK_NAME = os.environ.get("DEBATE_PUMP_TASK_NAME", "SqliteMemoryDebatePump")
PUMP_SCRIPT = ROOT / "hooks" / "debate_pump.py"
HEARTBEAT_PATH = Path(
    os.environ.get(
        "DEBATE_PUMP_HEARTBEAT",
        os.path.expanduser("~/.claude/memory/debate_pump_heartbeat.json"),
    )
)
DISABLE_FILE = Path(os.path.expanduser("~/.claude/memory/debate_wake.disable"))
SLEEP_FILE = Path(os.path.expanduser("~/.claude/memory/debate_wake.sleep_until"))
HEARTBEAT_FRESH_SECONDS = 90
# Machine-specific initial worker cap (task 0d806934): backlog waits, is not lost.
# action-kind explicitly includes PING (advocate BLOCK high-risk): the pump
# default omits it, and the governor's action_kinds intersection can only
# subtract — so without listing it here an explicit H/PING wake is dropped.
PUMP_ARGS = [
    "--max-concurrent-workers",
    "1",
    "--max-workers-per-scan",
    "1",
    "--mcp-prefix",
    "mcp__sqlite_unified__",
    "--action-kind",
    "Q",
    "--action-kind",
    "A",
    "--action-kind",
    "DECISION",
    "--action-kind",
    "STATE",
    "--action-kind",
    "PING",
    "--action-kind",
    "CLAIM",
    "--action-kind",
    "CHALLENGE",
    "--action-kind",
    "EVIDENCE",
    "--action-kind",
    "REBUT",
    "--action-kind",
    "CONCEDE",
    "--action-kind",
    "VERIFY",
    "--action-kind",
    "DISSENT",
    "--action-kind",
    "ESCALATE",
]


RUN_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_KEY_VALUE = TASK_NAME


def _pythonw() -> Path:
    candidate = Path(sys.executable).with_name("pythonw.exe")
    return candidate if candidate.is_file() else Path(sys.executable)


def _pump_command_line() -> str:
    return " ".join([f'"{_pythonw()}"', f'"{PUMP_SCRIPT}"', *PUMP_ARGS])


def _run_key_installed() -> bool:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH) as key:
            winreg.QueryValueEx(key, RUN_KEY_VALUE)
        return True
    except OSError:
        return False


def _install_run_key() -> dict[str, object]:
    """IT policy on managed machines can deny schtasks even for user tasks;
    the HKCU Run key is user-writable and gives the same at-logon start.
    MultipleInstances=IgnoreNew is enforced by the pump's own singleton
    guard; restart-on-failure is NOT available on this path (documented
    limitation — logon or ``debate_ops start`` restarts it)."""
    import winreg

    with winreg.OpenKey(
        winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0, winreg.KEY_SET_VALUE
    ) as key:
        winreg.SetValueEx(key, RUN_KEY_VALUE, 0, winreg.REG_SZ, _pump_command_line())
    return {
        "mechanism": "run_key",
        "value": RUN_KEY_VALUE,
        "command": _pump_command_line(),
    }


def _remove_run_key() -> bool:
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.DeleteValue(key, RUN_KEY_VALUE)
        return True
    except OSError:
        return False


def _spawn_pump_now() -> dict[str, object]:
    """Detached hidden pump start (the singleton guard makes this idempotent)."""
    proc = subprocess.Popen(
        [str(_pythonw()), str(PUMP_SCRIPT), *PUMP_ARGS],
        cwd=str(ROOT),
        creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return {"spawned_pid": proc.pid}


def _schtasks(args: list[str]) -> dict[str, object]:
    cmd = ["schtasks", *args]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    return {
        "cmd": cmd,
        "returncode": result.returncode,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
    }


def _task_xml() -> str:
    """Task Scheduler XML: the CLI cannot express IgnoreNew + RestartOnFailure."""
    pythonw = escape(str(_pythonw()))
    arguments = escape(" ".join([f'"{PUMP_SCRIPT}"', *PUMP_ARGS]))
    working_directory = escape(str(ROOT))
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>sqlite-memory debate pump: resident zero-paste wake delivery (task 0d806934)</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>true</Hidden>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>3</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{pythonw}</Command>
      <Arguments>{arguments}</Arguments>
      <WorkingDirectory>{working_directory}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def _read_heartbeat() -> dict[str, object] | None:
    try:
        data = json.loads(HEARTBEAT_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _heartbeat_pid_live(heartbeat: dict[str, object]) -> bool:
    try:
        import psutil

        pid = int(heartbeat.get("pid") or 0)
        expected = heartbeat.get("create_time")
        proc = psutil.Process(pid)
        if expected and abs(proc.create_time() - float(expected)) > 2.0:
            return False  # PID reuse — not our pump
        return True
    except Exception:
        return False


def _heartbeat_age_seconds(heartbeat: dict[str, object]) -> float | None:
    from datetime import datetime, timezone

    try:
        ts = datetime.fromisoformat(
            str(heartbeat.get("ts") or "").replace("Z", "+00:00")
        )
        return (datetime.now(timezone.utc) - ts).total_seconds()
    except Exception:
        return None


def pump_state() -> dict[str, object]:
    """running / stale / stopped from heartbeat + real process identity."""
    heartbeat = _read_heartbeat()
    if heartbeat is None:
        return {"state": "stopped", "heartbeat": None}
    age = _heartbeat_age_seconds(heartbeat)
    live = _heartbeat_pid_live(heartbeat)
    if live and age is not None and age <= HEARTBEAT_FRESH_SECONDS:
        state = "running"
    elif live:
        state = "stale"  # process exists but heartbeat is not advancing
    else:
        state = "stale" if age is not None else "stopped"
    return {
        "state": state,
        "heartbeat_age_seconds": age,
        "pid_live": live,
        "heartbeat": heartbeat,
    }


def cmd_install(*, start: bool = True) -> int:
    xml_path = ROOT / "systemd" / "user" / "SqliteMemoryDebatePump.xml"
    xml_path.parent.mkdir(parents=True, exist_ok=True)
    xml_path.write_text(_task_xml(), encoding="utf-16")
    payload: dict[str, object] = {
        "task": TASK_NAME,
        "xml": str(xml_path),
        "pythonw": str(_pythonw()),
        "pump_args": PUMP_ARGS,
        "actions": [],
    }
    actions = payload["actions"]
    assert isinstance(actions, list)
    created = _schtasks(["/Create", "/TN", TASK_NAME, "/XML", str(xml_path), "/F"])
    actions.append(created)
    if not created.get("returncode"):
        payload["mechanism"] = "scheduled_task"
        if start:
            actions.append(_schtasks(["/Run", "/TN", TASK_NAME]))
    else:
        # Managed-machine fallback: schtasks denied for standard users.
        actions.append(_install_run_key())
        payload["mechanism"] = "run_key"
        if start:
            actions.append(_spawn_pump_now())
    payload["pump_state"] = pump_state()
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    ok = payload["mechanism"] == "scheduled_task" and not any(
        item.get("returncode") for item in actions if "returncode" in item
    )
    return 0 if ok or payload["mechanism"] == "run_key" else 1


def cmd_start() -> int:
    out = _schtasks(["/Run", "/TN", TASK_NAME])
    payload: dict[str, object] = {"task": TASK_NAME, "run": out}
    if out.get("returncode"):
        payload["fallback"] = _spawn_pump_now()
        time.sleep(2.0)
    payload["pump_state"] = pump_state()
    print(json.dumps(payload, indent=2))
    return 0 if not out.get("returncode") or "fallback" in payload else 1


def cmd_stop(*, timeout_seconds: float = 20.0) -> int:
    """Graceful first: stop event → wait for exit; /End only as fallback.

    In-flight workers survive either path (own process group, no console).
    """
    graceful = False
    try:
        sys.path.insert(0, str(ROOT))
        from debate_wake_signal import signal_stop

        graceful = signal_stop()
    except Exception:
        graceful = False
    deadline = time.monotonic() + max(1.0, timeout_seconds)
    while time.monotonic() < deadline:
        state = pump_state()
        if not state.get("pid_live"):
            print(
                json.dumps(
                    {"task": TASK_NAME, "stopped": "graceful", "pump_state": state},
                    indent=2,
                )
            )
            return 0
        time.sleep(1.0)
    ended = _schtasks(["/End", "/TN", TASK_NAME])
    print(
        json.dumps(
            {
                "task": TASK_NAME,
                "stopped": "forced" if not ended.get("returncode") else "failed",
                "graceful_signal_sent": graceful,
                "end": ended,
                "pump_state": pump_state(),
            },
            indent=2,
        )
    )
    return int(bool(ended.get("returncode")))


def cmd_status() -> int:
    query = _schtasks(["/Query", "/TN", TASK_NAME, "/FO", "LIST", "/V"])
    state = pump_state()
    task_registered = not query.get("returncode")
    run_key = _run_key_installed()
    print(
        json.dumps(
            {
                "task": TASK_NAME,
                "task_registered": task_registered,
                "run_key_installed": run_key,
                "autostart_mechanism": "scheduled_task"
                if task_registered
                else ("run_key" if run_key else "none"),
                "pump_state": state,
                "kill_switches": {
                    "disable_file": DISABLE_FILE.exists(),
                    "sleep_until_file": SLEEP_FILE.exists(),
                },
                "schtasks": query,
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    autostart_ok = task_registered or run_key
    return 0 if autostart_ok and state.get("state") == "running" else 1


def cmd_uninstall() -> int:
    cmd_stop()
    out = _schtasks(["/Delete", "/TN", TASK_NAME, "/F"])
    run_key_removed = _remove_run_key()
    print(
        json.dumps(
            {"task": TASK_NAME, "delete": out, "run_key_removed": run_key_removed},
            indent=2,
        )
    )
    return 0 if run_key_removed or not out.get("returncode") else 1


def cmd_doctor() -> int:
    """Windows debate runtime doctor: adapters, task, heartbeat, switches."""
    checks: dict[str, object] = {}
    try:
        import psutil

        checks["psutil"] = {"ok": True, "version": psutil.__version__}
    except ImportError:
        checks["psutil"] = {"ok": False, "hint": "pip install psutil"}
    checks["pythonw"] = {
        "ok": _pythonw().name == "pythonw.exe",
        "path": str(_pythonw()),
    }
    try:
        sys.path.insert(0, str(ROOT))
        from debate_wake_signal import is_supported, signal_wake

        checks["wake_event"] = {"ok": is_supported() and signal_wake()}
    except Exception as exc:
        checks["wake_event"] = {"ok": False, "error": repr(exc)}
    try:
        sys.path.insert(0, str(ROOT / "hooks"))
        from debate_resource_budget import current_debate_resource_budget

        budget = current_debate_resource_budget()
        checks["resource_budget"] = {
            "ok": budget.snapshot.mem_total_mib > 0,
            "tier": budget.tier,
            "mem_total_mib": budget.snapshot.mem_total_mib,
            "live_agent_count": budget.snapshot.live_agent_count,
        }
    except Exception as exc:
        checks["resource_budget"] = {"ok": False, "error": repr(exc)}
    query = _schtasks(["/Query", "/TN", TASK_NAME])
    task_registered = not query.get("returncode")
    run_key = _run_key_installed()
    state = pump_state()
    # Autostart is satisfied by EITHER a Scheduled Task OR the HKCU Run key
    # (managed machines deny schtasks). doctor treats autostart + a running
    # pump as MANDATORY (advocate BLOCK high-risk #2: pump/task were advisory).
    checks["autostart"] = {
        "ok": task_registered or run_key,
        "mechanism": "scheduled_task"
        if task_registered
        else ("run_key" if run_key else "none"),
    }
    checks["pump_running"] = {"ok": state.get("state") == "running", **state}
    checks["kill_switches"] = {
        "disable_file": DISABLE_FILE.exists(),
        "sleep_until_file": SLEEP_FILE.exists(),
    }
    hard_keys = (
        "psutil",
        "pythonw",
        "wake_event",
        "resource_budget",
        "autostart",
        "pump_running",
    )
    ok = all(
        bool(checks[key].get("ok"))
        for key in hard_keys
        if isinstance(checks[key], dict)
    )
    print(json.dumps({"ok": ok, "checks": checks}, indent=2, ensure_ascii=False))
    return 0 if ok else 1
