"""Debate Protocol v2 — DAO, validators, and lifecycle state machine.

Single-channel inter-session coordination with role watermarks, lifecycle
states (INIT → ACTIVE → RESOLVED → ARCHIVED), priority + kind enums, and
COMPACTION snapshots for log compaction. Pure SQL + filesystem; no LLM,
no network calls in any code path.

Spec source: intersession-debate-log-2026-05-09 entity, conductor
[2026-05-09T16:35 EEST] [H] [Q] EXECUTOR INSTRUCTION block plus
[2026-05-09T16:55 EEST] [H] [DECISION] COMPACTION extension.

Schema lives in `schema.py` (3 tables: debates, debate_messages,
debate_watermarks). This module is the data-access layer that enforces
validators + state machine.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
import re
import secrets
import sqlite3
from typing import Any

from db_utils import json_dumps, json_loads, now_iso
from debate_protocol_v1 import (
    PROTOCOL_VERSION as DEBATE_PROTOCOL_V1,
    SEMANTIC_KINDS,
    ProtocolV1Error,
    configure_topic as _protocol_v1_configure_topic,
    preflight_post as _protocol_v1_preflight_post,
    record_post as _protocol_v1_record_post,
    visibility_sql as _protocol_v1_visibility_sql,
)


# ── Enums + regex validators ──────────────────────────────────────────


TOPIC_RE = re.compile(r"^[A-Z][A-Z0-9_]+$")
ROLE_RE = re.compile(r"^[A-Z][A-Z0-9_]+$")
NUMBERED_EXECUTOR_ROLE_RE = re.compile(r"^EXECUTOR_[1-9][0-9]*$")
# v3.9.3 widening (msg:34adcb3e amendment 1B): accept both 8-char
# (legacy v3.9.0–v3.9.2 rows) and 12-char (new writes) hex msg_ids.
# secrets.token_hex(6) → 48-bit entropy ≈ 16 M generations before 50 %
# birthday collision; ample headroom over the 8-char regime.
MSG_ID_RE = re.compile(r"^[a-f0-9]{8}(?:[a-f0-9]{4})?$")
ISO_UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?Z$")

# OODA structure required for kind=COMPACTION bodies (per CONDUCTOR
# 2026-05-09T16:35 EEST EXECUTOR INSTRUCTION + ADVOCATE turn 2
# acknowledgement msg:bf45a126). All four section labels must appear in
# order. Case-insensitive, multiline match across the body.
_OODA_RE = re.compile(
    r"\bOBSERVE\b.*\bORIENT\b.*\bDECIDE\b.*\bACT\b",
    re.DOTALL | re.IGNORECASE,
)

# Deferred-answer prefix recognized when computing RESOLVED gate. An
# A message with a body starting with "[DEFERRED:" counts as a matched
# answer for its parent Q (per CONDUCTOR msg:d29b7e58 ADVOCATE turn 2
# strict gate decision).
_DEFERRED_PREFIX = "[DEFERRED:"

# Pagination defaults for read_messages — compound (ts, msg_id) cursor
# fix per CONDUCTOR msg:7e3c8f10. Caller may override limit up to MAX.
DEFAULT_READ_LIMIT = 200
MAX_READ_LIMIT = 1000

# v3.9.2 prompt-time inbox signaling (per CONDUCTOR canonical
# msg:b3a87f15 + msg:c5e91d24). Session ids are namespaced by runtime
# so adapters can route messages back to the right session without
# colliding with role names. Pagination contract mirrors read_messages.
APPROVED_RUNTIME_PREFIXES = ("cc-", "codex-", "mcp-", "tray-", "human-")
BASE_SESSION_ID_RE = re.compile(r"^(cc|codex|mcp|tray|human)-[a-zA-Z0-9_]{4,64}$")
WORKER_SESSION_ID_RE = re.compile(
    r"^(?P<parent>(cc|codex|mcp|tray|human)-[a-zA-Z0-9_]{4,64})-W(?P<n>[1-9][0-9]{0,8})$"
)
SESSION_ID_RE = re.compile(
    r"^(cc|codex|mcp|tray|human)-[a-zA-Z0-9_]{4,64}(?:-W[1-9][0-9]{0,8})?$"
)
DEFAULT_SIGNAL_LIMIT = 200
MAX_SIGNAL_LIMIT = 1000
VALID_PRIORITY_ORDER = {"H": 3, "M": 2, "L": 1, "INFO": 0}
VALID_TOPIC_PRIORITY_LANES = (
    "P0",
    "P1",
    "P2",
    "P3",
    "P4",
    "P5",
    "P6",
    "P7",
)
TOPIC_PRIORITY_LANE_ORDER = {
    lane: len(VALID_TOPIC_PRIORITY_LANES) - idx
    for idx, lane in enumerate(VALID_TOPIC_PRIORITY_LANES)
}
WORK_KIND_ORDER = {
    "ESCALATE": 10,
    "DISSENT": 9,
    "CHALLENGE": 8,
    "PING": 7,
    "Q": 6,
    "DECISION": 5,
    "VERIFY": 5,
    "REBUT": 5,
    "CONCEDE": 5,
    "EVIDENCE": 5,
    "CLAIM": 5,
    "STATE": 4,
    "A": 3,
    "STATUS": 2,
    "COMPACTION": 1,
    "WATERMARK": 0,
}

# Canonical WATERMARK body parser — turn-3 correction msg:4c8a91be.
# Supports two forms:
#   'processed_up_to=2026-05-09T17:45:00Z:a8f3c192'
#   'processed_up_to_ts=2026-05-09T17:45:00Z processed_up_to_msg_id=a8f3c192'
# Both yield ts + msg_id together so debate_watermarks rows always
# carry both columns non-null; legacy ISO-only bodies are rejected on
# POST so callers are forced to enrich.
_WATERMARK_RE = re.compile(
    r"processed_up_to(?:_ts)?=(?P<ts>\S+?)"
    r"(?::|\s+processed_up_to_msg_id=)"
    r"(?P<msg_id>[a-f0-9]{8}(?:[a-f0-9]{4})?)"
)

# v3.9.5 canonical STATE body shape (CONDUCTOR msg:c5e2e575 +
# resolution msg:2c22988a). Two accepted forms only:
#   '<STATE>'                 — bare state transition (legacy form)
#   '<STATE> [reason: <text>]' — enriched transition with reason
# fullmatch-anchored to defend against the same prefix-acceptance bug
# class the v3.9.3 WATERMARK parser fixup taught (msg:b246664b →
# msg:932b9bab). Reason text accepts any single-line content (the
# `.+` excludes newlines by default); multi-line bodies are rejected
# intentionally so structured logs stay one row per transition.
_STATE_BODY_RE = re.compile(r"^(INIT|ACTIVE|RESOLVED|ARCHIVED)(\s+\[reason: .+\])?$")


VALID_PRIORITIES = ("H", "M", "L", "INFO")
VALID_KINDS = (
    "Q",
    "A",
    "STATUS",
    "DECISION",
    "PING",
    "WATERMARK",
    "STATE",
    "COMPACTION",
    *SEMANTIC_KINDS,
)
STANDING_SIGNAL_KINDS = ("DECISION", "STATE")
# ── Vehicle tagging (v3.12, conductor-approved solution #5) ────────────
# Classifies the work a message implies so the wake/pump router can decide
# whether a bounded no-edit wake-worker may pick it up. Wake-spawned
# ``-W<n>`` workers run no-edit, so an ``implementation``-tagged message
# would silently bounce if dispatched to one. The router FAILS CLOSED on
# ``implementation`` (see ``claim_worker_session`` + ``prepare_wake_dry_run``).
# NULL / absent → DEFAULT_VEHICLE for backcompat with pre-v3.12 rows.
VALID_VEHICLES = ("analysis", "review", "implementation")
DEFAULT_VEHICLE = "analysis"
# Vehicles a bounded wake-worker may execute. ``implementation`` is
# intentionally excluded — it requires a conductor-approved impl vehicle.
WAKE_WORKER_VEHICLES = ("analysis", "review")
_SINGLETON_MESSAGE_WAKE_RESULTS = {"implementation_requires_impl_vehicle"}
VALID_STATES = ("INIT", "ACTIVE", "RESOLVED", "ARCHIVED")
VALID_BINDING_STATES = ("active", "retired", "diagnostic")
VALID_CURSOR_MODES = ("head", "copy", "replay")
DEBATE_POST_RESPONSE_SCHEMA_VERSION = "debate_post_with_recipients.v1"
DEBATE_WAKE_SCHEMA_VERSION = "debate_wake.v1"
WAKE_SUPPRESSION_SECONDS = 60

VALID_TRANSITIONS: dict[str, set[str]] = {
    "INIT": {"ACTIVE"},
    "ACTIVE": {"RESOLVED"},
    "RESOLVED": {"ARCHIVED"},
    "ARCHIVED": set(),
}


class DebateError(ValueError):
    """Validation or state-machine rejection.

    Backward-compatible per v3.9.2 amendment 7 (msg:e0f47b29):
      - Legacy v3.9.0/v3.9.1 callers: ``raise DebateError("msg")`` still
        works; .error_type defaults to ``'debate_validation'``.
      - v3.9.2+ callers: ``raise DebateError("msg", error_type="...")``
        with a specific taxonomy string from DEBATE_ERROR_TYPES.

    ``error_type`` is keyword-only (the leading ``*`` enforces this) so a
    future positional arg (e.g. ``cause``) cannot silently shift
    semantics for existing callers.
    """

    def __init__(
        self,
        message: str,
        *,
        error_type: str = "debate_validation",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.details = details or {}


def _raise_protocol_error(exc: ProtocolV1Error) -> None:
    raise DebateError(str(exc), error_type=exc.error_type, details=exc.details) from exc


def validate_topic_id(topic_id: str) -> None:
    if not isinstance(topic_id, str) or not TOPIC_RE.fullmatch(topic_id):
        raise DebateError(
            f"invalid_topic_id: {topic_id!r} must match {TOPIC_RE.pattern}"
        )


def validate_role(role: str) -> None:
    if not isinstance(role, str) or not ROLE_RE.fullmatch(role):
        raise DebateError(f"invalid_role: {role!r} must match {ROLE_RE.pattern}")


def validate_numbered_executor_role(role: str) -> None:
    """Executor lanes are distinct addresses, never a shared generic role."""
    validate_role(role)
    if role.startswith("EXECUTOR") and not NUMBERED_EXECUTOR_ROLE_RE.fullmatch(role):
        raise DebateError(
            f"executor_role_must_be_numbered: {role!r}; use EXECUTOR_1, "
            "EXECUTOR_2, ...",
            error_type="executor_role_not_numbered",
        )


def _validate_unique_roster(roles: list[dict[str, Any]]) -> None:
    seen_roles: set[str] = set()
    seen_sessions: set[str] = set()
    for entry in roles:
        role = str(entry["role"])
        session_id = str(entry["session_id"])
        if role in seen_roles:
            raise DebateError(
                f"duplicate_role_in_roster: {role}",
                error_type="roster_duplicate_role",
            )
        if session_id in seen_sessions:
            raise DebateError(
                f"duplicate_session_in_roster: {session_id}",
                error_type="roster_duplicate_session",
            )
        seen_roles.add(role)
        seen_sessions.add(session_id)


def validate_msg_id(msg_id: str) -> None:
    if not isinstance(msg_id, str) or not MSG_ID_RE.fullmatch(msg_id):
        raise DebateError(f"invalid_msg_id: {msg_id!r} must match {MSG_ID_RE.pattern}")


def validate_iso_utc(ts: str) -> None:
    if not isinstance(ts, str) or not ISO_UTC_RE.fullmatch(ts):
        raise DebateError(f"invalid_iso_utc: {ts!r} (expected e.g. 2026-05-09T16:35Z)")


def validate_priority(priority: str) -> None:
    if priority not in VALID_PRIORITIES:
        raise DebateError(f"invalid_priority: {priority!r} not in {VALID_PRIORITIES}")


def validate_topic_priority_lane(lane: str) -> None:
    if lane not in VALID_TOPIC_PRIORITY_LANES:
        raise DebateError(
            f"invalid_topic_priority_lane: {lane!r} not in "
            f"{VALID_TOPIC_PRIORITY_LANES}",
            error_type="topic_priority_lane_invalid",
        )


def validate_kind(kind: str) -> None:
    if kind not in VALID_KINDS:
        raise DebateError(f"invalid_kind: {kind!r} not in {VALID_KINDS}")


def validate_vehicle(vehicle: str) -> None:
    """Validate a debate-message vehicle tag (v3.12).

    Accepts only the three declared vehicles. ``None``/absent is handled by
    the caller (defaults to ``DEFAULT_VEHICLE``) and never reaches here, so
    a value that arrives here is an explicit caller choice and must be one
    of the enum members. Raises a typed ``invalid_vehicle`` DebateError so
    the MCP wrapper surfaces a clean error_type rather than a raw sqlite
    CHECK IntegrityError (the column CHECK is only the backstop).
    """
    if vehicle not in VALID_VEHICLES:
        raise DebateError(
            f"invalid_vehicle: {vehicle!r} not in {VALID_VEHICLES}",
            error_type="invalid_vehicle",
        )


def normalize_vehicle(vehicle: str | None) -> str:
    """Resolve a possibly-absent vehicle to its effective value.

    NULL/empty/absent → ``DEFAULT_VEHICLE`` (backcompat: pre-v3.12 rows and
    untagged callers behave as ``analysis``). Any non-empty value is
    validated against the enum.
    """
    if vehicle is None or vehicle == "":
        return DEFAULT_VEHICLE
    validate_vehicle(vehicle)
    return vehicle


def validate_state(state: str) -> None:
    if state not in VALID_STATES:
        raise DebateError(f"invalid_state: {state!r} not in {VALID_STATES}")


def validate_transition(old_state: str, new_state: str) -> None:
    validate_state(old_state)
    validate_state(new_state)
    if new_state not in VALID_TRANSITIONS[old_state]:
        raise DebateError(
            f"invalid_transition: {old_state} -> {new_state}; "
            f"allowed from {old_state}: {sorted(VALID_TRANSITIONS[old_state])}"
        )


def new_msg_id() -> str:
    """Generate a unique 12-char hex message id (v3.9.3+).

    Uses ``secrets.token_hex(6)`` for 48-bit entropy (~16 M generations
    before 50 % birthday collision; ~80 K before any practical
    collision). Existing 8-char rows from v3.9.0–v3.9.2 remain valid;
    ``MSG_ID_RE`` accepts both widths during the transition window
    (msg:34adcb3e amendment 1B). ``uuid`` import dropped — the only
    prior use was ``uuid.uuid4().hex[:8]`` here, and ``secrets`` is a
    cleaner crypto-RNG primitive for this purpose anyway.
    """
    return secrets.token_hex(6)


# ── DAO: debates ──────────────────────────────────────────────────────


def init_debate(
    conn: sqlite3.Connection,
    *,
    topic_id: str,
    title: str,
    roles: list[dict[str, Any]],
    created_by_role: str,
    resolve_by: str | None = None,
    metadata: dict[str, Any] | None = None,
    require_priority: bool = False,
    require_numbered_executors: bool = False,
    protocol_version: str | None = None,
    blind_roles: list[str] | None = None,
    max_rounds: int = 3,
    phase_timeout_seconds: int = 300,
) -> dict[str, Any]:
    """Bootstrap a new debate. Idempotent: returns existing row when topic_id
    exists with same roles_json.
    """
    validate_topic_id(topic_id)
    validate_role(created_by_role)
    if not isinstance(title, str) or not title.strip():
        raise DebateError("invalid_title: must be non-empty string")
    if not isinstance(roles, list) or not roles:
        raise DebateError("invalid_roles: must be non-empty list")
    for entry in roles:
        if not isinstance(entry, dict):
            raise DebateError("invalid_roles_entry: each must be dict")
        role = entry.get("role")
        session_id = entry.get("session_id")
        if not isinstance(role, str):
            raise DebateError("invalid_roles_entry: missing role")
        validate_role(role)
        if not isinstance(session_id, str) or not session_id:
            raise DebateError(f"invalid_roles_entry: role {role} missing session_id")
    _validate_unique_roster(roles)
    if resolve_by is not None:
        validate_iso_utc(resolve_by)
    if protocol_version not in (None, "", DEBATE_PROTOCOL_V1):
        raise DebateError(
            f"invalid_protocol_version: {protocol_version!r}",
            error_type="INVALID_PROTOCOL_VERSION",
        )
    if protocol_version == DEBATE_PROTOCOL_V1 and blind_roles is None:
        raise DebateError(
            "blind_roles required for debate/v1",
            error_type="INVALID_PROTOCOL_CONFIG",
        )

    existing = conn.execute(
        "SELECT topic_id, title, state, created_at, created_by_role, "
        "resolve_by, archived_at, roles_json, metadata_json "
        "FROM debates WHERE topic_id = ?",
        (topic_id,),
    ).fetchone()
    if existing is not None:
        same_roles = json_loads(existing["roles_json"]) == roles
        if same_roles:
            out = _row_to_debate_dict(existing)
            if protocol_version == DEBATE_PROTOCOL_V1:
                try:
                    out["protocol_state"] = _protocol_v1_configure_topic(
                        conn,
                        topic_id=topic_id,
                        declared_roles=[str(item["role"]) for item in roles],
                        blind_roles=blind_roles or [],
                        max_rounds=max_rounds,
                        phase_timeout_seconds=phase_timeout_seconds,
                    )
                except ProtocolV1Error as exc:
                    _raise_protocol_error(exc)
            return out
        raise DebateError(f"topic_exists_with_different_roles: {topic_id}")

    if require_numbered_executors:
        for entry in roles:
            validate_numbered_executor_role(str(entry["role"]))

    now = now_iso()
    metadata = _normalize_initial_topic_priority_metadata(
        metadata,
        created_by_role=created_by_role,
        now=now,
        require_priority=require_priority,
    )
    conn.execute(
        "INSERT INTO debates (topic_id, title, state, created_at, "
        "created_by_role, resolve_by, archived_at, roles_json, metadata_json) "
        "VALUES (?, ?, 'INIT', ?, ?, ?, NULL, ?, ?)",
        (
            topic_id,
            title,
            now,
            created_by_role,
            resolve_by,
            json_dumps(roles),
            json_dumps(metadata) if metadata is not None else None,
        ),
    )
    out = {
        "topic_id": topic_id,
        "title": title,
        "state": "INIT",
        "created_at": now,
        "created_by_role": created_by_role,
        "resolve_by": resolve_by,
        "archived_at": None,
        "roles": roles,
        "metadata": metadata,
    }
    if protocol_version == DEBATE_PROTOCOL_V1:
        try:
            out["protocol_state"] = _protocol_v1_configure_topic(
                conn,
                topic_id=topic_id,
                declared_roles=[str(item["role"]) for item in roles],
                blind_roles=blind_roles or [],
                max_rounds=max_rounds,
                phase_timeout_seconds=phase_timeout_seconds,
            )
        except ProtocolV1Error as exc:
            _raise_protocol_error(exc)
    return out


def get_debate(conn: sqlite3.Connection, topic_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT topic_id, title, state, created_at, created_by_role, "
        "resolve_by, archived_at, roles_json, metadata_json "
        "FROM debates WHERE topic_id = ?",
        (topic_id,),
    ).fetchone()
    return _row_to_debate_dict(row) if row else None


def _metadata_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if value is None or value == "":
        return {}
    parsed = json_loads(value)
    return parsed if isinstance(parsed, dict) else {}


def _row_to_debate_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    d["roles"] = json_loads(d.pop("roles_json")) if d.get("roles_json") else []
    md = d.pop("metadata_json", None)
    d["metadata"] = json_loads(md) if md else None
    return d


def _topic_priority_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(metadata, dict):
        return {}
    value = metadata.get("conductor_priority")
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value:
        return {"lane": value}
    value = metadata.get("priority_lane")
    if isinstance(value, str) and value:
        return {"lane": value}
    return {}


def _explicit_topic_priority_lane(metadata: dict[str, Any] | None) -> str | None:
    priority = _topic_priority_metadata(metadata)
    lane = priority.get("lane")
    if isinstance(lane, str) and lane.upper() in TOPIC_PRIORITY_LANE_ORDER:
        return lane.upper()
    return None


def _topic_priority_reason(metadata: dict[str, Any] | None) -> str:
    if not isinstance(metadata, dict):
        return ""
    priority = _topic_priority_metadata(metadata)
    for key in ("reason", "priority_reason", "conductor_priority_reason"):
        value = priority.get(key) if key in priority else metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _normalize_initial_topic_priority_metadata(
    metadata: dict[str, Any] | None,
    *,
    created_by_role: str,
    now: str,
    require_priority: bool,
) -> dict[str, Any] | None:
    """Validate and normalize the initial cross-topic priority gate."""
    if metadata is None and not require_priority:
        return None

    normalized = _metadata_dict(metadata)
    lane = _explicit_topic_priority_lane(normalized)
    reason = _topic_priority_reason(normalized)

    if lane is None:
        if require_priority:
            raise DebateError(
                "topic_priority_required: debate_init requires metadata_json "
                "with conductor_priority.lane or priority_lane (P0..P7) plus "
                "a priority reason; ask the human for priority or have "
                "CONDUCTOR assess it before creating the topic",
                error_type="topic_priority_required",
            )
        return normalized if metadata is not None else None

    validate_topic_priority_lane(lane)
    if not reason:
        raise DebateError(
            "topic_priority_reason_required",
            error_type="topic_priority_reason_required",
        )

    priority = dict(_topic_priority_metadata(normalized))
    priority["lane"] = lane
    priority["rank"] = TOPIC_PRIORITY_LANE_ORDER[lane]
    priority["reason"] = reason
    priority.setdefault("next_action", str(normalized.get("next_action") or "").strip())
    priority.setdefault("blocked_by", str(normalized.get("blocked_by") or "").strip())
    priority.setdefault("updated_by_role", created_by_role)
    priority.setdefault("updated_at", now)
    priority.setdefault(
        "source",
        "conductor_assessed" if created_by_role == "CONDUCTOR" else "human_requested",
    )
    normalized["conductor_priority"] = priority
    normalized["initial_priority_gate"] = {
        "required": bool(require_priority),
        "lane": lane,
        "reason": reason,
        "created_by_role": created_by_role,
        "created_at": now,
    }
    return normalized


def role_in_debate(roles: list[dict[str, Any]], role: str) -> bool:
    return any(isinstance(r, dict) and r.get("role") == role for r in roles)


def validate_session_id(session_id: str) -> None:
    if not isinstance(session_id, str) or not SESSION_ID_RE.fullmatch(session_id):
        raise DebateError(
            f"session_id {session_id!r} must match {SESSION_ID_RE.pattern}",
            error_type="recipient_invalid_session_id",
        )


def is_worker_session_id(session_id: str) -> bool:
    return (
        isinstance(session_id, str)
        and WORKER_SESSION_ID_RE.fullmatch(session_id) is not None
    )


def worker_parent_session_id(session_id: str) -> str | None:
    m = (
        WORKER_SESSION_ID_RE.fullmatch(session_id)
        if isinstance(session_id, str)
        else None
    )
    return m.group("parent") if m else None


def validate_parent_session_id(session_id: str) -> None:
    if not isinstance(session_id, str) or not BASE_SESSION_ID_RE.fullmatch(session_id):
        raise DebateError(
            f"parent_session_id {session_id!r} must match {BASE_SESSION_ID_RE.pattern}",
            error_type="recipient_invalid_session_id",
        )


def validate_binding_state(state: str) -> None:
    if state not in VALID_BINDING_STATES:
        raise DebateError(
            f"invalid_binding_state: {state!r} not in {VALID_BINDING_STATES}",
            error_type="binding_state_invalid",
        )


def validate_cursor_mode(cursor_mode: str) -> None:
    if cursor_mode not in VALID_CURSOR_MODES:
        raise DebateError(
            f"invalid_cursor_mode: {cursor_mode!r} not in {VALID_CURSOR_MODES}",
            error_type="cursor_mode_invalid",
        )


def _validate_role_for_debate(debate: dict[str, Any], topic_id: str, role: str) -> None:
    validate_role(role)
    if not role_in_debate(debate["roles"], role):
        raise DebateError(
            f"role {role!r} not declared in topic {topic_id}",
            error_type="recipient_unknown_role",
        )


def _validate_conductor_override(
    conn: sqlite3.Connection,
    *,
    topic_id: str,
    override_msg_id: str | None,
) -> None:
    if not override_msg_id:
        raise DebateError(
            "conductor_override_required",
            error_type="conductor_override_required",
        )
    validate_msg_id(override_msg_id)
    row = conn.execute(
        "SELECT role, kind FROM debate_messages WHERE msg_id = ? AND topic_id = ?",
        (override_msg_id, topic_id),
    ).fetchone()
    if row is None or row["role"] != "CONDUCTOR" or row["kind"] != "DECISION":
        raise DebateError(
            f"invalid_conductor_override: {override_msg_id}",
            error_type="conductor_override_invalid",
        )


def _active_binding(
    conn: sqlite3.Connection, topic_id: str, role: str
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM debate_role_bindings "
        "WHERE topic_id = ? AND role = ? AND state = 'active' "
        "ORDER BY generation DESC LIMIT 1",
        (topic_id, role),
    ).fetchone()


def _binding_for_session(
    conn: sqlite3.Connection, topic_id: str, role: str, session_id: str
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM debate_role_bindings "
        "WHERE topic_id = ? AND role = ? AND session_id = ?",
        (topic_id, role, session_id),
    ).fetchone()


def _binding_count(conn: sqlite3.Connection, topic_id: str, role: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) AS c FROM debate_role_bindings "
        "WHERE topic_id = ? AND role = ?",
        (topic_id, role),
    ).fetchone()["c"]


def _next_binding_generation(conn: sqlite3.Connection, topic_id: str, role: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(generation), 0) + 1 AS generation "
        "FROM debate_role_bindings WHERE topic_id = ? AND role = ?",
        (topic_id, role),
    ).fetchone()
    return int(row["generation"])


def _runtime_from_session(session_id: str) -> str:
    return session_id.split("-", 1)[0]


def _standing_to_db(standing: bool | None) -> int | None:
    if standing is None:
        return None
    if not isinstance(standing, bool):
        raise DebateError(
            "standing must be bool or None",
            error_type="standing_invalid",
        )
    return 1 if standing else 0


def _parse_iso_utc_dt(value: str) -> datetime:
    validate_iso_utc(value)
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _validate_reclaim_cutoff(
    older_than_ts: str,
    minimum_age_seconds: int,
    *,
    error_namespace: str = "message_claim_reclaim",
) -> None:
    if isinstance(minimum_age_seconds, bool) or not isinstance(
        minimum_age_seconds, int
    ):
        raise DebateError(
            "minimum_age_seconds must be int",
            error_type=f"{error_namespace}_min_age_invalid",
        )
    if minimum_age_seconds < 0:
        raise DebateError(
            "minimum_age_seconds must be >= 0",
            error_type=f"{error_namespace}_min_age_invalid",
        )
    cutoff = _parse_iso_utc_dt(older_than_ts)
    safe_cutoff = datetime.now(timezone.utc) - timedelta(seconds=minimum_age_seconds)
    if cutoff > safe_cutoff:
        raise DebateError(
            f"{error_namespace}_cutoff_too_recent: older_than_ts={older_than_ts} "
            f"must be at least {minimum_age_seconds}s behind current UTC time",
            error_type=f"{error_namespace}_cutoff_too_recent",
        )


def _terminal_reply_for_trigger(
    conn: sqlite3.Connection, *, topic_id: str, role: str, trigger_msg_id: str
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT msg_id, ts FROM debate_messages "
        "WHERE topic_id = ? AND role = ? AND reply_to = ? "
        "AND kind IN ('A', 'STATUS', 'CLAIM', 'CHALLENGE', 'EVIDENCE', 'REBUT', "
        "'CONCEDE', 'VERIFY', 'DISSENT', 'ESCALATE') "
        "ORDER BY ts ASC, msg_id ASC LIMIT 1",
        (topic_id, role, trigger_msg_id),
    ).fetchone()


def _dispatch_still_covers_trigger(
    conn: sqlite3.Connection, *, topic_id: str, role: str, trigger_msg_id: str
) -> bool:
    """Is a prior 'dispatched' wake still covering this (role, trigger)?

    True when the work is either in-flight or done: the worker claim is
    active or completed, OR a terminal reply already exists. False when the
    claim was retired (dead worker) and no reply landed — the stale
    'dispatched' row must not suppress re-dispatch (advocate BLOCK #1)."""
    if _terminal_reply_for_trigger(
        conn, topic_id=topic_id, role=role, trigger_msg_id=trigger_msg_id
    ):
        return True
    claim = conn.execute(
        "SELECT state FROM debate_worker_claims "
        "WHERE topic_id = ? AND role = ? AND trigger_msg_id = ? "
        "ORDER BY claimed_at DESC LIMIT 1",
        (topic_id, role, trigger_msg_id),
    ).fetchone()
    # Only an explicitly RETIRED claim (a proven-dead worker) un-suppresses
    # a 'dispatched' trigger. active/completed = covered; a missing claim is
    # left covered too — in production the launcher records the claim BEFORE
    # marking 'dispatched' (preflight-then-claim order), so 'dispatched' with
    # no claim is not a real state and must not trigger spurious re-dispatch.
    if claim is None:
        return True
    return str(claim["state"]) != "retired"


def _decision_is_nonstanding(conn: sqlite3.Connection, msg_id: str) -> bool:
    row = conn.execute(
        "SELECT kind, standing FROM debate_messages WHERE msg_id = ?",
        (msg_id,),
    ).fetchone()
    return bool(row and row["kind"] == "DECISION" and row["standing"] == 0)


def _worker_claim_exists(
    conn: sqlite3.Connection, *, topic_id: str, role: str, trigger_msg_id: str
) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM debate_worker_claims "
            "WHERE topic_id = ? AND role = ? AND trigger_msg_id = ? LIMIT 1",
            (topic_id, role, trigger_msg_id),
        ).fetchone()
        is not None
    )


def _retire_worker_claims_for_parent_sessions(
    conn: sqlite3.Connection,
    *,
    topic_id: str,
    role: str,
    parent_session_ids: list[str],
    now: str,
) -> int:
    sessions = list(dict.fromkeys(s for s in parent_session_ids if s))
    if not sessions:
        return 0
    placeholders = ",".join("?" for _ in sessions)
    cur = conn.execute(
        "UPDATE debate_worker_claims SET state = 'retired', heartbeat_at = ? "
        f"WHERE topic_id = ? AND role = ? AND state = 'active' "
        f"AND parent_session_id IN ({placeholders})",
        (now, topic_id, role, *sessions),
    )
    return int(cur.rowcount or 0)


def _complete_nonstanding_decision_claims_for_reply(
    conn: sqlite3.Connection,
    *,
    topic_id: str,
    role: str,
    kind: str,
    reply_to: str | None,
    ack_msg_id: str,
    now: str,
) -> None:
    if kind not in ("A", "STATUS") or reply_to is None:
        return
    if not _decision_is_nonstanding(conn, reply_to):
        return
    conn.execute(
        "INSERT INTO debate_message_claims "
        "(msg_id, role, owner_session_id, state, claimed_at, heartbeat_at, "
        " completed_at, ack_msg_id) "
        "VALUES (?, ?, NULL, 'done', ?, ?, ?, ?) "
        "ON CONFLICT(msg_id, role) DO UPDATE SET "
        "state = 'done', heartbeat_at = excluded.heartbeat_at, "
        "completed_at = COALESCE(debate_message_claims.completed_at, excluded.completed_at), "
        "ack_msg_id = COALESCE(debate_message_claims.ack_msg_id, excluded.ack_msg_id)",
        (reply_to, role, now, now, now, ack_msg_id),
    )


def _claim_row_dict(row: sqlite3.Row) -> dict[str, Any]:
    out = dict(row)
    if out.get("details_json"):
        out["details"] = json_loads(out["details_json"])
    else:
        out["details"] = None
    out.pop("details_json", None)
    return out


def _claim_details_dict(row: sqlite3.Row) -> dict[str, Any]:
    if not row["details_json"]:
        return {}
    details = json_loads(row["details_json"])
    return details if isinstance(details, dict) else {"value": details}


def _worker_claim_for_session(
    conn: sqlite3.Connection,
    *,
    topic_id: str,
    role: str,
    worker_session_id: str,
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM debate_worker_claims "
        "WHERE topic_id = ? AND role = ? AND worker_session_id = ? "
        "ORDER BY claimed_at DESC LIMIT 1",
        (topic_id, role, worker_session_id),
    ).fetchone()


def _validate_worker_claim_for_signal(
    conn: sqlite3.Connection,
    *,
    topic_id: str,
    role: str,
    worker_session_id: str,
) -> sqlite3.Row:
    parent_session_id = worker_parent_session_id(worker_session_id)
    if parent_session_id is None:
        raise DebateError(
            f"worker_session_invalid: {worker_session_id}",
            error_type="worker_session_invalid",
        )
    claim = _worker_claim_for_session(
        conn,
        topic_id=topic_id,
        role=role,
        worker_session_id=worker_session_id,
    )
    if claim is None or claim["parent_session_id"] != parent_session_id:
        raise DebateError(
            f"worker_claim_required: {worker_session_id}",
            error_type="worker_claim_required",
        )
    parent_binding = _binding_for_session(conn, topic_id, role, parent_session_id)
    if parent_binding is None or parent_binding["state"] != "active":
        raise DebateError(
            f"worker_parent_binding_inactive: {parent_session_id}",
            error_type="worker_parent_binding_inactive",
        )
    return claim


def claim_worker_session(
    conn: sqlite3.Connection,
    *,
    topic_id: str,
    role: str,
    parent_session_id: str,
    trigger_msg_id: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Idempotently allocate a derived ``-W<n>`` worker for one trigger.

    The parent binding remains the role authority. This row is a scoped
    execution claim only; workers get their own cursor and inherit read
    access from the active parent binding.
    """
    validate_topic_id(topic_id)
    validate_role(role)
    validate_parent_session_id(parent_session_id)
    validate_msg_id(trigger_msg_id)
    debate = get_debate(conn, topic_id)
    if debate is None:
        raise DebateError(
            f"unknown_topic: {topic_id}",
            error_type="topic_not_found",
        )
    _validate_role_for_debate(debate, topic_id, role)
    parent_binding = _binding_for_session(conn, topic_id, role, parent_session_id)
    if parent_binding is None or parent_binding["state"] != "active":
        raise DebateError(
            f"worker_parent_binding_inactive: {parent_session_id}",
            error_type="worker_parent_binding_inactive",
        )
    trigger = conn.execute(
        "SELECT topic_id, vehicle FROM debate_messages WHERE msg_id = ?",
        (trigger_msg_id,),
    ).fetchone()
    if trigger is None or trigger["topic_id"] != topic_id:
        raise DebateError(
            f"worker_trigger_unknown: {trigger_msg_id}",
            error_type="worker_trigger_unknown",
        )
    # ── FAIL-CLOSED VEHICLE ROUTER (v3.12, solution #5) ──────────────────
    # This is the deepest shared chokepoint for spawning a bounded -W<n>
    # wake-worker: the wake hook (debate_wake._claim_worker_target), the
    # pump hook (debate_pump → debate_wake._maybe_dispatch), AND the direct
    # debate_worker_claim MCP tool all funnel through here. Wake-workers run
    # NO-EDIT, so an implementation-tagged trigger dispatched to one would
    # silently bounce. Per the advocate, solution #5 must FAIL CLOSED — else
    # it just renames the bounce. We REFUSE the claim with a typed error
    # instead of allocating a worker that cannot do the work. analysis /
    # review (and untagged → DEFAULT_VEHICLE) proceed unchanged.
    #
    # CONDUCTOR-APPROVED IMPL-VEHICLE SEAM (#3): when a future implementation
    # vehicle lands (#2 claim-for-impl / #1 IMPL-worker), gate it here — e.g.
    # accept implementation claims only when details carries an approved
    # impl-vehicle token (details.get("impl_vehicle_approved")), or branch to
    # an IMPL-worker allocator. Until then, implementation work is handled
    # out-of-band by the conductor via Agent sub-agents and is refused here.
    trigger_vehicle = normalize_vehicle(trigger["vehicle"])
    if trigger_vehicle not in WAKE_WORKER_VEHICLES:
        raise DebateError(
            f"implementation_requires_impl_vehicle: trigger {trigger_msg_id} "
            f"is vehicle={trigger_vehicle!r}; bounded wake-workers are "
            f"no-edit and cannot execute implementation work. Route to a "
            f"conductor-approved impl vehicle (Agent sub-agent).",
            error_type="implementation_requires_impl_vehicle",
        )
    addressed = conn.execute(
        "SELECT 1 FROM debate_message_recipients "
        "WHERE msg_id = ? AND recipient IN (?, ?) LIMIT 1",
        (trigger_msg_id, role, parent_session_id),
    ).fetchone()
    if addressed is None:
        raise DebateError(
            f"worker_trigger_unaddressed: {trigger_msg_id}",
            error_type="worker_trigger_unaddressed",
        )

    now = now_iso()
    existing = conn.execute(
        "SELECT * FROM debate_worker_claims "
        "WHERE topic_id = ? AND role = ? AND parent_session_id = ? "
        "AND trigger_msg_id = ?",
        (topic_id, role, parent_session_id, trigger_msg_id),
    ).fetchone()
    if existing is not None:
        if existing["state"] == "active":
            conn.execute(
                "UPDATE debate_worker_claims SET heartbeat_at = ? "
                "WHERE topic_id = ? AND role = ? AND parent_session_id = ? "
                "AND trigger_msg_id = ?",
                (now, topic_id, role, parent_session_id, trigger_msg_id),
            )
            existing = conn.execute(
                "SELECT * FROM debate_worker_claims "
                "WHERE topic_id = ? AND role = ? AND parent_session_id = ? "
                "AND trigger_msg_id = ?",
                (topic_id, role, parent_session_id, trigger_msg_id),
            ).fetchone()
        elif existing["state"] == "retired":
            # REQUEUE (advocate BLOCK critical #1): a retired orphan claim
            # previously suppressed re-dispatch forever — the trigger's work
            # was silently lost. Reactivate the same claim (PK forbids a
            # second row) with a bounded retry budget; past the budget the
            # claim reports requeue_exhausted so the router surfaces the
            # loss explicitly instead of retrying forever.
            details = _claim_details_dict(existing)
            requeues = int(details.get("requeue_count") or 0)
            max_requeues = int(os.environ.get("DEBATE_WORKER_MAX_REQUEUES", "2"))
            if requeues >= max_requeues:
                if not details.get("requeue_exhausted"):
                    details["requeue_exhausted"] = True
                    details["requeue_exhausted_at"] = now
                    conn.execute(
                        "UPDATE debate_worker_claims SET details_json = ? "
                        "WHERE topic_id = ? AND role = ? "
                        "AND parent_session_id = ? AND trigger_msg_id = ?",
                        (
                            json_dumps(details),
                            topic_id,
                            role,
                            parent_session_id,
                            trigger_msg_id,
                        ),
                    )
                    existing = conn.execute(
                        "SELECT * FROM debate_worker_claims "
                        "WHERE topic_id = ? AND role = ? "
                        "AND parent_session_id = ? AND trigger_msg_id = ?",
                        (topic_id, role, parent_session_id, trigger_msg_id),
                    ).fetchone()
                out = _claim_row_dict(existing)
                out["duplicate"] = True
                out["no_action"] = True
                out["requeue_exhausted"] = True
                return out
            details["requeue_count"] = requeues + 1
            details["reactivated_at"] = now
            conn.execute(
                "UPDATE debate_worker_claims SET state = 'active', "
                "heartbeat_at = ?, completed_at = NULL, ack_msg_id = NULL, "
                "details_json = ? "
                "WHERE topic_id = ? AND role = ? AND parent_session_id = ? "
                "AND trigger_msg_id = ? AND state = 'retired'",
                (
                    now,
                    json_dumps(details),
                    topic_id,
                    role,
                    parent_session_id,
                    trigger_msg_id,
                ),
            )
            row = conn.execute(
                "SELECT * FROM debate_worker_claims "
                "WHERE topic_id = ? AND role = ? AND parent_session_id = ? "
                "AND trigger_msg_id = ?",
                (topic_id, role, parent_session_id, trigger_msg_id),
            ).fetchone()
            out = _claim_row_dict(row)
            out["duplicate"] = False
            out["no_action"] = False
            out["reactivated"] = True
            return out
        out = _claim_row_dict(existing)
        out["duplicate"] = True
        out["no_action"] = existing["state"] != "active"
        return out

    source_cursor = conn.execute(
        "SELECT last_processed_msg_id, last_processed_ts "
        "FROM debate_signal_state "
        "WHERE session_id = ? AND role = ? AND topic_id = ?",
        (parent_session_id, role, topic_id),
    ).fetchone()
    counter = conn.execute(
        "SELECT next_worker_n FROM debate_worker_counters "
        "WHERE topic_id = ? AND role = ? AND parent_session_id = ?",
        (topic_id, role, parent_session_id),
    ).fetchone()
    if counter is None:
        worker_n = 1
        conn.execute(
            "INSERT INTO debate_worker_counters "
            "(topic_id, role, parent_session_id, next_worker_n, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (topic_id, role, parent_session_id, 2, now),
        )
    else:
        worker_n = int(counter["next_worker_n"])
        conn.execute(
            "UPDATE debate_worker_counters SET next_worker_n = ?, updated_at = ? "
            "WHERE topic_id = ? AND role = ? AND parent_session_id = ?",
            (worker_n + 1, now, topic_id, role, parent_session_id),
        )
    worker_session_id = f"{parent_session_id}-W{worker_n}"
    conn.execute(
        "INSERT INTO debate_worker_claims "
        "(topic_id, role, parent_session_id, trigger_msg_id, "
        " worker_session_id, state, parent_cursor_msg_id, parent_cursor_ts, "
        " claimed_at, heartbeat_at, completed_at, ack_msg_id, details_json) "
        "VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, NULL, NULL, ?)",
        (
            topic_id,
            role,
            parent_session_id,
            trigger_msg_id,
            worker_session_id,
            source_cursor["last_processed_msg_id"] if source_cursor else None,
            source_cursor["last_processed_ts"] if source_cursor else None,
            now,
            now,
            json_dumps(details or {}),
        ),
    )
    row = conn.execute(
        "SELECT * FROM debate_worker_claims "
        "WHERE topic_id = ? AND role = ? AND worker_session_id = ?",
        (topic_id, role, worker_session_id),
    ).fetchone()
    out = _claim_row_dict(row)
    out["duplicate"] = False
    out["no_action"] = False
    return out


def worker_no_action(
    conn: sqlite3.Connection,
    *,
    topic_id: str,
    role: str,
    worker_session_id: str,
    trigger_msg_id: str,
    reason: str = "",
) -> dict[str, Any]:
    """Mark an active wake worker claim complete without posting a message.

    This is the terminal path for an autonomous worker that inspected its
    addressed trigger and found no substantive debate work. It advances only
    the worker cursor, preserving the parent session cursor and avoiding an
    empty channel post.
    """
    validate_topic_id(topic_id)
    validate_role(role)
    validate_session_id(worker_session_id)
    validate_msg_id(trigger_msg_id)
    claim = _validate_worker_claim_for_signal(
        conn,
        topic_id=topic_id,
        role=role,
        worker_session_id=worker_session_id,
    )
    if claim["trigger_msg_id"] != trigger_msg_id:
        raise DebateError(
            f"worker_no_action_trigger_mismatch: worker_session_id="
            f"{worker_session_id!r} is claimed for {claim['trigger_msg_id']!r}, "
            f"not {trigger_msg_id!r}",
            error_type="worker_no_action_trigger_mismatch",
        )

    ref = conn.execute(
        "SELECT msg_id, ts FROM debate_messages WHERE msg_id = ? AND topic_id = ?",
        (trigger_msg_id, topic_id),
    ).fetchone()
    if ref is None:
        raise DebateError(
            f"worker_trigger_unknown: {trigger_msg_id}",
            error_type="worker_trigger_unknown",
        )

    if claim["state"] != "active":
        out = _claim_row_dict(claim)
        details = out.get("details") if isinstance(out.get("details"), dict) else {}
        out["duplicate"] = True
        out["no_action"] = bool(details.get("no_action"))
        return out

    current = conn.execute(
        "SELECT last_processed_msg_id, last_processed_ts "
        "FROM debate_signal_state "
        "WHERE session_id = ? AND role = ? AND topic_id = ?",
        (worker_session_id, role, topic_id),
    ).fetchone()
    cursor_msg_id = ref["msg_id"]
    cursor_ts = ref["ts"]
    advance_cursor = True
    if current and current["last_processed_ts"]:
        cur_ts = current["last_processed_ts"]
        cur_msg_id = current["last_processed_msg_id"] or ""
        proposed = (ref["ts"], ref["msg_id"])
        existing = (cur_ts, cur_msg_id)
        if proposed < existing:
            cursor_msg_id = cur_msg_id
            cursor_ts = cur_ts
            advance_cursor = False

    now = now_iso()
    if advance_cursor:
        conn.execute(
            "INSERT INTO debate_signal_state "
            "(session_id, role, topic_id, last_processed_msg_id, "
            " last_processed_ts, last_check_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(session_id, role, topic_id) DO UPDATE SET "
            "last_processed_msg_id = excluded.last_processed_msg_id, "
            "last_processed_ts = excluded.last_processed_ts, "
            "last_check_at = excluded.last_check_at",
            (worker_session_id, role, topic_id, ref["msg_id"], ref["ts"], now),
        )
    else:
        conn.execute(
            "UPDATE debate_signal_state SET last_check_at = ? "
            "WHERE session_id = ? AND role = ? AND topic_id = ?",
            (now, worker_session_id, role, topic_id),
        )
    details = _claim_details_dict(claim)
    details.update(
        {
            "no_action": True,
            "no_action_reason": str(reason or "").strip(),
            "no_action_at": now,
        }
    )
    conn.execute(
        "UPDATE debate_worker_claims SET state = 'completed', "
        "completed_at = ?, heartbeat_at = ?, ack_msg_id = NULL, "
        "details_json = ? "
        "WHERE topic_id = ? AND role = ? AND worker_session_id = ?",
        (
            now,
            now,
            json_dumps(details),
            topic_id,
            role,
            worker_session_id,
        ),
    )
    row = _worker_claim_for_session(
        conn,
        topic_id=topic_id,
        role=role,
        worker_session_id=worker_session_id,
    )
    out = _claim_row_dict(row)
    out.update(
        {
            "duplicate": False,
            "no_action": True,
            "cursor_unchanged": not advance_cursor,
            "last_processed_msg_id": cursor_msg_id,
            "last_processed_ts": cursor_ts,
            "last_check_at": now,
        }
    )
    return out


def recover_stale_worker_claims(
    conn: sqlite3.Connection,
    *,
    topic_id: str,
    older_than_ts: str,
    minimum_age_seconds: int = 120,
    live_worker_session_ids: set[str] | None = None,
) -> dict[str, Any]:
    """Reconcile active worker claims whose launcher process is gone.

    A spawned Claude/Codex worker can exit before it calls
    ``debate_worker_no_action`` (quota/session limit, crash, killed process).
    The old pump reaped the OS child but left the DB claim active forever.
    This recovery path is conservative:

    * a live worker session is skipped;
    * a terminal same-role A/STATUS completes the claim with its ack;
    * otherwise the orphan is retired without advancing either cursor, so the
      parent session still sees the addressed trigger as pending.

    Every transition is recorded in ``debate_worker_recovery_log``.
    """
    validate_topic_id(topic_id)
    _validate_reclaim_cutoff(
        older_than_ts,
        minimum_age_seconds,
        error_namespace="worker_claim_recovery",
    )
    debate = get_debate(conn, topic_id)
    if debate is None:
        raise DebateError(
            f"unknown_topic: {topic_id}",
            error_type="topic_not_found",
        )
    live = set(live_worker_session_ids or set())
    rows = conn.execute(
        "SELECT * FROM debate_worker_claims "
        "WHERE topic_id = ? AND state = 'active' AND heartbeat_at < ? "
        "ORDER BY heartbeat_at ASC, worker_session_id ASC",
        (topic_id, older_than_ts),
    ).fetchall()
    now = now_iso()
    completed: list[dict[str, Any]] = []
    retired: list[dict[str, Any]] = []
    skipped_live: list[str] = []
    for row in rows:
        worker_session_id = str(row["worker_session_id"])
        if worker_session_id in live:
            skipped_live.append(worker_session_id)
            continue
        ack = _terminal_reply_for_trigger(
            conn,
            topic_id=topic_id,
            role=row["role"],
            trigger_msg_id=row["trigger_msg_id"],
        )
        details = _claim_details_dict(row)
        recovery = {
            "recovered_at": now,
            "older_than_ts": older_than_ts,
            "minimum_age_seconds": minimum_age_seconds,
            "previous_heartbeat_at": row["heartbeat_at"],
            "launcher_process_live": False,
        }
        if ack is not None:
            result = "completed_from_terminal"
            new_state = "completed"
            ack_msg_id = ack["msg_id"]
            recovery["ack_msg_id"] = ack_msg_id
            completed.append(
                {
                    "worker_session_id": worker_session_id,
                    "trigger_msg_id": row["trigger_msg_id"],
                    "ack_msg_id": ack_msg_id,
                }
            )
        else:
            result = "retired_orphan_no_terminal"
            new_state = "retired"
            ack_msg_id = None
            recovery["parent_trigger_still_pending"] = True
            retired.append(
                {
                    "worker_session_id": worker_session_id,
                    "trigger_msg_id": row["trigger_msg_id"],
                }
            )
        details["stale_worker_recovery"] = recovery
        conn.execute(
            "UPDATE debate_worker_claims SET state = ?, heartbeat_at = ?, "
            "completed_at = ?, ack_msg_id = ?, details_json = ? "
            "WHERE topic_id = ? AND role = ? AND worker_session_id = ? "
            "AND state = 'active'",
            (
                new_state,
                now,
                now,
                ack_msg_id,
                json_dumps(details),
                topic_id,
                row["role"],
                worker_session_id,
            ),
        )
        recovery_id = new_msg_id()
        while conn.execute(
            "SELECT 1 FROM debate_worker_recovery_log WHERE recovery_id = ? LIMIT 1",
            (recovery_id,),
        ).fetchone():
            recovery_id = new_msg_id()
        conn.execute(
            "INSERT INTO debate_worker_recovery_log "
            "(recovery_id, topic_id, role, parent_session_id, "
            " worker_session_id, trigger_msg_id, previous_state, result, "
            " details_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)",
            (
                recovery_id,
                topic_id,
                row["role"],
                row["parent_session_id"],
                worker_session_id,
                row["trigger_msg_id"],
                result,
                json_dumps(recovery),
                now,
            ),
        )
    return {
        "topic_id": topic_id,
        "topic_state": debate["state"],
        "older_than_ts": older_than_ts,
        "minimum_age_seconds": minimum_age_seconds,
        "completed": completed,
        "retired": retired,
        "skipped_live": skipped_live,
        "completed_count": len(completed),
        "retired_count": len(retired),
        "skipped_live_count": len(skipped_live),
    }


def _complete_worker_claim_if_terminal(
    conn: sqlite3.Connection,
    *,
    topic_id: str,
    role: str,
    worker_session_id: str,
    now: str,
    claim: sqlite3.Row | None = None,
) -> dict[str, Any] | None:
    if not is_worker_session_id(worker_session_id):
        return None
    if claim is None:
        claim = _validate_worker_claim_for_signal(
            conn,
            topic_id=topic_id,
            role=role,
            worker_session_id=worker_session_id,
        )
    if claim["state"] != "active":
        return _claim_row_dict(claim)
    ack = _terminal_reply_for_trigger(
        conn,
        topic_id=topic_id,
        role=role,
        trigger_msg_id=claim["trigger_msg_id"],
    )
    if ack is None:
        return _claim_row_dict(claim)
    conn.execute(
        "UPDATE debate_worker_claims SET state = 'completed', "
        "completed_at = ?, heartbeat_at = ?, ack_msg_id = ? "
        "WHERE topic_id = ? AND role = ? AND worker_session_id = ?",
        (now, now, ack["msg_id"], topic_id, role, worker_session_id),
    )
    row = _worker_claim_for_session(
        conn,
        topic_id=topic_id,
        role=role,
        worker_session_id=worker_session_id,
    )
    return _claim_row_dict(row)


def reap_worker_claims(
    conn: sqlite3.Connection,
    *,
    topic_id: str,
    older_than_ts: str,
) -> dict[str, Any]:
    """Retire completed/closed worker claims older than ``older_than_ts``.

    This is deliberately explicit. There is no background cleanup path that
    could erase recovery evidence without an audit row.
    """
    validate_topic_id(topic_id)
    validate_iso_utc(older_than_ts)
    debate = get_debate(conn, topic_id)
    if debate is None:
        raise DebateError(
            f"unknown_topic: {topic_id}",
            error_type="topic_not_found",
        )
    rows = conn.execute(
        "SELECT * FROM debate_worker_claims "
        "WHERE topic_id = ? AND state IN ('completed', 'retired') "
        "AND heartbeat_at < ? "
        "ORDER BY heartbeat_at ASC, worker_session_id ASC",
        (topic_id, older_than_ts),
    ).fetchall()
    now = now_iso()
    reaped: list[dict[str, Any]] = []
    for row in rows:
        reap_id = new_msg_id()
        while conn.execute(
            "SELECT 1 FROM debate_worker_reap_log WHERE reap_id = ? LIMIT 1",
            (reap_id,),
        ).fetchone():
            reap_id = new_msg_id()
        details = {
            "state": row["state"],
            "completed_at": row["completed_at"],
            "ack_msg_id": row["ack_msg_id"],
        }
        conn.execute(
            "INSERT INTO debate_worker_reap_log "
            "(reap_id, topic_id, role, parent_session_id, worker_session_id, "
            " trigger_msg_id, result, details_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'reaped', ?, ?)",
            (
                reap_id,
                row["topic_id"],
                row["role"],
                row["parent_session_id"],
                row["worker_session_id"],
                row["trigger_msg_id"],
                json_dumps(details),
                now,
            ),
        )
        conn.execute(
            "DELETE FROM debate_worker_claims "
            "WHERE topic_id = ? AND role = ? AND worker_session_id = ?",
            (row["topic_id"], row["role"], row["worker_session_id"]),
        )
        reaped.append(
            {
                "reap_id": reap_id,
                "role": row["role"],
                "worker_session_id": row["worker_session_id"],
                "trigger_msg_id": row["trigger_msg_id"],
            }
        )
    return {
        "topic_id": topic_id,
        "topic_state": debate["state"],
        "older_than_ts": older_than_ts,
        "reaped": reaped,
        "count": len(reaped),
    }


def reclaim_stale_message_claims(
    conn: sqlite3.Connection,
    *,
    topic_id: str,
    older_than_ts: str,
    minimum_age_seconds: int = 60,
) -> dict[str, Any]:
    """Return stale one-shot DECISION claims to claimable state.

    Only ``standing=false`` DECISION claims in ``active`` state are eligible.
    If a terminal A/STATUS reply exists, the claim is completed instead of
    reclaimed so a late cleanup cannot resurrect already-handled work.
    """
    validate_topic_id(topic_id)
    _validate_reclaim_cutoff(older_than_ts, minimum_age_seconds)
    debate = get_debate(conn, topic_id)
    if debate is None:
        raise DebateError(
            f"unknown_topic: {topic_id}",
            error_type="topic_not_found",
        )

    rows = conn.execute(
        "SELECT c.msg_id, c.role, c.owner_session_id, c.claimed_at, "
        "c.heartbeat_at, c.ack_msg_id "
        "FROM debate_message_claims c "
        "JOIN debate_messages m ON m.msg_id = c.msg_id "
        "WHERE m.topic_id = ? AND m.kind = 'DECISION' AND m.standing = 0 "
        "AND c.state = 'active' AND c.heartbeat_at < ? "
        "ORDER BY c.heartbeat_at ASC, c.msg_id ASC, c.role ASC",
        (topic_id, older_than_ts),
    ).fetchall()

    now = now_iso()
    reclaimed: list[dict[str, Any]] = []
    completed: list[dict[str, Any]] = []
    for row in rows:
        ack = _terminal_reply_for_trigger(
            conn,
            topic_id=topic_id,
            role=row["role"],
            trigger_msg_id=row["msg_id"],
        )
        reclaim_id = new_msg_id()
        while conn.execute(
            "SELECT 1 FROM debate_message_claim_reclaim_log "
            "WHERE reclaim_id = ? LIMIT 1",
            (reclaim_id,),
        ).fetchone():
            reclaim_id = new_msg_id()

        details = {
            "claimed_at": row["claimed_at"],
            "heartbeat_at": row["heartbeat_at"],
            "older_than_ts": older_than_ts,
            "minimum_age_seconds": minimum_age_seconds,
            "ack_msg_id": ack["msg_id"] if ack else None,
        }
        if ack is not None:
            result = "completed_from_terminal"
            conn.execute(
                "UPDATE debate_message_claims SET state = 'done', "
                "heartbeat_at = ?, completed_at = ?, ack_msg_id = ? "
                "WHERE msg_id = ? AND role = ?",
                (now, now, ack["msg_id"], row["msg_id"], row["role"]),
            )
            completed.append(
                {
                    "msg_id": row["msg_id"],
                    "role": row["role"],
                    "ack_msg_id": ack["msg_id"],
                    "reclaim_id": reclaim_id,
                }
            )
        else:
            result = "reclaimed"
            conn.execute(
                "DELETE FROM debate_message_claims "
                "WHERE msg_id = ? AND role = ? AND state = 'active'",
                (row["msg_id"], row["role"]),
            )
            reclaimed.append(
                {
                    "msg_id": row["msg_id"],
                    "role": row["role"],
                    "owner_session_id": row["owner_session_id"],
                    "reclaim_id": reclaim_id,
                }
            )

        conn.execute(
            "INSERT INTO debate_message_claim_reclaim_log "
            "(reclaim_id, msg_id, topic_id, role, owner_session_id, "
            " result, details_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                reclaim_id,
                row["msg_id"],
                topic_id,
                row["role"],
                row["owner_session_id"],
                result,
                json_dumps(details),
                now,
            ),
        )

    return {
        "topic_id": topic_id,
        "topic_state": debate["state"],
        "older_than_ts": older_than_ts,
        "minimum_age_seconds": minimum_age_seconds,
        "reclaimed": reclaimed,
        "completed": completed,
        "reclaimed_count": len(reclaimed),
        "completed_count": len(completed),
    }


# ── DAO: messages ─────────────────────────────────────────────────────


def post_message(
    conn: sqlite3.Connection,
    *,
    topic_id: str,
    role: str,
    priority: str,
    kind: str,
    body: str,
    reply_to: str | None = None,
    standing: bool | None = None,
    vehicle: str | None = None,
    protocol_version: str | None = None,
    body_mode: str | None = None,
    payload_json: Any = None,
    author_session_id: str | None = None,
    recipients: list[str] | None = None,
) -> dict[str, Any]:
    """Append a message to a debate. Validates topic state, role membership,
    enums, and all kind-specific semantics BEFORE the INSERT (atomicity
    fix per CONDUCTOR msg:bf45a126). On any DebateError no row is
    persisted.

    ``vehicle`` (v3.12) tags the kind of work the message implies:
    ``analysis`` | ``review`` | ``implementation``. NULL/absent persists as
    NULL and reads back as the default ``analysis`` (backcompat). An
    explicit bad value raises a typed ``invalid_vehicle`` DebateError
    pre-INSERT. The value gates the wake/pump router downstream (see
    ``claim_worker_session`` + ``prepare_wake_dry_run``): ``implementation``
    fails closed instead of dispatching a no-edit wake-worker.

    Side-effects (post-INSERT, only when validations pass):
      kind=STATE: triggers transition via VALID_TRANSITIONS + UPDATE debates.
      kind=WATERMARK: updates debate_watermarks for (topic_id, role), then
      reconciles the active primary session's addressed-subset cursor. A full
      visible-ledger acknowledgement implies that its addressed subset was
      processed; the inverse is intentionally not true.
    """
    validate_topic_id(topic_id)
    validate_role(role)
    validate_priority(priority)
    validate_kind(kind)
    # v3.12: reject an explicit bad vehicle pre-INSERT (typed error). NULL/
    # absent is allowed through and stored as NULL (reads back as default).
    if vehicle is not None and vehicle != "":
        validate_vehicle(vehicle)
    vehicle_db = vehicle if (vehicle is not None and vehicle != "") else None
    if not isinstance(body, str) or not body.strip():
        # v3.9.3 (msg:76e96a96 P2.5): reject whitespace-only bodies in
        # addition to fully empty ones. A body that strips to empty
        # carries no semantic content and would silently bypass any
        # downstream OODA / kind-specific regex checks.
        raise DebateError("invalid_body: must be non-empty string")

    debate = get_debate(conn, topic_id)
    if debate is None:
        raise DebateError(f"unknown_topic: {topic_id}")
    if not role_in_debate(debate["roles"], role):
        raise DebateError(
            f"unknown_role_for_topic: {role} not in declared roles for {topic_id}"
        )

    if kind != "STATE" and debate["state"] == "ARCHIVED":
        raise DebateError(
            f"topic_archived_read_only: {topic_id} (only STATE messages allowed)"
        )
    if kind != "STATE" and debate["state"] == "RESOLVED":
        raise DebateError(
            f"topic_resolved_read_only: {topic_id} (only STATE -> ARCHIVED allowed)"
        )

    parent_kind: str | None = None
    parent_standing: int | None = None
    if reply_to is not None:
        validate_msg_id(reply_to)
        parent = conn.execute(
            "SELECT topic_id, kind, standing FROM debate_messages WHERE msg_id = ?",
            (reply_to,),
        ).fetchone()
        if parent is None:
            raise DebateError(f"unknown_reply_to: {reply_to}")
        if parent["topic_id"] != topic_id:
            raise DebateError(f"reply_to_cross_topic: {reply_to} not in {topic_id}")
        parent_kind = parent["kind"]
        parent_standing = parent["standing"]

    # ── Kind-specific PRE-INSERT validation (atomicity fix bf45a126) ──
    new_state_target: str | None = None
    watermark_resolved: tuple[str | None, str] | None = None

    if kind == "STATE":
        # v3.9.5 strict-regex validation (msg:2c22988a). Accepts EITHER
        # '<STATE>' OR '<STATE> [reason: ...]' fullmatch-anchored. The
        # bare `body.strip()` form used pre-v3.9.5 would reject an
        # enriched body wholesale; a naive leading-token parse would
        # silently swallow trailing junk — exactly the prefix-
        # acceptance class the v3.9.3 WATERMARK fixup ruled out.
        m = _STATE_BODY_RE.fullmatch(body.strip())
        if m is None:
            raise DebateError(
                f"invalid_state_body: {body!r}",
                error_type="invalid_state",
            )
        target = m.group(1)
        validate_transition(debate["state"], target)
        new_state_target = target

    if kind == "WATERMARK":
        watermark_target = body.strip()
        if MSG_ID_RE.fullmatch(watermark_target):
            # Canonical form (turn-4 per CONDUCTOR msg:c39e7d18): raw
            # msg_id only. DAO derives ts from the message row so the
            # body cannot tamper with the timestamp.
            ref_msg = conn.execute(
                "SELECT msg_id, ts FROM debate_messages "
                "WHERE msg_id = ? AND topic_id = ?",
                (watermark_target, topic_id),
            ).fetchone()
            if ref_msg is None:
                raise DebateError(f"watermark_msg_not_in_topic: {watermark_target}")
            watermark_resolved = (ref_msg["msg_id"], ref_msg["ts"])
        else:
            # Deprecated keyword form. Parsed for back-compat, but DAO
            # authoritatively derives ts from the message row and
            # raises watermark_ts_mismatch if the body's ts disagrees
            # — closes a tampering vector during the deprecation
            # window. New callers MUST use msg_id-only form.
            #
            # Per ADVOCATE turn-5 blocker (msg:b246664b): use fullmatch
            # not search. ``re.search`` matches anywhere in the input,
            # so a body like ``processed_up_to=<ts>:<valid12>ffff``
            # would accept the valid 12-char msg_id and silently drop
            # the trailing junk. ``fullmatch`` requires the entire
            # body to conform, rejecting any trailing or leading
            # extra characters. Body is already ``.strip()``-ed above.
            m = _WATERMARK_RE.fullmatch(watermark_target)
            if m is None:
                raise DebateError(
                    f"invalid_watermark_body: {watermark_target!r} "
                    "(expect raw msg_id; deprecated keyword form is "
                    "also accepted only if its ts matches the looked-up "
                    "row)"
                )
            wm_ts_claimed = m.group("ts")
            wm_msg_id = m.group("msg_id")
            if not ISO_UTC_RE.fullmatch(wm_ts_claimed):
                raise DebateError(
                    f"invalid_watermark_ts: {wm_ts_claimed!r} (expect ISO 8601 UTC)"
                )
            ref_msg = conn.execute(
                "SELECT msg_id, ts FROM debate_messages "
                "WHERE msg_id = ? AND topic_id = ?",
                (wm_msg_id, topic_id),
            ).fetchone()
            if ref_msg is None:
                raise DebateError(f"watermark_msg_not_in_topic: {wm_msg_id}")
            if ref_msg["ts"] != wm_ts_claimed:
                raise DebateError(
                    f"watermark_ts_mismatch: body claimed ts="
                    f"{wm_ts_claimed!r} but msg_id {wm_msg_id} actual "
                    f"ts={ref_msg['ts']!r}"
                )
            watermark_resolved = (wm_msg_id, ref_msg["ts"])

    standing_db = _standing_to_db(standing)

    if kind == "DECISION" and reply_to is not None and parent_kind != "Q":
        raise DebateError(f"decision_reply_to_must_be_Q: parent kind={parent_kind!r}")

    if kind in ("A", "STATUS") and reply_to is not None:
        one_shot_parent = (
            parent_kind == "DECISION" and parent_standing == 0
        ) or _worker_claim_exists(
            conn, topic_id=topic_id, role=role, trigger_msg_id=reply_to
        )
        if one_shot_parent:
            existing_terminal = _terminal_reply_for_trigger(
                conn, topic_id=topic_id, role=role, trigger_msg_id=reply_to
            )
            if existing_terminal is not None:
                raise DebateError(
                    f"terminal_reply_duplicate: {reply_to}",
                    error_type="terminal_reply_duplicate",
                )

    if kind == "COMPACTION" and not _OODA_RE.search(body):
        raise DebateError(
            "compaction_body_missing_OODA_sections: body must contain "
            "OBSERVE / ORIENT / DECIDE / ACT in that order"
        )

    # debate/v1 is enforced here, at the only message INSERT choke point.
    # The preflight is read-only: any rejection therefore produces exactly
    # zero message, recipient, protocol-state or audit rows.
    try:
        semantic = _protocol_v1_preflight_post(
            conn,
            topic_id=topic_id,
            role=role,
            kind=kind,
            reply_to=reply_to,
            payload=payload_json,
            body_mode=body_mode,
            protocol_version=protocol_version,
            author_session_id=author_session_id,
            recipients=recipients or (),
        )
    except ProtocolV1Error as exc:
        _raise_protocol_error(exc)

    msg_id = new_msg_id()
    while conn.execute(
        "SELECT 1 FROM debate_messages WHERE msg_id = ? LIMIT 1", (msg_id,)
    ).fetchone():
        msg_id = new_msg_id()

    ts = now_iso()
    if not ISO_UTC_RE.fullmatch(ts):
        ts = ts.replace("+00:00", "Z")

    conn.execute(
        "INSERT INTO debate_messages (msg_id, topic_id, role, ts, priority, "
        "kind, standing, vehicle, reply_to, body, protocol_version, round_no, "
        "body_mode, payload_json, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            msg_id,
            topic_id,
            role,
            ts,
            priority,
            kind,
            standing_db,
            vehicle_db,
            reply_to,
            body,
            semantic["protocol_version"] if semantic else None,
            semantic["round_no"] if semantic else None,
            semantic["body_mode"] if semantic else None,
            semantic["payload_json"] if semantic else None,
            ts,
        ),
    )
    try:
        protocol_state = _protocol_v1_record_post(
            conn,
            topic_id=topic_id,
            role=role,
            kind=kind,
            msg_id=msg_id,
            semantic=semantic,
        )
    except ProtocolV1Error as exc:
        _raise_protocol_error(exc)
    _complete_nonstanding_decision_claims_for_reply(
        conn,
        topic_id=topic_id,
        role=role,
        kind=kind,
        reply_to=reply_to,
        ack_msg_id=msg_id,
        now=ts,
    )

    # v3.13 (2026-07-12): PING is the explicit wake kind, yet plain-post
    # PINGs carried their target only as free text ("target=ROLE"), so the
    # wake resolver — which reads debate_message_recipients — never saw
    # them and ESCALATE:WAKE pings woke nobody.  Derive recipient rows
    # deterministically from `target=ROLE` tokens, accepting only roles
    # declared in this topic's roster (no free-form fan-out).
    if kind == "PING":
        declared_roles = {
            str(r.get("role", "")).upper()
            for r in debate["roles"]
            if isinstance(r, dict) and r.get("role")
        }
        derived = {
            token.upper()
            for token in re.findall(r"target=([A-Za-z0-9_]+)", body)
            if token.upper() in declared_roles
        }
        for recipient in sorted(derived):
            _enqueue_delivery(conn, msg_id, recipient, ts, mode="normal")

    new_state = debate["state"]
    if kind == "STATE" and new_state_target is not None:
        new_state = new_state_target
        archived_at = ts if new_state == "ARCHIVED" else None
        conn.execute(
            "UPDATE debates SET state = ?, "
            "archived_at = COALESCE(?, archived_at) WHERE topic_id = ?",
            (new_state, archived_at, topic_id),
        )

    signal_cursor_reconciliation: dict[str, Any] | None = None
    if kind == "WATERMARK" and watermark_resolved is not None:
        wm_id, wm_ts = watermark_resolved
        conn.execute(
            "INSERT INTO debate_watermarks (topic_id, role, "
            "last_processed_msg_id, last_processed_ts, updated_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(topic_id, role) DO UPDATE SET "
            "last_processed_msg_id = excluded.last_processed_msg_id, "
            "last_processed_ts = excluded.last_processed_ts, "
            "updated_at = excluded.updated_at",
            (topic_id, role, wm_id, wm_ts, ts),
        )
        signal_cursor_reconciliation = (
            _reconcile_active_signal_cursor_from_role_watermark(
                conn,
                topic_id=topic_id,
                role=role,
                watermark_msg_id=wm_id,
                watermark_ts=wm_ts,
                reconciled_at=ts,
            )
        )

    result = {
        "msg_id": msg_id,
        "ts": ts,
        "topic_state": new_state,
        "vehicle": vehicle_db if vehicle_db is not None else DEFAULT_VEHICLE,
    }
    if semantic is not None:
        result.update(
            {
                "protocol_version": DEBATE_PROTOCOL_V1,
                "round_no": semantic["round_no"],
                "body_mode": semantic["body_mode"],
                "protocol_state": protocol_state,
            }
        )
        if author_session_id and is_worker_session_id(author_session_id):
            result["worker_claim"] = _complete_worker_claim_if_terminal(
                conn,
                topic_id=topic_id,
                role=role,
                worker_session_id=author_session_id,
                now=ts,
            )
    if signal_cursor_reconciliation is not None:
        result["signal_cursor_reconciliation"] = signal_cursor_reconciliation
    return result


def get_watermark(
    conn: sqlite3.Connection, topic_id: str, role: str
) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT topic_id, role, last_processed_msg_id, last_processed_ts, "
        "updated_at FROM debate_watermarks "
        "WHERE topic_id = ? AND role = ?",
        (topic_id, role),
    ).fetchone()
    return dict(row) if row else None


def read_messages(
    conn: sqlite3.Connection,
    *,
    topic_id: str,
    role: str,
    since_msg_id: str | None = None,
    since_ts: str | None = None,
    since_latest_compaction: bool = False,
    kind_filter: list[str] | None = None,
    priority_filter: list[str] | None = None,
    limit: int | None = None,
    control_plane: bool = False,
) -> dict[str, Any]:
    """Read messages from a topic with compound (ts, msg_id) cursor.

    Cursor priority:
      1. since_latest_compaction=True (per CONDUCTOR msg:9b7c3d28 turn-3):
         use latest COMPACTION's (ts, msg_id) as cursor. Returns
         bootstrap_compaction_msg_id field. If no COMPACTION exists,
         falls through to the rest of the precedence.
      2. explicit since_msg_id (validated; raises if not found in topic).
      3. explicit since_ts (no exact-match requirement; pre-existing
         timestamps are allowed and return all messages after).
      4. debate_watermarks[(topic_id, role)].
      5. beginning of topic.

    WHERE clause is `(ts > :wm_ts) OR (ts = :wm_ts AND msg_id > :wm_msg_id)`
    so messages with the same ts but different msg_ids never get skipped
    (per CONDUCTOR msg:7e3c8f10 turn-2 fix). Legacy watermarks with only
    ts (no msg_id) treat msg_id as '' (lex-smallest) so the first
    compound-cursor read returns ALL messages at that ts.

    Pagination: limit defaults to DEFAULT_READ_LIMIT (200), capped at
    MAX_READ_LIMIT (1000). Returns truncated=True + next_*_cursor when
    more messages remain. Does NOT auto-advance the watermark.

    Unknown since_msg_id raises (turn-3 fix per CONDUCTOR msg:7da13e9f);
    silent fall-through to start of topic was a masked-bug pattern.
    """
    validate_topic_id(topic_id)
    validate_role(role)
    debate = get_debate(conn, topic_id)
    if debate is None:
        raise DebateError(f"unknown_topic: {topic_id}")
    visibility_predicate, visibility_params = _protocol_v1_visibility_sql(
        alias="m", viewer_role=role, control_plane=control_plane
    )

    cursor_ts: str | None = None
    cursor_msg_id: str = ""
    bootstrap_compaction_msg_id: str | None = None

    # Cursor precedence (turn-4 per CONDUCTOR msg:5a2f8c47):
    #   since_msg_id > since_ts > since_latest_compaction > watermark > start
    # Explicit caller intent (msg_id, ts) takes precedence over the
    # COMPACTION bootstrap shortcut so a caller asking for "from X"
    # never gets silently shifted to the latest compaction.
    if since_msg_id is not None:
        validate_msg_id(since_msg_id)
        ref = conn.execute(
            "SELECT m.ts, m.msg_id FROM debate_messages m "
            "WHERE m.msg_id = ? AND m.topic_id = ? AND " + visibility_predicate,
            (since_msg_id, topic_id, *visibility_params),
        ).fetchone()
        if ref is None:
            raise DebateError(
                f"unknown_since_msg_id: {since_msg_id} not found in topic {topic_id}"
            )
        cursor_ts = ref["ts"]
        cursor_msg_id = ref["msg_id"]

    if cursor_ts is None and since_ts is not None:
        validate_iso_utc(since_ts)
        cursor_ts = since_ts
        # v3.9.3 since_ts strict-exclusive (msg:946bcff6 amendment 3):
        # cursor_msg_id=None signals the WHERE-clause branch to emit
        # bare ``ts > ?`` instead of the compound form. Returns
        # messages strictly AFTER since_ts (no boundary inclusion).
        cursor_msg_id = None

    if cursor_ts is None and since_latest_compaction:
        comp = conn.execute(
            "SELECT m.msg_id, m.ts FROM debate_messages m "
            "WHERE m.topic_id = ? AND m.kind = 'COMPACTION' AND "
            + visibility_predicate
            + " ORDER BY m.ts DESC, m.msg_id DESC LIMIT 1",
            (topic_id, *visibility_params),
        ).fetchone()
        if comp is not None:
            cursor_ts = comp["ts"]
            cursor_msg_id = comp["msg_id"]
            bootstrap_compaction_msg_id = comp["msg_id"]

    if cursor_ts is None:
        wm = get_watermark(conn, topic_id, role)
        if wm and wm.get("last_processed_ts"):
            cursor_ts = wm["last_processed_ts"]
            cursor_msg_id = wm.get("last_processed_msg_id") or ""

    if limit is None:
        effective_limit = DEFAULT_READ_LIMIT
    else:
        if not isinstance(limit, int) or limit < 1:
            raise DebateError("invalid_limit: must be positive int or None")
        effective_limit = min(limit, MAX_READ_LIMIT)

    where: list[str] = ["m.topic_id = ?", visibility_predicate]
    params: list[Any] = [topic_id]
    params.extend(visibility_params)
    if cursor_ts is not None:
        # Dual-branch cursor (v3.9.3 msg:946bcff6 amendment 3,
        # read_messages naked-column form). cursor_msg_id is None ONLY
        # when the caller passed since_ts without since_msg_id — emit
        # strict-exclusive ts comparison so a message at ts ==
        # since_ts is NOT re-emitted. Compound form runs only when an
        # explicit msg_id cursor exists (since_msg_id, watermark,
        # compaction).
        if cursor_msg_id is None:
            where.append("m.ts > ?")
            params.extend([cursor_ts])
        else:
            where.append("(m.ts > ? OR (m.ts = ? AND m.msg_id > ?))")
            params.extend([cursor_ts, cursor_ts, cursor_msg_id])
    if kind_filter:
        for k in kind_filter:
            validate_kind(k)
        ph = ",".join("?" * len(kind_filter))
        where.append(f"m.kind IN ({ph})")
        params.extend(kind_filter)
    if priority_filter:
        for p in priority_filter:
            validate_priority(p)
        ph = ",".join("?" * len(priority_filter))
        where.append(f"m.priority IN ({ph})")
        params.extend(priority_filter)
    where_sql = "WHERE " + " AND ".join(where)

    fetch_limit = effective_limit + 1  # one extra row to detect truncation
    rows = conn.execute(
        f"SELECT m.msg_id, m.topic_id, m.role, m.ts, m.priority, m.kind, "
        f"m.reply_to, m.standing, m.body, m.protocol_version, m.round_no, "
        f"m.body_mode, m.payload_json, m.created_at "
        f"FROM debate_messages m {where_sql} "
        f"ORDER BY m.ts ASC, m.msg_id ASC LIMIT ?",
        [*params, fetch_limit],
    ).fetchall()

    truncated = len(rows) > effective_limit
    if truncated:
        rows = rows[:effective_limit]
    messages = []
    for row in rows:
        item = dict(row)
        if item.get("protocol_version") is None:
            for key in ("protocol_version", "round_no", "body_mode", "payload_json"):
                item.pop(key, None)
        messages.append(item)
    last_msg_id = messages[-1]["msg_id"] if messages else None
    last_ts = messages[-1]["ts"] if messages else None

    return {
        "messages": messages,
        "topic_state": debate["state"],
        "last_msg_id_returned": last_msg_id,
        "last_ts_returned": last_ts,
        "count": len(messages),
        "truncated": truncated,
        "next_msg_id_cursor": last_msg_id if truncated else None,
        "next_ts_cursor": last_ts if truncated else None,
        "limit": effective_limit,
        "bootstrap_compaction_msg_id": bootstrap_compaction_msg_id,
    }


def advance_watermark(
    conn: sqlite3.Connection,
    *,
    topic_id: str,
    role: str,
    processed_up_to_msg_id: str,
) -> dict[str, Any]:
    """Convenience helper: write a canonical WATERMARK message that
    advances the (topic_id, role) cursor to a specific msg_id.

    Canonical body (turn-4 per CONDUCTOR msg:c39e7d18): raw msg_id only.
    DAO derives ts from the message row, so callers can never insert a
    stale or tampered ts. Atomically updates debate_watermarks via
    post_message side-effect.
    """
    validate_topic_id(topic_id)
    validate_role(role)
    validate_msg_id(processed_up_to_msg_id)
    ref = conn.execute(
        "SELECT 1 FROM debate_messages WHERE msg_id = ? AND topic_id = ?",
        (processed_up_to_msg_id, topic_id),
    ).fetchone()
    if ref is None:
        raise DebateError(
            f"unknown_msg_id_for_watermark: {processed_up_to_msg_id} "
            f"not in topic {topic_id}"
        )
    return post_message(
        conn,
        topic_id=topic_id,
        role=role,
        priority="INFO",
        kind="WATERMARK",
        body=processed_up_to_msg_id,
    )


# ── v3.10: role/session lifecycle authority ───────────────────────────


def list_role_bindings(conn: sqlite3.Connection, *, topic_id: str) -> dict[str, Any]:
    validate_topic_id(topic_id)
    debate = get_debate(conn, topic_id)
    if debate is None:
        raise DebateError(
            f"unknown_topic: {topic_id}",
            error_type="topic_not_found",
        )
    rows = conn.execute(
        "SELECT b.topic_id, b.role, b.session_id, b.runtime, b.state, "
        "b.generation, b.created_at, b.updated_at, b.retired_at, "
        "b.reason, b.bound_by_role, b.bound_by_msg_id, "
        "s.last_processed_msg_id, s.last_processed_ts, s.last_check_at "
        "FROM debate_role_bindings b "
        "LEFT JOIN debate_signal_state s "
        "ON s.topic_id = b.topic_id AND s.role = b.role "
        "AND s.session_id = b.session_id "
        "WHERE b.topic_id = ? "
        "ORDER BY b.role ASC, b.generation ASC, b.session_id ASC",
        (topic_id,),
    ).fetchall()
    return {
        "topic_id": topic_id,
        "topic_state": debate["state"],
        "bindings": [dict(r) for r in rows],
    }


def bind_role_session(
    conn: sqlite3.Connection,
    *,
    topic_id: str,
    role: str,
    session_id: str,
    runtime: str = "",
    state: str = "active",
    reason: str,
    bound_by_role: str | None = None,
    bound_by_msg_id: str | None = None,
    replace_active: bool = False,
    conductor_override_msg_id: str | None = None,
) -> dict[str, Any]:
    validate_topic_id(topic_id)
    validate_session_id(session_id)
    validate_binding_state(state)
    if not isinstance(reason, str) or not reason.strip():
        raise DebateError("invalid_reason: must be non-empty string")
    if bound_by_role:
        validate_role(bound_by_role)
    if bound_by_msg_id:
        validate_msg_id(bound_by_msg_id)
    debate = get_debate(conn, topic_id)
    if debate is None:
        raise DebateError(
            f"unknown_topic: {topic_id}",
            error_type="topic_not_found",
        )
    _validate_role_for_debate(debate, topic_id, role)

    now = now_iso()
    runtime = runtime.strip() if isinstance(runtime, str) else ""
    if not runtime:
        runtime = _runtime_from_session(session_id)

    existing_active = _active_binding(conn, topic_id, role)
    retired_sessions: list[str] = []
    retired_worker_claims = 0

    if state == "active":
        session_owner = conn.execute(
            "SELECT role, session_id FROM debate_role_bindings "
            "WHERE topic_id = ? AND session_id = ? AND state = 'active' "
            "ORDER BY generation DESC LIMIT 1",
            (topic_id, session_id),
        ).fetchone()
        if session_owner is not None and session_owner["role"] != role:
            raise DebateError(
                f"duplicate_active_session: session {session_id} already owns "
                f"role {session_owner['role']} in {topic_id}",
                error_type="binding_duplicate_active_session",
            )
        if existing_active is not None and existing_active["session_id"] != session_id:
            if not replace_active:
                raise DebateError(
                    f"duplicate_active_binding: role {role} already "
                    f"owned by {existing_active['session_id']}",
                    error_type="binding_duplicate_active",
                )
            conn.execute(
                "UPDATE debate_role_bindings SET state = 'retired', "
                "retired_at = ?, updated_at = ?, reason = ? "
                "WHERE topic_id = ? AND role = ? AND state = 'active'",
                (
                    now,
                    now,
                    f"replaced_by={session_id}: {reason.strip()}",
                    topic_id,
                    role,
                ),
            )
            retired_sessions.append(existing_active["session_id"])
            retired_worker_claims += _retire_worker_claims_for_parent_sessions(
                conn,
                topic_id=topic_id,
                role=role,
                parent_session_ids=[existing_active["session_id"]],
                now=now,
            )
        generation = _next_binding_generation(conn, topic_id, role)
        conn.execute(
            "INSERT INTO debate_role_bindings "
            "(topic_id, role, session_id, runtime, state, generation, "
            " created_at, updated_at, retired_at, reason, bound_by_role, "
            " bound_by_msg_id) "
            "VALUES (?, ?, ?, ?, 'active', ?, ?, ?, NULL, ?, ?, ?) "
            "ON CONFLICT(topic_id, role, session_id) DO UPDATE SET "
            "runtime = excluded.runtime, state = 'active', "
            "generation = excluded.generation, updated_at = excluded.updated_at, "
            "retired_at = NULL, reason = excluded.reason, "
            "bound_by_role = excluded.bound_by_role, "
            "bound_by_msg_id = excluded.bound_by_msg_id",
            (
                topic_id,
                role,
                session_id,
                runtime,
                generation,
                now,
                now,
                reason.strip(),
                bound_by_role,
                bound_by_msg_id,
            ),
        )
        return {
            "topic_id": topic_id,
            "role": role,
            "session_id": session_id,
            "runtime": runtime,
            "state": "active",
            "generation": generation,
            "retired_sessions": retired_sessions,
            "retired_worker_claims": retired_worker_claims,
        }

    if state == "diagnostic":
        target = _binding_for_session(conn, topic_id, role, session_id)
        would_uncover = bool(target and target["state"] == "active")
        if would_uncover:
            _validate_conductor_override(
                conn, topic_id=topic_id, override_msg_id=conductor_override_msg_id
            )
            retired_worker_claims += _retire_worker_claims_for_parent_sessions(
                conn,
                topic_id=topic_id,
                role=role,
                parent_session_ids=[session_id],
                now=now,
            )
        generation = _next_binding_generation(conn, topic_id, role)
        conn.execute(
            "INSERT INTO debate_role_bindings "
            "(topic_id, role, session_id, runtime, state, generation, "
            " created_at, updated_at, retired_at, reason, bound_by_role, "
            " bound_by_msg_id) "
            "VALUES (?, ?, ?, ?, 'diagnostic', ?, ?, ?, NULL, ?, ?, ?) "
            "ON CONFLICT(topic_id, role, session_id) DO UPDATE SET "
            "runtime = excluded.runtime, state = 'diagnostic', "
            "generation = excluded.generation, updated_at = excluded.updated_at, "
            "retired_at = NULL, reason = excluded.reason, "
            "bound_by_role = excluded.bound_by_role, "
            "bound_by_msg_id = excluded.bound_by_msg_id",
            (
                topic_id,
                role,
                session_id,
                runtime,
                generation,
                now,
                now,
                reason.strip(),
                bound_by_role,
                bound_by_msg_id,
            ),
        )
        return {
            "topic_id": topic_id,
            "role": role,
            "session_id": session_id,
            "runtime": runtime,
            "state": "diagnostic",
            "generation": generation,
            "ownership_gap_override": would_uncover,
            "retired_worker_claims": retired_worker_claims,
        }

    # Retiring a role owner without replacement is an explicit override path.
    target = _binding_for_session(conn, topic_id, role, session_id)
    if target is None:
        raise DebateError(
            f"unknown_binding: {topic_id}/{role}/{session_id}",
            error_type="binding_not_found",
        )
    would_uncover = target["state"] == "active"
    if would_uncover:
        _validate_conductor_override(
            conn, topic_id=topic_id, override_msg_id=conductor_override_msg_id
        )
        retired_worker_claims += _retire_worker_claims_for_parent_sessions(
            conn,
            topic_id=topic_id,
            role=role,
            parent_session_ids=[session_id],
            now=now,
        )
    conn.execute(
        "UPDATE debate_role_bindings SET state = 'retired', retired_at = ?, "
        "updated_at = ?, reason = ? "
        "WHERE topic_id = ? AND role = ? AND session_id = ?",
        (now, now, reason.strip(), topic_id, role, session_id),
    )
    return {
        "topic_id": topic_id,
        "role": role,
        "session_id": session_id,
        "state": "retired",
        "ownership_gap_override": would_uncover,
        "retired_worker_claims": retired_worker_claims,
    }


def seed_initial_role_bindings(
    conn: sqlite3.Connection,
    *,
    topic_id: str,
    roles: list[dict[str, Any]],
    bound_by_role: str,
    reason: str = "seeded from debate_init roles_json",
) -> list[dict[str, Any]]:
    """Ensure roles_json has matching active primary bindings.

    ``roles_json`` declares the topic roles, but wake delivery resolves through
    debate_role_bindings.  MCP-created topics must seed the binding authority,
    otherwise the first addressed messages resolve to ``no_active_binding`` and
    require manual wake.
    """
    validate_topic_id(topic_id)
    validate_role(bound_by_role)
    seeded: list[dict[str, Any]] = []
    seen_roles: set[str] = set()
    for entry in roles:
        if not isinstance(entry, dict):
            continue
        role = entry.get("role")
        session_id = entry.get("session_id")
        if not isinstance(role, str) or not isinstance(session_id, str):
            continue
        validate_role(role)
        validate_session_id(session_id)
        if role in seen_roles:
            continue
        seen_roles.add(role)
        if _active_binding(conn, topic_id, role) is not None:
            continue
        seeded.append(
            bind_role_session(
                conn,
                topic_id=topic_id,
                role=role,
                session_id=session_id,
                runtime=_runtime_from_session(session_id),
                reason=reason,
                bound_by_role=bound_by_role,
            )
        )
    return seeded


def add_role_to_debate(
    conn: sqlite3.Connection,
    *,
    topic_id: str,
    role: str,
    session_id: str,
    runtime: str = "",
    reason: str,
    bound_by_role: str | None = None,
    bound_by_msg_id: str | None = None,
    replace_active: bool = False,
    conductor_override_msg_id: str | None = None,
) -> dict[str, Any]:
    """Add a role to an EXISTING topic after debate_init froze ``roles_json``.

    Roles were historically frozen at ``debate_init`` (re-init with a new role
    raised ``topic_exists_with_different_roles``, and ``debate_bind_role`` on an
    undeclared role raised ``recipient_unknown_role``). This is the
    flexible-roster path: it appends ``role`` to the declared roster
    (``debates.roles_json``) and installs an active primary binding in the SAME
    transaction, so the role is both addressable (recipient validation consults
    ``roles_json``) and wake-resolvable (binding is the runtime authority).

    Idempotency / invariant contract:
      * role NOT declared  -> append to roles_json + active binding for
        ``session_id``.
      * role declared, ``session_id`` already the active owner -> no-op:
        returns the existing binding with ``added_role=False``.
      * role declared, a DIFFERENT session is the active owner -> rejected as
        ``binding_duplicate_active`` unless ``replace_active=True`` (atomic
        swap; the old owner is retired in the same transaction). For an
        exhausted-session handoff that must preserve the read cursor, prefer
        ``rotate_role_binding`` / ``debate_rotate_binding`` instead.

    Every mutation is audited by the binding row itself (generation, reason,
    bound_by_role, bound_by_msg_id) — consistent with the rest of v3.10. The
    declared-role no-active-primary case (e.g. a previously retired role) is a
    plain reactivation handled by ``bind_role_session(state='active')``.
    """
    validate_topic_id(topic_id)
    validate_role(role)
    validate_session_id(session_id)
    if not isinstance(reason, str) or not reason.strip():
        raise DebateError("invalid_reason: must be non-empty string")

    debate = get_debate(conn, topic_id)
    if debate is None:
        raise DebateError(
            f"unknown_topic: {topic_id}",
            error_type="topic_not_found",
        )

    already_declared = role_in_debate(debate["roles"], role)
    if not already_declared:
        validate_numbered_executor_role(role)
    if already_declared:
        existing_active = _active_binding(conn, topic_id, role)
        if (
            existing_active is not None
            and existing_active["session_id"] == session_id
            and not replace_active
        ):
            # Fully idempotent: role declared and this session already owns it.
            return {
                "topic_id": topic_id,
                "role": role,
                "session_id": session_id,
                "runtime": existing_active["runtime"],
                "state": "active",
                "generation": int(existing_active["generation"]),
                "added_role": False,
                "retired_sessions": [],
                "retired_worker_claims": 0,
            }
    else:
        # Append the role to the declared roster atomically with the binding.
        new_roles = list(debate["roles"]) + [{"role": role, "session_id": session_id}]
        conn.execute(
            "UPDATE debates SET roles_json = ? WHERE topic_id = ?",
            (json_dumps(new_roles), topic_id),
        )

    binding = bind_role_session(
        conn,
        topic_id=topic_id,
        role=role,
        session_id=session_id,
        runtime=runtime,
        state="active",
        reason=reason,
        bound_by_role=bound_by_role,
        bound_by_msg_id=bound_by_msg_id,
        replace_active=replace_active,
        conductor_override_msg_id=conductor_override_msg_id,
    )
    binding["added_role"] = not already_declared
    return binding


def rotate_role_binding(
    conn: sqlite3.Connection,
    *,
    topic_id: str,
    role: str,
    old_session_id: str,
    new_session_id: str,
    runtime: str = "",
    cursor_mode: str,
    reason: str,
    bound_by_role: str | None = None,
    bound_by_msg_id: str | None = None,
) -> dict[str, Any]:
    validate_cursor_mode(cursor_mode)
    validate_session_id(old_session_id)
    validate_session_id(new_session_id)
    old = _binding_for_session(conn, topic_id, role, old_session_id)
    if old is None or old["state"] != "active":
        raise DebateError(
            f"rotate_predecessor_not_active: {old_session_id}",
            error_type="binding_predecessor_not_active",
        )
    result = bind_role_session(
        conn,
        topic_id=topic_id,
        role=role,
        session_id=new_session_id,
        runtime=runtime,
        state="active",
        reason=reason,
        bound_by_role=bound_by_role,
        bound_by_msg_id=bound_by_msg_id,
        replace_active=True,
    )

    warning: str | None = None
    now = now_iso()
    if cursor_mode == "head":
        head = conn.execute(
            "SELECT msg_id, ts FROM debate_messages "
            "WHERE topic_id = ? ORDER BY ts DESC, msg_id DESC LIMIT 1",
            (topic_id,),
        ).fetchone()
        if head is not None:
            conn.execute(
                "INSERT INTO debate_signal_state "
                "(session_id, role, topic_id, last_processed_msg_id, "
                " last_processed_ts, last_check_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(session_id, role, topic_id) DO UPDATE SET "
                "last_processed_msg_id = excluded.last_processed_msg_id, "
                "last_processed_ts = excluded.last_processed_ts, "
                "last_check_at = excluded.last_check_at",
                (new_session_id, role, topic_id, head["msg_id"], head["ts"], now),
            )
    elif cursor_mode == "copy":
        source = conn.execute(
            "SELECT last_processed_msg_id, last_processed_ts "
            "FROM debate_signal_state "
            "WHERE session_id = ? AND role = ? AND topic_id = ?",
            (old_session_id, role, topic_id),
        ).fetchone()
        if source is None:
            warning = "copy_source_cursor_missing"
            conn.execute(
                "DELETE FROM debate_signal_state "
                "WHERE session_id = ? AND role = ? AND topic_id = ?",
                (new_session_id, role, topic_id),
            )
        else:
            conn.execute(
                "INSERT INTO debate_signal_state "
                "(session_id, role, topic_id, last_processed_msg_id, "
                " last_processed_ts, last_check_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(session_id, role, topic_id) DO UPDATE SET "
                "last_processed_msg_id = excluded.last_processed_msg_id, "
                "last_processed_ts = excluded.last_processed_ts, "
                "last_check_at = excluded.last_check_at",
                (
                    new_session_id,
                    role,
                    topic_id,
                    source["last_processed_msg_id"],
                    source["last_processed_ts"],
                    now,
                ),
            )
    else:  # replay
        conn.execute(
            "DELETE FROM debate_signal_state "
            "WHERE session_id = ? AND role = ? AND topic_id = ?",
            (new_session_id, role, topic_id),
        )

    result.update(
        {
            "old_session_id": old_session_id,
            "new_session_id": new_session_id,
            "cursor_mode": cursor_mode,
            "warning": warning,
        }
    )
    return result


def _retire_bindings_for_transition(
    conn: sqlite3.Connection, *, topic_id: str, new_state: str, reason: str
) -> tuple[int, int]:
    now = now_iso()
    if new_state == "RESOLVED":
        states = ("active",)
    elif new_state == "ARCHIVED":
        states = ("active", "diagnostic")
    else:
        return 0, 0
    worker_parents = conn.execute(
        "SELECT role, session_id FROM debate_role_bindings "
        "WHERE topic_id = ? AND state = 'active'",
        (topic_id,),
    ).fetchall()
    placeholders = ",".join("?" for _ in states)
    cur = conn.execute(
        "UPDATE debate_role_bindings SET state = 'retired', "
        "retired_at = COALESCE(retired_at, ?), updated_at = ?, reason = ? "
        f"WHERE topic_id = ? AND state IN ({placeholders})",
        (
            now,
            now,
            f"topic_{new_state.lower()}:{reason}"
            if reason
            else f"topic_{new_state.lower()}",
            topic_id,
            *states,
        ),
    )
    retired_worker_claims = 0
    for row in worker_parents:
        retired_worker_claims += _retire_worker_claims_for_parent_sessions(
            conn,
            topic_id=topic_id,
            role=row["role"],
            parent_session_ids=[row["session_id"]],
            now=now,
        )
    return int(cur.rowcount or 0), retired_worker_claims


# ── State transitions outside of post_message ──────────────────────────


def transition_state(
    conn: sqlite3.Connection,
    *,
    topic_id: str,
    role: str,
    new_state: str,
    reason: str = "",
) -> dict[str, Any]:
    """Transition a debate to a new state, writing a synthetic STATE message.

    For RESOLVED transitions, asserts every H+Q has a matching A reply
    (where A.reply_to == Q.msg_id). Returns blocking_questions list when
    the transition is rejected for that reason.
    """
    validate_topic_id(topic_id)
    validate_role(role)
    validate_state(new_state)
    debate = get_debate(conn, topic_id)
    if debate is None:
        raise DebateError(f"unknown_topic: {topic_id}")
    old_state = debate["state"]
    validate_transition(old_state, new_state)
    if not role_in_debate(debate["roles"], role):
        raise DebateError(f"unknown_role_for_topic: {role} not in declared roles")

    if new_state == "RESOLVED":
        blocking = _open_blocking_questions(conn, topic_id)
        if blocking:
            return {
                "old_state": old_state,
                "new_state": old_state,
                "ts": now_iso(),
                "blocking_questions": blocking,
            }

    # v3.9.5 fix (msg:c5e2e575 + ADVOCATE msg:2ccadbff): fold the
    # transition reason INTO the persisted STATE body instead of
    # writing a separate STATUS row. The deprecated dual-record
    # pattern failed catastrophically on RESOLVED→ARCHIVED transitions
    # because once the state flip lands, the topic enters read-only
    # mode and the follow-up STATUS write hits topic_resolved_read_only.
    # Reason text now lives where it semantically belongs — alongside
    # the state it explains — and the return value's `body` field
    # accurately reflects what's in debate_messages.
    state_body = new_state if not reason else f"{new_state} [reason: {reason}]"
    msg = post_message(
        conn,
        topic_id=topic_id,
        role=role,
        priority="H",
        kind="STATE",
        body=state_body,
    )
    retired_bindings, retired_worker_claims = _retire_bindings_for_transition(
        conn, topic_id=topic_id, new_state=new_state, reason=reason
    )
    return {
        "old_state": old_state,
        "new_state": new_state,
        "ts": msg["ts"],
        "blocking_questions": [],
        "transition_msg_id": msg["msg_id"],
        "body": state_body,
        "retired_bindings": retired_bindings,
        "retired_worker_claims": retired_worker_claims,
    }


def _open_blocking_questions(
    conn: sqlite3.Connection, topic_id: str
) -> list[dict[str, Any]]:
    """Return Q messages of any priority without a matching A reply.

    Per CONDUCTOR msg:d29b7e58 ADVOCATE turn 2 strict gate decision: ALL
    open Qs block RESOLVED, not just H-priority. An A whose body starts
    with the DEFERRED prefix counts as a matched answer (resolution-
    equivalent — the question is intentionally deferred for follow-up).
    """
    rows = conn.execute(
        "SELECT q.msg_id, q.role, q.priority, q.ts, q.body "
        "FROM debate_messages q "
        "WHERE q.topic_id = ? AND q.kind = 'Q' "
        "AND NOT EXISTS ("
        "  SELECT 1 FROM debate_messages a "
        "  WHERE a.topic_id = q.topic_id "
        "  AND a.kind = 'A' AND a.reply_to = q.msg_id"
        ") ORDER BY q.ts ASC",
        (topic_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def set_topic_priority(
    conn: sqlite3.Connection,
    *,
    topic_id: str,
    role: str,
    lane: str,
    reason: str,
    next_action: str = "",
    blocked_by: str = "",
) -> dict[str, Any]:
    """Set the CONDUCTOR-owned deterministic priority lane for a topic.

    This updates ``debates.metadata_json`` rather than posting a STATUS row.
    Debate messages still carry local H/M/L/INFO priority; the topic lane is
    the cross-topic scheduling authority used by work-queue views.
    """
    validate_topic_id(topic_id)
    validate_role(role)
    lane = lane.upper()
    validate_topic_priority_lane(lane)
    if role != "CONDUCTOR":
        raise DebateError(
            "topic_priority_requires_conductor",
            error_type="topic_priority_requires_conductor",
        )
    if not isinstance(reason, str) or not reason.strip():
        raise DebateError(
            "topic_priority_reason_required",
            error_type="topic_priority_reason_required",
        )
    row = conn.execute(
        "SELECT topic_id, roles_json, metadata_json FROM debates WHERE topic_id = ?",
        (topic_id,),
    ).fetchone()
    if row is None:
        raise DebateError(
            f"unknown_topic: {topic_id}",
            error_type="topic_not_found",
        )
    roles = json_loads(row["roles_json"]) if row["roles_json"] else []
    if not role_in_debate(roles, role):
        raise DebateError(
            f"unknown_role_for_topic: {role} not in declared roles",
            error_type="recipient_unknown_role",
        )

    now = now_iso()
    metadata = _metadata_dict(row["metadata_json"])
    metadata["conductor_priority"] = {
        "lane": lane,
        "rank": TOPIC_PRIORITY_LANE_ORDER[lane],
        "reason": reason.strip(),
        "next_action": str(next_action or "").strip(),
        "blocked_by": str(blocked_by or "").strip(),
        "updated_by_role": role,
        "updated_at": now,
    }
    conn.execute(
        "UPDATE debates SET metadata_json = ? WHERE topic_id = ?",
        (json_dumps(metadata), topic_id),
    )
    return {
        "topic_id": topic_id,
        "lane": lane,
        "rank": TOPIC_PRIORITY_LANE_ORDER[lane],
        "reason": reason.strip(),
        "next_action": str(next_action or "").strip(),
        "blocked_by": str(blocked_by or "").strip(),
        "updated_at": now,
    }


def _due_reason_and_score(
    resolve_by: str | None, now_dt: datetime
) -> tuple[str | None, int]:
    if not resolve_by:
        return None, 0
    try:
        due = _parse_iso_utc_dt(resolve_by)
    except DebateError:
        return "resolve_by_invalid", 0
    seconds = (due - now_dt).total_seconds()
    if seconds < 0:
        return "resolve_by_overdue", 60_000
    if seconds <= 4 * 3600:
        return "resolve_by_due_4h", 40_000
    if seconds <= 24 * 3600:
        return "resolve_by_due_24h", 20_000
    return None, 0


def _default_topic_lane(
    *,
    explicit_lane: str | None,
    open_questions: list[dict[str, Any]],
    max_priority: str | None,
    stale_active_claims: int,
    missing_active_roles: list[str],
    due_reason: str | None,
) -> str:
    if explicit_lane is not None:
        return explicit_lane
    if missing_active_roles:
        return "P1"
    if any(q.get("priority") == "H" for q in open_questions):
        return "P1"
    if due_reason in {"resolve_by_overdue", "resolve_by_due_4h"}:
        return "P2"
    if stale_active_claims:
        return "P2"
    if open_questions:
        return "P3"
    if max_priority == "H":
        return "P4"
    return "P5"


def _default_next_action(
    *,
    metadata_priority: dict[str, Any],
    open_questions: list[dict[str, Any]],
    missing_active_roles: list[str],
    stale_active_claims: int,
    due_reason: str | None,
) -> str:
    explicit = metadata_priority.get("next_action")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    if missing_active_roles:
        return "bind active owners for missing roles before waking workers"
    if open_questions:
        top = sorted(
            open_questions,
            key=lambda q: (
                -VALID_PRIORITY_ORDER.get(str(q.get("priority")), -1),
                str(q.get("ts") or ""),
                str(q.get("msg_id") or ""),
            ),
        )[0]
        return f"answer open {top['priority']} Q {top['msg_id']}"
    if stale_active_claims:
        return "reclaim or reap stale worker claims before spawning more work"
    if due_reason:
        return "advance gate before resolve_by deadline"
    return "monitor; close/archive if no remaining material work"


def list_open_debate_work(
    conn: sqlite3.Connection,
    *,
    states: list[str] | None = None,
    topics: list[str] | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Return active debate topics in deterministic CONDUCTOR priority order."""
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise DebateError(
            "invalid_limit: must be positive int",
            error_type="limit_out_of_range",
        )
    effective_limit = min(limit, MAX_SIGNAL_LIMIT)
    states = states or ["INIT", "ACTIVE"]
    for state in states:
        validate_state(state)
    topics = topics or []
    for topic_id in topics:
        validate_topic_id(topic_id)

    where = [f"d.state IN ({','.join('?' for _ in states)})"]
    params: list[Any] = list(states)
    if topics:
        where.append(f"d.topic_id IN ({','.join('?' for _ in topics)})")
        params.extend(topics)

    rows = conn.execute(
        "SELECT d.topic_id, d.title, d.state, d.created_at, d.created_by_role, "
        "d.resolve_by, d.archived_at, d.roles_json, d.metadata_json "
        "FROM debates d "
        f"WHERE {' AND '.join(where)}",
        params,
    ).fetchall()
    now_dt = datetime.now(timezone.utc)
    items: list[dict[str, Any]] = []

    for row in rows:
        debate = _row_to_debate_dict(row)
        topic_id = debate["topic_id"]
        metadata = (
            debate.get("metadata") if isinstance(debate.get("metadata"), dict) else {}
        )
        priority_metadata = _topic_priority_metadata(metadata)
        explicit_lane = _explicit_topic_priority_lane(metadata)
        messages = conn.execute(
            "SELECT msg_id, ts, priority, kind, role, reply_to "
            "FROM debate_messages WHERE topic_id = ? "
            "ORDER BY ts ASC, msg_id ASC",
            (topic_id,),
        ).fetchall()
        latest = dict(messages[-1]) if messages else None
        max_priority = None
        max_kind = None
        if messages:
            max_priority = max(
                (m["priority"] for m in messages),
                key=lambda p: VALID_PRIORITY_ORDER.get(p, -1),
            )
            max_kind = max(
                (m["kind"] for m in messages),
                key=lambda k: WORK_KIND_ORDER.get(k, -1),
            )
        open_questions = _open_blocking_questions(conn, topic_id)
        active_claim_rows = conn.execute(
            "SELECT worker_session_id, role, trigger_msg_id, claimed_at, heartbeat_at "
            "FROM debate_worker_claims "
            "WHERE topic_id = ? AND state = 'active'",
            (topic_id,),
        ).fetchall()
        active_claims = [dict(r) for r in active_claim_rows]
        stale_active_claims = 0
        for claim in active_claims:
            try:
                heartbeat = _parse_iso_utc_dt(claim["heartbeat_at"])
            except DebateError:
                continue
            if (now_dt - heartbeat).total_seconds() >= 900:
                stale_active_claims += 1

        declared_roles = [
            r.get("role")
            for r in debate["roles"]
            if isinstance(r, dict) and isinstance(r.get("role"), str)
        ]
        active_roles = {
            r["role"]
            for r in conn.execute(
                "SELECT role FROM debate_role_bindings "
                "WHERE topic_id = ? AND state = 'active'",
                (topic_id,),
            ).fetchall()
        }
        missing_active_roles = sorted(
            role for role in declared_roles if role not in active_roles
        )
        due_reason, due_score = _due_reason_and_score(debate.get("resolve_by"), now_dt)
        lane = _default_topic_lane(
            explicit_lane=explicit_lane,
            open_questions=open_questions,
            max_priority=max_priority,
            stale_active_claims=stale_active_claims,
            missing_active_roles=missing_active_roles,
            due_reason=due_reason,
        )

        reason_codes: list[str] = []
        if explicit_lane:
            reason_codes.append("explicit_conductor_priority")
        if due_reason:
            reason_codes.append(due_reason)
        if open_questions:
            reason_codes.append(f"open_questions_{len(open_questions)}")
        if any(q.get("priority") == "H" for q in open_questions):
            reason_codes.append("open_h_question")
        if stale_active_claims:
            reason_codes.append(f"stale_active_claims_{stale_active_claims}")
        if missing_active_roles:
            reason_codes.append("missing_active_role_binding")
        if not reason_codes:
            reason_codes.append("default_open_topic")

        priority_score = (
            TOPIC_PRIORITY_LANE_ORDER[lane] * 1_000_000
            + due_score
            + len(open_questions) * 5_000
            + sum(1 for q in open_questions if q.get("priority") == "H") * 10_000
            + stale_active_claims * 2_000
            + len(missing_active_roles) * 20_000
            + VALID_PRIORITY_ORDER.get(max_priority or "INFO", 0) * 1_000
            + WORK_KIND_ORDER.get(max_kind or "WATERMARK", 0) * 100
        )
        item = {
            "topic_id": topic_id,
            "title": debate["title"],
            "state": debate["state"],
            "lane": lane,
            "priority_score": priority_score,
            "reason_codes": reason_codes,
            "next_action": _default_next_action(
                metadata_priority=priority_metadata,
                open_questions=open_questions,
                missing_active_roles=missing_active_roles,
                stale_active_claims=stale_active_claims,
                due_reason=due_reason,
            ),
            "blocked_by": str(priority_metadata.get("blocked_by") or "").strip(),
            "resolve_by": debate.get("resolve_by"),
            "open_question_count": len(open_questions),
            "open_h_question_count": sum(
                1 for q in open_questions if q.get("priority") == "H"
            ),
            "active_claim_count": len(active_claims),
            "stale_active_claim_count": stale_active_claims,
            "missing_active_roles": missing_active_roles,
            "max_message_priority": max_priority,
            "max_work_kind": max_kind,
            "latest_message": latest,
        }
        items.append(item)

    items.sort(
        key=lambda item: (
            -int(item["priority_score"]),
            str(item.get("resolve_by") or "9999-12-31T23:59:59Z"),
            str(item["topic_id"]),
        )
    )
    return {
        "items": items[:effective_limit],
        "count": min(len(items), effective_limit),
        "total": len(items),
        "limit": effective_limit,
        "ordering": [
            "explicit conductor lane P0..P7",
            "resolve_by urgency",
            "open H/Q and active-claim/binding blockers",
            "message priority/kind",
            "topic_id tie-break",
        ],
    }


# ── Escalation + compaction ───────────────────────────────────────────


def escalate(
    conn: sqlite3.Connection,
    *,
    topic_id: str,
    role: str,
    reason: str,
    target_role: str = "HUMAN",
) -> dict[str, Any]:
    """Force-write an H-priority PING message tagged for target_role."""
    validate_topic_id(topic_id)
    validate_role(role)
    validate_role(target_role)
    if not isinstance(reason, str) or not reason.strip():
        raise DebateError("invalid_reason: must be non-empty string")
    body = f"[ESCALATE:{reason}] target={target_role}"
    msg = post_message(
        conn,
        topic_id=topic_id,
        role=role,
        priority="H",
        kind="PING",
        body=body,
    )
    return msg


def compact(
    conn: sqlite3.Connection,
    *,
    topic_id: str,
    role: str,
    body: str,
    since_ts: str | None = None,
    until_ts: str | None = None,
) -> dict[str, Any]:
    """Convenience wrapper writing a COMPACTION snapshot message."""
    if since_ts is not None:
        validate_iso_utc(since_ts)
    if until_ts is not None:
        validate_iso_utc(until_ts)
    header_parts = []
    if since_ts:
        header_parts.append(f"since={since_ts}")
    if until_ts:
        header_parts.append(f"until={until_ts}")
    header = " ".join(header_parts).strip()
    full_body = f"[COMPACTION {header}]\n\n{body}" if header else body
    return post_message(
        conn,
        topic_id=topic_id,
        role=role,
        priority="INFO",
        kind="COMPACTION",
        body=full_body,
    )


# ── v3.9.2: Inbox signaling DAO ────────────────────────────────────────
# Two-table model per CONDUCTOR canonical msg:b3a87f15 + msg:c5e91d24
# (with amendments msg:5e2d1c89 + msg:7831af04 + msg:a08c61b3 + msg:1d8e7c20
# + msg:e0f47b29 — last-amendment-wins for the error-contract layer).
# debate_message_recipients carries WHO is addressed (intent), normalized.
# debate_signal_state carries WHERE each per-session read cursor sits
# (compound (ts, msg_id), race-safe per turn-2 fix). Adapters poll via
# debate_signal_check at prompt time; the watcher daemon is deferred to
# v3.10.0 pending empirical proof of resource budget.


def _validate_recipient(
    recipient: str,
    topic_id: str,
    conn: sqlite3.Connection,
    debate: dict[str, Any] | None = None,
) -> None:
    """Normal recipients must be declared roles.

    Direct session_id recipients moved behind the explicit diagnostic path
    in v3.10 so stale runtime bindings cannot silently consume role work.

    Raises DebateError with a specific error_type taxonomy:
      - 'recipient_unknown_role' for role-shaped names that don't match
        the topic's declared roles
      - 'recipient_invalid_session_id' for ids that don't match
        SESSION_ID_RE

    Per v3.9.3 P1.2 (msg:76e96a96 + amendment 1A): the optional
    ``debate`` dict (as returned by ``get_debate(conn, topic_id)``)
    short-circuits the per-recipient ``SELECT roles_json`` round-trip
    and eliminates a TOCTOU between repeated lookups when validating
    many recipients in one call. When ``None``, falls back to fetch
    (legacy path; backward-compatible signature).

    Recipient strings are truncated to ``[:64]`` in error messages
    (msg:76e96a96 P3.7) to bound DoS / log-flood from caller-supplied
    arbitrary-length input.
    """
    safe_repr = repr(recipient[:64]) if isinstance(recipient, str) else repr(recipient)
    if not isinstance(recipient, str) or not recipient:
        raise DebateError(
            f"invalid_recipient: {safe_repr} must be a non-empty string",
            error_type="recipient_invalid_session_id",
        )
    if debate is not None:
        # Pass-through path: caller already loaded the debate row.
        roles_iter = debate.get("roles") or []
    else:
        debates_row = conn.execute(
            "SELECT roles_json FROM debates WHERE topic_id = ?", (topic_id,)
        ).fetchone()
        if debates_row is None:
            raise DebateError(
                f"unknown_topic: {topic_id}",
                error_type="topic_not_found",
            )
        roles_iter = json_loads(debates_row["roles_json"])
    declared_roles = {
        r["role"] for r in roles_iter if isinstance(r, dict) and "role" in r
    }
    if recipient in declared_roles:
        return
    if SESSION_ID_RE.fullmatch(recipient):
        raise DebateError(
            f"direct_session_recipient_requires_diagnostic: {safe_repr}",
            error_type="recipient_direct_session_requires_diagnostic",
        )
    # Classify: role-shaped (uppercase, no dash, no prefix) → unknown role;
    # otherwise → malformed session id. Both yield a clear caller-fixable
    # error_type so MCP wrappers don't lump validation under
    # 'internal_error'.
    looks_like_role = recipient.isupper() and "-" not in recipient
    if looks_like_role:
        raise DebateError(
            f"recipient {safe_repr} is not a declared role of topic "
            f"{topic_id} (declared: {sorted(declared_roles)})",
            error_type="recipient_unknown_role",
        )
    raise DebateError(
        f"recipient {safe_repr} is not a valid session_id "
        f"(approved prefixes: {APPROVED_RUNTIME_PREFIXES}; suffix must "
        f"match [a-zA-Z0-9_]{{4,64}})",
        error_type="recipient_invalid_session_id",
    )


def _validate_diagnostic_recipient(
    recipient: str,
    topic_id: str,
    conn: sqlite3.Connection,
    *,
    conductor_override_msg_id: str | None = None,
) -> None:
    safe_repr = repr(recipient[:64]) if isinstance(recipient, str) else repr(recipient)
    if not isinstance(recipient, str) or not SESSION_ID_RE.fullmatch(recipient):
        raise DebateError(
            f"diagnostic_recipient {safe_repr} must be a valid session_id",
            error_type="recipient_invalid_session_id",
        )
    binding = conn.execute(
        "SELECT 1 FROM debate_role_bindings "
        "WHERE topic_id = ? AND session_id = ? AND state = 'diagnostic' "
        "LIMIT 1",
        (topic_id, recipient),
    ).fetchone()
    if binding is not None:
        return
    if conductor_override_msg_id:
        _validate_conductor_override(
            conn, topic_id=topic_id, override_msg_id=conductor_override_msg_id
        )
        return
    raise DebateError(
        f"diagnostic_binding_required: {recipient}",
        error_type="recipient_diagnostic_binding_required",
    )


def _dedupe_preserve_order(items: list[str]) -> list[str]:
    """First-occurrence-wins dedupe. Per amendment msg:a08c61b3: caller
    intent is 'send to these recipients' — duplicates are typo, not
    malice. DAO silently dedupes before INSERT to avoid PK violations
    that would force a full transaction rollback."""
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _enqueue_delivery(
    conn: sqlite3.Connection,
    msg_id: str,
    recipient: str,
    enqueued_at: str,
    *,
    mode: str,
) -> None:
    """Idempotently persist recipient intent and its durable delivery row."""
    conn.execute(
        "INSERT OR IGNORE INTO debate_message_recipients "
        "(msg_id, recipient, recipient_mode) VALUES (?, ?, ?)",
        (msg_id, recipient, mode),
    )
    conn.execute(
        "INSERT OR IGNORE INTO debate_delivery_queue "
        "(msg_id, recipient, enqueued_at, completed_at) "
        "VALUES (?, ?, ?, NULL)",
        (msg_id, recipient, enqueued_at),
    )


def debate_post_with_recipients(
    conn: sqlite3.Connection,
    *,
    topic_id: str,
    role: str,
    priority: str,
    kind: str,
    body: str,
    addressed_to: list[str],
    diagnostic_to: list[str] | None = None,
    conductor_override_msg_id: str | None = None,
    reply_to: str | None = None,
    standing: bool | None = None,
    vehicle: str | None = None,
    protocol_version: str | None = None,
    body_mode: str | None = None,
    payload_json: Any = None,
    author_session_id: str | None = None,
) -> dict[str, Any]:
    """Atomic insert: debate_messages row + per-recipient
    debate_message_recipients rows.

    Per CONDUCTOR canonical msg:c5e91d24 with amendments msg:a08c61b3
    (dedupe + ARCHIVED terminal) and msg:1d8e7c20 / msg:e0f47b29
    (DebateError taxonomy).

    Validation order (fail fast, before any DB write):
      1. addressed_to non-empty (else 'recipient_empty')
      2. topic exists, lifecycle gate (ARCHIVED blocks ALL kinds incl.
         STATE; RESOLVED blocks all non-STATE)
      3. dedupe addressed_to preserving order
      4. validate each recipient via _validate_recipient

    Atomicity: BEGIN → post_message (which itself INSERTs into
    debate_messages and may UPDATE debates / debate_watermarks) →
    INSERT INTO debate_message_recipients per dedupd recipient → COMMIT.
    On any exception during the transaction, ROLLBACK leaves no row in
    debate_messages or debate_message_recipients.

    Returns: ``{msg_id, ts, recipient_count, topic_state}``.

    RACE-SAFETY CONTRACT (v3.9.3, msg:34adcb3e amendment 1A +
    msg:3d3442cb amendment 2B): this DAO requires the caller to provide
    a connection in BEGIN IMMEDIATE mode (or stronger). Use
    ``db_utils.get_conn_immediate()`` — the regular ``get_conn()``
    starts in DEFERRED mode and does NOT serialize the lifecycle/
    recipient-validation reads against concurrent writers, exposing a
    snapshot race during high-contention bursts. The MCP wrapper at
    ``intel_server.debate_post_with_recipients`` always uses the
    immediate-mode wrapper. Direct DAO callers bypassing it accept the
    race risk; the contract is wrapper-scoped, not DAO-enforced (no
    runtime check — SQLite does not expose the txn mode through the
    Python sqlite3 module).
    """
    if not isinstance(addressed_to, list):
        raise DebateError(
            "addressed_to must be a list",
            error_type="recipient_empty",
        )
    if diagnostic_to is None:
        diagnostic_to = []
    if not isinstance(diagnostic_to, list):
        raise DebateError(
            "diagnostic_to must be a list",
            error_type="recipient_invalid_session_id",
        )
    if not addressed_to and not diagnostic_to:
        raise DebateError(
            "addressed_to or diagnostic_to required and non-empty "
            "(broadcast not supported)",
            error_type="recipient_empty",
        )

    validate_topic_id(topic_id)
    debate = get_debate(conn, topic_id)
    if debate is None:
        raise DebateError(
            f"unknown_topic: {topic_id}",
            error_type="topic_not_found",
        )
    if debate["state"] == "ARCHIVED":
        raise DebateError(
            f"topic_archived_terminal: {topic_id} blocks all message "
            f"kinds including STATE",
            error_type="lifecycle_archived",
        )
    if debate["state"] == "RESOLVED" and kind != "STATE":
        raise DebateError(
            f"topic_resolved_blocks_non_STATE: {topic_id} accepts only "
            f"kind=STATE for ARCHIVED transition",
            error_type="lifecycle_resolved_non_state",
        )

    deduped = _dedupe_preserve_order(addressed_to)
    diagnostic_deduped = _dedupe_preserve_order(diagnostic_to)
    for recipient in deduped:
        # Pass the already-loaded debate dict to skip the per-recipient
        # SELECT roles_json round-trip and close the TOCTOU window
        # (v3.9.3 P1.2).
        _validate_recipient(recipient, topic_id, conn, debate=debate)
    for recipient in diagnostic_deduped:
        _validate_diagnostic_recipient(
            recipient,
            topic_id,
            conn,
            conductor_override_msg_id=conductor_override_msg_id,
        )

    # Atomicity is provided by the caller's context manager
    # (db_utils.get_conn() wraps every block in BEGIN/COMMIT). Issuing
    # an inner BEGIN here would nest transactions and SQLite would raise
    # "cannot start a transaction within a transaction" under real MCP
    # usage. Matches the existing post_message DAO contract: the DAO
    # never owns its own outer transaction.
    post_result = post_message(
        conn,
        topic_id=topic_id,
        role=role,
        priority=priority,
        kind=kind,
        body=body,
        reply_to=reply_to,
        standing=standing,
        vehicle=vehicle,
        protocol_version=protocol_version,
        body_mode=body_mode,
        payload_json=payload_json,
        author_session_id=author_session_id,
        recipients=[*deduped, *diagnostic_deduped],
    )
    msg_id = post_result["msg_id"]
    for recipient in deduped:
        _enqueue_delivery(conn, msg_id, recipient, post_result["ts"], mode="normal")
    for recipient in diagnostic_deduped:
        _enqueue_delivery(
            conn,
            msg_id,
            recipient,
            post_result["ts"],
            mode="diagnostic",
        )

    result = {
        "msg_id": msg_id,
        "ts": post_result["ts"],
        "recipient_count": len(deduped) + len(diagnostic_deduped),
        "diagnostic_recipient_count": len(diagnostic_deduped),
        "topic_state": post_result["topic_state"],
        # v3.12: effective vehicle (default 'analysis' when untagged). Carried
        # for observability only — the router reads the authoritative value
        # from the DB row, never from this response (which is unauthenticated
        # at the hook boundary).
        "vehicle": post_result["vehicle"],
        "schema_version": DEBATE_POST_RESPONSE_SCHEMA_VERSION,
    }
    for key in (
        "protocol_version",
        "round_no",
        "body_mode",
        "protocol_state",
        "worker_claim",
        "signal_cursor_reconciliation",
    ):
        if key in post_result:
            result[key] = post_result[key]
    return result


def _reconcile_active_signal_cursor_from_role_watermark(
    conn: sqlite3.Connection,
    *,
    topic_id: str,
    role: str,
    watermark_msg_id: str,
    watermark_ts: str,
    reconciled_at: str,
    expected_session_id: str | None = None,
) -> dict[str, str] | None:
    """Project a role-ledger acknowledgement onto its addressed subset.

    ``debate_watermarks`` covers the complete visible role ledger while
    ``debate_signal_state`` covers only messages addressed to one concrete
    session (or its role). Therefore the implication is intentionally one-way:
    acknowledging the full ledger through ``watermark`` also acknowledges the
    addressed subset through that point, but an inbox acknowledgement must
    never skip non-addressed ledger messages by moving the role watermark.

    Only the active primary binding is reconciled. Diagnostic sessions and
    derived ``-W<n>`` workers retain independent cursors. The compound cursor
    update is monotonic and selects the newest *visible addressed* message at
    or before the role watermark rather than copying an unaddressed target.
    """
    active = conn.execute(
        "SELECT session_id FROM debate_role_bindings "
        "WHERE topic_id=? AND role=? AND state='active' LIMIT 2",
        (topic_id, role),
    ).fetchall()
    if len(active) != 1:
        # Legacy topics without a binding registry — or a corrupt ambiguous
        # registry — must not guess which concrete session owns the role.
        return None
    session_id = str(active[0]["session_id"])
    if expected_session_id is not None and session_id != expected_session_id:
        return None
    if is_worker_session_id(session_id) or SESSION_ID_RE.fullmatch(session_id) is None:
        return None

    visibility_predicate, visibility_params = _protocol_v1_visibility_sql(
        alias="m", viewer_role=role, control_plane=False
    )
    candidate = conn.execute(
        "SELECT m.msg_id,m.ts FROM debate_messages m "
        "WHERE m.topic_id=? "
        "AND (m.ts < ? OR (m.ts = ? AND m.msg_id <= ?)) "
        "AND " + visibility_predicate + " AND EXISTS ("
        " SELECT 1 FROM debate_message_recipients r "
        " WHERE r.msg_id=m.msg_id AND r.recipient IN (?,?)"
        ") ORDER BY m.ts DESC,m.msg_id DESC LIMIT 1",
        (
            topic_id,
            watermark_ts,
            watermark_ts,
            watermark_msg_id,
            *visibility_params,
            role,
            session_id,
        ),
    ).fetchone()
    if candidate is None:
        return None

    current = conn.execute(
        "SELECT last_processed_msg_id,last_processed_ts "
        "FROM debate_signal_state "
        "WHERE session_id=? AND role=? AND topic_id=?",
        (session_id, role, topic_id),
    ).fetchone()
    proposed_cursor = (candidate["ts"], candidate["msg_id"])
    if current is not None and current["last_processed_ts"]:
        current_cursor = (
            current["last_processed_ts"],
            current["last_processed_msg_id"] or "",
        )
        if proposed_cursor <= current_cursor:
            return None

    conn.execute(
        "INSERT INTO debate_signal_state "
        "(session_id,role,topic_id,last_processed_msg_id,last_processed_ts,last_check_at) "
        "VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(session_id,role,topic_id) DO UPDATE SET "
        "last_processed_msg_id=excluded.last_processed_msg_id,"
        "last_processed_ts=excluded.last_processed_ts,"
        "last_check_at=excluded.last_check_at "
        "WHERE debate_signal_state.last_processed_ts IS NULL "
        "OR excluded.last_processed_ts > debate_signal_state.last_processed_ts "
        "OR (excluded.last_processed_ts = debate_signal_state.last_processed_ts "
        "AND excluded.last_processed_msg_id > "
        "COALESCE(debate_signal_state.last_processed_msg_id,''))",
        (
            session_id,
            role,
            topic_id,
            candidate["msg_id"],
            candidate["ts"],
            reconciled_at,
        ),
    )
    persisted = conn.execute(
        "SELECT last_processed_msg_id,last_processed_ts "
        "FROM debate_signal_state "
        "WHERE session_id=? AND role=? AND topic_id=?",
        (session_id, role, topic_id),
    ).fetchone()
    if (
        persisted is None
        or (
            persisted["last_processed_ts"],
            persisted["last_processed_msg_id"] or "",
        )
        != proposed_cursor
    ):
        # A concurrent/newer cursor won the monotonic upsert. That is already
        # the desired invariant, so there is no reconciliation event to report.
        return None
    return {
        "session_id": session_id,
        "last_processed_msg_id": candidate["msg_id"],
        "last_processed_ts": candidate["ts"],
        "source_watermark_msg_id": watermark_msg_id,
    }


def _validate_signal_caller(
    session_id: str, role: str, topic_id: str, conn: sqlite3.Connection
) -> dict[str, Any]:
    """Shared input validation for signal_check + signal_advance.

    Verifies session_id matches SESSION_ID_RE and role is declared in the
    topic's roles_json. Returns the debate row dict for downstream use.
    """
    validate_session_id(session_id)
    validate_role(role)
    validate_topic_id(topic_id)
    debate = get_debate(conn, topic_id)
    if debate is None:
        raise DebateError(
            f"unknown_topic: {topic_id}",
            error_type="topic_not_found",
        )
    if not role_in_debate(debate["roles"], role):
        raise DebateError(
            f"role {role!r} not declared in topic {topic_id}",
            error_type="recipient_unknown_role",
        )
    return debate


def _signal_recipients_for_binding(
    conn: sqlite3.Connection,
    *,
    topic_id: str,
    role: str,
    session_id: str,
    worker_claim: sqlite3.Row | None = None,
) -> list[str]:
    if is_worker_session_id(session_id):
        claim = worker_claim
        if claim is None:
            claim = _validate_worker_claim_for_signal(
                conn,
                topic_id=topic_id,
                role=role,
                worker_session_id=session_id,
            )
        if claim["state"] == "active":
            return [role, str(claim["parent_session_id"])]
        return []

    binding_count = _binding_count(conn, topic_id, role)
    if binding_count == 0:
        # Legacy topics without a binding registry keep v3.9.x behavior.
        return [role, session_id]
    binding = _binding_for_session(conn, topic_id, role, session_id)
    if binding is None:
        return []
    if binding["state"] == "active":
        return [role, session_id]
    if binding["state"] == "diagnostic":
        return [session_id]
    return []


def _validate_signal_limit(limit: int) -> int:
    """Strict limit validation per amendment msg:7831af04. Bools are
    rejected explicitly because ``isinstance(True, int) is True`` would
    otherwise allow them to silently coerce to 1/0."""
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise DebateError(
            f"limit must be int (not {type(limit).__name__})",
            error_type="limit_invalid_type",
        )
    if limit < 1:
        raise DebateError(
            f"limit must be >= 1 (got {limit})",
            error_type="limit_out_of_range",
        )
    if limit > MAX_SIGNAL_LIMIT:
        raise DebateError(
            f"limit {limit} exceeds MAX_SIGNAL_LIMIT {MAX_SIGNAL_LIMIT}",
            error_type="limit_out_of_range",
        )
    return limit


def _protocol_v1_enabled(conn: sqlite3.Connection, topic_id: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM debate_protocol_state WHERE topic_id=?", (topic_id,)
        ).fetchone()
        is not None
    )


def _claim_or_filter_nonstanding_decision(
    conn: sqlite3.Connection,
    *,
    msg: sqlite3.Row,
    role: str,
    session_id: str,
) -> bool:
    if msg["kind"] != "DECISION" or msg["standing"] != 0:
        return True
    msg_id = msg["msg_id"]
    ack = _terminal_reply_for_trigger(
        conn, topic_id=msg["topic_id"], role=role, trigger_msg_id=msg_id
    )
    if ack is not None:
        now = now_iso()
        conn.execute(
            "INSERT INTO debate_message_claims "
            "(msg_id, role, owner_session_id, state, claimed_at, heartbeat_at, "
            " completed_at, ack_msg_id) "
            "VALUES (?, ?, NULL, 'done', ?, ?, ?, ?) "
            "ON CONFLICT(msg_id, role) DO UPDATE SET "
            "state = 'done', heartbeat_at = excluded.heartbeat_at, "
            "completed_at = COALESCE(debate_message_claims.completed_at, excluded.completed_at), "
            "ack_msg_id = COALESCE(debate_message_claims.ack_msg_id, excluded.ack_msg_id)",
            (msg_id, role, now, now, now, ack["msg_id"]),
        )
        return False

    now = now_iso()
    claim = conn.execute(
        "SELECT * FROM debate_message_claims WHERE msg_id = ? AND role = ?",
        (msg_id, role),
    ).fetchone()
    if claim is None:
        conn.execute(
            "INSERT INTO debate_message_claims "
            "(msg_id, role, owner_session_id, state, claimed_at, heartbeat_at, "
            " completed_at, ack_msg_id) "
            "VALUES (?, ?, ?, 'active', ?, ?, NULL, NULL)",
            (msg_id, role, session_id, now, now),
        )
        return True
    if claim["state"] == "done":
        return False
    if claim["owner_session_id"] == session_id:
        conn.execute(
            "UPDATE debate_message_claims SET heartbeat_at = ? "
            "WHERE msg_id = ? AND role = ?",
            (now, msg_id, role),
        )
        return True
    return False


def debate_signal_check(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    role: str,
    topic_id: str,
    since_msg_id: str | None = None,
    since_ts: str | None = None,
    limit: int = DEFAULT_SIGNAL_LIMIT,
) -> dict[str, Any]:
    """Return pending messages addressed to (role OR session_id) past
    the caller's compound cursor.

    A derived worker on a debate/v1 topic is a one-trigger execution lane: its
    authoritative inbox contains only the trigger recorded in its active claim,
    independent of a newer parent cursor. Legacy workers retain the historical
    role-inbox behavior.

    Per CONDUCTOR canonical msg:c5e91d24 with amendments msg:7831af04
    (limit validation matrix), msg:c798c786 (EXISTS de-dupe so a single
    msg addressed to BOTH role and session_id counts once), and
    msg:e0f47b29 (DebateError taxonomy).

    Cursor precedence (matches read_messages from v3.9.0):
      1. since_msg_id explicit (pagination walk)
      2. since_ts explicit
      3. debate_signal_state row for (session_id, role, topic_id), after
         one-way reconciliation from a newer role watermark for the active
         primary binding
      4. start of topic (no filter)

    Pagination contract: fetch limit+1 rows; ``truncated=True`` when
    more remain; ``next_cursor`` carries (ts, msg_id) for the next page.
    Returns ``max_priority`` so adapters can short-circuit on H without
    enumerating every row.

    NOTE on session_id ownership (v3.9.3): ``session_id`` is caller-
    asserted at the DAO layer; no cryptographic binding between the
    live process and the asserted id. MCP-layer enforcement deferred
    to v3.9.4. ``since_ts`` (when supplied without ``since_msg_id``) is
    strictly-exclusive — messages with ``ts == since_ts`` are NOT
    re-emitted (msg:946bcff6 amendment 3 fix). Use ``since_msg_id``
    for resume-where-cursor-left-off semantics that include intra-ts
    ordering.
    """
    effective_limit = _validate_signal_limit(limit)
    debate = _validate_signal_caller(session_id, role, topic_id, conn)
    visibility_predicate, visibility_params = _protocol_v1_visibility_sql(
        alias="m", viewer_role=role, control_plane=False
    )
    worker_claim: sqlite3.Row | None = None
    if is_worker_session_id(session_id):
        worker_claim = _validate_worker_claim_for_signal(
            conn,
            topic_id=topic_id,
            role=role,
            worker_session_id=session_id,
        )
    protocol_enabled = _protocol_v1_enabled(conn, topic_id)
    scoped_trigger_msg_id = (
        str(worker_claim["trigger_msg_id"])
        if worker_claim is not None and protocol_enabled
        else None
    )
    cursor_reconciliation: dict[str, str] | None = None
    if since_msg_id is None and since_ts is None and worker_claim is None:
        role_watermark = get_watermark(conn, topic_id, role)
        if (
            role_watermark is not None
            and role_watermark.get("last_processed_msg_id")
            and role_watermark.get("last_processed_ts")
        ):
            cursor_reconciliation = _reconcile_active_signal_cursor_from_role_watermark(
                conn,
                topic_id=topic_id,
                role=role,
                watermark_msg_id=role_watermark["last_processed_msg_id"],
                watermark_ts=role_watermark["last_processed_ts"],
                reconciled_at=now_iso(),
                expected_session_id=session_id,
            )

    cursor_ts: str | None = None
    cursor_msg_id: str = ""
    cursor_from_state = False

    if since_msg_id is not None:
        validate_msg_id(since_msg_id)
        ref = conn.execute(
            "SELECT m.ts, m.msg_id FROM debate_messages m "
            "WHERE m.msg_id = ? AND m.topic_id = ? AND " + visibility_predicate,
            (since_msg_id, topic_id, *visibility_params),
        ).fetchone()
        if ref is None:
            raise DebateError(
                f"unknown_since_msg_id: {since_msg_id} not in topic {topic_id}",
                error_type="watermark_msg_id_unknown",
            )
        cursor_ts = ref["ts"]
        cursor_msg_id = ref["msg_id"]
    elif since_ts is not None:
        validate_iso_utc(since_ts)
        cursor_ts = since_ts
        # v3.9.3 since_ts strict-exclusive (msg:946bcff6 amendment 3):
        # see read_messages note above. Same semantics here so
        # signal_check's contract matches read_messages's.
        cursor_msg_id = None
    else:
        state_row = conn.execute(
            "SELECT last_processed_msg_id, last_processed_ts "
            "FROM debate_signal_state "
            "WHERE session_id = ? AND role = ? AND topic_id = ?",
            (session_id, role, topic_id),
        ).fetchone()
        if state_row and state_row["last_processed_ts"]:
            cursor_ts = state_row["last_processed_ts"]
            cursor_msg_id = state_row["last_processed_msg_id"] or ""
            cursor_from_state = True
        elif (
            worker_claim is not None
            and scoped_trigger_msg_id is None
            and worker_claim["parent_cursor_ts"]
        ):
            cursor_ts = worker_claim["parent_cursor_ts"]
            cursor_msg_id = worker_claim["parent_cursor_msg_id"] or ""
            cursor_from_state = True

    signal_recipients = _signal_recipients_for_binding(
        conn,
        topic_id=topic_id,
        role=role,
        session_id=session_id,
        worker_claim=worker_claim,
    )
    if not signal_recipients:
        return {
            "pending": [],
            "count": 0,
            "truncated": False,
            "next_cursor": None,
            "max_priority": None,
            "topic_state": debate["state"],
            "limit": effective_limit,
        }

    where = ["m.topic_id = ?", visibility_predicate]
    params: list[Any] = [topic_id]
    params.extend(visibility_params)
    recipient_placeholders = ",".join("?" for _ in signal_recipients)
    where.append(
        "EXISTS (SELECT 1 FROM debate_message_recipients r "
        f"WHERE r.msg_id = m.msg_id AND r.recipient IN ({recipient_placeholders}))"
    )
    params.extend(signal_recipients)
    if scoped_trigger_msg_id is not None:
        where.append("m.msg_id = ?")
        params.append(scoped_trigger_msg_id)
    where.append(
        "NOT (m.kind = 'DECISION' AND m.standing = 0 AND EXISTS ("
        " SELECT 1 FROM debate_message_claims c "
        " WHERE c.msg_id = m.msg_id AND c.role = ? "
        " AND (c.state = 'done' OR (c.state = 'active' "
        " AND COALESCE(c.owner_session_id, '') <> ?))"
        "))"
    )
    params.extend([role, session_id])
    if cursor_ts is not None:
        # Dual-branch cursor (v3.9.3 msg:946bcff6 amendment 3,
        # signal_check m.-aliased form). Same strict-exclusive
        # semantics as read_messages above; the ``m.`` prefix matches
        # the existing aliased FROM clause for this query.
        if cursor_msg_id is None:
            where.append("m.ts > ?")
            params.extend([cursor_ts])
        else:
            cursor_clause = "(m.ts > ? OR (m.ts = ? AND m.msg_id > ?))"
            if cursor_from_state:
                where.append(
                    f"({cursor_clause} "
                    "OR m.standing = 1 "
                    "OR (m.standing IS NULL AND "
                    f"m.kind IN ({','.join('?' for _ in STANDING_SIGNAL_KINDS)})) "
                    "OR (m.kind = 'DECISION' AND m.standing = 0))"
                )
                params.extend(
                    [
                        cursor_ts,
                        cursor_ts,
                        cursor_msg_id,
                        *STANDING_SIGNAL_KINDS,
                    ]
                )
            else:
                where.append(cursor_clause)
                params.extend([cursor_ts, cursor_ts, cursor_msg_id])

    fetch_limit = effective_limit + 1  # +1 to detect truncation
    rows = conn.execute(
        "SELECT m.msg_id, m.topic_id, m.role, m.ts, m.priority, m.kind, "
        "m.reply_to, m.standing, m.body, m.protocol_version, m.round_no, "
        "m.body_mode, m.payload_json, m.created_at "
        "FROM debate_messages m "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY m.ts ASC, m.msg_id ASC LIMIT ?",
        [*params, fetch_limit],
    ).fetchall()

    truncated = len(rows) > effective_limit
    if truncated:
        rows = rows[:effective_limit]
    pending = []
    for row in rows:
        if not _claim_or_filter_nonstanding_decision(
            conn, msg=row, role=role, session_id=session_id
        ):
            continue
        item = dict(row)
        if item.get("protocol_version") is None:
            for key in ("protocol_version", "round_no", "body_mode", "payload_json"):
                item.pop(key, None)
        pending.append(item)

    next_cursor: dict[str, str] | None = None
    if truncated and pending:
        last = pending[-1]
        next_cursor = {"ts": last["ts"], "msg_id": last["msg_id"]}

    max_priority: str | None = None
    if pending:
        max_priority = max(
            (m["priority"] for m in pending),
            key=lambda p: VALID_PRIORITY_ORDER.get(p, -1),
        )

    if protocol_enabled:
        check_at = now_iso()
        conn.execute(
            "INSERT INTO debate_signal_state "
            "(session_id,role,topic_id,last_processed_msg_id,last_processed_ts,last_check_at) "
            "VALUES (?,?,?,NULL,NULL,?) "
            "ON CONFLICT(session_id,role,topic_id) DO UPDATE SET "
            "last_check_at=excluded.last_check_at",
            (session_id, role, topic_id, check_at),
        )
        if pending:
            delivered = max(pending, key=lambda item: (item["ts"], item["msg_id"]))
            conn.execute(
                "INSERT INTO debate_signal_deliveries "
                "(session_id,role,topic_id,delivered_up_to_msg_id,"
                "delivered_up_to_ts,delivered_at) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(session_id,role,topic_id) DO UPDATE SET "
                "delivered_up_to_msg_id=excluded.delivered_up_to_msg_id,"
                "delivered_up_to_ts=excluded.delivered_up_to_ts,"
                "delivered_at=excluded.delivered_at "
                "WHERE (excluded.delivered_up_to_ts,excluded.delivered_up_to_msg_id) >= "
                "(debate_signal_deliveries.delivered_up_to_ts,"
                " debate_signal_deliveries.delivered_up_to_msg_id)",
                (
                    session_id,
                    role,
                    topic_id,
                    delivered["msg_id"],
                    delivered["ts"],
                    check_at,
                ),
            )

    result = {
        "pending": pending,
        "count": len(pending),
        "truncated": truncated,
        "next_cursor": next_cursor,
        "max_priority": max_priority,
        "topic_state": debate["state"],
        "limit": effective_limit,
    }
    if cursor_reconciliation is not None:
        result["cursor_reconciled_from_watermark"] = cursor_reconciliation[
            "last_processed_msg_id"
        ]
    return result


def debate_signal_advance(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    role: str,
    topic_id: str,
    last_processed_msg_id: str,
) -> dict[str, Any]:
    """Advance the (session_id, role, topic_id) compound cursor.

    Per CONDUCTOR canonical msg:c5e91d24 with critical amendment 3a
    (msg:5e2d1c89 turn-12 fix): the target msg_id MUST be addressed to
    this caller (role OR session_id). Otherwise a buggy adapter could
    advance its cursor past unprocessed addressed work, making
    high-priority pending messages permanently invisible.

    Atomic upsert: derives ts from the message row (DAO authority over
    timestamp; body cannot tamper) and writes BOTH last_processed_msg_id
    and last_processed_ts so the compound (ts, msg_id) cursor never
    falls back to ts-only on a subsequent read. last_check_at also
    updated to the advance time.

    RACE-SAFETY CONTRACT (v3.9.3, msg:34adcb3e amendment 1A +
    msg:3d3442cb amendment 2B): this DAO requires the caller to provide
    a connection in BEGIN IMMEDIATE mode (or stronger). The cursor-
    monotonicity check (current vs proposed compound (ts, msg_id))
    reads ``debate_signal_state`` BEFORE the upsert; under regular
    DEFERRED ``get_conn()`` that read can race with another writer's
    upsert and let an older proposed cursor pass the guard. Use
    ``db_utils.get_conn_immediate()``. The MCP wrapper at
    ``intel_server.debate_signal_advance`` always uses immediate mode.
    Direct DAO callers bypassing it accept the race risk; contract is
    wrapper-scoped, not DAO-enforced.

    NOTE on session_id ownership: ``session_id`` is caller-asserted at
    the DAO layer; there is no cryptographic binding between the live
    process and the asserted session_id. MCP-layer enforcement (e.g.
    requiring a per-session capability token) is deferred to v3.9.4.
    """
    _validate_signal_caller(session_id, role, topic_id, conn)
    validate_msg_id(last_processed_msg_id)
    worker_claim: sqlite3.Row | None = None
    if is_worker_session_id(session_id):
        worker_claim = _validate_worker_claim_for_signal(
            conn,
            topic_id=topic_id,
            role=role,
            worker_session_id=session_id,
        )
    protocol_enabled = _protocol_v1_enabled(conn, topic_id)
    if (
        worker_claim is not None
        and protocol_enabled
        and last_processed_msg_id != worker_claim["trigger_msg_id"]
    ):
        raise DebateError(
            "worker_trigger_scope: debate/v1 worker may advance only its "
            f"claimed trigger {worker_claim['trigger_msg_id']}",
            error_type="worker_trigger_scope",
            details={"trigger_msg_id": worker_claim["trigger_msg_id"]},
        )

    visibility_predicate, visibility_params = _protocol_v1_visibility_sql(
        alias="m", viewer_role=role, control_plane=False
    )
    ref = conn.execute(
        "SELECT m.msg_id, m.ts FROM debate_messages m "
        "WHERE m.msg_id = ? AND m.topic_id = ? AND " + visibility_predicate,
        (last_processed_msg_id, topic_id, *visibility_params),
    ).fetchone()
    if ref is None:
        raise DebateError(
            f"unknown_msg_id_for_advance: {last_processed_msg_id} not in "
            f"topic {topic_id}",
            error_type="watermark_msg_id_unknown",
        )

    signal_recipients = _signal_recipients_for_binding(
        conn,
        topic_id=topic_id,
        role=role,
        session_id=session_id,
        worker_claim=worker_claim,
    )
    if signal_recipients:
        recipient_placeholders = ",".join("?" for _ in signal_recipients)
        addressed = conn.execute(
            "SELECT 1 FROM debate_message_recipients "
            f"WHERE msg_id = ? AND recipient IN ({recipient_placeholders}) "
            "LIMIT 1",
            (last_processed_msg_id, *signal_recipients),
        ).fetchone()
    else:
        addressed = None
    if addressed is None:
        raise DebateError(
            f"watermark_advance_unaddressed: msg_id "
            f"{last_processed_msg_id} is not addressed to role={role!r} "
            f"or session_id={session_id!r}; advancing past it would "
            f"hide unprocessed addressed work",
            error_type="watermark_advance_unaddressed",
        )

    if protocol_enabled:
        delivery = conn.execute(
            "SELECT delivered_up_to_msg_id,delivered_up_to_ts "
            "FROM debate_signal_deliveries "
            "WHERE session_id=? AND role=? AND topic_id=?",
            (session_id, role, topic_id),
        ).fetchone()
        if delivery is None or (ref["ts"], ref["msg_id"]) > (
            delivery["delivered_up_to_ts"],
            delivery["delivered_up_to_msg_id"],
        ):
            raise DebateError(
                f"signal_advance_not_delivered: {last_processed_msg_id}",
                error_type="signal_advance_not_delivered",
                details={"last_processed_msg_id": last_processed_msg_id},
            )

    # Monotonic compound (ts, msg_id) guard per ADVOCATE turn-18
    # msg:ca22ee19. Plain ON CONFLICT DO UPDATE would let two threads
    # racing different msg_ids overwrite a newer cursor with an older
    # one. Reject regressions; equal cursor is idempotent (no-op
    # rewrite is safe).
    current = conn.execute(
        "SELECT last_processed_msg_id, last_processed_ts "
        "FROM debate_signal_state "
        "WHERE session_id = ? AND role = ? AND topic_id = ?",
        (session_id, role, topic_id),
    ).fetchone()
    if current and current["last_processed_ts"]:
        cur_ts = current["last_processed_ts"]
        cur_msg_id = current["last_processed_msg_id"] or ""
        proposed = (ref["ts"], ref["msg_id"])
        existing = (cur_ts, cur_msg_id)
        if proposed < existing:
            raise DebateError(
                f"watermark_regression: proposed cursor "
                f"({ref['ts']}, {ref['msg_id']}) is older than "
                f"existing ({cur_ts}, {cur_msg_id}); advancing "
                f"backwards would re-deliver already-processed work",
                error_type="watermark_regression",
            )

    now = now_iso()
    conn.execute(
        "INSERT INTO debate_signal_state "
        "(session_id, role, topic_id, last_processed_msg_id, "
        " last_processed_ts, last_check_at) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(session_id, role, topic_id) DO UPDATE SET "
        "last_processed_msg_id = excluded.last_processed_msg_id, "
        "last_processed_ts = excluded.last_processed_ts, "
        "last_check_at = excluded.last_check_at",
        (session_id, role, topic_id, ref["msg_id"], ref["ts"], now),
    )
    completed_worker_claim = _complete_worker_claim_if_terminal(
        conn,
        topic_id=topic_id,
        role=role,
        worker_session_id=session_id,
        now=now,
        claim=worker_claim,
    )

    out = {
        "session_id": session_id,
        "role": role,
        "topic_id": topic_id,
        "last_processed_msg_id": ref["msg_id"],
        "last_processed_ts": ref["ts"],
        "last_check_at": now,
    }
    if completed_worker_claim is not None:
        out["worker_claim"] = completed_worker_claim
    return out


# ── v3.10: dry-run wake target resolution/audit ────────────────────────


def _insert_wake_log(
    conn: sqlite3.Connection,
    *,
    trigger_msg_id: str,
    topic_id: str,
    recipient: str,
    action: str,
    result: str,
    target_role: str | None = None,
    target_session_id: str | None = None,
    target_runtime: str | None = None,
    binding_generation: int | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    # Message-level terminal routing decisions do not have a target session,
    # so the historical partial unique index could not dedupe them.  A pump
    # rescan consequently wrote tens of thousands of identical refusal rows.
    # Return the first durable receipt instead of growing an audit hot-loop.
    if target_session_id is None and result in _SINGLETON_MESSAGE_WAKE_RESULTS:
        existing = conn.execute(
            "SELECT * FROM debate_wake_log "
            "WHERE trigger_msg_id = ? AND topic_id = ? AND recipient = ? "
            "AND action = ? AND result = ? AND target_session_id IS NULL "
            "ORDER BY created_at ASC, wake_id ASC LIMIT 1",
            (trigger_msg_id, topic_id, recipient, action, result),
        ).fetchone()
        if existing is not None:
            out = dict(existing)
            out["details"] = json_loads(out.pop("details_json") or "{}")
            out["duplicate"] = True
            return out
    wake_id = new_msg_id()
    while conn.execute(
        "SELECT 1 FROM debate_wake_log WHERE wake_id = ? LIMIT 1",
        (wake_id,),
    ).fetchone():
        wake_id = new_msg_id()
    now = now_iso()
    row = {
        "wake_id": wake_id,
        "trigger_msg_id": trigger_msg_id,
        "topic_id": topic_id,
        "recipient": recipient,
        "target_role": target_role,
        "target_session_id": target_session_id,
        "target_runtime": target_runtime,
        "binding_generation": binding_generation,
        "action": action,
        "result": result,
        "schema_version": DEBATE_WAKE_SCHEMA_VERSION,
        "details": details or {},
        "created_at": now,
        "duplicate": False,
    }
    conn.execute(
        "INSERT INTO debate_wake_log "
        "(wake_id, trigger_msg_id, topic_id, recipient, target_role, "
        " target_session_id, target_runtime, binding_generation, action, "
        " result, schema_version, details_json, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            wake_id,
            trigger_msg_id,
            topic_id,
            recipient,
            target_role,
            target_session_id,
            target_runtime,
            binding_generation,
            action,
            result,
            DEBATE_WAKE_SCHEMA_VERSION,
            json_dumps(details or {}),
            now,
        ),
    )
    return row


def _reset_wake_log_for_redispatch(
    conn: sqlite3.Connection,
    *,
    trigger_msg_id: str,
    target_session_id: str,
    action: str,
    result: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Reset an existing wake row so a dead worker's trigger can re-dispatch.

    The (trigger, session, action) UNIQUE index forbids a second row, so a
    re-dispatch must UPDATE the stale 'dispatched' row in place (fresh result
    + created_at) rather than INSERT (advocate BLOCK #1 — the INSERT path
    crashed on UNIQUE). Returns the updated row, or None if none existed."""
    existing = conn.execute(
        "SELECT wake_id FROM debate_wake_log "
        "WHERE trigger_msg_id = ? AND target_session_id = ? AND action = ? "
        "ORDER BY created_at DESC LIMIT 1",
        (trigger_msg_id, target_session_id, action),
    ).fetchone()
    if existing is None:
        return None
    now = now_iso()
    conn.execute(
        "UPDATE debate_wake_log SET result = ?, created_at = ?, details_json = ? "
        "WHERE wake_id = ?",
        (result, now, json_dumps(details or {"redispatch": True}), existing["wake_id"]),
    )
    return {"wake_id": existing["wake_id"], "result": result, "created_at": now}


def _latest_wake_result(
    conn: sqlite3.Connection,
    *,
    trigger_msg_id: str,
    target_session_id: str,
    action: str,
) -> str | None:
    row = conn.execute(
        "SELECT result FROM debate_wake_log "
        "WHERE trigger_msg_id = ? AND target_session_id = ? "
        "AND action = ? ORDER BY created_at DESC LIMIT 1",
        (trigger_msg_id, target_session_id, action),
    ).fetchone()
    return str(row["result"]) if row is not None else None


def prepare_wake_dry_run(
    conn: sqlite3.Connection,
    *,
    tool_response: dict[str, Any],
    action: str = "dry_run_wake",
    expected_schema_version: str = DEBATE_POST_RESPONSE_SCHEMA_VERSION,
) -> dict[str, Any]:
    """Resolve wake targets and write audit rows without waking anything.

    This is deliberately signal-only. It never writes debate_messages and
    fails closed on response schema drift.
    """
    if not isinstance(tool_response, dict):
        raise DebateError(
            "tool_response must be a JSON object",
            error_type="wake_tool_response_invalid",
        )
    msg_id = str(tool_response.get("msg_id") or "")
    response_schema = str(tool_response.get("schema_version") or "")
    if response_schema != expected_schema_version:
        log = _insert_wake_log(
            conn,
            trigger_msg_id=msg_id,
            topic_id=str(tool_response.get("topic_id") or ""),
            recipient="",
            action=action,
            result="schema_mismatch",
            details={
                "expected_schema_version": expected_schema_version,
                "actual_schema_version": response_schema,
            },
        )
        return {"targets": [], "logs": [log], "suppressed": 0}

    validate_msg_id(msg_id)
    msg = conn.execute(
        "SELECT topic_id, vehicle FROM debate_messages WHERE msg_id = ?",
        (msg_id,),
    ).fetchone()
    if msg is None:
        log = _insert_wake_log(
            conn,
            trigger_msg_id=msg_id,
            topic_id=str(tool_response.get("topic_id") or ""),
            recipient="",
            action=action,
            result="unknown_trigger_msg_id",
        )
        return {"targets": [], "logs": [log], "suppressed": 0}

    topic_id = msg["topic_id"]
    try:
        blind_waiting = conn.execute(
            "SELECT 1 FROM debate_blind_commits WHERE msg_id=? AND released_at IS NULL",
            (msg_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        blind_waiting = None  # mixed-version upgrade window
    if blind_waiting is not None:
        log = _insert_wake_log(
            conn,
            trigger_msg_id=msg_id,
            topic_id=topic_id,
            recipient="",
            action=action,
            result="blind_commit_waiting",
            details={"protocol_version": DEBATE_PROTOCOL_V1},
        )
        return {"targets": [], "logs": [log], "suppressed": 0}
    # ── FAIL-CLOSED VEHICLE ROUTER (v3.12, solution #5) — resolution seam ──
    # Refuse to resolve any wake targets for an implementation-tagged trigger.
    # This is the signal-only counterpart to the hard guard in
    # claim_worker_session: it fails closed *before* dispatch, emits a typed
    # audit row (debate_wake_log.result is unconstrained TEXT), and returns
    # zero targets so neither the dry-run path nor the real-spawn hook can
    # proceed to allocate a no-edit worker. The authoritative vehicle is read
    # from the DB row here, never from the (unauthenticated) tool_response.
    #
    # CONDUCTOR-APPROVED IMPL-VEHICLE SEAM (#3): a future impl vehicle plugs
    # in here by branching this refusal into an impl-target resolution path
    # (return impl-worker targets instead of []). Until then implementation
    # work is conductor-handled out-of-band via Agent sub-agents.
    #
    # v3.13 IMPL-NOTIFY branch (2026-07-12 operator "дебатът е счупен" fix):
    # the refusal used to swallow the wake signal ENTIRELY, so addressed
    # standing impl-vehicle sessions were never signalled and the operator
    # had to nudge every hand-off manually (140/200 recent wake attempts
    # died here).  The worker guard stays intact — `targets` remains [] so
    # no dispatch path can allocate a no-edit wake worker — but the
    # addressed ACTIVE bindings are now resolved as NOTIFY-ONLY targets
    # under the separate `notify_targets` key (desktop signal, no spawn).
    trigger_vehicle = normalize_vehicle(msg["vehicle"])
    if trigger_vehicle not in WAKE_WORKER_VEHICLES:
        log = _insert_wake_log(
            conn,
            trigger_msg_id=msg_id,
            topic_id=topic_id,
            recipient="",
            action=action,
            result="implementation_requires_impl_vehicle",
            details={"vehicle": trigger_vehicle},
        )
        notify_targets: list[dict[str, Any]] = []
        impl_recipients = conn.execute(
            "SELECT recipient, recipient_mode FROM debate_message_recipients "
            "WHERE msg_id = ? ORDER BY recipient",
            (msg_id,),
        ).fetchall()
        for rec in impl_recipients:
            if rec["recipient_mode"] != "normal":
                continue
            recipient = rec["recipient"]
            if SESSION_ID_RE.fullmatch(recipient):
                # KNOWN LIMITATION (ADVOCATE 3fd85c9584e3 minor 1): a trigger
                # addressed ONLY to session-ids resolves zero notify targets
                # and skips silently — notify needs a binding lookup by
                # session, not by role. Revive if impl hand-offs ever address
                # sessions directly.
                continue
            binding = _active_binding(conn, topic_id, recipient)
            if binding is None:
                continue
            # Dedupe across the hook fast path and the pump rescans: one
            # notification per (message, session, action).
            latest_result = _latest_wake_result(
                conn,
                trigger_msg_id=msg_id,
                target_session_id=binding["session_id"],
                action=action,
            )
            if latest_result == "impl_notified":
                continue
            notify_targets.append(
                {
                    "recipient": recipient,
                    "target_role": binding["role"],
                    "target_session_id": binding["session_id"],
                    "target_runtime": binding["runtime"],
                    "result": "impl_notify_only",
                }
            )
        return {
            "targets": [],
            "logs": [log],
            "suppressed": 0,
            "notify_targets": notify_targets,
        }
    recipients = conn.execute(
        "SELECT recipient, recipient_mode FROM debate_message_recipients "
        "WHERE msg_id = ? ORDER BY recipient",
        (msg_id,),
    ).fetchall()
    targets: list[dict[str, Any]] = []
    logs: list[dict[str, Any]] = []
    suppressed = 0

    for rec in recipients:
        recipient = rec["recipient"]
        mode = rec["recipient_mode"]
        binding: sqlite3.Row | None = None
        result = "dry_run"
        if mode == "normal":
            if SESSION_ID_RE.fullmatch(recipient):
                result = "direct_session_not_diagnostic"
            else:
                binding = _active_binding(conn, topic_id, recipient)
                if binding is None:
                    result = "no_active_binding"
        else:
            binding = conn.execute(
                "SELECT * FROM debate_role_bindings "
                "WHERE topic_id = ? AND session_id = ? "
                "AND state = 'diagnostic' "
                "ORDER BY generation DESC LIMIT 1",
                (topic_id, recipient),
            ).fetchone()
            if binding is None:
                result = "no_diagnostic_binding"

        if binding is not None:
            latest_result = _latest_wake_result(
                conn,
                trigger_msg_id=msg_id,
                target_session_id=binding["session_id"],
                action=action,
            )
            # A 'dispatched' wake row suppresses re-dispatch ONLY while the
            # worker is genuinely covering the trigger — i.e. its claim is
            # active (in-flight) or completed, or a terminal reply exists.
            # If the claim was retired (dead worker) and no terminal reply
            # landed, the stale 'dispatched' row must NOT suppress: the work
            # is unfinished and must be re-dispatched (advocate BLOCK #1, the
            # at-least-once defect). 'notified'/'terminal_no_action' remain
            # terminal.
            suppress = False
            if latest_result and (
                action == "dry_run_wake"
                or latest_result in {"notified", "terminal_no_action"}
            ):
                suppress = True
            elif latest_result == "dispatched":
                suppress = _dispatch_still_covers_trigger(
                    conn,
                    topic_id=topic_id,
                    role=binding["role"],
                    trigger_msg_id=msg_id,
                )
            if suppress:
                suppressed += 1
                targets.append(
                    {
                        "recipient": recipient,
                        "target_role": binding["role"],
                        "target_session_id": binding["session_id"],
                        "target_runtime": binding["runtime"],
                        "result": "suppressed",
                    }
                )
                continue
            if latest_result == "dry_run":
                targets.append(
                    {
                        "recipient": recipient,
                        "target_role": binding["role"],
                        "target_session_id": binding["session_id"],
                        "target_runtime": binding["runtime"],
                        "result": result,
                    }
                )
                continue
            if latest_result is not None:
                # A prior wake row exists for this (session, action) — a dead
                # worker we chose NOT to suppress. The UNIQUE index forbids a
                # second row, so reset the existing one in place instead of
                # INSERTing (advocate BLOCK #1: INSERT crashed here).
                log = _reset_wake_log_for_redispatch(
                    conn,
                    trigger_msg_id=msg_id,
                    target_session_id=binding["session_id"],
                    action=action,
                    result=result,
                    details={"recipient_mode": mode, "redispatch": True},
                )
            else:
                log = _insert_wake_log(
                    conn,
                    trigger_msg_id=msg_id,
                    topic_id=topic_id,
                    recipient=recipient,
                    action=action,
                    result=result,
                    target_role=binding["role"],
                    target_session_id=binding["session_id"],
                    target_runtime=binding["runtime"],
                    binding_generation=binding["generation"],
                    details={"recipient_mode": mode},
                )
            if log is not None:
                logs.append(log)
            targets.append(
                {
                    "recipient": recipient,
                    "target_role": binding["role"],
                    "target_session_id": binding["session_id"],
                    "target_runtime": binding["runtime"],
                    "result": result,
                }
            )
            continue

        logs.append(
            _insert_wake_log(
                conn,
                trigger_msg_id=msg_id,
                topic_id=topic_id,
                recipient=recipient,
                action=action,
                result=result,
                details={"recipient_mode": mode},
            )
        )

    return {"targets": targets, "logs": logs, "suppressed": suppressed}
