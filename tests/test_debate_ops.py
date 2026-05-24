import json
import stat
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import debate_ops
import install_doctor


def test_debate_ops_smoke_runs_expected_test_subset(monkeypatch):
    captured = {}

    def fake_run(cmd, cwd, check, capture_output=False, text=False):
        captured["cmd"] = cmd
        captured["cwd"] = cwd
        captured["check"] = check
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    rc = debate_ops.main(["smoke"])

    assert rc == 0
    assert captured["cmd"][:3] == [sys.executable, "-m", "pytest"]
    assert captured["cmd"][3:-1] == debate_ops.SMOKE_TESTS
    assert captured["cmd"][-1] == "-q"
    assert captured["cwd"] == debate_ops.ROOT
    assert captured["check"] is False


def test_debate_ops_install_service_dry_run(capsys):
    rc = debate_ops.main(["install-service", "--dry-run"])
    payload = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert payload["service"] == "sqlite-memory-debate-pump.service"
    assert payload["dry_run"] is True
    assert payload["target"].endswith("/.config/systemd/user/sqlite-memory-debate-pump.service")


def test_debate_ops_install_service_runs_systemctl(monkeypatch, tmp_path, capsys):
    calls = []
    service_dst = tmp_path / "sqlite-memory-debate-pump.service"
    monkeypatch.setattr(debate_ops, "SERVICE_DST", service_dst)

    def fake_run(cmd, cwd, check, capture_output=False, text=False):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="ok\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    rc = debate_ops.main(["install-service"])
    payload = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert service_dst.read_text(encoding="utf-8") == debate_ops.SERVICE_SRC.read_text(
        encoding="utf-8"
    )
    assert [call[:3] for call in calls] == [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable"],
        ["systemctl", "--user", "restart"],
    ]
    assert payload["actions"][-1]["returncode"] == 0


def test_debate_pump_service_template_is_agent_safe():
    text = debate_ops.SERVICE_SRC.read_text(encoding="utf-8")

    assert "Restart=always" in text
    assert "KillMode=process" in text
    assert "DEBATE_WAKE_ACTION=agent" in text
    assert "DEBATE_RESOURCE_BUDGET=auto" in text
    assert "--action-kind Q,DECISION" in text
    assert "MemoryMax=5G" in text
    exec_start = next(line for line in text.splitlines() if line.startswith("ExecStart="))
    assert "codex" not in exec_start.lower()


def test_install_doctor_codex_wrapper_check_accepts_expected_symlink(monkeypatch, tmp_path):
    monkeypatch.delenv("CODEX_DEBATE_CODEX_BIN", raising=False)
    monkeypatch.delenv("CODEX_DEBATE_WRAPPER", raising=False)
    wrapper = tmp_path / "codex-debate-wrapper"
    wrapper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR)
    codex_bin = tmp_path / "codex"
    codex_bin.symlink_to(wrapper)
    codex_real = tmp_path / "codex-real"
    codex_real.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    codex_real.chmod(codex_real.stat().st_mode | stat.S_IXUSR)

    check = install_doctor._check_codex_debate_wrapper(
        codex_bin=codex_bin,
        expected_wrapper=wrapper,
    )

    assert check["ok"] is True


def test_install_doctor_codex_wrapper_check_rejects_plain_binary(monkeypatch, tmp_path):
    monkeypatch.delenv("CODEX_DEBATE_CODEX_BIN", raising=False)
    monkeypatch.delenv("CODEX_DEBATE_WRAPPER", raising=False)
    codex_bin = tmp_path / "codex"
    codex_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    codex_bin.chmod(codex_bin.stat().st_mode | stat.S_IXUSR)

    check = install_doctor._check_codex_debate_wrapper(
        codex_bin=codex_bin,
        expected_wrapper=tmp_path / "codex-debate-wrapper",
    )

    assert check["ok"] is False
    assert "not a symlink" in check["detail"]
