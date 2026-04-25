import json
import importlib.util
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "bin" / "bridge_ops.py"


def _load_bridge_ops():
    spec = importlib.util.spec_from_file_location("bridge_ops", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_bridge_ops_smoke_runs_expected_test_subset(monkeypatch):
    bridge_ops = _load_bridge_ops()
    captured = {}

    def fake_run(cmd, cwd, check):
        captured["cmd"] = cmd
        captured["cwd"] = cwd
        captured["check"] = check
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    rc = bridge_ops.main(["smoke"])

    assert rc == 0
    assert captured["cmd"][:3] == [bridge_ops.sys.executable, "-m", "pytest"]
    assert captured["cmd"][3:-1] == bridge_ops.SMOKE_TESTS
    assert captured["cmd"][-1] == "-q"
    assert captured["cwd"] == bridge_ops.ROOT
    assert captured["check"] is False


def test_bridge_ops_smoke_verbose_omits_quiet_flag(monkeypatch):
    bridge_ops = _load_bridge_ops()
    captured = {}

    def fake_run(cmd, cwd, check):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    rc = bridge_ops.main(["smoke", "--verbose"])

    assert rc == 0
    assert captured["cmd"] == [
        bridge_ops.sys.executable,
        "-m",
        "pytest",
        *bridge_ops.SMOKE_TESTS,
    ]


def test_bridge_ops_refresh_hooks_prints_sync_payload(monkeypatch, capsys):
    bridge_ops = _load_bridge_ops()

    def fake_sync_runtime_hooks(*, dry_run):
        return {
            "dry_run": dry_run,
            "updated": [{"name": "bridge_auto_sync.py"}],
            "skipped": [],
            "missing_repo": [],
            "before": {"all_synced": False},
            "after": {"all_synced": True},
        }

    monkeypatch.setitem(
        sys.modules,
        "runtime_parity",
        type(
            "RuntimeParityStub",
            (),
            {"sync_runtime_hooks": staticmethod(fake_sync_runtime_hooks)},
        ),
    )

    rc = bridge_ops.main(["refresh-hooks", "--dry-run"])
    payload = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert payload["dry_run"] is True
    assert payload["updated"][0]["name"] == "bridge_auto_sync.py"
