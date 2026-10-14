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
- Freshness: compare the frozen review state and field versions under a
  writer lock. Changed/missing review states invalidate the candidate with a
  durable refusal; a fresh run and review are required.
- Atomicity: each mutation, ledger update and snapshot share a savepoint.
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


def _apply_candidate(conn, run_id, cid, handlers, applied_by):
    # The caller holds the writer lock. Re-read the decision and idempotency
    # marker here: the earlier accepted-candidate list is only a work queue.
    cand = dao.get_candidate(conn, cid)
    if cand is None or cand["run_id"] != run_id:
        return "candidate_not_found"
    if dao.has_apply_snapshot(conn, run_id, cid):
        return "already_applied"
    if cand["human_decision"] != "accept":
        return "decision_not_accepted"
    run = dao.get_run(conn, run_id)
    if run is None or run["status"] != "completed":
        return "run_not_completed"
    if cand["apply_error"] is not None:
        return "candidate_requires_new_review"
    if cand["target_kind"] != "task":
        return f"{cand['target_kind']}_apply_not_implemented_in_mvp"
    handler = handlers.get(cand["candidate_type"])
    if handler is None:
        return "no_handler_for_candidate_type"

    current = dao.task_review_state(conn, cand["target_ref"])
    reviewed = cand["reviewed_state"]
    reason = None
    if current is None:
        reason = "target_not_found"
    elif not isinstance(reviewed, dict) or set(reviewed) != {"task", "field_versions"}:
        reason = "missing_review_state"
    elif reviewed != current:
        reason = "stale_review"
    if reason:
        dao.record_apply_refusal(conn, cid, reason, applied_by)
        return reason

    result = apply_task_mutation(
        conn,
        cand["target_ref"],
        handler["changes"],
        tool_name="reflect_apply",
        actor_type="reflection",
        actor_id=run_id,
        source_kind="reflection_run",
        source_ref=run_id,
    )
    if result.get("updated") != 1:
        raise dao.ReflectionStateError("task_mutation_not_applied")
    after = dao.task_review_state(conn, cand["target_ref"])
    if after is None:
        raise dao.ReflectionStateError("target_missing_after_mutation")
    # These writes are in the same savepoint as the mutation and its ledger.
    # A snapshot failure rolls all of them back before a failure is reported.
    try:
        dao.add_apply_snapshot(
            conn,
            run_id,
            cid,
            target_kind="task",
            target_ref=cand["target_ref"],
            before_state=current["task"],
            after_state=after["task"],
            applied_by=applied_by,
        )
    except Exception as exc:
        raise dao.ReflectionStateError(
            f"snapshot_write_failed: {str(exc)[:300]}"
        ) from exc
    return None


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
        raise dao.ReflectionStateError(f"run_not_completed: status={run['status']}")

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
        try:
            with dao.candidate_write_transaction(conn, cid):
                reason = _apply_candidate(conn, run_id, cid, handlers, applied_by)
        except Exception as exc:
            summary["failed"].append({"candidate_id": cid, "error": str(exc)[:300]})
            continue
        if reason is not None:
            skipped = {"candidate_id": cid, "reason": reason}
            if reason.endswith("_apply_not_implemented_in_mvp"):
                skipped["target_kind"] = cand["target_kind"]
            elif reason == "no_handler_for_candidate_type":
                skipped["candidate_type"] = cand["candidate_type"]
            elif reason == "target_not_found":
                skipped["target_ref"] = cand["target_ref"]
            summary["skipped"].append(skipped)
        else:
            summary["applied"] += 1

    return summary
