"""Phase 1 reflect_apply orchestration — accepted candidates → mutations.

Maps accepted reflection_candidates rows to canonical mutations on their
target rows (currently tasks; entities are skipped in MVP), writes
reflection_apply_snapshots with before/after JSON, and aggregates
per-candidate results into a summary.

Design choices baked into MVP:

- All mutations route through `db_utils.apply_task_mutation` so that
  memory_events + task_field_versions are written consistently with
  any other update path. No raw SQL UPDATEs.
- Action handlers per candidate_type are conservative defaults: every
  action either archives the row or clears a single field. We never
  hard-delete and never auto-merge or auto-fill — those decisions
  warrant their own per-candidate review and aren't safe defaults.
- Idempotency: a candidate that already has at least one snapshot
  for this run is skipped with reason="already_applied". This makes
  reflect_apply safe to retry without double-mutating.
- Entities are intentionally not applied in MVP — the entities table
  has no status column and archive semantics are non-trivial. Skipped
  with reason="entity_apply_not_implemented_in_mvp" and reported in
  the summary so a future iteration knows what work remains.

Spec source: corrections C8 (per-candidate review preserved + atomic
apply mode opt-in) and C9 (snapshots, never in-place mutation) on
entity MemoryReflection_DreamsAlignmentCorrections.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from db_utils import apply_task_mutation

import reflection_dao as dao


# Conservative defaults. Pass `task_action_handlers=...` to apply_run to
# override per call without touching this constant.
DEFAULT_TASK_ACTION_HANDLERS: dict[str, dict[str, Any]] = {
    "exact_duplicate_titles": {
        "changes": {"status": "archived"},
        "rationale": "duplicate_title_archive",
    },
    "stale_overdue_tasks": {
        "changes": {"status": "archived"},
        "rationale": "stale_overdue_archive",
    },
    "empty_description_notes": {
        "changes": {"status": "archived"},
        "rationale": "empty_note_archive",
    },
    "orphan_parent_tasks": {
        "changes": {"parent_id": None},
        "rationale": "orphan_parent_cleared",
    },
    "abandoned_inbox_items": {
        "changes": {"status": "archived"},
        "rationale": "abandoned_inbox_archive",
    },
}


_TASK_SNAPSHOT_FIELDS = (
    "id",
    "title",
    "description",
    "status",
    "priority",
    "section",
    "due_date",
    "project",
    "parent_id",
    "notes",
    "type",
    "updated_at",
)


def _fetch_task_state(
    conn: sqlite3.Connection, task_id: str
) -> dict[str, Any] | None:
    cols = ", ".join(_TASK_SNAPSHOT_FIELDS)
    row = conn.execute(
        f"SELECT {cols} FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    return dict(row) if row else None


def apply_run(
    conn: sqlite3.Connection,
    run_id: str,
    *,
    candidate_ids: list[str] | None = None,
    applied_by: str = "user",
    task_action_handlers: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Apply accepted candidates from a completed run; return summary.

    Args:
        run_id: id of a run in `completed` status.
        candidate_ids: optional whitelist; if provided, only these
            accepted candidates are considered. Useful for partial apply.
        applied_by: actor recorded on each snapshot row.
        task_action_handlers: optional override of the candidate_type ->
            mutation mapping. Defaults to DEFAULT_TASK_ACTION_HANDLERS.

    Returns:
        {
          run_id, considered, applied,
          skipped: [{candidate_id, reason, ...}],
          failed:  [{candidate_id, error, ...}],
        }
    """
    handlers = task_action_handlers or DEFAULT_TASK_ACTION_HANDLERS

    run = dao.get_run(conn, run_id)
    if run is None:
        raise dao.ReflectionStateError(f"run_not_found: {run_id}")
    if run["status"] != "completed":
        raise dao.ReflectionStateError(
            f"run_not_completed: status={run['status']}"
        )

    rows, _ = dao.list_candidates(
        conn, run_id, decision_filter="accept", limit=dao.MAX_CANDIDATES_PER_RUN
    )
    if candidate_ids is not None:
        wanted = set(candidate_ids)
        rows = [r for r in rows if r["candidate_id"] in wanted]

    summary: dict[str, Any] = {
        "run_id": run_id,
        "considered": len(rows),
        "applied": 0,
        "skipped": [],
        "failed": [],
    }

    for cand in rows:
        cid = cand["candidate_id"]
        target_kind = cand["target_kind"]
        target_ref = cand["target_ref"]
        cand_type = cand["candidate_type"]

        if dao.has_apply_snapshot(conn, run_id, cid):
            summary["skipped"].append(
                {"candidate_id": cid, "reason": "already_applied"}
            )
            continue

        if target_kind != "task":
            summary["skipped"].append(
                {
                    "candidate_id": cid,
                    "reason": f"{target_kind}_apply_not_implemented_in_mvp",
                    "target_kind": target_kind,
                }
            )
            continue

        handler = handlers.get(cand_type)
        if handler is None:
            summary["skipped"].append(
                {
                    "candidate_id": cid,
                    "reason": "no_handler_for_candidate_type",
                    "candidate_type": cand_type,
                }
            )
            continue

        before = _fetch_task_state(conn, target_ref)
        if before is None:
            summary["skipped"].append(
                {
                    "candidate_id": cid,
                    "reason": "target_not_found",
                    "target_ref": target_ref,
                }
            )
            continue

        try:
            apply_task_mutation(
                conn,
                target_ref,
                handler["changes"],
                tool_name="reflect_apply",
                actor_type="reflection",
                actor_id=run_id,
                source_kind="reflection_run",
                source_ref=run_id,
            )
        except Exception as exc:
            summary["failed"].append(
                {"candidate_id": cid, "error": str(exc)[:300]}
            )
            continue

        after = _fetch_task_state(conn, target_ref) or {}

        try:
            dao.add_apply_snapshot(
                conn,
                run_id,
                cid,
                target_kind=target_kind,
                target_ref=target_ref,
                before_state=before,
                after_state=after,
                applied_by=applied_by,
            )
        except Exception as exc:
            summary["failed"].append(
                {
                    "candidate_id": cid,
                    "error": f"snapshot_write_failed: {str(exc)[:300]}",
                }
            )
            continue

        summary["applied"] += 1

    return summary
