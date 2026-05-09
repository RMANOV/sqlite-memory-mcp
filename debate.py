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
    """Validation or state-machine rejection."""


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
    enums, and reply_to existence.

    Side-effects:
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

    if reply_to is not None:
        validate_msg_id(reply_to)
        parent = conn.execute(
            "SELECT topic_id FROM debate_messages WHERE msg_id = ?",
            (reply_to,),
        ).fetchone()
        if parent is None:
            raise DebateError(f"unknown_reply_to: {reply_to}")
        if parent["topic_id"] != topic_id:
            raise DebateError(
                f"reply_to_cross_topic: {reply_to} not in {topic_id}"
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
    if kind == "STATE":
        new_state = body.strip()
        validate_transition(debate["state"], new_state)
        archived_at = ts if new_state == "ARCHIVED" else None
        conn.execute(
            "UPDATE debates SET state = ?, "
            "archived_at = COALESCE(?, archived_at) WHERE topic_id = ?",
            (new_state, archived_at, topic_id),
        )

    if kind == "WATERMARK":
        watermark_target = body.strip()
        if MSG_ID_RE.fullmatch(watermark_target):
            ref_msg = conn.execute(
                "SELECT msg_id, ts FROM debate_messages "
                "WHERE msg_id = ? AND topic_id = ?",
                (watermark_target, topic_id),
            ).fetchone()
            if ref_msg is None:
                raise DebateError(
                    f"watermark_msg_not_in_topic: {watermark_target}"
                )
            wm_ts = ref_msg["ts"]
            wm_id: str | None = ref_msg["msg_id"]
        elif ISO_UTC_RE.fullmatch(watermark_target):
            wm_ts = watermark_target
            wm_id = None
        else:
            raise DebateError(
                f"invalid_watermark_body: {watermark_target!r} "
                "(expect msg_id or ISO UTC)"
            )
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
    kind_filter: list[str] | None = None,
    priority_filter: list[str] | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """Read messages from a topic with cursor resolution + filters.

    Cursor priority: explicit since_msg_id > explicit since_ts >
    debate_watermarks[(topic_id, role)] > beginning of topic.
    Does NOT auto-advance the watermark — caller writes WATERMARK message.
    """
    validate_topic_id(topic_id)
    validate_role(role)
    debate = get_debate(conn, topic_id)
    if debate is None:
        raise DebateError(f"unknown_topic: {topic_id}")

    cursor_ts: str | None = None
    if since_msg_id is not None:
        validate_msg_id(since_msg_id)
        ref = conn.execute(
            "SELECT ts FROM debate_messages "
            "WHERE msg_id = ? AND topic_id = ?",
            (since_msg_id, topic_id),
        ).fetchone()
        if ref is not None:
            cursor_ts = ref["ts"]
        else:
            cursor_ts = None
    elif since_ts is not None:
        validate_iso_utc(since_ts)
        cursor_ts = since_ts
    else:
        wm = get_watermark(conn, topic_id, role)
        if wm and wm.get("last_processed_ts"):
            cursor_ts = wm["last_processed_ts"]

    where: list[str] = ["topic_id = ?"]
    params: list[Any] = [topic_id]
    if cursor_ts is not None:
        where.append("ts > ?")
        params.append(cursor_ts)
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
    limit_sql = ""
    if limit is not None:
        if not isinstance(limit, int) or limit < 1:
            raise DebateError("invalid_limit: must be positive int or None")
        limit_sql = "LIMIT ?"
        params.append(limit)
    rows = conn.execute(
        f"SELECT msg_id, topic_id, role, ts, priority, kind, reply_to, "
        f"body, created_at FROM debate_messages {where_sql} "
        f"ORDER BY ts ASC, msg_id ASC {limit_sql}",
        params,
    ).fetchall()
    messages = [dict(r) for r in rows]
    last_msg_id = messages[-1]["msg_id"] if messages else None
    return {
        "messages": messages,
        "topic_state": debate["state"],
        "last_msg_id_returned": last_msg_id,
        "count": len(messages),
    }


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
        blocking = _open_high_questions(conn, topic_id)
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


def _open_high_questions(
    conn: sqlite3.Connection, topic_id: str
) -> list[dict[str, Any]]:
    """Return high-priority Q messages without a matching A reply."""
    rows = conn.execute(
        "SELECT q.msg_id, q.role, q.ts, q.body "
        "FROM debate_messages q "
        "WHERE q.topic_id = ? AND q.kind = 'Q' AND q.priority = 'H' "
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
