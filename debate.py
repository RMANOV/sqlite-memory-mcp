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
import re
import secrets
import sqlite3
from typing import Any

from db_utils import json_dumps, json_loads, now_iso


# ── Enums + regex validators ──────────────────────────────────────────


TOPIC_RE = re.compile(r"^[A-Z][A-Z0-9_]+$")
ROLE_RE = re.compile(r"^[A-Z][A-Z0-9_]+$")
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
_STATE_BODY_RE = re.compile(
    r"^(INIT|ACTIVE|RESOLVED|ARCHIVED)(\s+\[reason: .+\])?$"
)


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
)
STANDING_SIGNAL_KINDS = ("DECISION", "STATE")
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
        self, message: str, *, error_type: str = "debate_validation"
    ) -> None:
        super().__init__(message)
        self.error_type = error_type


def validate_topic_id(topic_id: str) -> None:
    if not isinstance(topic_id, str) or not TOPIC_RE.fullmatch(topic_id):
        raise DebateError(
            f"invalid_topic_id: {topic_id!r} must match {TOPIC_RE.pattern}"
        )


def validate_role(role: str) -> None:
    if not isinstance(role, str) or not ROLE_RE.fullmatch(role):
        raise DebateError(
            f"invalid_role: {role!r} must match {ROLE_RE.pattern}"
        )


def validate_msg_id(msg_id: str) -> None:
    if not isinstance(msg_id, str) or not MSG_ID_RE.fullmatch(msg_id):
        raise DebateError(
            f"invalid_msg_id: {msg_id!r} must match {MSG_ID_RE.pattern}"
        )


def validate_iso_utc(ts: str) -> None:
    if not isinstance(ts, str) or not ISO_UTC_RE.fullmatch(ts):
        raise DebateError(
            f"invalid_iso_utc: {ts!r} (expected e.g. 2026-05-09T16:35Z)"
        )


def validate_priority(priority: str) -> None:
    if priority not in VALID_PRIORITIES:
        raise DebateError(
            f"invalid_priority: {priority!r} not in {VALID_PRIORITIES}"
        )


def validate_kind(kind: str) -> None:
    if kind not in VALID_KINDS:
        raise DebateError(
            f"invalid_kind: {kind!r} not in {VALID_KINDS}"
        )


def validate_state(state: str) -> None:
    if state not in VALID_STATES:
        raise DebateError(
            f"invalid_state: {state!r} not in {VALID_STATES}"
        )


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
            raise DebateError(
                f"invalid_roles_entry: role {role} missing session_id"
            )
    if resolve_by is not None:
        validate_iso_utc(resolve_by)

    existing = conn.execute(
        "SELECT topic_id, title, state, created_at, created_by_role, "
        "resolve_by, archived_at, roles_json, metadata_json "
        "FROM debates WHERE topic_id = ?",
        (topic_id,),
    ).fetchone()
    if existing is not None:
        same_roles = json_loads(existing["roles_json"]) == roles
        if same_roles:
            return _row_to_debate_dict(existing)
        raise DebateError(
            f"topic_exists_with_different_roles: {topic_id}"
        )

    now = now_iso()
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
    return {
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


def get_debate(
    conn: sqlite3.Connection, topic_id: str
) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT topic_id, title, state, created_at, created_by_role, "
        "resolve_by, archived_at, roles_json, metadata_json "
        "FROM debates WHERE topic_id = ?",
        (topic_id,),
    ).fetchone()
    return _row_to_debate_dict(row) if row else None


def _row_to_debate_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    d["roles"] = json_loads(d.pop("roles_json")) if d.get("roles_json") else []
    md = d.pop("metadata_json", None)
    d["metadata"] = json_loads(md) if md else None
    return d


def role_in_debate(roles: list[dict[str, Any]], role: str) -> bool:
    return any(isinstance(r, dict) and r.get("role") == role for r in roles)


def validate_session_id(session_id: str) -> None:
    if not isinstance(session_id, str) or not SESSION_ID_RE.fullmatch(session_id):
        raise DebateError(
            f"session_id {session_id!r} must match {SESSION_ID_RE.pattern}",
            error_type="recipient_invalid_session_id",
        )


def is_worker_session_id(session_id: str) -> bool:
    return isinstance(session_id, str) and WORKER_SESSION_ID_RE.fullmatch(session_id) is not None


def worker_parent_session_id(session_id: str) -> str | None:
    m = WORKER_SESSION_ID_RE.fullmatch(session_id) if isinstance(session_id, str) else None
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
        "SELECT role, kind FROM debate_messages "
        "WHERE msg_id = ? AND topic_id = ?",
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


def _next_binding_generation(
    conn: sqlite3.Connection, topic_id: str, role: str
) -> int:
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
    older_than_ts: str, minimum_age_seconds: int
) -> None:
    if isinstance(minimum_age_seconds, bool) or not isinstance(
        minimum_age_seconds, int
    ):
        raise DebateError(
            "minimum_age_seconds must be int",
            error_type="message_claim_reclaim_min_age_invalid",
        )
    if minimum_age_seconds < 0:
        raise DebateError(
            "minimum_age_seconds must be >= 0",
            error_type="message_claim_reclaim_min_age_invalid",
        )
    cutoff = _parse_iso_utc_dt(older_than_ts)
    safe_cutoff = datetime.now(timezone.utc) - timedelta(
        seconds=minimum_age_seconds
    )
    if cutoff > safe_cutoff:
        raise DebateError(
            f"message_claim_reclaim_cutoff_too_recent: older_than_ts={older_than_ts} "
            f"must be at least {minimum_age_seconds}s behind current UTC time",
            error_type="message_claim_reclaim_cutoff_too_recent",
        )


def _terminal_reply_for_trigger(
    conn: sqlite3.Connection, *, topic_id: str, role: str, trigger_msg_id: str
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT msg_id, ts FROM debate_messages "
        "WHERE topic_id = ? AND role = ? AND reply_to = ? "
        "AND kind IN ('A', 'STATUS') "
        "ORDER BY ts ASC, msg_id ASC LIMIT 1",
        (topic_id, role, trigger_msg_id),
    ).fetchone()


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
        "SELECT topic_id FROM debate_messages WHERE msg_id = ?",
        (trigger_msg_id,),
    ).fetchone()
    if trigger is None or trigger["topic_id"] != topic_id:
        raise DebateError(
            f"worker_trigger_unknown: {trigger_msg_id}",
            error_type="worker_trigger_unknown",
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
        "SELECT msg_id, ts FROM debate_messages "
        "WHERE msg_id = ? AND topic_id = ?",
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
        (worker_session_id, role, topic_id, ref["msg_id"], ref["ts"], now),
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
            "last_processed_msg_id": ref["msg_id"],
            "last_processed_ts": ref["ts"],
            "last_check_at": now,
        }
    )
    return out


def _complete_worker_claim_if_terminal(
    conn: sqlite3.Connection,
    *,
    topic_id: str,
    role: str,
    worker_session_id: str,
    now: str,
) -> dict[str, Any] | None:
    if not is_worker_session_id(worker_session_id):
        return None
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
) -> dict[str, Any]:
    """Append a message to a debate. Validates topic state, role membership,
    enums, and all kind-specific semantics BEFORE the INSERT (atomicity
    fix per CONDUCTOR msg:bf45a126). On any DebateError no row is
    persisted.

    Side-effects (post-INSERT, only when validations pass):
      kind=STATE: triggers transition via VALID_TRANSITIONS + UPDATE debates.
      kind=WATERMARK: updates debate_watermarks for (topic_id, role).
    """
    validate_topic_id(topic_id)
    validate_role(role)
    validate_priority(priority)
    validate_kind(kind)
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
            raise DebateError(
                f"reply_to_cross_topic: {reply_to} not in {topic_id}"
            )
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
                raise DebateError(
                    f"watermark_msg_not_in_topic: {watermark_target}"
                )
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
                    f"invalid_watermark_ts: {wm_ts_claimed!r} "
                    "(expect ISO 8601 UTC)"
                )
            ref_msg = conn.execute(
                "SELECT msg_id, ts FROM debate_messages "
                "WHERE msg_id = ? AND topic_id = ?",
                (wm_msg_id, topic_id),
            ).fetchone()
            if ref_msg is None:
                raise DebateError(
                    f"watermark_msg_not_in_topic: {wm_msg_id}"
                )
            if ref_msg["ts"] != wm_ts_claimed:
                raise DebateError(
                    f"watermark_ts_mismatch: body claimed ts="
                    f"{wm_ts_claimed!r} but msg_id {wm_msg_id} actual "
                    f"ts={ref_msg['ts']!r}"
                )
            watermark_resolved = (wm_msg_id, ref_msg["ts"])

    standing_db = _standing_to_db(standing)

    if kind == "DECISION" and reply_to is not None and parent_kind != "Q":
        raise DebateError(
            f"decision_reply_to_must_be_Q: parent kind={parent_kind!r}"
        )

    if kind in ("A", "STATUS") and reply_to is not None:
        one_shot_parent = (
            (parent_kind == "DECISION" and parent_standing == 0)
            or _worker_claim_exists(
                conn, topic_id=topic_id, role=role, trigger_msg_id=reply_to
            )
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
        "kind, standing, reply_to, body, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            msg_id,
            topic_id,
            role,
            ts,
            priority,
            kind,
            standing_db,
            reply_to,
            body,
            ts,
        ),
    )
    _complete_nonstanding_decision_claims_for_reply(
        conn,
        topic_id=topic_id,
        role=role,
        kind=kind,
        reply_to=reply_to,
        ack_msg_id=msg_id,
        now=ts,
    )

    new_state = debate["state"]
    if kind == "STATE" and new_state_target is not None:
        new_state = new_state_target
        archived_at = ts if new_state == "ARCHIVED" else None
        conn.execute(
            "UPDATE debates SET state = ?, "
            "archived_at = COALESCE(?, archived_at) WHERE topic_id = ?",
            (new_state, archived_at, topic_id),
        )

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

    return {"msg_id": msg_id, "ts": ts, "topic_state": new_state}


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
            "SELECT ts, msg_id FROM debate_messages "
            "WHERE msg_id = ? AND topic_id = ?",
            (since_msg_id, topic_id),
        ).fetchone()
        if ref is None:
            raise DebateError(
                f"unknown_since_msg_id: {since_msg_id} not found in "
                f"topic {topic_id}"
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
            "SELECT msg_id, ts FROM debate_messages "
            "WHERE topic_id = ? AND kind = 'COMPACTION' "
            "ORDER BY ts DESC, msg_id DESC LIMIT 1",
            (topic_id,),
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

    where: list[str] = ["topic_id = ?"]
    params: list[Any] = [topic_id]
    if cursor_ts is not None:
        # Dual-branch cursor (v3.9.3 msg:946bcff6 amendment 3,
        # read_messages naked-column form). cursor_msg_id is None ONLY
        # when the caller passed since_ts without since_msg_id — emit
        # strict-exclusive ts comparison so a message at ts ==
        # since_ts is NOT re-emitted. Compound form runs only when an
        # explicit msg_id cursor exists (since_msg_id, watermark,
        # compaction).
        if cursor_msg_id is None:
            where.append("ts > ?")
            params.extend([cursor_ts])
        else:
            where.append("(ts > ? OR (ts = ? AND msg_id > ?))")
            params.extend([cursor_ts, cursor_ts, cursor_msg_id])
    if kind_filter:
        for k in kind_filter:
            validate_kind(k)
        ph = ",".join("?" * len(kind_filter))
        where.append(f"kind IN ({ph})")
        params.extend(kind_filter)
    if priority_filter:
        for p in priority_filter:
            validate_priority(p)
        ph = ",".join("?" * len(priority_filter))
        where.append(f"priority IN ({ph})")
        params.extend(priority_filter)
    where_sql = "WHERE " + " AND ".join(where)

    fetch_limit = effective_limit + 1  # one extra row to detect truncation
    rows = conn.execute(
        f"SELECT msg_id, topic_id, role, ts, priority, kind, reply_to, "
        f"standing, body, created_at FROM debate_messages {where_sql} "
        f"ORDER BY ts ASC, msg_id ASC LIMIT ?",
        [*params, fetch_limit],
    ).fetchall()

    truncated = len(rows) > effective_limit
    if truncated:
        rows = rows[:effective_limit]
    messages = [dict(r) for r in rows]
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


def list_role_bindings(
    conn: sqlite3.Connection, *, topic_id: str
) -> dict[str, Any]:
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

    if state == "active":
        if (
            existing_active is not None
            and existing_active["session_id"] != session_id
        ):
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
        }

    if state == "diagnostic":
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
    }


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
) -> int:
    now = now_iso()
    if new_state == "RESOLVED":
        states = ("active",)
    elif new_state == "ARCHIVED":
        states = ("active", "diagnostic")
    else:
        return 0
    placeholders = ",".join("?" for _ in states)
    cur = conn.execute(
        "UPDATE debate_role_bindings SET state = 'retired', "
        "retired_at = COALESCE(retired_at, ?), updated_at = ?, reason = ? "
        f"WHERE topic_id = ? AND state IN ({placeholders})",
        (
            now,
            now,
            f"topic_{new_state.lower()}:{reason}" if reason else f"topic_{new_state.lower()}",
            topic_id,
            *states,
        ),
    )
    return int(cur.rowcount or 0)


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
        raise DebateError(
            f"unknown_role_for_topic: {role} not in declared roles"
        )

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
    state_body = (
        new_state if not reason else f"{new_state} [reason: {reason}]"
    )
    msg = post_message(
        conn,
        topic_id=topic_id,
        role=role,
        priority="H",
        kind="STATE",
        body=state_body,
    )
    retired_bindings = _retire_bindings_for_transition(
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
        r["role"]
        for r in roles_iter
        if isinstance(r, dict) and "role" in r
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
    )
    msg_id = post_result["msg_id"]
    for recipient in deduped:
        conn.execute(
            "INSERT INTO debate_message_recipients "
            "(msg_id, recipient, recipient_mode) VALUES (?, ?, 'normal')",
            (msg_id, recipient),
        )
    for recipient in diagnostic_deduped:
        conn.execute(
            "INSERT INTO debate_message_recipients "
            "(msg_id, recipient, recipient_mode) "
            "VALUES (?, ?, 'diagnostic')",
            (msg_id, recipient),
        )

    return {
        "msg_id": msg_id,
        "ts": post_result["ts"],
        "recipient_count": len(deduped) + len(diagnostic_deduped),
        "diagnostic_recipient_count": len(diagnostic_deduped),
        "topic_state": post_result["topic_state"],
        "schema_version": DEBATE_POST_RESPONSE_SCHEMA_VERSION,
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
) -> list[str]:
    if is_worker_session_id(session_id):
        claim = _validate_worker_claim_for_signal(
            conn,
            topic_id=topic_id,
            role=role,
            worker_session_id=session_id,
        )
        if claim["state"] == "active":
            return [role, session_id]
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

    Per CONDUCTOR canonical msg:c5e91d24 with amendments msg:7831af04
    (limit validation matrix), msg:c798c786 (EXISTS de-dupe so a single
    msg addressed to BOTH role and session_id counts once), and
    msg:e0f47b29 (DebateError taxonomy).

    Cursor precedence (matches read_messages from v3.9.0):
      1. since_msg_id explicit (pagination walk)
      2. since_ts explicit
      3. debate_signal_state row for (session_id, role, topic_id)
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
    worker_claim: sqlite3.Row | None = None
    if is_worker_session_id(session_id):
        worker_claim = _validate_worker_claim_for_signal(
            conn,
            topic_id=topic_id,
            role=role,
            worker_session_id=session_id,
        )

    cursor_ts: str | None = None
    cursor_msg_id: str = ""
    cursor_from_state = False

    if since_msg_id is not None:
        validate_msg_id(since_msg_id)
        ref = conn.execute(
            "SELECT ts, msg_id FROM debate_messages "
            "WHERE msg_id = ? AND topic_id = ?",
            (since_msg_id, topic_id),
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
        elif worker_claim is not None and worker_claim["parent_cursor_ts"]:
            cursor_ts = worker_claim["parent_cursor_ts"]
            cursor_msg_id = worker_claim["parent_cursor_msg_id"] or ""
            cursor_from_state = True

    signal_recipients = _signal_recipients_for_binding(
        conn, topic_id=topic_id, role=role, session_id=session_id
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

    where = ["m.topic_id = ?"]
    params: list[Any] = [topic_id]
    recipient_placeholders = ",".join("?" for _ in signal_recipients)
    where.append(
        "EXISTS (SELECT 1 FROM debate_message_recipients r "
        f"WHERE r.msg_id = m.msg_id AND r.recipient IN ({recipient_placeholders}))"
    )
    params.extend(signal_recipients)
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
                    "OR (m.kind = 'DECISION' AND m.standing = 0 "
                    "AND NOT EXISTS ("
                    " SELECT 1 FROM debate_message_claims c "
                    " WHERE c.msg_id = m.msg_id AND c.role = ? "
                    " AND c.state = 'done'"
                    ")))"
                )
                params.extend(
                    [
                        cursor_ts,
                        cursor_ts,
                        cursor_msg_id,
                        *STANDING_SIGNAL_KINDS,
                        role,
                    ]
                )
            else:
                where.append(cursor_clause)
                params.extend([cursor_ts, cursor_ts, cursor_msg_id])

    fetch_limit = effective_limit + 1  # +1 to detect truncation
    rows = conn.execute(
        "SELECT m.msg_id, m.topic_id, m.role, m.ts, m.priority, m.kind, "
        "m.reply_to, m.standing, m.body, m.created_at "
        "FROM debate_messages m "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY m.ts ASC, m.msg_id ASC LIMIT ?",
        [*params, fetch_limit],
    ).fetchall()

    truncated = len(rows) > effective_limit
    if truncated:
        rows = rows[:effective_limit]
    pending = [
        dict(r)
        for r in rows
        if _claim_or_filter_nonstanding_decision(
            conn, msg=r, role=role, session_id=session_id
        )
    ]

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

    return {
        "pending": pending,
        "count": len(pending),
        "truncated": truncated,
        "next_cursor": next_cursor,
        "max_priority": max_priority,
        "topic_state": debate["state"],
        "limit": effective_limit,
    }


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

    ref = conn.execute(
        "SELECT msg_id, ts FROM debate_messages "
        "WHERE msg_id = ? AND topic_id = ?",
        (last_processed_msg_id, topic_id),
    ).fetchone()
    if ref is None:
        raise DebateError(
            f"unknown_msg_id_for_advance: {last_processed_msg_id} not in "
            f"topic {topic_id}",
            error_type="watermark_msg_id_unknown",
        )

    signal_recipients = _signal_recipients_for_binding(
        conn, topic_id=topic_id, role=role, session_id=session_id
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
    worker_claim = _complete_worker_claim_if_terminal(
        conn,
        topic_id=topic_id,
        role=role,
        worker_session_id=session_id,
        now=now,
    )

    out = {
        "session_id": session_id,
        "role": role,
        "topic_id": topic_id,
        "last_processed_msg_id": ref["msg_id"],
        "last_processed_ts": ref["ts"],
        "last_check_at": now,
    }
    if worker_claim is not None:
        out["worker_claim"] = worker_claim
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


def _wake_already_logged(
    conn: sqlite3.Connection,
    *,
    trigger_msg_id: str,
    target_session_id: str,
    action: str,
) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM debate_wake_log "
            "WHERE trigger_msg_id = ? AND target_session_id = ? "
            "AND action = ? LIMIT 1",
            (trigger_msg_id, target_session_id, action),
        ).fetchone()
        is not None
    )


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
        "SELECT topic_id FROM debate_messages WHERE msg_id = ?",
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
            if _wake_already_logged(
                conn,
                trigger_msg_id=msg_id,
                target_session_id=binding["session_id"],
                action=action,
            ):
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
