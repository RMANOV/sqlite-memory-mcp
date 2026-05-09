"""Phase 1 DAO for Memory Reflection — async consolidation runs.

CRUD over the 4 Phase 1 tables (reflection_runs, reflection_inputs,
reflection_candidates, reflection_apply_snapshots). Pure SQL helpers, no
MCP dependency. State machine guards live here so MCP tools and any
direct callers stay consistent: cancel rejects terminal states, archive
requires terminal state, decide validates enum.

Spec: notes 0ea75f2a + 5a4be019, corrections C1, C2, C5, C6, C9, C10,
C11, C13 (entity MemoryReflection_DreamsAlignmentCorrections).
"""

from __future__ import annotations

import sqlite3
import uuid
from typing import Any

from db_utils import json_dumps, json_loads, now_iso


# ── Constants ─────────────────────────────────────────────────────────────

VALID_STATUSES = ("pending", "running", "completed", "failed", "canceled")
TERMINAL_STATUSES = ("completed", "failed", "canceled")
VALID_INPUT_TYPES = ("tasks", "sessions", "entities", "notes")
VALID_TARGET_KINDS = ("task", "entity", "note", "observation")
VALID_DECISIONS = ("accept", "reject", "defer")

VALID_ERROR_TYPES = (
    "timeout",
    "internal_error",
    "input_session_unavailable",
    "input_too_large",
    "instructions_too_long",
    "candidate_limit_exceeded",
)

# Limits (C14)
MAX_INSTRUCTIONS_CHARS = 4096
MAX_SESSIONS_PER_RUN = 100
MAX_CANDIDATES_PER_RUN = 10_000


class ReflectionStateError(ValueError):
    """Raised when a state-machine transition is rejected."""


# ── Helpers ───────────────────────────────────────────────────────────────


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:16]}"


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


# ── reflection_runs ──────────────────────────────────────────────────────


def create_run(
    conn: sqlite3.Connection,
    *,
    version: str = "reflect_v1.0",
    model: str | None = None,
    instructions: str | None = None,
    created_by: str = "system",
    run_id: str | None = None,
) -> str:
    """Insert a fresh run row in `pending` state. Returns the run_id.

    Validates instructions length per C14. Caller is responsible for
    creating reflection_inputs rows; they are not part of this insert.
    """
    if instructions is not None and len(instructions) > MAX_INSTRUCTIONS_CHARS:
        raise ReflectionStateError(
            f"instructions_too_long: {len(instructions)} > {MAX_INSTRUCTIONS_CHARS}"
        )
    rid = run_id or _new_id("rfl")
    conn.execute(
        "INSERT INTO reflection_runs (run_id, version, status, model, instructions, "
        "created_by, created_at) VALUES (?, ?, 'pending', ?, ?, ?, ?)",
        (rid, version, model, instructions, created_by, now_iso()),
    )
    return rid


def get_run(conn: sqlite3.Connection, run_id: str) -> dict[str, Any] | None:
    """Fetch run by id; returns dict or None if missing."""
    row = conn.execute(
        "SELECT * FROM reflection_runs WHERE run_id = ?", (run_id,)
    ).fetchone()
    return _row_to_dict(row)


def start_run(conn: sqlite3.Connection, run_id: str) -> bool:
    """Transition pending → running. Idempotent if already running.

    Returns True if a state change occurred. Rejects terminal states.
    """
    row = get_run(conn, run_id)
    if row is None:
        raise ReflectionStateError(f"run_not_found: {run_id}")
    if row["status"] == "running":
        return False
    if row["status"] in TERMINAL_STATUSES:
        raise ReflectionStateError(
            f"cannot_start_terminal_run: status={row['status']}"
        )
    conn.execute(
        "UPDATE reflection_runs SET status = 'running', started_at = ? WHERE run_id = ?",
        (now_iso(), run_id),
    )
    return True


def finish_run(
    conn: sqlite3.Connection,
    run_id: str,
    status: str,
    *,
    error_type: str | None = None,
    error_message: str | None = None,
    usage: dict[str, Any] | None = None,
) -> bool:
    """Transition to a terminal status. Sets ended_at and optional error/usage.

    Status must be one of completed | failed | canceled. Returns True on
    successful transition; False if already terminal.
    """
    if status not in TERMINAL_STATUSES:
        raise ReflectionStateError(
            f"finish_requires_terminal_status: got {status}"
        )
    if error_type is not None and error_type not in VALID_ERROR_TYPES:
        raise ReflectionStateError(f"unknown_error_type: {error_type}")
    row = get_run(conn, run_id)
    if row is None:
        raise ReflectionStateError(f"run_not_found: {run_id}")
    if row["status"] in TERMINAL_STATUSES:
        return False
    usage_json = json_dumps(usage) if usage is not None else None
    conn.execute(
        "UPDATE reflection_runs SET status = ?, ended_at = ?, error_type = ?, "
        "error_message = ?, usage_json = ? WHERE run_id = ?",
        (status, now_iso(), error_type, error_message, usage_json, run_id),
    )
    return True


def cancel_run(conn: sqlite3.Connection, run_id: str) -> bool:
    """Cancel a pending/running run. Rejects terminal states (Dreams parity)."""
    row = get_run(conn, run_id)
    if row is None:
        raise ReflectionStateError(f"run_not_found: {run_id}")
    if row["status"] in TERMINAL_STATUSES:
        raise ReflectionStateError(
            f"cannot_cancel_terminal_run: status={row['status']}"
        )
    return finish_run(conn, run_id, "canceled")


def archive_run(conn: sqlite3.Connection, run_id: str) -> bool:
    """Archive a terminal run. Idempotent; rejects pending/running."""
    row = get_run(conn, run_id)
    if row is None:
        raise ReflectionStateError(f"run_not_found: {run_id}")
    if row["status"] not in TERMINAL_STATUSES:
        raise ReflectionStateError(
            f"cannot_archive_active_run: status={row['status']}"
        )
    if row["archived_at"] is not None:
        return False
    conn.execute(
        "UPDATE reflection_runs SET archived_at = ? WHERE run_id = ?",
        (now_iso(), run_id),
    )
    return True


def list_runs(
    conn: sqlite3.Connection,
    *,
    limit: int = 20,
    offset: int = 0,
    include_archived: bool = False,
    status_filter: str | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Paginated list of runs newest-first. Returns (rows, total_count)."""
    limit = max(1, min(limit, 100))
    offset = max(0, offset)
    where_clauses: list[str] = []
    params: list[Any] = []
    if not include_archived:
        where_clauses.append("archived_at IS NULL")
    if status_filter is not None:
        if status_filter not in VALID_STATUSES:
            raise ReflectionStateError(f"unknown_status: {status_filter}")
        where_clauses.append("status = ?")
        params.append(status_filter)
    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
    total_row = conn.execute(
        f"SELECT COUNT(*) AS c FROM reflection_runs {where_sql}", params
    ).fetchone()
    total = int(total_row["c"]) if total_row else 0
    rows = conn.execute(
        f"SELECT * FROM reflection_runs {where_sql} "
        f"ORDER BY created_at DESC LIMIT ? OFFSET ?",
        [*params, limit, offset],
    ).fetchall()
    return [dict(r) for r in rows], total


# ── reflection_inputs ────────────────────────────────────────────────────


def add_input(
    conn: sqlite3.Connection,
    run_id: str,
    input_type: str,
    input_ref: dict[str, Any] | list[Any],
) -> str:
    """Attach an input resource to a run."""
    if input_type not in VALID_INPUT_TYPES:
        raise ReflectionStateError(f"unknown_input_type: {input_type}")
    if input_type == "sessions" and isinstance(input_ref, dict):
        sids = input_ref.get("session_ids") or []
        if isinstance(sids, list) and len(sids) > MAX_SESSIONS_PER_RUN:
            raise ReflectionStateError(
                f"input_too_large: {len(sids)} sessions > {MAX_SESSIONS_PER_RUN}"
            )
    iid = _new_id("in")
    conn.execute(
        "INSERT INTO reflection_inputs (input_id, run_id, input_type, "
        "input_ref_json, created_at) VALUES (?, ?, ?, ?, ?)",
        (iid, run_id, input_type, json_dumps(input_ref), now_iso()),
    )
    return iid


def list_inputs(conn: sqlite3.Connection, run_id: str) -> list[dict[str, Any]]:
    """All inputs for a run, decoded JSON in `input_ref` field."""
    rows = conn.execute(
        "SELECT * FROM reflection_inputs WHERE run_id = ? ORDER BY created_at",
        (run_id,),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        try:
            d["input_ref"] = json_loads(d.pop("input_ref_json") or "{}")
        except (ValueError, TypeError):
            d["input_ref"] = {}
        out.append(d)
    return out


# ── reflection_candidates ────────────────────────────────────────────────


def add_candidate(
    conn: sqlite3.Connection,
    run_id: str,
    *,
    candidate_type: str,
    suggested_action: str,
    target_kind: str,
    target_ref: str,
    evidence: dict[str, Any],
    proposed_state: dict[str, Any] | None = None,
    confidence: float | None = None,
    candidate_id: str | None = None,
) -> str:
    """Persist one consolidation proposal. Decision starts NULL."""
    if target_kind not in VALID_TARGET_KINDS:
        raise ReflectionStateError(f"unknown_target_kind: {target_kind}")
    cid = candidate_id or _new_id("cand")
    conn.execute(
        "INSERT INTO reflection_candidates "
        "(candidate_id, run_id, candidate_type, suggested_action, target_kind, "
        "target_ref, evidence_json, proposed_state_json, confidence, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            cid,
            run_id,
            candidate_type,
            suggested_action,
            target_kind,
            target_ref,
            json_dumps(evidence),
            json_dumps(proposed_state) if proposed_state is not None else None,
            confidence,
            now_iso(),
        ),
    )
    return cid


def get_candidate(
    conn: sqlite3.Connection, candidate_id: str
) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM reflection_candidates WHERE candidate_id = ?",
        (candidate_id,),
    ).fetchone()
    if row is None:
        return None
    d = dict(row)
    try:
        d["evidence"] = json_loads(d.pop("evidence_json") or "{}")
    except (ValueError, TypeError):
        d["evidence"] = {}
    proposed_raw = d.pop("proposed_state_json", None)
    if proposed_raw:
        try:
            d["proposed_state"] = json_loads(proposed_raw)
        except (ValueError, TypeError):
            d["proposed_state"] = None
    else:
        d["proposed_state"] = None
    return d


def list_candidates(
    conn: sqlite3.Connection,
    run_id: str,
    *,
    decision_filter: str | None = None,
    candidate_type_filter: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    """Paginated list of candidates for a run, optional decision/type filter."""
    if decision_filter is not None and decision_filter not in (
        *VALID_DECISIONS,
        "pending",
    ):
        raise ReflectionStateError(f"unknown_decision_filter: {decision_filter}")
    limit = max(1, min(limit, 1000))
    offset = max(0, offset)
    where_clauses = ["run_id = ?"]
    params: list[Any] = [run_id]
    if decision_filter == "pending":
        where_clauses.append("human_decision IS NULL")
    elif decision_filter is not None:
        where_clauses.append("human_decision = ?")
        params.append(decision_filter)
    if candidate_type_filter:
        where_clauses.append("candidate_type = ?")
        params.append(candidate_type_filter)
    where_sql = "WHERE " + " AND ".join(where_clauses)
    total_row = conn.execute(
        f"SELECT COUNT(*) AS c FROM reflection_candidates {where_sql}", params
    ).fetchone()
    total = int(total_row["c"]) if total_row else 0
    rows = conn.execute(
        f"SELECT * FROM reflection_candidates {where_sql} "
        f"ORDER BY created_at LIMIT ? OFFSET ?",
        [*params, limit, offset],
    ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        try:
            d["evidence"] = json_loads(d.pop("evidence_json") or "{}")
        except (ValueError, TypeError):
            d["evidence"] = {}
        d.pop("proposed_state_json", None)
        out.append(d)
    return out, total


def decide_candidate(
    conn: sqlite3.Connection,
    candidate_id: str,
    decision: str,
    decided_by: str = "user",
) -> bool:
    """Apply human_decision to a candidate. Validates enum."""
    if decision not in VALID_DECISIONS:
        raise ReflectionStateError(f"unknown_decision: {decision}")
    cur = conn.execute(
        "UPDATE reflection_candidates SET human_decision = ?, decided_by = ?, "
        "decided_at = ? WHERE candidate_id = ?",
        (decision, decided_by, now_iso(), candidate_id),
    )
    return cur.rowcount > 0


def candidate_decision_counts(
    conn: sqlite3.Connection, run_id: str
) -> dict[str, int]:
    """Aggregate count of candidates per decision (incl. pending=NULL)."""
    rows = conn.execute(
        "SELECT COALESCE(human_decision, 'pending') AS d, COUNT(*) AS c "
        "FROM reflection_candidates WHERE run_id = ? GROUP BY d",
        (run_id,),
    ).fetchall()
    counts = {"pending": 0, "accept": 0, "reject": 0, "defer": 0}
    for r in rows:
        counts[r["d"]] = int(r["c"])
    counts["total"] = sum(counts.values())
    return counts


# ── Limits enforcement (C14) ────────────────────────────────────────────


def enforce_candidate_limit(conn: sqlite3.Connection, run_id: str) -> int:
    """Return current candidate count for a run. Caller raises if > MAX."""
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM reflection_candidates WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    return int(row["c"]) if row else 0
