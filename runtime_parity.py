"""Runtime parity checks for repo hooks vs live runtime copies."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

RUNTIME_PARITY_VERSION = "bridge_runtime_v1"
REPO_ROOT = Path(__file__).resolve().parent
REPO_HOOK_DIR = REPO_ROOT / "hooks"
RUNTIME_HOOK_DIR = Path.home() / ".claude" / "hooks"
MANIFEST_PATH = Path.home() / ".claude" / "memory" / ".bridge_runtime_parity.json"
TRACKED_RUNTIME_FILES = (
    "bridge_auto_sync.py",
    "bridge_sync_worker.py",
    "debate_agent_events.py",
    "debate_pump.py",
    "debate_wake.py",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_sha(repo_root: Path) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    value = (proc.stdout or "").strip()
    return value or None


def _runtime_file_status(
    repo_exists: bool, runtime_exists: bool, same_hash: bool
) -> str:
    if repo_exists and runtime_exists and same_hash:
        return "in_sync"
    if repo_exists and not runtime_exists:
        return "runtime_missing"
    if not repo_exists and runtime_exists:
        return "repo_missing"
    if not repo_exists and not runtime_exists:
        return "missing_both"
    return "mismatch"


def collect_runtime_parity(
    *,
    repo_root: str | os.PathLike[str] | None = None,
    runtime_dir: str | os.PathLike[str] | None = None,
    manifest_path: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Collect checksums for repo hooks vs live runtime hook copies."""
    repo_root_path = Path(repo_root) if repo_root is not None else REPO_ROOT
    repo_hook_dir = repo_root_path / "hooks"
    runtime_dir_path = (
        Path(runtime_dir) if runtime_dir is not None else RUNTIME_HOOK_DIR
    )
    manifest_path_obj = (
        Path(manifest_path) if manifest_path is not None else MANIFEST_PATH
    )

    repo_sha = _git_sha(repo_root_path)
    files: list[dict[str, Any]] = []
    warnings: list[str] = []

    for name in TRACKED_RUNTIME_FILES:
        repo_path = repo_hook_dir / name
        runtime_path = runtime_dir_path / name
        repo_exists = repo_path.is_file()
        runtime_exists = runtime_path.is_file()
        repo_hash = _sha256_file(repo_path)
        runtime_hash = _sha256_file(runtime_path)
        same_hash = bool(repo_hash and runtime_hash and repo_hash == runtime_hash)
        status = _runtime_file_status(repo_exists, runtime_exists, same_hash)
        entry = {
            "name": name,
            "repo_path": str(repo_path),
            "repo_exists": repo_exists,
            "repo_sha256": repo_hash,
            "runtime_path": str(runtime_path),
            "runtime_exists": runtime_exists,
            "runtime_sha256": runtime_hash,
            "status": status,
        }
        files.append(entry)
        if status != "in_sync":
            warnings.append(f"{name}: {status}")

    return {
        "version": RUNTIME_PARITY_VERSION,
        "generated_at": _now_iso(),
        "repo_root": str(repo_root_path),
        "repo_git_sha": repo_sha,
        "repo_hook_dir": str(repo_hook_dir),
        "runtime_hook_dir": str(runtime_dir_path),
        "manifest_path": str(manifest_path_obj),
        "all_synced": all(entry["status"] == "in_sync" for entry in files),
        "warnings": warnings,
        "files": files,
    }


def write_runtime_parity_manifest(
    *,
    repo_root: str | os.PathLike[str] | None = None,
    runtime_dir: str | os.PathLike[str] | None = None,
    manifest_path: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Persist the latest runtime parity state to disk."""
    report = collect_runtime_parity(
        repo_root=repo_root,
        runtime_dir=runtime_dir,
        manifest_path=manifest_path,
    )
    target = Path(manifest_path) if manifest_path is not None else MANIFEST_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f"{target.name}.tmp")
    tmp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, target)
    return report


def sync_runtime_hooks(
    *,
    repo_root: str | os.PathLike[str] | None = None,
    runtime_dir: str | os.PathLike[str] | None = None,
    manifest_path: str | os.PathLike[str] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Copy tracked repo hook files into the live Claude runtime hook dir."""
    before = collect_runtime_parity(
        repo_root=repo_root,
        runtime_dir=runtime_dir,
        manifest_path=manifest_path,
    )
    runtime_dir_path = Path(runtime_dir) if runtime_dir is not None else RUNTIME_HOOK_DIR
    updates: list[dict[str, Any]] = []
    skipped: list[str] = []
    missing_repo: list[str] = []

    if not dry_run:
        runtime_dir_path.mkdir(parents=True, exist_ok=True)

    for entry in before["files"]:
        name = str(entry["name"])
        if not entry["repo_exists"]:
            missing_repo.append(name)
            continue
        if entry["status"] == "in_sync":
            skipped.append(name)
            continue
        update = {
            "name": name,
            "from": entry["repo_path"],
            "to": entry["runtime_path"],
            "previous_status": entry["status"],
            "action": "would_copy" if dry_run else "copied",
        }
        updates.append(update)
        if dry_run:
            continue
        runtime_path = Path(entry["runtime_path"])
        runtime_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(entry["repo_path"], runtime_path)

    after = (
        before
        if dry_run
        else write_runtime_parity_manifest(
            repo_root=repo_root,
            runtime_dir=runtime_dir,
            manifest_path=manifest_path,
        )
    )
    return {
        "dry_run": dry_run,
        "updated": updates,
        "skipped": skipped,
        "missing_repo": missing_repo,
        "before": before,
        "after": after,
    }


def runtime_warning_summary(report: dict[str, Any]) -> str | None:
    """Return a single operator-facing warning string when parity is broken."""
    if not report or report.get("all_synced"):
        return None
    warnings = report.get("warnings") or []
    short = ", ".join(warnings[:3])
    if len(warnings) > 3:
        short += f", +{len(warnings) - 3} more"
    return (
        "BRIDGE WARNING: runtime drift detected: "
        f"{short}. Run bridge_doctor() and refresh runtime hook copies."
    )
