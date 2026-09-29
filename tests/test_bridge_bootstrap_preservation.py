"""Canonical fresh-peer sync keeps committed attachments under the local opt-in."""
from pathlib import Path
import json
import sqlite3
import subprocess

import pytest

import bridge_sync_worker
import db_utils
from schema import init_db


@pytest.mark.parametrize("preserve", ["0", "1"])
def test_canonical_bootstrap_preserves_committed_orphans_and_machine_id(
    tmp_path, monkeypatch, preserve
):
    source_root = Path(__file__).resolve().parents[1]
    assert Path(bridge_sync_worker.__file__).resolve() == source_root / "bridge_sync_worker.py"
    assert Path(db_utils.__file__).resolve() == source_root / "db_utils.py"
    bridge = tmp_path / "bridge"
    bridge.mkdir()
    seed_db = tmp_path / "seed.db"
    fresh_db = tmp_path / "fresh.db"
    seed_attachments = tmp_path / "seed-attachments"
    fresh_attachments = tmp_path / "fresh-attachments"
    init_db(str(seed_db))
    init_db(str(fresh_db))
    source = tmp_path / "source.bin"
    source.write_bytes(b"mapped attachment\x00\r\n\xff")
    with db_utils.get_conn(str(seed_db)) as conn:
        now = db_utils.now_iso()
        conn.execute(
            "INSERT INTO tasks (id, title, description, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("bootstrap-task", "Existing task", "Existing full body", now, now),
        )
        mapped = db_utils.add_task_attachment(
            conn, "bootstrap-task", str(source), local_root=str(seed_attachments)
        )
        db_utils.export_task_files(conn, str(bridge), attachment_root=str(seed_attachments))
        db_utils.export_index_json(conn, str(bridge))
    (bridge / "shared.json").write_text(
        json.dumps({"version": 4, "entities": [], "relations": [], "tasks": []}),
        encoding="utf-8",
    )
    orphan_paths = ["legacy/first.bin", "legacy/nested/second.md"]
    for rel in orphan_paths:
        blob = bridge / "attachments" / rel
        blob.parent.mkdir(parents=True, exist_ok=True)
        blob.write_bytes(b"unmapped retained bytes\x00\r\n\xff" + rel.encode())
    original = {
        path.relative_to(bridge / "attachments").as_posix(): path.read_bytes()
        for path in (bridge / "attachments").rglob("*") if path.is_file()
    }

    def git(*args):
        return subprocess.run(
            ["git", "-C", str(bridge), *args], capture_output=True, text=True, check=True
        )

    git("init", "-b", "main")
    git("config", "user.name", "bootstrap test")
    git("config", "user.email", "test@localhost")
    git("config", "core.autocrlf", "false")
    git("add", ".")
    git("commit", "-m", "seed committed attachments")
    initial_head = git("rev-parse", "HEAD").stdout.strip()
    monkeypatch.setenv("BRIDGE_PRESERVE_ORPHAN_ATTACHMENTS", preserve)
    monkeypatch.setenv("MACHINE_ID", "fixture-machine")
    monkeypatch.setenv("GITHUB_USER", "bootstrap-test")
    monkeypatch.delenv("CLOUDFLARE_API_TOKEN", raising=False)
    monkeypatch.delenv("BRIDGE_GH_REPO", raising=False)
    monkeypatch.setattr(db_utils, "TASK_ATTACHMENT_ROOT", str(fresh_attachments))
    monkeypatch.setattr(db_utils, "MACHINE_ID", "fixture-machine")
    monkeypatch.setattr(bridge_sync_worker, "_push_backoff_until", 0.0)
    monkeypatch.setattr(bridge_sync_worker, "_push_failure_count", 0)
    monkeypatch.setattr(bridge_sync_worker, "ensure_bridge_git_identity", lambda _repo: {})
    push_calls = []

    def local_git_transport(repo, *args, **kwargs):
        assert Path(repo).resolve() == bridge.resolve()
        if args == ("fetch", "origin", "main"):
            return subprocess.CompletedProcess(args, 0, "", "")
        if args in {("rev-parse", "HEAD"), ("rev-parse", "origin/main"),
                    ("merge-base", "HEAD", "origin/main")}:
            return subprocess.CompletedProcess(args, 0, initial_head + "\n", "")
        if args == ("push",):
            push_calls.append(args)
            return subprocess.CompletedProcess(args, 1, "", "LOCAL_TEST_PUSH_DEFERRED")
        raise AssertionError(f"Unexpected transport operation: {args}")

    # Only network transport is intercepted. Real preflight, import, attachment
    # hydration, safety checks, export, Git staging and commit run in tmp_path.
    monkeypatch.setattr(bridge_sync_worker, "git_retry", local_git_transport)
    result = bridge_sync_worker.main(db_path=str(fresh_db), bridge_repo=str(bridge))

    assert result["pushed"] is False
    assert result["message"] == "LOCAL_TEST_PUSH_DEFERRED"
    assert push_calls == [("push",)]
    assert result["imported_new"] == 1
    with sqlite3.connect(fresh_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM task_attachments").fetchone()[0] == 1
    mapped_rel = mapped["stored_relpath"]
    assert (fresh_attachments / mapped_rel).read_bytes() == original[mapped_rel]
    payload = json.loads((bridge / "shared.json").read_text(encoding="utf-8"))
    assert payload["machine_id"] == "fixture-machine"
    assert git("status", "--porcelain").stdout == ""
    after = {
        path.relative_to(bridge / "attachments").as_posix(): path.read_bytes()
        for path in (bridge / "attachments").rglob("*") if path.is_file()
    }
    if preserve == "1":
        assert after == original
        assert git("diff", "--name-only", "--diff-filter=D", initial_head, "HEAD", "--", "attachments").stdout == ""
    else:
        assert after == {mapped_rel: original[mapped_rel]}
