"""Regression tests: Kanban render payload (preview-only) — bridge payload-bloat fix.

The Kanban PWA was choking on a 19MB shared.json containing a single 540KB note.
Fix: a SEPARATE derived `kanban_payload.json` with truncated task descriptions,
while the transport (shared.json / index.json / tasks/*.json) keeps FULL bodies.

Hard guards under test:
  - transport / input payload is NEVER mutated (full bodies preserved)
  - non-active notes (done/archived/someday) collapse broadly (the real size lever)
  - active >20KB notes truncate; small active notes pass through full
  - preview carries _mirror_preview / _full_len / _full_hash (sha256 of full body)
  - kanban_payload.json is valid JSON and materially smaller (giant cards capped)
  - idempotent (same task-preview fields across two writes)
  - surface contract: pull=False (NEVER imported) + git_stage=True (tracked/synced)
  - parse-or-regenerate guard (load side): a union-merge-corrupted preview is
    rebuilt from the transport payload and NEVER blocks pull/sync
  - acceptance (b): fresh DB pull/restore from the transport recovers the FULL
    description even when a preview kanban_payload.json sits in the bridge dir
"""

import hashlib
import json
import os
import sqlite3
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import bridge_sync_worker  # noqa: E402
import db_utils  # noqa: E402
from db_utils import (  # noqa: E402
    BRIDGE_GENERATED_FILES,
    KANBAN_BIG_THRESHOLD,
    KANBAN_COLLAPSE_MAX,
    KANBAN_PREVIEW_MAX,
    _kanban_preview_task,
    ensure_kanban_payload_parseable,
    export_index_json,
    export_task_files,
    is_generated_bridge_path,
    load_remote_tasks_for_merge,
    merge_import_tasks,
    now_iso,
    write_kanban_payload,
)
from schema import init_db  # noqa: E402
from surface_contract import BRIDGE_ARTIFACT_SURFACE_CONTRACT  # noqa: E402


def _payload(tasks):
    return {
        "version": 4,
        "pushed_at": "2026-06-08T00:00:00+00:00",
        "machine_id": "TEST",
        "entities": [],
        "relations": [],
        "tasks": tasks,
    }


def test_giant_active_note_truncated_with_integrity():
    big = "X" * 540000
    t = {"id": "d539", "status": "not_started", "section": "inbox", "description": big}
    out = _kanban_preview_task(t)
    assert len(out["description"]) == KANBAN_PREVIEW_MAX
    assert out["_mirror_preview"] is True
    assert out["_full_len"] == 540000
    assert out["_full_hash"] == hashlib.sha256(big.encode("utf-8")).hexdigest()


def test_nonactive_collapses_broadly_even_when_small():
    # done/someday collapse REGARDLESS of length — broad collapse is the size lever
    body = "Y" * 5000  # below the 20KB active threshold
    for t in (
        {"id": "a", "status": "done", "section": "done", "description": body},
        {"id": "b", "status": "not_started", "section": "someday", "description": body},
        {"id": "c", "status": "archived", "section": "next", "description": body},
    ):
        out = _kanban_preview_task(t)
        assert len(out["description"]) == KANBAN_COLLAPSE_MAX
        assert out["_mirror_preview"] is True


def test_small_active_passes_through_full():
    body = "Z" * 1234  # active, under threshold
    t = {"id": "s", "status": "not_started", "section": "today", "description": body}
    out = _kanban_preview_task(t)
    assert out["description"] == body
    assert "_mirror_preview" not in out


def test_input_payload_never_mutated():
    """Transport safety: previewing must not touch the source task descriptions."""
    big = "Q" * (KANBAN_BIG_THRESHOLD + 5000)
    tasks = [
        {"id": "x", "status": "not_started", "section": "inbox", "description": big}
    ]
    _kanban_preview_task(tasks[0])
    assert tasks[0]["description"] == big  # original untouched
    assert "_mirror_preview" not in tasks[0]


def test_write_payload_valid_json_and_capped(tmp_path):
    big = "W" * 540000
    payload = _payload(
        [
            {"id": "big", "status": "done", "section": "someday", "description": big},
            {
                "id": "ok",
                "status": "not_started",
                "section": "today",
                "description": "short",
            },
        ]
    )
    rel = write_kanban_payload(str(tmp_path), payload)
    assert rel == "kanban_payload.json"
    f = tmp_path / "kanban_payload.json"
    data = json.loads(f.read_text(encoding="utf-8"))  # (d) valid JSON / parse guard
    assert data["_render_only"] is True
    by_id = {t["id"]: t for t in data["tasks"]}
    # (e) giant card capped; small active card intact
    assert len(by_id["big"]["description"]) <= KANBAN_COLLAPSE_MAX
    assert by_id["ok"]["description"] == "short"
    # whole file is far smaller than the raw 540KB body
    assert f.stat().st_size < 50_000


def test_idempotent_task_previews(tmp_path):
    payload = _payload(
        [
            {
                "id": "big",
                "status": "done",
                "section": "someday",
                "description": "M" * 100000,
            }
        ]
    )
    write_kanban_payload(str(tmp_path), payload)
    first = json.loads((tmp_path / "kanban_payload.json").read_text(encoding="utf-8"))[
        "tasks"
    ]
    write_kanban_payload(str(tmp_path), payload)
    second = json.loads((tmp_path / "kanban_payload.json").read_text(encoding="utf-8"))[
        "tasks"
    ]
    assert first == second  # scoped to tasks (not whole file / pushed_at)


def test_surface_contract_render_only_and_tracked():
    spec = BRIDGE_ARTIFACT_SURFACE_CONTRACT["kanban_payload.json"]
    assert spec["pull"] is False  # (c) NEVER imported back into the DB
    assert spec["git_stage"] is True  # tracked/synced -> opens immediately on peers
    assert spec["export"] is True


def test_kanban_payload_is_recognized_as_generated_bridge_path():
    """v3.12.5 regression: the pre-sync clean-check allows a dirty path only when
    is_generated_bridge_path() returns True. v3.12.4 wired kanban_payload.json into
    surface_contract + the merge-driver but NOT this set, so each export left it
    uncommitted and the readiness gate blocked sync with 'commit or stash bridge repo
    edits before sync: kanban_payload.json'. It is a regenerable derived mirror, so it
    MUST be treated as a generated artifact (allowed-dirty + restored from DB state)."""
    assert "kanban_payload.json" in BRIDGE_GENERATED_FILES
    assert is_generated_bridge_path("kanban_payload.json") is True
    # the readiness check normalizes leading separators before matching
    assert is_generated_bridge_path("/kanban_payload.json") is True


# ── parse-or-regenerate guard (load side; SPEC e401c69d fold #2) ──────────


def test_parse_guard_regenerates_corrupt_payload(tmp_path):
    (tmp_path / "kanban_payload.json").write_text(
        '{"tasks": [}{ UNION MERGE GARBAGE', encoding="utf-8"
    )
    payload = _payload(
        [
            {
                "id": "big",
                "status": "done",
                "section": "someday",
                "description": "B" * 90000,
            }
        ]
    )

    status = ensure_kanban_payload_parseable(str(tmp_path), payload)

    assert status == "regenerated"
    data = json.loads((tmp_path / "kanban_payload.json").read_text(encoding="utf-8"))
    assert data["_render_only"] is True
    assert len(data["tasks"][0]["description"]) <= KANBAN_COLLAPSE_MAX


def test_parse_guard_leaves_valid_payload_untouched(tmp_path):
    write_kanban_payload(str(tmp_path), _payload([{"id": "keep", "description": "ok"}]))
    before = (tmp_path / "kanban_payload.json").read_bytes()

    # Even when handed a DIFFERENT transport payload, a valid file is not rewritten.
    status = ensure_kanban_payload_parseable(
        str(tmp_path), _payload([{"id": "other", "description": "changed"}])
    )

    assert status == "ok"
    assert (tmp_path / "kanban_payload.json").read_bytes() == before


def test_parse_guard_missing_file_is_noop(tmp_path):
    status = ensure_kanban_payload_parseable(str(tmp_path), _payload([]))
    assert status == "missing"
    assert not (tmp_path / "kanban_payload.json").exists()


def test_parse_guard_corrupt_without_transport_payload_never_raises(tmp_path):
    (tmp_path / "kanban_payload.json").write_text("not json", encoding="utf-8")
    assert ensure_kanban_payload_parseable(str(tmp_path), {}) == "skipped"
    assert ensure_kanban_payload_parseable(str(tmp_path), None) == "skipped"
    # left for the next export to regenerate -- guard never deletes
    assert (tmp_path / "kanban_payload.json").read_text(encoding="utf-8") == "not json"


def test_parse_guard_never_raises_when_regeneration_fails(tmp_path, monkeypatch):
    (tmp_path / "kanban_payload.json").write_text("{corrupt", encoding="utf-8")

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(db_utils, "write_kanban_payload", boom)
    status = ensure_kanban_payload_parseable(str(tmp_path), _payload([]))
    assert status == "failed"  # logged, non-fatal -- sync must never block


# ── acceptance (b): fresh DB pull/restore keeps FULL descriptions ─────────


def test_fresh_db_pull_restores_full_description_not_preview(tmp_path):
    """SPEC e401c69d main P0 guard: the transport round-trip must recover the
    FULL body on a fresh machine even though a preview kanban_payload.json is
    present in the bridge dir (it is render-only and ignored by import)."""
    full_body = "D" * 540408  # mirrors the d539f5ab ground-truth giant note
    source_db_path = str(tmp_path / "source.db")
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    init_db(source_db_path)
    created = now_iso()
    source = sqlite3.connect(source_db_path, isolation_level=None)
    source.row_factory = sqlite3.Row
    try:
        source.execute(
            "INSERT INTO tasks (id, title, description, status, section, priority, "
            "type, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "d539-full",
                "Giant note",
                full_body,
                "not_started",
                "inbox",
                "medium",
                "task",
                created,
                created,
            ),
        )
        export_task_files(source, str(bridge_dir))
        export_index_json(source, str(bridge_dir))
    finally:
        source.close()

    # The render preview sits alongside the transport, as it does after export.
    write_kanban_payload(
        str(bridge_dir),
        {
            "tasks": [
                {
                    "id": "d539-full",
                    "status": "not_started",
                    "section": "inbox",
                    "description": full_body,
                }
            ]
        },
    )
    preview = json.loads(
        (bridge_dir / "kanban_payload.json").read_text(encoding="utf-8")
    )
    assert preview["tasks"][0]["_mirror_preview"] is True  # preview IS truncated

    # Fresh machine: pull/restore from the transport only.
    remote_tasks, loaded_from_index = load_remote_tasks_for_merge(str(bridge_dir), {})
    restored_db_path = str(tmp_path / "restored.db")
    init_db(restored_db_path)
    restored = sqlite3.connect(restored_db_path, isolation_level=None)
    restored.row_factory = sqlite3.Row
    try:
        new_count, _updated = merge_import_tasks(
            restored, remote_tasks, import_content=True
        )
        row = restored.execute(
            "SELECT description FROM tasks WHERE id='d539-full'"
        ).fetchone()
    finally:
        restored.close()

    assert loaded_from_index is True
    assert new_count == 1
    assert len(row["description"]) == 540408
    assert row["description"] == full_body  # FULL body, NOT the preview
    # and nothing imported carries preview markers
    assert all("_mirror_preview" not in t for t in remote_tasks)


# ── wiring: pull paths engage the guard (never block, never serve corrupt) ─


def _git_cp(args, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(["git", *args], returncode, stdout, stderr)


def test_sync_worker_pull_only_regenerates_corrupt_kanban_payload(
    tmp_path, monkeypatch
):
    """pull_only skips export, so the guard is the ONLY thing standing between a
    union-merge-corrupted preview and the next Pages/PWA load."""
    db_path = str(tmp_path / "memory.db")
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    init_db(db_path)

    (bridge_dir / "shared.json").write_text(
        json.dumps(
            _payload(
                [
                    {
                        "id": "w1",
                        "title": "Done giant",
                        "type": "task",
                        "status": "done",
                        "section": "someday",
                        "priority": "medium",
                        "description": "H" * 80000,
                        "created_at": "2026-06-08T00:00:00+00:00",
                        "updated_at": "2026-06-08T00:00:00+00:00",
                    }
                ]
            )
        ),
        encoding="utf-8",
    )
    (bridge_dir / "kanban_payload.json").write_text("} corrupt {", encoding="utf-8")

    def fake_git_retry(repo_dir, *args, max_retries=3, timeout=30):
        if args[:3] == ("fetch", "origin", "main"):
            return _git_cp(args)
        if args[0] in ("rev-parse", "merge-base"):
            return _git_cp(args, stdout="same-sha\n")
        raise AssertionError(f"Unexpected git_retry call: {args}")

    monkeypatch.setattr(
        bridge_sync_worker, "ensure_bridge_repo_ready", lambda repo: (True, None)
    )
    monkeypatch.setattr(bridge_sync_worker, "git_retry", fake_git_retry)
    monkeypatch.setattr(
        bridge_sync_worker,
        "import_remote_bridge_data",
        lambda *a, **k: {"entities": 0, "relations": 0, "ratings": 0},
    )
    monkeypatch.setattr(
        bridge_sync_worker, "sync_task_attachments_from_remote", lambda *a, **k: (0, 0)
    )

    result = bridge_sync_worker.main(
        db_path=db_path,
        bridge_repo=str(bridge_dir),
        pull_only=True,
    )

    assert result["pull_only"] is True
    assert result["pushed"] is False
    data = json.loads((bridge_dir / "kanban_payload.json").read_text(encoding="utf-8"))
    assert data["_render_only"] is True
    assert len(data["tasks"][0]["description"]) <= KANBAN_COLLAPSE_MAX


def test_parse_guard_failure_does_not_block_pull_only(tmp_path, monkeypatch):
    """Even if the guard itself blows up, pull_only must complete (never block)."""
    db_path = str(tmp_path / "memory.db")
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    init_db(db_path)
    (bridge_dir / "kanban_payload.json").write_text("{nope", encoding="utf-8")

    def fake_git_retry(repo_dir, *args, max_retries=3, timeout=30):
        if args[:3] == ("fetch", "origin", "main"):
            return _git_cp(args)
        if args[0] in ("rev-parse", "merge-base"):
            return _git_cp(args, stdout="same-sha\n")
        raise AssertionError(f"Unexpected git_retry call: {args}")

    monkeypatch.setattr(
        bridge_sync_worker, "ensure_bridge_repo_ready", lambda repo: (True, None)
    )
    monkeypatch.setattr(bridge_sync_worker, "git_retry", fake_git_retry)
    monkeypatch.setattr(
        bridge_sync_worker,
        "import_remote_bridge_data",
        lambda *a, **k: {"entities": 0, "relations": 0, "ratings": 0},
    )
    monkeypatch.setattr(
        bridge_sync_worker, "load_remote_tasks_for_merge", lambda *a, **k: ([], True)
    )

    # write_kanban_payload exploding inside the guard must stay non-fatal
    def boom(*args, **kwargs):
        raise RuntimeError("regeneration blew up")

    monkeypatch.setattr(db_utils, "write_kanban_payload", boom)

    result = bridge_sync_worker.main(
        db_path=db_path,
        bridge_repo=str(bridge_dir),
        pull_only=True,
    )
    assert result["pull_only"] is True
