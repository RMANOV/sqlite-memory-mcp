import json
import os
import sqlite3
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import collab_server
import db_utils
from schema import init_db


def _conn_factory(db_path: str):
    def _open():
        conn = sqlite3.connect(db_path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    return _open


@pytest.fixture
def collab_env(tmp_path, monkeypatch):
    db_path = str(tmp_path / "memory.db")
    init_db(db_path)
    monkeypatch.setattr(collab_server, "_get_conn", _conn_factory(db_path))
    return db_path


def test_manage_collaborators_rejects_invalid_github_username(collab_env):
    result = json.loads(
        collab_server.manage_collaborators.fn(
            action="add",
            github_user="../bad-user",
        )
    )

    assert "Invalid GitHub username" in result["error"]


def test_share_knowledge_rejects_unknown_collaborators(collab_env):
    db_path = collab_env
    now = "2026-03-28T12:00:00+00:00"
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "INSERT INTO collaborators (github_user, trust_level, added_at) VALUES (?, ?, ?)",
        ("alice", "read_write", now),
    )
    conn.execute(
        "INSERT INTO entities (name, entity_type, created_at, updated_at) VALUES (?, ?, ?, ?)",
        ("Knowledge", "note", now, now),
    )
    conn.close()

    result = json.loads(
        collab_server.share_knowledge.fn(
            entity_names=["Knowledge"],
            target_users=["bob"],
        )
    )

    assert result["error"] == "Unknown collaborator(s): bob"


def _cp(args, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(["git", *args], returncode, stdout, stderr)


def test_ensure_bridge_repo_ready_blocks_symlinked_generated_artifacts(
    tmp_path, monkeypatch
):
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    try:
        (bridge_dir / "shared.json").symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    def fake_git_run(repo_dir, *args, timeout=30):
        if args == ("rev-parse", "--abbrev-ref", "HEAD"):
            return _cp(args, stdout="main\n")
        if args == ("status", "--porcelain"):
            return _cp(args)
        return _cp(args)

    monkeypatch.setattr(db_utils, "git_run", fake_git_run)

    ok, msg = db_utils.ensure_bridge_repo_ready(str(bridge_dir))

    assert ok is False
    assert "unsafe generated symlinks/escaped paths" in msg
    assert "shared.json" in msg
