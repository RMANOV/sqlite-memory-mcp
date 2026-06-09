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
"""

import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from db_utils import (  # noqa: E402
    BRIDGE_GENERATED_FILES,
    KANBAN_BIG_THRESHOLD,
    KANBAN_COLLAPSE_MAX,
    KANBAN_PREVIEW_MAX,
    _kanban_preview_task,
    is_generated_bridge_path,
    write_kanban_payload,
)
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
