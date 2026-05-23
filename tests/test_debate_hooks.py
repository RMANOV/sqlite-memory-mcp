import os
import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def _load_hook_module(name: str, rel_path: str):
    spec = spec_from_file_location(name, ROOT / rel_path)
    module = module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_debate_wake_agent_budget_caps_successful_launches(monkeypatch):
    module = _load_hook_module("debate_wake_budget_test", "hooks/debate_wake.py")
    monkeypatch.setenv("DEBATE_WAKE_ACTION", "agent")
    monkeypatch.setenv("DEBATE_WAKE_BUDGET", "1")
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


def test_debate_pump_sets_default_wake_budget_from_worker_limits(monkeypatch, tmp_path):
    module = _load_hook_module("debate_pump_budget_default_test", "hooks/debate_pump.py")
    module.LOG_PATH = tmp_path / "pump.jsonl"
    module.STATE_PATH = tmp_path / "pump_state.json"
    module.STOP = False
    rows = [{"msg_id": "m1", "topic_id": "T", "ts": "2026-05-20T00:00:01Z"}]
    seen_budget = []

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
