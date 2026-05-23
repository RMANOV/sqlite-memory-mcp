"""Smart Retrieval (Layer 1) — BM25 + multi-signal re-ranking.

Always-on, query-time enrichment: FTS5 top-N → Python re-rank with 6 signals → top-K.
Falls back gracefully to pure BM25 on any error.
"""

from __future__ import annotations

import math
import json
import logging
import sqlite3
from datetime import date, datetime, timezone
from typing import Any

from db_utils import TASK_ACTIVE_EXCLUSIONS, parse_iso_date, priority_sort_key

log = logging.getLogger(__name__)

# ── Tunable constants ──────────────────────────────────────────────────────

RECENCY_HALF_LIFE_DAYS = 14
PROJECT_BOOST = 1.5
GRAPH_BOOST_1HOP = 1.8
GRAPH_BOOST_2HOP = 1.3
RICHNESS_DIVISOR = 3.0
FACT_BOOST = 1.4
SESSION_BOOST = 2.0
RERANKING_POOL_SIZE = 100


# ── Scoring helpers ────────────────────────────────────────────────────────


def compute_recency_decay(updated_at: str | None, half_life_days: float = RECENCY_HALF_LIFE_DAYS) -> float:
    """Exponential decay: 2^(-days / half_life). Returns 1.0 if unparseable."""
    if not updated_at:
        return 0.5
    try:
        dt = datetime.fromisoformat(updated_at)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        days = (datetime.now(timezone.utc) - dt).total_seconds() / 86400
        return max(math.pow(2, -days / half_life_days), 0.1)
    except (ValueError, TypeError):
        return 0.5


def compute_composite_score(
    bm25_rank: float,
    updated_at: str | None,
    project: str | None,
    current_project: str | None,
    obs_count: int,
    relation_hops: int,
    has_canonical_facts: bool,
    in_active_session: bool,
) -> float:
    """Multiplicative composite score from 6 signals.

    bm25_rank is negative (lower = better match in FTS5), so we invert it.
    """
    # Base: invert BM25 rank (FTS5 rank is negative, closer to 0 = worse match)
    base = 1.0 / (1.0 + abs(bm25_rank))

    # Signal 1: recency
    recency = compute_recency_decay(updated_at)

    # Signal 2: project affinity
    proj = PROJECT_BOOST if (current_project and project == current_project) else 1.0

    # Signal 3: graph proximity
    if relation_hops == 1:
        graph = GRAPH_BOOST_1HOP
    elif relation_hops == 2:
        graph = GRAPH_BOOST_2HOP
    else:
        graph = 1.0

    # Signal 4: observation richness (log scale, capped)
    richness = 1.0 + math.log1p(obs_count) / RICHNESS_DIVISOR

    # Signal 5: canonical facts existence
    facts = FACT_BOOST if has_canonical_facts else 1.0

    # Signal 6: active session
    session = SESSION_BOOST if in_active_session else 1.0

    return base * recency * proj * graph * richness * facts * session


# ── Main re-ranking entry point ────────────────────────────────────────────


def rerank_entities(
    conn: sqlite3.Connection,
    fts_rows: list[Any],
    current_project: str | None,
    session_id: str | None,
    query_entity_ids: list[int] | None,
    limit: int = 50,
) -> list[dict]:
    """Re-rank FTS5 results using composite scoring.

    Args:
        conn: Active SQLite connection (within transaction).
        fts_rows: Rows from FTS5 query with eid, name, entity_type, project, rank.
        current_project: Current project for affinity boost.
        session_id: Current session ID for active-file boost.
        query_entity_ids: Entity IDs from the query itself (for graph proximity).
        limit: Max results to return.

    Returns:
        List of dicts with entity info + _meta scoring details.
    """
    if not fts_rows:
        return []

    eids = [r["eid"] for r in fts_rows]
    ph = ",".join("?" * len(eids))

    # Batch-fetch observation counts
    obs_counts: dict[int, int] = {}
    for row in conn.execute(
        f"SELECT entity_id, COUNT(*) AS cnt FROM observations "
        f"WHERE entity_id IN ({ph}) GROUP BY entity_id",
        eids,
    ):
        obs_counts[row["entity_id"]] = row["cnt"]

    # Batch-fetch updated_at timestamps
    updated_map: dict[int, str] = {}
    for row in conn.execute(
        f"SELECT id, updated_at FROM entities WHERE id IN ({ph})", eids
    ):
        updated_map[row["id"]] = row["updated_at"]

    # Batch-fetch 1-hop relations (entities connected to query entities)
    one_hop: set[int] = set()
    two_hop: set[int] = set()
    if query_entity_ids:
        q_ph = ",".join("?" * len(query_entity_ids))
        for row in conn.execute(
            f"SELECT DISTINCT to_id FROM relations WHERE from_id IN ({q_ph}) "
            f"UNION SELECT DISTINCT from_id FROM relations WHERE to_id IN ({q_ph})",
            query_entity_ids + query_entity_ids,
        ):
            one_hop.add(row[0])
        # Expand to 2-hop (only if manageable)
        if one_hop and len(one_hop) < 500:
            hop_ph = ",".join("?" * len(one_hop))
            hop_list = list(one_hop)
            for row in conn.execute(
                f"SELECT DISTINCT to_id FROM relations WHERE from_id IN ({hop_ph}) "
                f"UNION SELECT DISTINCT from_id FROM relations WHERE to_id IN ({hop_ph})",
                hop_list + hop_list,
            ):
                if row[0] not in one_hop:
                    two_hop.add(row[0])

    # Batch-check canonical_facts subjects
    facts_subjects: set[str] = set()
    eid_names = {r["eid"]: r["name"] for r in fts_rows}
    name_list = list(eid_names.values())
    if name_list:
        n_ph = ",".join("?" * len(name_list))
        try:
            for row in conn.execute(
                f"SELECT DISTINCT subject FROM canonical_facts WHERE subject IN ({n_ph})",
                name_list,
            ):
                facts_subjects.add(row["subject"])
        except sqlite3.OperationalError as exc:
            log.debug("canonical_facts lookup skipped: %s", exc, exc_info=True)

    # Session active files
    active_file_entities: set[str] = set()
    if session_id:
        try:
            srow = conn.execute(
                "SELECT active_files FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if srow and srow["active_files"]:
                files = json.loads(srow["active_files"])
                if isinstance(files, list):
                    active_file_entities = set(files)
        except (sqlite3.Error, json.JSONDecodeError, TypeError) as exc:
            log.debug("session active_files lookup skipped: %s", exc, exc_info=True)

    # Compute scores and build results
    scored: list[tuple[float, dict]] = []
    for r in fts_rows:
        eid = r["eid"]
        name = r["name"]

        # Determine graph proximity
        if eid in one_hop:
            hops = 1
        elif eid in two_hop:
            hops = 2
        else:
            hops = 0

        score = compute_composite_score(
            bm25_rank=r["rank"],
            updated_at=updated_map.get(eid),
            project=r["project"],
            current_project=current_project,
            obs_count=obs_counts.get(eid, 0),
            relation_hops=hops,
            has_canonical_facts=name in facts_subjects,
            in_active_session=name in active_file_entities,
        )

        scored.append((score, {
            "eid": eid,
            "name": name,
            "entity_type": r["entity_type"],
            "project": r["project"],
            "_score": round(score, 6),
        }))

    # Sort descending by score, truncate
    scored.sort(key=lambda x: x[0], reverse=True)
    return [item for _, item in scored[:limit]]


# ── Ready context surfaces ────────────────────────────────────────────────

READY_CONTEXT_CONTRACT_VERSION = "ready_context.v0"

READY_STATES = (
    "ready_now",
    "suggested_ready",
    "blocked",
    "waiting",
    "cleanup_candidate",
    "excluded",
)

REASON_CODES = frozenset(
    {
        "active_delivery_pressure",
        "blocked_by_open_item",
        "bridge_sync_caution",
        "cleanup_candidate",
        "critical_priority",
        "done_but_recently_confused",
        "due_or_overdue",
        "explicit_user_correction",
        "external_commitment_risk",
        "machine_anomaly_open",
        "reading_surface",
        "reopen_requested_by_user",
        "stale_but_unresolved",
        "waiting_followup_date",
    }
)

_READY_STATE_RANK = {
    "ready_now": 0,
    "suggested_ready": 1,
    "blocked": 2,
    "waiting": 3,
    "cleanup_candidate": 4,
    "excluded": 5,
}
_READY_URGENCY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}
_BLOCKER_TERMS = (
    ("needs_user_decision", ("under-specified", "underspecified", "acceptance rule")),
    ("missing_input", ("missing input", "needs input", "permission", "awaiting")),
    ("unsafe_without_verification", ("unsafe", "verify first", "verification")),
    ("external_dependency", ("waiting on", "external dependency")),
    ("blocked_by", ("blocked by", "blocker")),
)
_READING_MARKERS = (
    "reading",
    "readings",
    "mama-reading",
    "critical-readings",
    "booklet",
    "epub",
)


def _ready_task_text(task: dict[str, Any]) -> str:
    return " ".join(
        str(task.get(k) or "")
        for k in ("title", "description", "notes", "project", "section")
    ).casefold()


def _ready_is_reading(task: dict[str, Any]) -> bool:
    text = _ready_task_text(task)
    return any(marker in text for marker in _READING_MARKERS)


def _ready_has_explicit_surface(task: dict[str, Any]) -> bool:
    text = _ready_task_text(task)
    return any(
        marker in text
        for marker in (
            "pin",
            "pinned",
            "surface",
            "surface_until",
            "curated-reading",
        )
    )


def _ready_bridge_sync_caution(task: dict[str, Any]) -> bool:
    text = _ready_task_text(task)
    return "bridge" in text and any(
        marker in text for marker in ("sync", "updated_at", "import", "churn")
    )


def _infer_ready_blockers(task: dict[str, Any]) -> list[dict[str, str]]:
    text = _ready_task_text(task)
    blockers: list[dict[str, str]] = []

    if task.get("section") == "waiting":
        blockers.append({"category": "waiting_on", "detail": "section=waiting"})

    for category, terms in _BLOCKER_TERMS:
        if any(term in text for term in terms):
            blockers.append({"category": category, "detail": "matched task text"})

    seen: set[tuple[str, str]] = set()
    unique = []
    for blocker in blockers:
        key = (blocker["category"], blocker["detail"])
        if key not in seen:
            unique.append(blocker)
            seen.add(key)
    return unique


def _ready_reason_codes(
    task: dict[str, Any],
    *,
    due: date | None,
    today: date,
    include_readings: bool,
) -> list[str]:
    codes: list[str] = []
    text = _ready_task_text(task)

    if task.get("priority") == "critical":
        codes.append("critical_priority")
    if due is not None and due <= today:
        codes.append("due_or_overdue")
    if task.get("section") == "today" or _ready_has_explicit_surface(task):
        codes.append("explicit_user_correction")
    if task.get("section") == "waiting" or task.get("reminder_at"):
        codes.append("waiting_followup_date")
    if any(term in text for term in ("delivery", "ship", "release", "deadline")):
        codes.append("active_delivery_pressure")
    if any(term in text for term in ("external commitment", "commitment", "apply")):
        codes.append("external_commitment_risk")
    if any(term in text for term in ("machine", "thermal", "bridge", "tray", "sync")):
        codes.append("machine_anomaly_open")
    if "stale" in text or "unresolved" in text:
        codes.append("stale_but_unresolved")
    if _ready_bridge_sync_caution(task):
        codes.append("bridge_sync_caution")
    if _ready_is_reading(task):
        codes.append("reading_surface")
    if "cleanup_candidate" in text or "superseded" in text or "duplicate" in text:
        codes.append("cleanup_candidate")
    if "done_but_recently_confused" in text:
        codes.append("done_but_recently_confused")
    if "reopen_requested_by_user" in text or "reopen" in text:
        codes.append("reopen_requested_by_user")
    if _infer_ready_blockers(task):
        codes.append("blocked_by_open_item")

    if _ready_is_reading(task) and not include_readings and "reading_surface" not in codes:
        codes.append("reading_surface")

    return [code for code in dict.fromkeys(codes) if code in REASON_CODES]


def _ready_urgency(
    task: dict[str, Any],
    *,
    due: date | None,
    today: date,
    blockers: list[dict[str, str]],
    reason_codes: list[str],
) -> tuple[str, str]:
    priority = task.get("priority") or "medium"
    if due is not None and due < today and priority in {"critical", "high"}:
        return "critical", "overdue high-risk item"
    if "critical_priority" in reason_codes or "external_commitment_risk" in reason_codes:
        return "high", "critical priority or external commitment"
    if due is not None and due <= today:
        return "high", "due now"
    if blockers:
        return "medium", "blocked but still operationally relevant"
    if task.get("section") == "today" or task.get("status") == "in_progress":
        return "medium", "explicitly active"
    if priority == "high":
        return "medium", "high priority"
    return "low", "background candidate"


def _ready_next_action(
    task: dict[str, Any],
    *,
    ready_state: str,
    blockers: list[dict[str, str]],
) -> str:
    if blockers:
        categories = ", ".join(b["category"] for b in blockers)
        return f"Resolve blocker(s): {categories}"
    if ready_state == "cleanup_candidate":
        return "Review and close/archive only if evidence is unambiguous"
    if task.get("section") == "today" or ready_state == "ready_now":
        return "Execute or assign next concrete step"
    if ready_state == "waiting":
        return "Check waiting condition or follow-up date"
    return "Clarify next action or schedule"


def _ready_state(
    task: dict[str, Any],
    *,
    due: date | None,
    today: date,
    blockers: list[dict[str, str]],
    reason_codes: list[str],
    include_readings: bool,
) -> str:
    status = task.get("status")
    section = task.get("section")
    task_type = task.get("type") or "task"

    if status in TASK_ACTIVE_EXCLUSIONS:
        reopen_codes = {
            "cleanup_candidate",
            "done_but_recently_confused",
            "reopen_requested_by_user",
        }
        if reopen_codes & set(reason_codes):
            return "cleanup_candidate"
        return "excluded"

    if task_type == "note" and _ready_is_reading(task) and not include_readings:
        if section == "today" or due is not None or _ready_has_explicit_surface(task):
            return "suggested_ready"
        return "excluded"

    if "cleanup_candidate" in reason_codes:
        return "cleanup_candidate"
    if blockers:
        return "waiting" if any(b["category"] == "waiting_on" for b in blockers) else "blocked"
    if section == "waiting":
        return "waiting"
    if status == "in_progress" or section == "today" or (due is not None and due <= today):
        return "ready_now"
    if section == "someday" and not _ready_has_explicit_surface(task) and due is None:
        return "excluded"
    if task.get("priority") in {"critical", "high"} or section in {"inbox", "next"}:
        return "suggested_ready"
    return "suggested_ready"


def _ready_sort_key(record: dict[str, Any]) -> tuple:
    task = record["task"]
    due = task.get("due_date") or "9999-12-31"
    return (
        _READY_STATE_RANK.get(record["ready_state"], 9),
        _READY_URGENCY_RANK.get(record["urgency"], 9),
        priority_sort_key(task),
        due,
        task.get("created_at") or "",
    )


def build_ready_record(
    task: dict[str, Any],
    *,
    include_readings: bool = False,
    today: date | None = None,
) -> dict[str, Any]:
    """Build one deterministic ready-context record from a task row."""
    today = today or date.today()
    due = parse_iso_date(task.get("due_date"))
    blockers = _infer_ready_blockers(task)
    reason_codes = _ready_reason_codes(
        task,
        due=due,
        today=today,
        include_readings=include_readings,
    )
    ready_state = _ready_state(
        task,
        due=due,
        today=today,
        blockers=blockers,
        reason_codes=reason_codes,
        include_readings=include_readings,
    )
    urgency, urgency_reason = _ready_urgency(
        task,
        due=due,
        today=today,
        blockers=blockers,
        reason_codes=reason_codes,
    )
    stale_warning = (
        "bridge/import/updated_at signal requires content-level verification"
        if "bridge_sync_caution" in reason_codes
        else ""
    )
    confidence = "high"
    if blockers or stale_warning:
        confidence = "medium"
    if ready_state in {"cleanup_candidate", "excluded"}:
        confidence = "low"

    return {
        "id": task.get("id"),
        "type": task.get("type") or "task",
        "title": task.get("title") or "",
        "status": task.get("status") or "not_started",
        "section": task.get("section") or "inbox",
        "priority": task.get("priority") or "medium",
        "due_date": task.get("due_date"),
        "project": task.get("project"),
        "ready_state": ready_state,
        "blockers": blockers,
        "urgency": urgency,
        "urgency_reason": urgency_reason,
        "reason_codes": reason_codes or ["stale_but_unresolved"],
        "provenance": {
            "source_kind": "task",
            "source_id": task.get("id"),
            "created_at": task.get("created_at"),
            "updated_at": task.get("updated_at"),
            "rule_version": READY_CONTEXT_CONTRACT_VERSION,
        },
        "next_action": _ready_next_action(
            task,
            ready_state=ready_state,
            blockers=blockers,
        ),
        "confidence": confidence,
        "stale_warning": stale_warning,
        "task": dict(task),
    }


def ready_context(
    tasks: list[dict[str, Any]],
    *,
    include_readings: bool = False,
    include_excluded: bool = False,
    today: date | None = None,
) -> list[dict[str, Any]]:
    """Return structured ready-context records sorted by operational readiness."""
    records = [
        build_ready_record(task, include_readings=include_readings, today=today)
        for task in tasks
    ]
    if not include_excluded:
        records = [r for r in records if r["ready_state"] != "excluded"]
    return sorted(records, key=_ready_sort_key)


def attach_ready_metadata(record: dict[str, Any]) -> dict[str, Any]:
    """Return a task row copy annotated for tray display/filtering."""
    task = dict(record["task"])
    task["_ready_state"] = record["ready_state"]
    task["_ready_urgency"] = record["urgency"]
    task["_ready_reason_codes"] = list(record["reason_codes"])
    task["_ready_next_action"] = record["next_action"]
    task["_ready_blockers"] = list(record["blockers"])
    task["_ready_stale_warning"] = record["stale_warning"]
    task["_ready_confidence"] = record["confidence"]
    return task


def suggested_ready(
    tasks: list[dict[str, Any]],
    *,
    include_readings: bool = False,
    limit: int | None = 12,
    today: date | None = None,
) -> list[dict[str, Any]]:
    """Return tray-ready task rows derived from ready-context policy."""
    records = [
        record
        for record in ready_context(
            tasks,
            include_readings=include_readings,
            include_excluded=False,
            today=today,
        )
        if record["ready_state"]
        in {"ready_now", "suggested_ready", "blocked", "waiting"}
    ]
    rows = [attach_ready_metadata(record) for record in records]
    return rows if limit is None else rows[:limit]


def prime_context(
    tasks: list[dict[str, Any]],
    *,
    include_readings: bool = False,
    limit: int = 12,
    today: date | None = None,
) -> dict[str, Any]:
    """Build a compact boot pack from the same ready-context records."""
    records = ready_context(tasks, include_readings=include_readings, today=today)
    top = records[:limit]
    return {
        "contract_version": READY_CONTEXT_CONTRACT_VERSION,
        "current_mandate": "Use deterministic ready_context records before broad memory search.",
        "top_ready_items": top,
        "blocked_or_waiting": [
            r for r in records if r["ready_state"] in {"blocked", "waiting"}
        ][:limit],
        "cleanup_candidates": [
            r for r in records if r["ready_state"] == "cleanup_candidate"
        ][:limit],
        "explicit_exclusions": [
            build_ready_record(t, include_readings=include_readings, today=today)
            for t in tasks
            if (t.get("status") in TASK_ACTIVE_EXCLUSIONS)
        ][:limit],
        "risk_or_escalation_items": [
            r
            for r in records
            if r["urgency"] in {"critical", "high"} or r["blockers"]
        ][:limit],
        "evidence_refs": [r["provenance"] for r in top],
    }
