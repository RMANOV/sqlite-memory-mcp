import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def _load_hook_module(name: str, rel_path: str):
    spec = spec_from_file_location(name, ROOT / rel_path)
    module = module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_debate_wake_agent_budget_caps_successful_launches(monkeypatch):
    module = _load_hook_module("debate_wake_budget_test", "hooks/debate_wake.py")
    monkeypatch.setenv("DEBATE_WAKE_ACTION", "agent")
    monkeypatch.setenv("DEBATE_WAKE_BUDGET", "1")
    monkeypatch.setenv("DEBATE_RESOURCE_BUDGET", "off")
    monkeypatch.setenv("DEBATE_WAKE_DISABLE_FILE", "/tmp/sqlite-memory-test-no-disable")
    monkeypatch.setattr(
        module, "_mark_source_wake_result", lambda *args, **kwargs: None
    )
    launches = []

    def fake_launch(target, trigger_msg_id, topic_id):
        launches.append((target["target_session_id"], trigger_msg_id, topic_id))
        return {"launched": True, "pid": 1000 + len(launches)}

    monkeypatch.setattr(module, "_launch_agent", fake_launch)
    out = module._maybe_dispatch(
        {
            "msg_id": "abc123",
            "topic_id": "T",
            "schema_version": "debate_post_with_recipients.v1",
        },
        {
            "targets": [
                {
                    "recipient": "EXECUTOR",
                    "target_role": "EXECUTOR",
                    "target_session_id": "codex-exec1",
                    "target_runtime": "codex",
                    "result": "dry_run",
                },
                {
                    "recipient": "ADVOCATE",
                    "target_role": "ADVOCATE",
                    "target_session_id": "cc-adv1",
                    "target_runtime": "cc",
                    "result": "dry_run",
                },
            ]
        },
    )

    assert len(out["launches"]) == 1
    assert launches == [("codex-exec1", "abc123", "T")]


def test_debate_wake_accepts_claude_runtime_alias():
    module = _load_hook_module("debate_wake_claude_alias_test", "hooks/debate_wake.py")

    cmd = module._agent_command(
        {"target_runtime": "claude"},
        trigger_msg_id="abc123",
        topic_id="T",
    )

    assert cmd is not None
    assert cmd[0] == "claude"


def test_debate_wake_accepts_post_tool_under_any_mcp_prefix():
    module = _load_hook_module("debate_wake_prefix_test", "hooks/debate_wake.py")

    assert module._is_target_tool("mcp__sqlite_unified__debate_post_with_recipients")
    assert module._is_target_tool("mcp__sqlite_intel__debate_post_with_recipients")
    assert not module._is_target_tool("mcp__sqlite_unified__debate_post")


def test_resource_budget_blocks_supercritical_heat_and_large_agent_set():
    module = _load_hook_module(
        "debate_resource_budget_blocked_test", "hooks/debate_resource_budget.py"
    )

    budget = module.compute_debate_resource_budget(
        module.ResourceSnapshot(
            mem_total_mib=32768,
            mem_available_mib=16000,
            swap_free_mib=4096,
            cpu_count=8,
            load1=2,
            memory_full_avg10=0,
            max_temp_c=106,
            live_agent_count=46,
        )
    )

    assert budget.allow_agent is False
    assert budget.wake_budget == 0
    assert budget.tier == "blocked"
    assert "temperature_critical" in budget.reason


def test_resource_budget_treats_single_hot_core_sample_as_soft_signal():
    module = _load_hook_module(
        "debate_resource_budget_hot_spike_test", "hooks/debate_resource_budget.py"
    )

    budget = module.compute_debate_resource_budget(
        module.ResourceSnapshot(
            mem_total_mib=32768,
            mem_available_mib=18000,
            swap_total_mib=4096,
            swap_free_mib=4096,
            cpu_count=8,
            load1=2,
            memory_full_avg10=0,
            max_temp_c=96,
            live_agent_count=1,
        )
    )

    assert budget.allow_agent is True
    assert budget.tier == "low"
    assert "temperature_spike_96c" in budget.reason


def test_resource_budget_operator_sleep_blocks_until_timestamp(tmp_path):
    module = _load_hook_module(
        "debate_resource_budget_sleep_test", "hooks/debate_resource_budget.py"
    )
    path = tmp_path / "sleep_until"
    now = datetime(2026, 5, 24, 9, 0, tzinfo=timezone.utc)
    path.write_text(
        '{"until":"2026-05-24T12:15:00Z","reason":"operator rest"}\n',
        encoding="utf-8",
    )
    budget = module.compute_debate_resource_budget(
        module.ResourceSnapshot(
            mem_total_mib=32768,
            mem_available_mib=18000,
            swap_total_mib=4096,
            swap_free_mib=4096,
            cpu_count=8,
            load1=2,
            memory_full_avg10=0,
            max_temp_c=64,
            live_agent_count=1,
        )
    )

    sleeping = module.apply_operator_sleep(budget, path=path, now=now)
    assert sleeping.allow_agent is False
    assert sleeping.tier == "sleep"
    assert sleeping.wake_budget == 0
    assert sleeping.reason == "operator_sleep_until_2026-05-24T12:15:00Z"

    expired = module.apply_operator_sleep(
        budget,
        path=path,
        now=now + timedelta(hours=4),
    )
    assert expired.allow_agent is True
    assert not path.exists()


def test_resource_budget_allows_small_budget_on_healthy_machine():
    module = _load_hook_module(
        "debate_resource_budget_healthy_test", "hooks/debate_resource_budget.py"
    )

    budget = module.compute_debate_resource_budget(
        module.ResourceSnapshot(
            mem_total_mib=32768,
            mem_available_mib=18000,
            swap_free_mib=6000,
            cpu_count=8,
            load1=4,
            memory_full_avg10=0,
            max_temp_c=55,
            live_agent_count=1,
        )
    )

    assert budget.allow_agent is True
    assert budget.wake_budget == 2
    assert budget.max_concurrent_workers == 2
    assert budget.tier == "normal"


def test_resource_budget_treats_unknown_temperature_as_soft_signal():
    module = _load_hook_module(
        "debate_resource_budget_unknown_temp_test", "hooks/debate_resource_budget.py"
    )

    budget = module.compute_debate_resource_budget(
        module.ResourceSnapshot(
            mem_total_mib=32768,
            mem_available_mib=18000,
            swap_total_mib=0,
            swap_free_mib=0,
            cpu_count=8,
            load1=2,
            memory_full_avg10=0,
            max_temp_c=None,
            live_agent_count=1,
        )
    )

    assert budget.allow_agent is True
    assert budget.tier == "guarded"
    assert budget.max_concurrent_workers == 2
    assert budget.reason == "temperature_unknown"


def test_resource_budget_does_not_require_swap_on_no_swap_host():
    module = _load_hook_module(
        "debate_resource_budget_no_swap_test", "hooks/debate_resource_budget.py"
    )

    budget = module.compute_debate_resource_budget(
        module.ResourceSnapshot(
            mem_total_mib=32768,
            mem_available_mib=18000,
            swap_total_mib=0,
            swap_free_mib=0,
            cpu_count=8,
            load1=2,
            memory_full_avg10=0,
            max_temp_c=55,
            live_agent_count=1,
        )
    )

    assert budget.allow_agent is True
    assert "swap_free" not in budget.reason


def test_resource_budget_low_memory_machine_uses_relative_headroom():
    module = _load_hook_module(
        "debate_resource_budget_small_machine_test", "hooks/debate_resource_budget.py"
    )

    budget = module.compute_debate_resource_budget(
        module.ResourceSnapshot(
            mem_total_mib=4096,
            mem_available_mib=1024,
            swap_total_mib=0,
            swap_free_mib=0,
            cpu_count=2,
            load1=1,
            memory_full_avg10=0,
            max_temp_c=55,
            live_agent_count=1,
        )
    )

    assert budget.allow_agent is True
    assert budget.tier in {"guarded", "normal"}


def test_resource_budget_recovery_hysteresis_requires_repeated_healthy_samples(
    tmp_path,
):
    module = _load_hook_module(
        "debate_resource_budget_hysteresis_test", "hooks/debate_resource_budget.py"
    )
    path = tmp_path / "state.json"
    spike = module.compute_debate_resource_budget(
        module.ResourceSnapshot(
            mem_total_mib=32768,
            mem_available_mib=18000,
            swap_total_mib=4096,
            swap_free_mib=4096,
            cpu_count=8,
            load1=2,
            memory_full_avg10=0,
            max_temp_c=96,
            live_agent_count=1,
        )
    )
    healthy = module.compute_debate_resource_budget(
        module.ResourceSnapshot(
            mem_total_mib=32768,
            mem_available_mib=18000,
            swap_total_mib=4096,
            swap_free_mib=4096,
            cpu_count=8,
            load1=2,
            memory_full_avg10=0,
            max_temp_c=55,
            live_agent_count=1,
        )
    )

    for _ in range(4):
        assert (
            module.apply_recovery_hysteresis(spike, state_path=path).allow_agent is True
        )
    sustained = module.apply_recovery_hysteresis(spike, state_path=path)
    assert sustained.allow_agent is False
    assert sustained.reason == "sustained_temperature_ewma_96c"
    path.unlink()
    supercritical = module.compute_debate_resource_budget(
        module.ResourceSnapshot(
            mem_total_mib=32768,
            mem_available_mib=18000,
            swap_total_mib=4096,
            swap_free_mib=4096,
            cpu_count=8,
            load1=2,
            memory_full_avg10=0,
            max_temp_c=106,
            live_agent_count=1,
        )
    )
    assert (
        module.apply_recovery_hysteresis(supercritical, state_path=path).allow_agent
        is False
    )
    path.unlink()
    first_spike = module.apply_recovery_hysteresis(spike, state_path=path)
    assert first_spike.allow_agent is True
    first = module.apply_recovery_hysteresis(healthy, state_path=path)
    second = module.apply_recovery_hysteresis(healthy, state_path=path)
    third = module.apply_recovery_hysteresis(healthy, state_path=path)

    assert first.allow_agent is True
    assert second.allow_agent is True
    assert third.allow_agent is True


def test_resource_budget_recovery_hysteresis_allows_sustained_mid_90s(tmp_path):
    module = _load_hook_module(
        "debate_resource_budget_mid_90s_test", "hooks/debate_resource_budget.py"
    )
    path = tmp_path / "state.json"
    hot_but_workable = module.compute_debate_resource_budget(
        module.ResourceSnapshot(
            mem_total_mib=32768,
            mem_available_mib=18000,
            swap_total_mib=4096,
            swap_free_mib=4096,
            cpu_count=8,
            load1=2,
            memory_full_avg10=0,
            max_temp_c=95,
            live_agent_count=1,
        )
    )

    for _ in range(6):
        budget = module.apply_recovery_hysteresis(hot_but_workable, state_path=path)

    assert budget.allow_agent is True
    assert budget.wake_budget == 1


def test_resource_budget_live_agent_count_ignores_sqlite_memory_sidecars(
    monkeypatch, tmp_path
):
    module = _load_hook_module(
        "debate_resource_budget_count_test", "hooks/debate_resource_budget.py"
    )
    proc = tmp_path / "proc"
    proc.mkdir()
    commands = {
        "100": "python3 /home/rmanov/.local/bin/sqlite-memory-intel",
        "101": "python3 /home/rmanov/.local/bin/sqlite-memory-tasks",
        "102": "node /home/rmanov/.npm-global/bin/codex",
        "103": "/home/rmanov/.npm-global/lib/node_modules/@openai/codex/vendor/codex",
        "104": "claude",
        "105": "/home/rmanov/.local/share/claude/versions/2.1.150 --chrome-native-host",
        "106": "python3 /home/rmanov/.claude/mcp_servers/maintenance.py",
    }
    for pid, cmd in commands.items():
        d = proc / pid
        d.mkdir()
        (d / "cmdline").write_bytes(cmd.replace(" ", "\0").encode())

    assert module._count_live_agents(proc) == 2


def test_debate_pump_resource_cap_does_not_ratchet_base_budget():
    module = _load_hook_module(
        "debate_pump_budget_ratchet_test", "hooks/debate_pump.py"
    )

    assert module._clamp_wake_budget(3, 1) == 1
    assert module._clamp_wake_budget(3, 3) == 3


def test_debate_pump_live_worker_census_failure_is_nonfatal(monkeypatch, tmp_path):
    module = _load_hook_module(
        "debate_pump_census_failure_test", "hooks/debate_pump.py"
    )
    module.LOG_PATH = tmp_path / "pump.jsonl"
    module.CHILDREN.clear()
    monkeypatch.setattr(
        module,
        "_machine_live_worker_count",
        lambda _topics: (_ for _ in ()).throw(sqlite3.OperationalError("locked")),
    )

    assert module._safe_machine_live_worker_count([]) == 0
    assert "machine_live_worker_count_failed" in module.LOG_PATH.read_text(
        encoding="utf-8"
    )


def test_debate_pump_reads_operator_disable_before_scan(monkeypatch, tmp_path):
    module = _load_hook_module(
        "debate_pump_operator_disable_test", "hooks/debate_pump.py"
    )
    disable_file = tmp_path / "debate_wake.disable"
    monkeypatch.setenv("DEBATE_WAKE_DISABLE_FILE", str(disable_file))

    assert module._operator_wake_disabled() is False
    disable_file.write_text("disabled", encoding="utf-8")
    assert module._operator_wake_disabled() is True


def test_debate_pump_sets_default_wake_budget_from_worker_limits(monkeypatch, tmp_path):
    module = _load_hook_module(
        "debate_pump_budget_default_test", "hooks/debate_pump.py"
    )
    module.LOG_PATH = tmp_path / "pump.jsonl"
    module.STATE_PATH = tmp_path / "pump_state.json"
    module.STOP = False
    rows = [{"msg_id": "m1", "topic_id": "T", "ts": "2026-05-20T00:00:01Z"}]
    seen_budget = []

    monkeypatch.setenv("DEBATE_RESOURCE_BUDGET", "off")
    monkeypatch.delenv("DEBATE_WAKE_BUDGET", raising=False)
    monkeypatch.setattr(module, "_fetch_new", lambda *args, **kwargs: rows)
    monkeypatch.setattr(module, "_fetch_pending_deliveries", lambda *args: [])
    monkeypatch.setattr(module, "_estimate_worker_demand", lambda *args, **kwargs: 1)
    # Synthetic rows are not inserted into DB_PATH; the terminal gate would
    # otherwise treat the unknown msg as terminal and skip dispatch. This
    # unit test exercises the dispatch path, so force not-terminal.
    monkeypatch.setattr(module, "_trigger_is_terminal", lambda *args, **kwargs: False)
    monkeypatch.setattr(module, "_reap_children", lambda: None)
    # Unit isolation: a real hidden worker may be alive on the Windows host
    # while this test runs.  The scenario models an empty worker pool, so do
    # not let machine-wide production state throttle the fake dispatch.
    monkeypatch.setattr(module, "_machine_live_worker_count", lambda *args: 0)
    monkeypatch.setattr(module, "_reclaim_stale_message_claims", lambda **kwargs: None)
    monkeypatch.setattr(module, "_save_state", lambda ts, msg_id: None)

    def capture_dispatch(row, suppressed_roles):
        seen_budget.append(os.environ.get("DEBATE_WAKE_BUDGET"))
        return 0

    monkeypatch.setattr(module, "_dispatch_row", capture_dispatch)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "debate_pump.py",
            "--once",
            "--since",
            "2026-05-20T00:00:00Z",
            "--max-workers-per-scan",
            "3",
            "--max-concurrent-workers",
            "2",
            "--message-claim-reclaim-seconds",
            "0",
        ],
    )

    assert module.main() == 0
    assert seen_budget == ["3"]


def test_debate_pump_treats_implementation_as_zero_bounded_worker_demand(
    monkeypatch, tmp_path
):
    module = _load_hook_module(
        "debate_pump_implementation_demand_test", "hooks/debate_pump.py"
    )
    module.DB_PATH = tmp_path / "memory.db"
    monkeypatch.setenv("DEBATE_WAKE_ACTION_NAME", "post_tool_use_wake")

    con = sqlite3.connect(module.DB_PATH)
    try:
        con.executescript(
            """
            CREATE TABLE debate_messages (
                msg_id TEXT PRIMARY KEY,
                topic_id TEXT NOT NULL,
                vehicle TEXT
            );
            CREATE TABLE debate_message_recipients (
                msg_id TEXT NOT NULL,
                recipient TEXT NOT NULL,
                recipient_mode TEXT NOT NULL
            );
            CREATE TABLE debate_role_bindings (
                topic_id TEXT NOT NULL,
                role TEXT NOT NULL,
                session_id TEXT NOT NULL,
                state TEXT NOT NULL,
                generation INTEGER NOT NULL
            );
            CREATE TABLE debate_wake_log (
                trigger_msg_id TEXT NOT NULL,
                target_session_id TEXT,
                action TEXT NOT NULL,
                result TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        con.execute(
            "INSERT INTO debate_messages VALUES (?, ?, ?)",
            ("impl-q", "T", "implementation"),
        )
        con.execute(
            "INSERT INTO debate_message_recipients VALUES (?, ?, ?)",
            ("impl-q", "EXECUTOR", "normal"),
        )
        con.execute(
            "INSERT INTO debate_role_bindings VALUES (?, ?, ?, ?, ?)",
            ("T", "EXECUTOR", "codex-executor", "active", 1),
        )
        # The fail-closed resolver writes a message-level refusal with no
        # target_session_id.  The pump must still regard the message as
        # terminal for bounded-worker demand.
        con.execute(
            "INSERT INTO debate_wake_log VALUES (?, ?, ?, ?, ?)",
            (
                "impl-q",
                None,
                "post_tool_use_wake",
                "implementation_requires_impl_vehicle",
                "2026-07-11T00:00:00Z",
            ),
        )
        con.commit()
    finally:
        con.close()

    assert module._estimate_worker_demand("impl-q", set()) == 0


def test_debate_pump_does_not_advance_cursor_after_dispatch_exception(
    monkeypatch, tmp_path
):
    module = _load_hook_module("debate_pump_cursor_test", "hooks/debate_pump.py")
    module.LOG_PATH = tmp_path / "pump.jsonl"
    module.STATE_PATH = tmp_path / "pump_state.json"
    module.STOP = False
    rows = [
        {"msg_id": "m1", "topic_id": "T", "ts": "2026-05-20T00:00:01Z"},
        {"msg_id": "m2", "topic_id": "T", "ts": "2026-05-20T00:00:02Z"},
    ]
    dispatches = []
    saved = []

    monkeypatch.setenv("DEBATE_RESOURCE_BUDGET", "off")
    monkeypatch.setattr(module, "_fetch_new", lambda *args, **kwargs: rows)
    monkeypatch.setattr(module, "_fetch_pending_deliveries", lambda *args: [])
    monkeypatch.setattr(module, "_estimate_worker_demand", lambda *args, **kwargs: 1)
    monkeypatch.setattr(module, "_trigger_is_terminal", lambda *args, **kwargs: False)
    monkeypatch.setattr(module, "_reap_children", lambda: None)
    monkeypatch.setattr(module, "_machine_live_worker_count", lambda *args: 0)
    monkeypatch.setattr(module, "_reclaim_stale_message_claims", lambda **kwargs: None)
    monkeypatch.setattr(
        module, "_save_state", lambda ts, msg_id: saved.append((ts, msg_id))
    )

    def fail_dispatch(row, suppressed_roles):
        dispatches.append(row["msg_id"])
        raise RuntimeError("launch failed")

    monkeypatch.setattr(module, "_dispatch_row", fail_dispatch)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "debate_pump.py",
            "--once",
            "--since",
            "2026-05-20T00:00:00Z",
            "--max-workers-per-scan",
            "0",
            "--max-concurrent-workers",
            "0",
            "--message-claim-reclaim-seconds",
            "0",
        ],
    )

    assert module.main() == 0
    assert dispatches == ["m1"]
    assert saved == []


def test_debate_pump_allows_partial_dispatch_when_message_has_more_targets_than_scan_budget():
    module = _load_hook_module(
        "debate_pump_partial_throttle_test", "hooks/debate_pump.py"
    )

    assert (
        module._throttle_reason(
            estimated_worker_demand=3,
            launched_this_scan=0,
            live_children=0,
            max_workers_per_scan=1,
            max_concurrent_workers=1,
        )
        is None
    )
    assert (
        module._throttle_reason(
            estimated_worker_demand=1,
            launched_this_scan=1,
            live_children=0,
            max_workers_per_scan=1,
            max_concurrent_workers=1,
        )
        == "max_workers_per_scan"
    )


def test_debate_pump_keeps_cursor_on_partially_dispatched_multi_recipient_message(
    monkeypatch, tmp_path
):
    module = _load_hook_module(
        "debate_pump_partial_cursor_test", "hooks/debate_pump.py"
    )
    module.LOG_PATH = tmp_path / "pump.jsonl"
    module.STATE_PATH = tmp_path / "pump_state.json"
    module.STOP = False
    rows = [{"msg_id": "m1", "topic_id": "T", "ts": "2026-05-20T00:00:01Z"}]
    dispatches = []
    saved = []

    monkeypatch.setenv("DEBATE_RESOURCE_BUDGET", "off")
    monkeypatch.setattr(module, "_fetch_new", lambda *args, **kwargs: rows)
    monkeypatch.setattr(module, "_fetch_pending_deliveries", lambda *args: [])
    monkeypatch.setattr(module, "_estimate_worker_demand", lambda *args, **kwargs: 3)
    # Not terminal → after dispatching, the just-dispatched (in-flight)
    # trigger holds the cursor until a later scan proves it terminal.
    monkeypatch.setattr(module, "_trigger_is_terminal", lambda *args, **kwargs: False)
    monkeypatch.setattr(module, "_reap_children", lambda: None)
    monkeypatch.setattr(module, "_machine_live_worker_count", lambda *args: 0)
    monkeypatch.setattr(module, "_reclaim_stale_message_claims", lambda **kwargs: None)
    monkeypatch.setattr(
        module, "_save_state", lambda ts, msg_id: saved.append((ts, msg_id))
    )

    def dispatch_one(row, suppressed_roles):
        dispatches.append(row["msg_id"])
        return 1

    monkeypatch.setattr(module, "_dispatch_row", dispatch_one)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "debate_pump.py",
            "--once",
            "--since",
            "2026-05-20T00:00:00Z",
            "--max-workers-per-scan",
            "1",
            "--max-concurrent-workers",
            "1",
            "--message-claim-reclaim-seconds",
            "0",
        ],
    )

    assert module.main() == 0
    assert dispatches == ["m1"]
    assert saved == []
    assert "pump_dispatched_hold_cursor" in module.LOG_PATH.read_text(encoding="utf-8")


def test_debate_pump_advances_cursor_after_last_recipient_is_handled(
    monkeypatch, tmp_path
):
    module = _load_hook_module(
        "debate_pump_complete_cursor_test", "hooks/debate_pump.py"
    )
    module.LOG_PATH = tmp_path / "pump.jsonl"
    module.STATE_PATH = tmp_path / "pump_state.json"
    module.STOP = False
    rows = [{"msg_id": "m1", "topic_id": "T", "ts": "2026-05-20T00:00:01Z"}]
    saved = []

    monkeypatch.setenv("DEBATE_RESOURCE_BUDGET", "off")
    monkeypatch.setattr(module, "_fetch_new", lambda *args, **kwargs: rows)
    monkeypatch.setattr(module, "_fetch_pending_deliveries", lambda *args: [])
    monkeypatch.setattr(module, "_estimate_worker_demand", lambda *args, **kwargs: 0)
    # Last recipient handled → the trigger is terminal → cursor advances
    # past it (new contract: advance only on a proven-terminal trigger).
    monkeypatch.setattr(module, "_trigger_is_terminal", lambda *args, **kwargs: True)
    monkeypatch.setattr(module, "_reap_children", lambda: None)
    monkeypatch.setattr(module, "_reclaim_stale_message_claims", lambda **kwargs: None)
    monkeypatch.setattr(
        module, "_save_state", lambda ts, msg_id: saved.append((ts, msg_id))
    )
    monkeypatch.setattr(module, "_dispatch_row", lambda row, suppressed_roles: 1)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "debate_pump.py",
            "--once",
            "--since",
            "2026-05-20T00:00:00Z",
            "--max-workers-per-scan",
            "1",
            "--max-concurrent-workers",
            "1",
            "--message-claim-reclaim-seconds",
            "0",
        ],
    )

    assert module.main() == 0
    assert saved == [("2026-05-20T00:00:01Z", "m1")]


def test_targeted_event_dispatch_skips_older_inflight_trigger(monkeypatch, tmp_path):
    module = _load_hook_module(
        "debate_pump_targeted_no_hol_test", "hooks/debate_pump.py"
    )
    module.LOG_PATH = tmp_path / "pump.jsonl"
    module.STATE_PATH = tmp_path / "pump_state.json"
    module.STOP = False
    rows = [
        {"msg_id": "old", "topic_id": "T", "ts": "2026-07-22T10:00:00Z"},
        {"msg_id": "new", "topic_id": "T", "ts": "2026-07-22T10:00:01Z"},
    ]
    dispatches = []

    monkeypatch.setenv("DEBATE_RESOURCE_BUDGET", "off")
    monkeypatch.setattr(module, "_fetch_pending_deliveries", lambda *args: rows)
    monkeypatch.setattr(module, "_fetch_new", lambda *args: [])
    monkeypatch.setattr(module, "_fetch_released_blind_replay", lambda *args: [])
    monkeypatch.setattr(module, "_trigger_is_terminal", lambda *args: False)
    monkeypatch.setattr(
        module,
        "_estimate_worker_demand",
        lambda msg_id, _suppressed: 0 if msg_id == "old" else 1,
    )
    monkeypatch.setattr(module, "_reap_children", lambda: None)
    monkeypatch.setattr(module, "_machine_live_worker_count", lambda *args: 0)
    monkeypatch.setattr(module, "_reclaim_stale_message_claims", lambda **kwargs: None)
    monkeypatch.setattr(
        module,
        "_protocol_maintenance",
        lambda *args: {"timed_out": [], "role_recoveries": []},
    )
    monkeypatch.setattr(module, "_save_state", lambda *args: None)
    monkeypatch.setattr(
        module,
        "_dispatch_row",
        lambda row, _suppressed: dispatches.append(row["msg_id"]) or 1,
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "debate_pump.py",
            "--once",
            "--since",
            "2026-07-22T09:59:00Z",
            "--max-workers-per-scan",
            "2",
            "--max-concurrent-workers",
            "2",
            "--message-claim-reclaim-seconds",
            "0",
        ],
    )

    assert module.main() == 0
    assert dispatches == ["new"]


def test_pending_delivery_queue_is_durable_and_cursor_independent(tmp_path):
    module = _load_hook_module(
        "debate_pump_targeted_queue_test", "hooks/debate_pump.py"
    )
    module.DB_PATH = tmp_path / "memory.db"
    con = sqlite3.connect(module.DB_PATH)
    try:
        con.executescript(
            """
            CREATE TABLE debates(topic_id TEXT PRIMARY KEY, state TEXT NOT NULL);
            CREATE TABLE debate_messages(
                msg_id TEXT PRIMARY KEY,
                topic_id TEXT NOT NULL,
                ts TEXT NOT NULL,
                priority TEXT NOT NULL,
                kind TEXT NOT NULL
            );
            CREATE TABLE debate_message_recipients(
                msg_id TEXT NOT NULL,
                recipient TEXT NOT NULL,
                recipient_mode TEXT NOT NULL,
                PRIMARY KEY(msg_id, recipient)
            );
            CREATE TABLE debate_delivery_queue(
                msg_id TEXT NOT NULL,
                recipient TEXT NOT NULL,
                enqueued_at TEXT NOT NULL,
                completed_at TEXT,
                PRIMARY KEY(msg_id, recipient)
            );
            INSERT INTO debates VALUES('T','ACTIVE');
            INSERT INTO debate_messages VALUES(
                'old','T','2026-07-22T10:00:00Z','H','Q'
            );
            INSERT INTO debate_messages VALUES(
                'new','T','2026-07-22T10:00:01Z','H','Q'
            );
            INSERT INTO debate_messages VALUES(
                'legacy','T','2026-07-22T10:00:02Z','H','Q'
            );
            INSERT INTO debate_message_recipients VALUES('old','EXECUTOR_1','normal');
            INSERT INTO debate_message_recipients VALUES('new','EXECUTOR_2','normal');
            INSERT INTO debate_message_recipients VALUES('legacy','ADVOCATE','normal');
            INSERT INTO debate_delivery_queue VALUES(
                'old','EXECUTOR_1','2026-07-22T10:00:00Z',NULL
            );
            INSERT INTO debate_delivery_queue VALUES(
                'new','EXECUTOR_2','2026-07-22T10:00:01Z',NULL
            );
            """
        )
        con.commit()
    finally:
        con.close()

    queued = module._fetch_pending_deliveries([], ["Q"], 1)
    assert [row["msg_id"] for row in queued] == ["new"]
    assert module._complete_pending_deliveries("new") == 1
    queued_after_ack = module._fetch_pending_deliveries([], ["Q"], 10)
    assert [row["msg_id"] for row in queued_after_ack] == ["old", "legacy"]
    assert (
        module._complete_pending_deliveries(
            "legacy", enqueued_at="2026-07-22T10:00:02Z"
        )
        == 1
    )
    queued_after_legacy_ack = module._fetch_pending_deliveries([], ["Q"], 10)
    assert [row["msg_id"] for row in queued_after_legacy_ack] == ["old"]


def test_pending_delivery_budget_is_hard_and_reserves_oldest_fairness(tmp_path):
    module = _load_hook_module(
        "debate_pump_targeted_budget_test", "hooks/debate_pump.py"
    )
    module.DB_PATH = tmp_path / "memory.db"
    con = sqlite3.connect(module.DB_PATH)
    try:
        con.executescript(
            """
            CREATE TABLE debates(topic_id TEXT PRIMARY KEY, state TEXT NOT NULL);
            CREATE TABLE debate_messages(
                msg_id TEXT PRIMARY KEY, topic_id TEXT NOT NULL, ts TEXT NOT NULL,
                priority TEXT NOT NULL, kind TEXT NOT NULL
            );
            CREATE TABLE debate_message_recipients(
                msg_id TEXT NOT NULL, recipient TEXT NOT NULL,
                recipient_mode TEXT NOT NULL, PRIMARY KEY(msg_id, recipient)
            );
            CREATE TABLE debate_delivery_queue(
                msg_id TEXT NOT NULL, recipient TEXT NOT NULL,
                enqueued_at TEXT NOT NULL, completed_at TEXT,
                PRIMARY KEY(msg_id, recipient)
            );
            INSERT INTO debates VALUES('T','ACTIVE');
            """
        )
        for index in range(8):
            msg_id = f"m{index}"
            ts = f"2026-07-22T10:00:0{index}Z"
            con.execute(
                "INSERT INTO debate_messages VALUES(?, 'T', ?, 'H', 'Q')",
                (msg_id, ts),
            )
            con.execute(
                "INSERT INTO debate_message_recipients VALUES(?, 'EXECUTOR_1', 'normal')",
                (msg_id,),
            )
            con.execute(
                "INSERT INTO debate_delivery_queue VALUES(?, 'EXECUTOR_1', ?, NULL)",
                (msg_id, ts),
            )
        con.commit()
    finally:
        con.close()

    queued = module._fetch_pending_deliveries([], ["Q"], 2)
    assert len(queued) == 2
    assert [row["msg_id"] for row in queued] == ["m7", "m0"]


def test_pending_delivery_compat_never_replays_pre_queue_history(tmp_path):
    module = _load_hook_module(
        "debate_pump_targeted_history_test", "hooks/debate_pump.py"
    )
    module.DB_PATH = tmp_path / "memory.db"
    con = sqlite3.connect(module.DB_PATH)
    try:
        con.executescript(
            """
            CREATE TABLE debates(topic_id TEXT PRIMARY KEY, state TEXT NOT NULL);
            CREATE TABLE debate_messages(
                msg_id TEXT PRIMARY KEY, topic_id TEXT NOT NULL, ts TEXT NOT NULL,
                priority TEXT NOT NULL, kind TEXT NOT NULL
            );
            CREATE TABLE debate_message_recipients(
                msg_id TEXT NOT NULL, recipient TEXT NOT NULL,
                recipient_mode TEXT NOT NULL, PRIMARY KEY(msg_id, recipient)
            );
            CREATE TABLE debate_delivery_queue(
                msg_id TEXT NOT NULL, recipient TEXT NOT NULL,
                enqueued_at TEXT NOT NULL, completed_at TEXT,
                PRIMARY KEY(msg_id, recipient)
            );
            INSERT INTO debates VALUES('T','ACTIVE');
            INSERT INTO debate_messages VALUES(
                'historical','T','2026-07-01T10:00:00Z','H','Q'
            );
            INSERT INTO debate_message_recipients
            VALUES('historical','EXECUTOR_1','normal');
            """
        )
        con.commit()
    finally:
        con.close()

    assert module._fetch_pending_deliveries([], ["Q"], 10) == []

    con = sqlite3.connect(module.DB_PATH)
    try:
        con.executescript(
            """
            INSERT INTO debate_messages VALUES(
                'activation','T','2026-07-22T10:00:00Z','H','Q'
            );
            INSERT INTO debate_message_recipients
            VALUES('activation','EXECUTOR_1','normal');
            INSERT INTO debate_delivery_queue VALUES(
                'activation','EXECUTOR_1','2026-07-22T10:00:00Z',
                '2026-07-22T10:00:01Z'
            );
            INSERT INTO debate_messages VALUES(
                'mixed-gap','T','2026-07-22T10:00:02Z','H','Q'
            );
            INSERT INTO debate_message_recipients
            VALUES('mixed-gap','EXECUTOR_1','normal');
            """
        )
        con.commit()
    finally:
        con.close()

    recovered = module._fetch_pending_deliveries([], ["Q"], 10)
    assert [row["msg_id"] for row in recovered] == ["mixed-gap"]
