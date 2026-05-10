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

import re
import sqlite3
import uuid
from typing import Any

from db_utils import json_dumps, json_loads, now_iso


# ── Enums + regex validators ──────────────────────────────────────────


TOPIC_RE = re.compile(r"^[A-Z][A-Z0-9_]+$")
ROLE_RE = re.compile(r"^[A-Z][A-Z0-9_]+$")
MSG_ID_RE = re.compile(r"^[a-f0-9]{8}$")
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
SESSION_ID_RE = re.compile(r"^(cc|codex|mcp|tray|human)-[a-zA-Z0-9_]{4,64}$")
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
    r"(?P<msg_id>[a-f0-9]{8})"
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
VALID_STATES = ("INIT", "ACTIVE", "RESOLVED", "ARCHIVED")

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
    """Generate a unique 8-char hex message id (uuid4 prefix)."""
    return uuid.uuid4().hex[:8]


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
    if not isinstance(body, str) or not body:
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
    if reply_to is not None:
        validate_msg_id(reply_to)
        parent = conn.execute(
            "SELECT topic_id, kind FROM debate_messages WHERE msg_id = ?",
            (reply_to,),
        ).fetchone()
        if parent is None:
            raise DebateError(f"unknown_reply_to: {reply_to}")
        if parent["topic_id"] != topic_id:
            raise DebateError(
                f"reply_to_cross_topic: {reply_to} not in {topic_id}"
            )
        parent_kind = parent["kind"]

    # ── Kind-specific PRE-INSERT validation (atomicity fix bf45a126) ──
    new_state_target: str | None = None
    watermark_resolved: tuple[str | None, str] | None = None

    if kind == "STATE":
        target = body.strip()
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
            m = _WATERMARK_RE.search(watermark_target)
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

    if kind == "DECISION" and reply_to is not None and parent_kind != "Q":
        raise DebateError(
            f"decision_reply_to_must_be_Q: parent kind={parent_kind!r}"
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
        "kind, reply_to, body, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (msg_id, topic_id, role, ts, priority, kind, reply_to, body, ts),
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
        cursor_msg_id = ""

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
        f"body, created_at FROM debate_messages {where_sql} "
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

    body = new_state if not reason else f"{new_state} [reason: {reason}]"
    msg = post_message(
        conn,
        topic_id=topic_id,
        role=role,
        priority="H",
        kind="STATE",
        body=new_state,
    )
    if reason:
        post_message(
            conn,
            topic_id=topic_id,
            role=role,
            priority="INFO",
            kind="STATUS",
            body=f"state transition reason: {reason}",
        )
    return {
        "old_state": old_state,
        "new_state": new_state,
        "ts": msg["ts"],
        "blocking_questions": [],
        "transition_msg_id": msg["msg_id"],
        "body": body,
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
    recipient: str, topic_id: str, conn: sqlite3.Connection
) -> None:
    """Recipient must be a declared role OR a SESSION_ID_RE-shaped session
    id with an approved runtime prefix. Forward-compat: a well-formed
    session_id is accepted even if not yet registered in
    debate_signal_state — registration is implicit on first signal_check.

    Raises DebateError with a specific error_type taxonomy:
      - 'recipient_unknown_role' for role-shaped names that don't match
        the topic's declared roles
      - 'recipient_invalid_session_id' for ids that don't match
        SESSION_ID_RE
    """
    if not isinstance(recipient, str) or not recipient:
        raise DebateError(
            f"invalid_recipient: {recipient!r} must be a non-empty string",
            error_type="recipient_invalid_session_id",
        )
    debates_row = conn.execute(
        "SELECT roles_json FROM debates WHERE topic_id = ?", (topic_id,)
    ).fetchone()
    if debates_row is None:
        raise DebateError(
            f"unknown_topic: {topic_id}",
            error_type="topic_not_found",
        )
    declared_roles = {
        r["role"]
        for r in json_loads(debates_row["roles_json"])
        if isinstance(r, dict) and "role" in r
    }
    if recipient in declared_roles:
        return
    if SESSION_ID_RE.fullmatch(recipient):
        return
    # Classify: role-shaped (uppercase, no dash, no prefix) → unknown role;
    # otherwise → malformed session id. Both yield a clear caller-fixable
    # error_type so MCP wrappers don't lump validation under
    # 'internal_error'.
    looks_like_role = recipient.isupper() and "-" not in recipient
    if looks_like_role:
        raise DebateError(
            f"recipient {recipient!r} is not a declared role of topic "
            f"{topic_id} (declared: {sorted(declared_roles)})",
            error_type="recipient_unknown_role",
        )
    raise DebateError(
        f"recipient {recipient!r} is not a valid session_id "
        f"(approved prefixes: {APPROVED_RUNTIME_PREFIXES}; suffix must "
        f"match [a-zA-Z0-9_]{{4,64}})",
        error_type="recipient_invalid_session_id",
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
    reply_to: str | None = None,
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
    """
    if not isinstance(addressed_to, list) or not addressed_to:
        raise DebateError(
            "addressed_to required and non-empty (broadcast not "
            "supported in v3.9.x)",
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
    for recipient in deduped:
        _validate_recipient(recipient, topic_id, conn)

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
    )
    msg_id = post_result["msg_id"]
    for recipient in deduped:
        conn.execute(
            "INSERT INTO debate_message_recipients (msg_id, recipient) "
            "VALUES (?, ?)",
            (msg_id, recipient),
        )

    return {
        "msg_id": msg_id,
        "ts": post_result["ts"],
        "recipient_count": len(deduped),
        "topic_state": post_result["topic_state"],
    }


def _validate_signal_caller(
    session_id: str, role: str, topic_id: str, conn: sqlite3.Connection
) -> dict[str, Any]:
    """Shared input validation for signal_check + signal_advance.

    Verifies session_id matches SESSION_ID_RE and role is declared in the
    topic's roles_json. Returns the debate row dict for downstream use.
    """
    if not isinstance(session_id, str) or not SESSION_ID_RE.fullmatch(session_id):
        raise DebateError(
            f"session_id {session_id!r} must match {SESSION_ID_RE.pattern}",
            error_type="recipient_invalid_session_id",
        )
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
    """
    effective_limit = _validate_signal_limit(limit)
    debate = _validate_signal_caller(session_id, role, topic_id, conn)

    cursor_ts: str | None = None
    cursor_msg_id: str = ""

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
        cursor_msg_id = ""
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

    where = ["m.topic_id = ?"]
    params: list[Any] = [topic_id]
    where.append(
        "EXISTS (SELECT 1 FROM debate_message_recipients r "
        "WHERE r.msg_id = m.msg_id AND r.recipient IN (?, ?))"
    )
    params.extend([role, session_id])
    if cursor_ts is not None:
        where.append("(m.ts > ? OR (m.ts = ? AND m.msg_id > ?))")
        params.extend([cursor_ts, cursor_ts, cursor_msg_id])

    fetch_limit = effective_limit + 1  # +1 to detect truncation
    rows = conn.execute(
        "SELECT m.msg_id, m.topic_id, m.role, m.ts, m.priority, m.kind, "
        "m.reply_to, m.body, m.created_at "
        "FROM debate_messages m "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY m.ts ASC, m.msg_id ASC LIMIT ?",
        [*params, fetch_limit],
    ).fetchall()

    truncated = len(rows) > effective_limit
    if truncated:
        rows = rows[:effective_limit]
    pending = [dict(r) for r in rows]

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

    addressed = conn.execute(
        "SELECT 1 FROM debate_message_recipients "
        "WHERE msg_id = ? AND recipient IN (?, ?) LIMIT 1",
        (last_processed_msg_id, role, session_id),
    ).fetchone()
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

    return {
        "session_id": session_id,
        "role": role,
        "topic_id": topic_id,
        "last_processed_msg_id": ref["msg_id"],
        "last_processed_ts": ref["ts"],
        "last_check_at": now,
    }
