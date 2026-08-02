"""Smart Retrieval (Layer 1) — BM25 + multi-signal re-ranking.

Always-on, query-time enrichment: FTS5 top-N → Python re-rank with 6 signals → top-K.
Falls back gracefully to pure BM25 on any error.
"""

from __future__ import annotations

import math
import json
import logging
import re
import sqlite3
from datetime import date
from typing import Any

from db_utils import (
    TASK_ACTIVE_EXCLUSIONS,
    compute_recency_decay as _compute_recency_decay,
    parse_iso_date,
    priority_sort_key,
)

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


def compute_recency_decay(
    updated_at: str | None,
    half_life_days: float = RECENCY_HALF_LIFE_DAYS,
) -> float:
    """Layer-1 recency policy backed by the shared decay primitive."""
    return _compute_recency_decay(
        updated_at,
        half_life_days=half_life_days,
        floor=0.1,
    )


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

        scored.append(
            (
                score,
                {
                    "eid": eid,
                    "name": name,
                    "entity_type": r["entity_type"],
                    "project": r["project"],
                    "_score": round(score, 6),
                },
            )
        )

    # Sort descending by score, truncate
    scored.sort(key=lambda x: x[0], reverse=True)
    return [item for _, item in scored[:limit]]


# ── Ready context surfaces ────────────────────────────────────────────────

# v0 -> v1 (Wave-2 B3): ADDITIVE response fields only — today_used,
# reason_primary, sort_position; prime mode emits mandate when empty. No field
# was removed or renamed, so v1 readers stay backward compatible with v0 data.
READY_CONTEXT_CONTRACT_VERSION = "ready_context.v1"

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

# Deterministic precedence for reason_primary (B3, contract v1). The winning
# reason is the highest-precedence code present in a record's reason_codes; if
# none of these match (defensive), fall back to the first emitted code. This is
# a pure, total ordering over REASON_CODES so reason_primary is reproducible.
_REASON_PRIMARY_ORDER = (
    "explicit_user_correction",
    "reopen_requested_by_user",
    "done_but_recently_confused",
    "blocked_by_open_item",
    "due_or_overdue",
    "external_commitment_risk",
    "active_delivery_pressure",
    "critical_priority",
    "machine_anomaly_open",
    "waiting_followup_date",
    "bridge_sync_caution",
    "cleanup_candidate",
    "reading_surface",
    "stale_but_unresolved",
)
_REASON_PRIMARY_RANK = {code: i for i, code in enumerate(_REASON_PRIMARY_ORDER)}


def _ready_reason_primary(reason_codes: list[str]) -> str:
    """Deterministically pick the winning reason from reason_codes.

    Highest-precedence code in ``_REASON_PRIMARY_ORDER`` wins; on a tie/miss the
    first emitted code is used so the result is total and reproducible for a
    fixed reason_codes list. Empty input yields the contract sentinel.
    """
    if not reason_codes:
        return "stale_but_unresolved"
    return min(
        reason_codes,
        key=lambda code: (_REASON_PRIMARY_RANK.get(code, len(_REASON_PRIMARY_ORDER)),),
    )


# Inflected forms an operator actually writes. Word-boundary anchoring alone
# turns every inflection into a silent false *negative*: "two blockers remain"
# stopped matching ``blocker`` entirely, so the blocked state and its blocker
# list were lost, not merely downgraded. ``_READING_MARKERS`` already carried
# this precedent by hand (``reading`` *and* ``readings``); this table
# generalises it.
#
# Forms are enumerated rather than expressed as a suffix character class on
# purpose: every accepted string stays greppable, so widening one term can
# never leak into another. Only unambiguous forms are listed — ``import``
# gains "imported"/"importer" but never "important"/"importance", and
# ``bridge`` gains nothing, because "bridged" was one of the false positives
# the word-boundary fix was written to kill.
_MARKER_INFLECTIONS: dict[str, tuple[str, ...]] = {
    "blocker": ("blockers",),
    "commitment": ("commitments",),
    "deadline": ("deadlines",),
    "duplicate": ("duplicates",),
    "import": ("imports", "imported", "importer", "importers"),
    "machine": ("machines",),
    "permission": ("permissions",),
    "reopen": ("reopens", "reopened", "reopening"),
    "stale": ("staleness",),
    "sync": ("syncs", "synced", "syncing", "unsynced"),
}


def _marker_pattern(*terms: str) -> re.Pattern[str]:
    """Compile one word-boundary alternation over literal marker terms.

    Every classifier below used ``marker in text`` substring matching, which
    fired on incidental prose: ``pin`` matched "mapping"/"opinion"/"shaping",
    ``sync`` matched "asynchronous", ``bridge`` matched "bridged". Anchoring
    each term with ``\\b`` removes that whole collision class. Patterns are
    compiled once at import, never per call.

    Each term is expanded through ``_MARKER_INFLECTIONS`` first, so a plural or
    participle the operator wrote still matches the marker it inflects.

    Terms are ordered longest-first so an underscored/hyphenated variant is
    tried before its own prefix (``surface_until`` before ``surface``) — ``_``
    is a word character, so the short prefix alone can never close the
    boundary there. Ordering is fully deterministic (length, then term).
    """
    expanded = [
        form for term in terms for form in (term, *_MARKER_INFLECTIONS.get(term, ()))
    ]
    ordered = sorted(dict.fromkeys(expanded), key=lambda term: (-len(term), term))
    return re.compile(r"\b(?:" + "|".join(re.escape(t) for t in ordered) + r")\b")


_BLOCKER_TERMS = (
    (
        "needs_user_decision",
        _marker_pattern("under-specified", "underspecified", "acceptance rule"),
    ),
    (
        "missing_input",
        _marker_pattern("missing input", "needs input", "permission", "awaiting"),
    ),
    (
        "unsafe_without_verification",
        _marker_pattern("unsafe", "verify first", "verification"),
    ),
    ("external_dependency", _marker_pattern("waiting on", "external dependency")),
    ("blocked_by", _marker_pattern("blocked by", "blocker")),
)
_READING_MARKERS = _marker_pattern(
    "reading",
    "readings",
    "mama-reading",
    "critical-readings",
    "booklet",
    "epub",
)

# ── Intent markers ─────────────────────────────────────────────────────────
# These encode what the *operator* declared about a task, not what the task
# happens to talk about. Matched against the whole body as bare words they
# inverted their own meaning — a task whose notes merely discussed pinning or
# mentioned a superseded design was read as an explicit user instruction.
#
# The discriminator is the marker's *form*, not the field it sits in. A
# structured directive (`surface_until=…`, the `curated-reading` label) is
# machine-shaped: nobody writes it by accident, so it is honoured wherever it
# appears — including `description`, which this server's tool contract names
# as "the default primary body for task/note content" and is therefore exactly
# where an operator types a pin. Scoping those to the title made pinned tasks
# vanish outright: `section=someday` / `project=readings` rows whose
# description carried a live `surface_until=` fell through the someday and
# reading gates into `excluded`.
#
# A bare English word ("we should pin the weights once the corpus settles") is
# a thought, not an instruction, and stays restricted to the declared surface.
_SURFACE_DIRECTIVE_MARKERS = _marker_pattern("surface_until", "curated-reading")
_SURFACE_MARKERS = _marker_pattern("pin", "pinned", "surface")
# `duplicate` travels with cleanup_candidate/superseded: it is the same class
# of operator label and is at least as collision-prone in body prose.
_CLEANUP_MARKERS = _marker_pattern("cleanup_candidate", "superseded", "duplicate")

# ── Topical markers: full-body scope (description/notes included) ──────────
_BRIDGE_MARKERS = _marker_pattern("bridge")
_BRIDGE_SYNC_MARKERS = _marker_pattern("sync", "updated_at", "import", "churn")
_DELIVERY_MARKERS = _marker_pattern("delivery", "ship", "release", "deadline")
_COMMITMENT_MARKERS = _marker_pattern("external commitment", "commitment", "apply")
_MACHINE_MARKERS = _marker_pattern("machine", "thermal", "bridge", "tray", "sync")
_STALE_MARKERS = _marker_pattern("stale", "unresolved")
_CONFUSED_MARKERS = _marker_pattern("done_but_recently_confused")
_REOPEN_MARKERS = _marker_pattern("reopen_requested_by_user", "reopen")


def _ready_task_text(task: dict[str, Any]) -> str:
    """Full task body — topical signals may legitimately live in the prose."""
    return " ".join(
        str(task.get(k) or "")
        for k in ("title", "description", "notes", "project", "section")
    ).casefold()


def _ready_intent_text(task: dict[str, Any]) -> str:
    """Declared surface for *bare-word* intent markers: the task title.

    Section is joined in as well, but `TASK_SECTIONS` is a closed vocabulary
    (inbox/today/next/someday/waiting/done) and none of those strings contains
    a marker, so in practice this is title-only today; the field stays in the
    join so a future section label is honoured without another edit.

    Structured directives are deliberately *not* read from here — see
    `_ready_has_explicit_surface`: `surface_until=` is an instruction whichever
    field it lands in.
    """
    return " ".join(str(task.get(k) or "") for k in ("title", "section")).casefold()


def _ready_is_reading(task: dict[str, Any]) -> bool:
    return bool(_READING_MARKERS.search(_ready_task_text(task)))


def _ready_has_explicit_surface(task: dict[str, Any]) -> bool:
    """True when the operator asked for this task to stay visible.

    Two tiers, split by marker form: a structured directive counts anywhere in
    the task body, a bare prose word only on the declared intent surface.
    """
    return bool(_SURFACE_DIRECTIVE_MARKERS.search(_ready_task_text(task))) or bool(
        _SURFACE_MARKERS.search(_ready_intent_text(task))
    )


def _ready_is_cleanup_candidate(task: dict[str, Any]) -> bool:
    return bool(_CLEANUP_MARKERS.search(_ready_intent_text(task)))


def _ready_bridge_sync_caution(task: dict[str, Any]) -> bool:
    text = _ready_task_text(task)
    return bool(_BRIDGE_MARKERS.search(text)) and bool(
        _BRIDGE_SYNC_MARKERS.search(text)
    )


def _infer_ready_blockers(task: dict[str, Any]) -> list[dict[str, str]]:
    text = _ready_task_text(task)
    blockers: list[dict[str, str]] = []

    if task.get("section") == "waiting":
        blockers.append({"category": "waiting_on", "detail": "section=waiting"})

    for category, pattern in _BLOCKER_TERMS:
        if pattern.search(text):
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
    if _DELIVERY_MARKERS.search(text):
        codes.append("active_delivery_pressure")
    if _COMMITMENT_MARKERS.search(text):
        codes.append("external_commitment_risk")
    if _MACHINE_MARKERS.search(text):
        codes.append("machine_anomaly_open")
    if _STALE_MARKERS.search(text):
        codes.append("stale_but_unresolved")
    if _ready_bridge_sync_caution(task):
        codes.append("bridge_sync_caution")
    if _ready_is_reading(task):
        codes.append("reading_surface")
    if _ready_is_cleanup_candidate(task):
        codes.append("cleanup_candidate")
    if _CONFUSED_MARKERS.search(text):
        codes.append("done_but_recently_confused")
    if _REOPEN_MARKERS.search(text):
        codes.append("reopen_requested_by_user")
    if _infer_ready_blockers(task):
        codes.append("blocked_by_open_item")

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
    if (
        "critical_priority" in reason_codes
        or "external_commitment_risk" in reason_codes
    ):
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

    if status in TASK_ACTIVE_EXCLUSIONS:
        reopen_codes = {
            "cleanup_candidate",
            "done_but_recently_confused",
            "reopen_requested_by_user",
        }
        if reopen_codes & set(reason_codes):
            return "cleanup_candidate"
        return "excluded"

    if _ready_is_reading(task) and not include_readings:
        if _ready_has_explicit_surface(task):
            return "suggested_ready"
        return "excluded"

    # ── Parked or blocked wins over everything below ──────────────────────
    # A live task that is blocked is blocked, not ready: liveness must never
    # empty the blocked/waiting bucket. section='waiting' is the one explicit
    # field whose purpose is to park a task; `blockers` already carries a
    # waiting_on entry for it, so the two agree by construction.
    if section == "waiting":
        return "waiting"
    if blockers:
        return (
            "waiting"
            if any(b["category"] == "waiting_on" for b in blockers)
            else "blocked"
        )

    # ── Explicit liveness outranks the text-inferred cleanup label ────────
    # This single swap is the whole ordering defect: status and section are set
    # deliberately, by the user or by tooling acting on the user's instruction,
    # while `cleanup_candidate` is merely read out of the task's own words. A
    # keyword must not bury work the operator marked as running.
    if status == "in_progress" or section == "today":
        return "ready_now"
    if "cleanup_candidate" in reason_codes:
        return "cleanup_candidate"
    if due is not None and due <= today:
        return "ready_now"
    if section == "someday" and not _ready_has_explicit_surface(task) and due is None:
        return "excluded"
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

    effective_reason_codes = reason_codes or ["stale_but_unresolved"]

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
        "reason_codes": effective_reason_codes,
        # B3 (v1, additive): the deterministic winning reason and the exact
        # wall-clock date the rules were evaluated against, so date.today()
        # dependence is auditable in the emitted record itself.
        "reason_primary": _ready_reason_primary(effective_reason_codes),
        "today_used": today.isoformat(),
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
    ordered = sorted(records, key=_ready_sort_key)
    # B3 (v1, additive): stamp the final 0-based rank after the deterministic
    # sort so callers can cite an item's position without re-deriving the order.
    for position, record in enumerate(ordered):
        record["sort_position"] = position
    return ordered


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
    task["_ready_provenance"] = dict(record["provenance"])
    # B3 (v1, additive): mirror the new auditable fields onto tray rows.
    task["_ready_reason_primary"] = record.get("reason_primary")
    task["_ready_today_used"] = record.get("today_used")
    if "sort_position" in record:
        task["_ready_sort_position"] = record["sort_position"]
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
        in {"ready_now", "suggested_ready", "blocked", "waiting", "cleanup_candidate"}
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
    """Build a compact boot pack from the same ready-context records.

    The mandate and guidance are static and therefore present even when the
    boot pack is empty (no tasks / nothing ready). Every item list is derived
    strictly from the supplied tasks, so an empty input yields empty lists and
    never fabricates work — only the mandate/guidance survive.
    """
    # Resolve the wall-clock date once so the pack records the exact day its
    # rules ran against (B3, v1 — auditable date.today() dependence).
    effective_today = today or date.today()
    records = ready_context(
        tasks, include_readings=include_readings, today=effective_today
    )
    top = records[:limit]
    mandate = "Use deterministic ready_context records before broad memory search."
    return {
        "contract_version": READY_CONTEXT_CONTRACT_VERSION,
        "current_mandate": mandate,
        # B3 (v1, additive): explicit mandate/guidance always present, even on
        # an empty pack; today_used + items_empty make the empty case auditable.
        "mandate": mandate,
        "guidance": (
            "Start from top_ready_items; resolve blocked_or_waiting next; treat "
            "cleanup_candidates and explicit_exclusions as review-only. When the "
            "pack is empty, honor the mandate and do NOT invent tasks — query or "
            "wait for real ones."
        ),
        "today_used": effective_today.isoformat(),
        "items_empty": not records,
        "top_ready_items": top,
        "blocked_or_waiting": [
            r for r in records if r["ready_state"] in {"blocked", "waiting"}
        ][:limit],
        "cleanup_candidates": [
            r for r in records if r["ready_state"] == "cleanup_candidate"
        ][:limit],
        "explicit_exclusions": [
            build_ready_record(
                t, include_readings=include_readings, today=effective_today
            )
            for t in tasks
            if (t.get("status") in TASK_ACTIVE_EXCLUSIONS)
        ][:limit],
        "risk_or_escalation_items": [
            r for r in records if r["urgency"] in {"critical", "high"} or r["blockers"]
        ][:limit],
        "evidence_refs": [r["provenance"] for r in top],
    }
