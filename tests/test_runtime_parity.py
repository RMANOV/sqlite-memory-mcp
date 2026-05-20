"""Tests for runtime parity manifests and drift reporting."""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from runtime_parity import (
    collect_runtime_parity,
    runtime_warning_summary,
    sync_runtime_hooks,
    write_runtime_parity_manifest,
)


def _seed_runtime_tree(
    repo_root: Path, runtime_root: Path, *, same_worker: bool
) -> None:
    hooks_dir = repo_root / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    runtime_root.mkdir(parents=True, exist_ok=True)
    (hooks_dir / "bridge_auto_sync.py").write_text(
        "print('repo hook')\n", encoding="utf-8"
    )
    (hooks_dir / "bridge_sync_worker.py").write_text(
        "print('repo worker')\n", encoding="utf-8"
    )
    for name in ("debate_agent_events.py", "debate_pump.py", "debate_wake.py"):
        (hooks_dir / name).write_text(f"print('repo {name}')\n", encoding="utf-8")
        (runtime_root / name).write_text(f"print('repo {name}')\n", encoding="utf-8")
    (runtime_root / "bridge_auto_sync.py").write_text(
        "print('runtime hook drift')\n", encoding="utf-8"
    )
    worker_body = (
        "print('repo worker')\n" if same_worker else "print('runtime worker drift')\n"
    )
    (runtime_root / "bridge_sync_worker.py").write_text(worker_body, encoding="utf-8")


def test_collect_runtime_parity_detects_mismatch(tmp_path):
    repo_root = tmp_path / "repo"
    runtime_root = tmp_path / "runtime"
    _seed_runtime_tree(repo_root, runtime_root, same_worker=True)

    report = collect_runtime_parity(
        repo_root=repo_root,
        runtime_dir=runtime_root,
        manifest_path=tmp_path / "manifest.json",
    )

    statuses = {entry["name"]: entry["status"] for entry in report["files"]}
    assert report["all_synced"] is False
    assert statuses["bridge_auto_sync.py"] == "mismatch"
    assert statuses["bridge_sync_worker.py"] == "in_sync"
    assert "bridge_auto_sync.py: mismatch" in (report["warnings"] or [])
    assert "runtime drift detected" in (runtime_warning_summary(report) or "")


def test_write_runtime_parity_manifest_persists_report(tmp_path):
    repo_root = tmp_path / "repo"
    runtime_root = tmp_path / "runtime"
    manifest_path = tmp_path / "parity.json"
    _seed_runtime_tree(repo_root, runtime_root, same_worker=False)

    report = write_runtime_parity_manifest(
        repo_root=repo_root,
        runtime_dir=runtime_root,
        manifest_path=manifest_path,
    )
    saved = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert saved["version"] == report["version"]
    assert saved["manifest_path"] == str(manifest_path)
    assert len(saved["files"]) == 5
    assert {entry["status"] for entry in saved["files"]} == {
        "in_sync",
        "mismatch",
    }


def test_sync_runtime_hooks_copies_drifted_files_and_writes_manifest(tmp_path):
    repo_root = tmp_path / "repo"
    runtime_root = tmp_path / "runtime"
    manifest_path = tmp_path / "parity.json"
    _seed_runtime_tree(repo_root, runtime_root, same_worker=False)

    result = sync_runtime_hooks(
        repo_root=repo_root,
        runtime_dir=runtime_root,
        manifest_path=manifest_path,
    )
    saved = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert result["dry_run"] is False
    assert {entry["name"] for entry in result["updated"]} == {
        "bridge_auto_sync.py",
        "bridge_sync_worker.py",
    }
    assert result["after"]["all_synced"] is True
    assert saved["all_synced"] is True
    assert (runtime_root / "bridge_auto_sync.py").read_text(encoding="utf-8") == (
        repo_root / "hooks" / "bridge_auto_sync.py"
    ).read_text(encoding="utf-8")


def test_sync_runtime_hooks_dry_run_does_not_write(tmp_path):
    repo_root = tmp_path / "repo"
    runtime_root = tmp_path / "runtime"
    manifest_path = tmp_path / "parity.json"
    _seed_runtime_tree(repo_root, runtime_root, same_worker=False)

    result = sync_runtime_hooks(
        repo_root=repo_root,
        runtime_dir=runtime_root,
        manifest_path=manifest_path,
        dry_run=True,
    )

    assert result["dry_run"] is True
    assert {entry["action"] for entry in result["updated"]} == {"would_copy"}
    assert result["after"]["all_synced"] is False
    assert not manifest_path.exists()
