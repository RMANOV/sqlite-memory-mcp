import os
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
    monkeypatch.setattr(module, "_mark_source_wake_result", lambda *args, **kwargs: None)
    launches = []

    def fake_launch(target, trigger_msg_id, topic_id):
        launches.append((target["target_session_id"], trigger_msg_id, topic_id))
        return {"launched": True, "pid": 1000 + len(launches)}

    monkeypatch.setattr(module, "_launch_agent", fake_launch)
    out = module._maybe_dispatch(
        {"msg_id": "abc123", "topic_id": "T", "schema_version": "debate_post_with_recipients.v1"},
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


def test_resource_budget_blocks_supercritical_heat_and_large_agent_set():
    module = _load_hook_module("debate_resource_budget_blocked_test", "hooks/debate_resource_budget.py")

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
    module = _load_hook_module("debate_resource_budget_hot_spike_test", "hooks/debate_resource_budget.py")

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
    module = _load_hook_module("debate_resource_budget_sleep_test", "hooks/debate_resource_budget.py")
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
    module = _load_hook_module("debate_resource_budget_healthy_test", "hooks/debate_resource_budget.py")

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
    module = _load_hook_module("debate_resource_budget_unknown_temp_test", "hooks/debate_resource_budget.py")

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
    assert budget.tier == "low"
    assert budget.reason == "temperature_unknown"


def test_resource_budget_does_not_require_swap_on_no_swap_host():
    module = _load_hook_module("debate_resource_budget_no_swap_test", "hooks/debate_resource_budget.py")

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
    module = _load_hook_module("debate_resource_budget_small_machine_test", "hooks/debate_resource_budget.py")

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


def test_resource_budget_recovery_hysteresis_requires_repeated_healthy_samples(tmp_path):
    module = _load_hook_module("debate_resource_budget_hysteresis_test", "hooks/debate_resource_budget.py")
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
        assert module.apply_recovery_hysteresis(spike, state_path=path).allow_agent is True
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
    assert module.apply_recovery_hysteresis(supercritical, state_path=path).allow_agent is False
    path.unlink()
    first_spike = module.apply_recovery_hysteresis(spike, state_path=path)
    assert first_spike.allow_agent is True
    first = module.apply_recovery_hysteresis(healthy, state_path=path)
    second = module.apply_recovery_hysteresis(healthy, state_path=path)
    third = module.apply_recovery_hysteresis(healthy, state_path=path)

    assert first.allow_agent is True
    assert second.allow_agent is True
    assert third.allow_agent is True


def test_resource_budget_live_agent_count_ignores_sqlite_memory_sidecars(monkeypatch, tmp_path):
    module = _load_hook_module("debate_resource_budget_count_test", "hooks/debate_resource_budget.py")
    proc = tmp_path / "proc"
    proc.mkdir()
    commands = {
        "100": "python3 /home/rmanov/.local/bin/sqlite-memory-intel",
        "101": "python3 /home/rmanov/.local/bin/sqlite-memory-tasks",
        "102": "node /home/rmanov/.npm-global/bin/codex",
        "103": "/home/rmanov/.npm-global/lib/node_modules/@openai/codex/vendor/codex",
        "104": "claude",
        "105": "/home/rmanov/.local/share/claude/versions/2.1.150 --chrome-native-host",
    }
    for pid, cmd in commands.items():
        d = proc / pid
        d.mkdir()
        (d / "cmdline").write_bytes(cmd.replace(" ", "\0").encode())

    assert module._count_live_agents(proc) == 2


def test_debate_pump_sets_default_wake_budget_from_worker_limits(monkeypatch, tmp_path):
    module = _load_hook_module("debate_pump_budget_default_test", "hooks/debate_pump.py")
    module.LOG_PATH = tmp_path / "pump.jsonl"
    module.STATE_PATH = tmp_path / "pump_state.json"
    module.STOP = False
    rows = [{"msg_id": "m1", "topic_id": "T", "ts": "2026-05-20T00:00:01Z"}]
    seen_budget = []

    monkeypatch.setenv("DEBATE_RESOURCE_BUDGET", "off")
    monkeypatch.delenv("DEBATE_WAKE_BUDGET", raising=False)
    monkeypatch.setattr(module, "_fetch_new", lambda *args, **kwargs: rows)
    monkeypatch.setattr(module, "_estimate_worker_demand", lambda *args, **kwargs: 1)
    monkeypatch.setattr(module, "_reap_children", lambda: None)
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


def test_debate_pump_does_not_advance_cursor_after_dispatch_exception(monkeypatch, tmp_path):
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
    monkeypatch.setattr(module, "_estimate_worker_demand", lambda *args, **kwargs: 1)
    monkeypatch.setattr(module, "_reap_children", lambda: None)
    monkeypatch.setattr(module, "_reclaim_stale_message_claims", lambda **kwargs: None)
    monkeypatch.setattr(module, "_save_state", lambda ts, msg_id: saved.append((ts, msg_id)))

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
