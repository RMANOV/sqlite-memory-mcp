"""Stale kanban_payload.json.tmp must not block bridge sync.

write_kanban_payload writes kanban_payload.json.tmp and then os.replace()s it.
A process killed between the two leaves an empty tmp behind. The readiness
preflight treated it as a user edit ("commit or stash bridge repo edits before
sync"), sync never ran again, so the export that would overwrite the tmp never
ran either. Incident 2026-09-25: 34 blocked syncs after PID 12592 died.
"""

import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import db_utils  # noqa: E402


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )


def _bridge_repo(tmp_path):
    repo = tmp_path / "bridge"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "test")
    (repo / "shared.json").write_text("{}", encoding="utf-8")
    (repo / "kanban_payload.json").write_text("{}", encoding="utf-8")
    _git(repo, "add", "shared.json", "kanban_payload.json")
    _git(repo, "commit", "-q", "-m", "seed")
    return repo


def test_kanban_payload_tmp_is_a_generated_temp_file():
    assert db_utils.is_generated_bridge_path("kanban_payload.json.tmp")


def test_empty_kanban_tmp_does_not_block_readiness(tmp_path):
    repo = _bridge_repo(tmp_path)
    (repo / "kanban_payload.json.tmp").write_bytes(b"")

    ok, msg = db_utils.ensure_bridge_repo_ready(str(repo))

    assert ok is True, msg
    assert not (repo / "kanban_payload.json.tmp").exists()
    assert (repo / "kanban_payload.json").read_text(encoding="utf-8") == "{}"


def test_user_file_still_blocks_readiness(tmp_path):
    repo = _bridge_repo(tmp_path)
    (repo / "notes.txt").write_text("user work", encoding="utf-8")

    ok, msg = db_utils.ensure_bridge_repo_ready(str(repo))

    assert ok is False
    assert "notes.txt" in msg
    assert (repo / "notes.txt").exists()
