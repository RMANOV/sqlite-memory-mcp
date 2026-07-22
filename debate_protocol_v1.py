"""Deterministic server semantics for Debate Protocol ``debate/v1``.

This module is deliberately framework-neutral.  It owns the typed envelope,
protocol micro-state, blind-commit visibility barrier, stale-read guard,
bounded rounds, judge order-swap projections, same-role binding repair and
adaptive wait policy.  Transport remains in :mod:`debate` and
``hooks/debate_pump.py``.

No protocol decision is inferred from prose.  ``body`` can remain useful for
humans, but every machine-relevant value is a validated column or JSON field.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import re
import sqlite3
from typing import Any, Iterable, Sequence


PROTOCOL_VERSION = "debate/v1"
SEMANTIC_KINDS = (
    "CLAIM",
    "CHALLENGE",
    "EVIDENCE",
    "REBUT",
    "CONCEDE",
    "VERIFY",
    "DISSENT",
    "ESCALATE",
)
DEBATE_TURN_KINDS = (
    "CLAIM",
    "CHALLENGE",
    "EVIDENCE",
    "REBUT",
    "CONCEDE",
    "VERIFY",
)
TARGET_REQUIRED_KINDS = (
    "CHALLENGE",
    "EVIDENCE",
    "REBUT",
    "CONCEDE",
    "VERIFY",
    "DISSENT",
)
BODY_MODES = ("structured", "live_text")
PHASES = (
    "BLIND_CLAIM",
    "DEBATE",
    "ADJUDICATE",
    "STALEMATE",
    "ESCALATED",
    "STOPPED",
)
BLIND_BARRIER_STATES = ("not_required", "waiting", "released")
DEFAULT_MAX_ROUNDS = 3
MAX_ROUNDS_LIMIT = 10

_HUMAN_RECIPIENT_RE = re.compile(
    r"^(?:HUMAN|OPERATOR|human(?:[-_].+)?|operator)$", re.IGNORECASE
)
_SESSION_SAFE_RE = re.compile(r"[^A-Za-z0-9_]+")
_RETRY_BACKOFF_SECONDS = (1.0, 2.0, 5.0, 10.0, 30.0)


class ProtocolV1Error(ValueError):
    """Typed, detail-carrying server rejection."""

    def __init__(
        self,
        message: str,
        *,
        error_type: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.details = details or {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _json_dict(value: Any, *, field: str) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ProtocolV1Error(
                f"invalid_{field}: {exc.msg}",
                error_type="INVALID_PAYLOAD",
                details={"field": field},
            ) from exc
        if isinstance(parsed, dict):
            return parsed
    raise ProtocolV1Error(
        f"invalid_{field}: expected JSON object",
        error_type="INVALID_PAYLOAD",
        details={"field": field},
    )


def _require_nonempty_string(payload: dict[str, Any], key: str) -> None:
    if not isinstance(payload.get(key), str) or not payload[key].strip():
        raise ProtocolV1Error(
            f"invalid_payload: {key} must be a non-empty string",
            error_type="INVALID_PAYLOAD",
            details={"field": key},
        )


def _require_list(payload: dict[str, Any], key: str) -> None:
    if not isinstance(payload.get(key), list):
        raise ProtocolV1Error(
            f"invalid_payload: {key} must be an array",
            error_type="INVALID_PAYLOAD",
            details={"field": key},
        )


def normalize_payload(kind: str, payload: Any) -> dict[str, Any]:
    """Validate a kind-specific machine payload and return a clean copy."""
    if kind not in SEMANTIC_KINDS:
        return {}
    value = _json_dict(payload, field="payload_json")
    if kind == "CLAIM":
        _require_nonempty_string(value, "summary")
        _require_list(value, "assumptions")
        _require_list(value, "evidence_refs")
    elif kind == "CHALLENGE":
        _require_nonempty_string(value, "target")
        _require_nonempty_string(value, "challenge_type")
        _require_nonempty_string(value, "requested_disposition")
    elif kind == "EVIDENCE":
        for key in ("target", "source_id", "locator", "retrieved_at", "content_hash"):
            _require_nonempty_string(value, key)
        status = value.get("verification_status")
        if status not in {"verified", "contested", "unsupported", "unknown"}:
            raise ProtocolV1Error(
                "invalid_payload: verification_status is not canonical",
                error_type="INVALID_PAYLOAD",
                details={"field": "verification_status"},
            )
    elif kind == "REBUT":
        _require_nonempty_string(value, "target")
        _require_nonempty_string(value, "disposition")
        _require_list(value, "evidence_refs")
    elif kind == "CONCEDE":
        for key in ("target", "scope", "consequence"):
            _require_nonempty_string(value, key)
    elif kind == "VERIFY":
        _require_nonempty_string(value, "target")
        if value.get("result") not in {
            "verified",
            "contested",
            "unsupported",
            "unknown",
        }:
            raise ProtocolV1Error(
                "invalid_payload: VERIFY.result is not canonical",
                error_type="INVALID_PAYLOAD",
                details={"field": "result"},
            )
        _require_list(value, "checks")
    elif kind == "DISSENT":
        for key in ("decision_target", "unresolved_point", "strongest_evidence"):
            _require_nonempty_string(value, key)
    elif kind == "ESCALATE":
        for key in (
            "decision_question",
            "unresolved_point",
            "exact_human_action",
        ):
            _require_nonempty_string(value, key)
        for key in ("options", "decisive_evidence", "consequence_by_option"):
            _require_list(value, key)
    return value


def get_protocol_state(
    conn: sqlite3.Connection, topic_id: str
) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM debate_protocol_state WHERE topic_id = ?", (topic_id,)
    ).fetchone()
    if row is None:
        return None
    out = dict(row)
    try:
        out["blind_roles"] = json.loads(out.pop("blind_roles_json"))
    except (TypeError, json.JSONDecodeError):
        out["blind_roles"] = []
    return out


def configure_topic(
    conn: sqlite3.Connection,
    *,
    topic_id: str,
    declared_roles: Sequence[str],
    blind_roles: Sequence[str],
    max_rounds: int = DEFAULT_MAX_ROUNDS,
    phase_timeout_seconds: int = 300,
) -> dict[str, Any]:
    """Idempotently enable ``debate/v1`` for a topic."""
    roles = [str(role) for role in declared_roles if str(role)]
    blind = list(dict.fromkeys(str(role) for role in blind_roles if str(role)))
    if len(blind) != 2:
        raise ProtocolV1Error(
            "blind_roles must contain exactly two distinct declared roles",
            error_type="INVALID_PROTOCOL_CONFIG",
            details={"blind_roles": blind},
        )
    unknown = [role for role in blind if role not in roles]
    if unknown:
        raise ProtocolV1Error(
            "blind_roles contain undeclared roles",
            error_type="INVALID_PROTOCOL_CONFIG",
            details={"unknown_roles": unknown},
        )
    if isinstance(max_rounds, bool) or not isinstance(max_rounds, int):
        raise ProtocolV1Error(
            "max_rounds must be an integer",
            error_type="INVALID_PROTOCOL_CONFIG",
        )
    if not 1 <= max_rounds <= MAX_ROUNDS_LIMIT:
        raise ProtocolV1Error(
            f"max_rounds must be between 1 and {MAX_ROUNDS_LIMIT}",
            error_type="INVALID_PROTOCOL_CONFIG",
        )
    if not isinstance(phase_timeout_seconds, int) or phase_timeout_seconds < 30:
        raise ProtocolV1Error(
            "phase_timeout_seconds must be an integer >= 30",
            error_type="INVALID_PROTOCOL_CONFIG",
        )
    existing = get_protocol_state(conn, topic_id)
    if existing is not None:
        if (
            existing["protocol_version"] == PROTOCOL_VERSION
            and existing["blind_roles"] == blind
            and int(existing["max_rounds"]) == max_rounds
            and int(existing["phase_timeout_seconds"]) == phase_timeout_seconds
        ):
            return existing
        raise ProtocolV1Error(
            "protocol already configured with a different contract",
            error_type="PROTOCOL_CONFIG_CONFLICT",
            details={"existing": existing},
        )
    now = _now_iso()
    deadline = (
        (datetime.now(timezone.utc) + timedelta(seconds=phase_timeout_seconds))
        .isoformat()
        .replace("+00:00", "Z")
    )
    conn.execute(
        "INSERT INTO debate_protocol_state "
        "(topic_id, protocol_version, phase, round_no, max_rounds, "
        " blind_barrier_state, blind_roles_json, stalemate_reason, "
        " transition_version, phase_deadline_at, phase_timeout_seconds, updated_at) "
        "VALUES (?, ?, 'BLIND_CLAIM', 1, ?, 'waiting', ?, NULL, 1, ?, ?, ?)",
        (
            topic_id,
            PROTOCOL_VERSION,
            max_rounds,
            json.dumps(blind, ensure_ascii=False, separators=(",", ":")),
            deadline,
            phase_timeout_seconds,
            now,
        ),
    )
    return get_protocol_state(conn, topic_id) or {}


def _resolve_author_session(
    conn: sqlite3.Connection,
    *,
    topic_id: str,
    role: str,
    author_session_id: str | None,
) -> str:
    if author_session_id:
        binding = conn.execute(
            "SELECT 1 FROM debate_role_bindings "
            "WHERE topic_id=? AND role=? AND session_id=? AND state='active'",
            (topic_id, role, author_session_id),
        ).fetchone()
        worker = conn.execute(
            "SELECT 1 FROM debate_worker_claims "
            "WHERE topic_id=? AND role=? AND worker_session_id=? AND state='active'",
            (topic_id, role, author_session_id),
        ).fetchone()
        if binding is None and worker is None:
            raise ProtocolV1Error(
                "author_session_id does not own the role",
                error_type="ROLE_UNAVAILABLE",
                details={"role": role, "session_id": author_session_id},
            )
        return author_session_id
    rows = conn.execute(
        "SELECT session_id FROM debate_role_bindings "
        "WHERE topic_id=? AND role=? AND state='active' "
        "ORDER BY generation DESC LIMIT 2",
        (topic_id, role),
    ).fetchall()
    if len(rows) != 1:
        raise ProtocolV1Error(
            "semantic post requires one active author binding",
            error_type="ROLE_UNAVAILABLE",
            details={"role": role, "active_binding_count": len(rows)},
        )
    return str(rows[0]["session_id"])


def assert_fresh_read(
    conn: sqlite3.Connection,
    *,
    topic_id: str,
    role: str,
    author_session_id: str | None,
) -> str:
    """Reject a post when an earlier addressed H message is unread."""
    session_id = _resolve_author_session(
        conn,
        topic_id=topic_id,
        role=role,
        author_session_id=author_session_id,
    )
    cursor = conn.execute(
        "SELECT last_processed_ts,last_processed_msg_id "
        "FROM debate_signal_state WHERE session_id=? AND role=? AND topic_id=?",
        (session_id, role, topic_id),
    ).fetchone()
    cursor_ts = cursor["last_processed_ts"] if cursor else None
    cursor_id = (cursor["last_processed_msg_id"] or "") if cursor else ""
    params: list[Any] = [topic_id, role, session_id, role]
    after = ""
    if cursor_ts:
        after = "AND (m.ts > ? OR (m.ts = ? AND m.msg_id > ?)) "
        params.extend([cursor_ts, cursor_ts, cursor_id])
    blocker = conn.execute(
        "SELECT m.msg_id,m.ts,m.role,m.kind FROM debate_messages m "
        "WHERE m.topic_id=? AND m.priority='H' "
        "AND EXISTS (SELECT 1 FROM debate_message_recipients r "
        " WHERE r.msg_id=m.msg_id AND r.recipient_mode='normal' "
        " AND r.recipient IN (?,?)) "
        "AND NOT EXISTS (SELECT 1 FROM debate_blind_commits bc "
        " WHERE bc.msg_id=m.msg_id AND bc.released_at IS NULL "
        " AND bc.role<>?) "
        f"{after}ORDER BY m.ts ASC,m.msg_id ASC LIMIT 1",
        params,
    ).fetchone()
    if blocker is not None:
        details = dict(blocker)
        details["author_session_id"] = session_id
        raise ProtocolV1Error(
            f"STALE_READ: unread addressed H message {blocker['msg_id']}",
            error_type="STALE_READ",
            details=details,
        )
    return session_id


_TARGET_PARENT_KINDS: dict[str, set[str]] = {
    "CHALLENGE": {"CLAIM", "EVIDENCE", "REBUT"},
    "EVIDENCE": {"CLAIM", "CHALLENGE", "REBUT"},
    "REBUT": {"CHALLENGE"},
    "CONCEDE": {"CHALLENGE", "CLAIM"},
    "VERIFY": {"CLAIM", "EVIDENCE", "REBUT", "CONCEDE"},
    "DISSENT": {"VERIFY", "DECISION", "ESCALATE"},
}


def _has_human_recipient(recipients: Iterable[str]) -> bool:
    return any(_HUMAN_RECIPIENT_RE.fullmatch(str(value or "")) for value in recipients)


def preflight_post(
    conn: sqlite3.Connection,
    *,
    topic_id: str,
    role: str,
    kind: str,
    reply_to: str | None,
    payload: Any,
    body_mode: str | None,
    protocol_version: str | None,
    author_session_id: str | None,
    recipients: Sequence[str] = (),
) -> dict[str, Any] | None:
    """Read-only semantic validation performed before the message INSERT."""
    if kind not in SEMANTIC_KINDS:
        if protocol_version not in (None, "", PROTOCOL_VERSION):
            raise ProtocolV1Error(
                "unknown protocol_version",
                error_type="INVALID_PROTOCOL_VERSION",
            )
        configured = get_protocol_state(conn, topic_id)
        if configured is not None and kind in {"Q", "A", "STATUS", "DECISION"}:
            raise ProtocolV1Error(
                f"legacy conversational kind {kind} is disabled on debate/v1 topics",
                error_type="SEMANTIC_KIND_REQUIRED",
                details={"kind": kind, "protocol_version": PROTOCOL_VERSION},
            )
        return None
    state = get_protocol_state(conn, topic_id)
    if state is None or state.get("protocol_version") != PROTOCOL_VERSION:
        raise ProtocolV1Error(
            "semantic kind requires a debate/v1 configured topic",
            error_type="PROTOCOL_NOT_CONFIGURED",
        )
    if protocol_version not in (None, "", PROTOCOL_VERSION):
        raise ProtocolV1Error(
            "semantic post protocol_version mismatch",
            error_type="INVALID_PROTOCOL_VERSION",
        )
    mode = body_mode or "structured"
    if mode not in BODY_MODES:
        raise ProtocolV1Error(
            f"invalid body_mode {mode!r}",
            error_type="INVALID_BODY_MODE",
        )
    normalized = normalize_payload(kind, payload)
    phase = str(state["phase"])
    round_no = int(state["round_no"])
    max_rounds = int(state["max_rounds"])

    if kind in DEBATE_TURN_KINDS:
        assert_fresh_read(
            conn,
            topic_id=topic_id,
            role=role,
            author_session_id=author_session_id,
        )

    if phase == "BLIND_CLAIM":
        if kind != "CLAIM" or role not in set(state["blind_roles"]):
            raise ProtocolV1Error(
                "blind commit barrier accepts only initial CLAIMs",
                error_type="BLIND_NOT_RELEASED",
                details={"phase": phase, "blind_roles": state["blind_roles"]},
            )
        duplicate = conn.execute(
            "SELECT 1 FROM debate_blind_commits WHERE topic_id=? AND role=?",
            (topic_id, role),
        ).fetchone()
        if duplicate is not None:
            raise ProtocolV1Error(
                "role already committed its initial CLAIM",
                error_type="BLIND_CLAIM_DUPLICATE",
                details={"role": role},
            )
    elif phase == "DEBATE":
        if kind not in DEBATE_TURN_KINDS and kind != "ESCALATE":
            raise ProtocolV1Error(
                f"kind {kind} is not allowed during DEBATE",
                error_type="WRONG_PHASE",
                details={"phase": phase},
            )
    elif phase == "ADJUDICATE":
        if kind != "ESCALATE":
            raise ProtocolV1Error(
                "ADJUDICATE accepts judge operations or ESCALATE only",
                error_type="WRONG_PHASE",
                details={"phase": phase},
            )
    elif phase == "STALEMATE":
        if kind not in {"DISSENT", "ESCALATE"}:
            raise ProtocolV1Error(
                "protocol is in STALEMATE",
                error_type="PROTOCOL_STALEMATE",
                details={"stalemate_reason": state.get("stalemate_reason")},
            )
        if kind == "DISSENT":
            duplicate = conn.execute(
                "SELECT 1 FROM debate_messages WHERE topic_id=? AND role=? "
                "AND protocol_version=? AND kind='DISSENT' LIMIT 1",
                (topic_id, role, PROTOCOL_VERSION),
            ).fetchone()
            if duplicate is not None:
                raise ProtocolV1Error(
                    "only one DISSENT per semantic role is allowed",
                    error_type="DISSENT_DUPLICATE",
                )
    elif phase in {"ESCALATED", "STOPPED"}:
        raise ProtocolV1Error(
            f"protocol phase {phase} is terminal for posts",
            error_type="PROTOCOL_TERMINAL",
            details={"phase": phase},
        )

    if round_no > max_rounds and kind not in {"DISSENT", "ESCALATE"}:
        raise ProtocolV1Error(
            "server round cap exceeded",
            error_type="ROUND_CAP",
            details={"round_no": round_no, "max_rounds": max_rounds},
        )

    if kind in TARGET_REQUIRED_KINDS:
        if not reply_to:
            raise ProtocolV1Error(
                f"{kind} requires reply_to",
                error_type="INVALID_TARGET",
            )
        parent = conn.execute(
            "SELECT topic_id,kind FROM debate_messages WHERE msg_id=?",
            (reply_to,),
        ).fetchone()
        if parent is None or parent["topic_id"] != topic_id:
            raise ProtocolV1Error(
                "target must exist in the same topic",
                error_type="INVALID_TARGET",
                details={"reply_to": reply_to},
            )
        allowed = _TARGET_PARENT_KINDS[kind]
        if parent["kind"] not in allowed:
            raise ProtocolV1Error(
                f"{kind} cannot target {parent['kind']}",
                error_type="INVALID_TARGET",
                details={"reply_to": reply_to, "parent_kind": parent["kind"]},
            )
        target_key = "decision_target" if kind == "DISSENT" else "target"
        if str(normalized.get(target_key)) != reply_to:
            raise ProtocolV1Error(
                f"payload.{target_key} must equal reply_to",
                error_type="INVALID_TARGET",
                details={
                    "reply_to": reply_to,
                    "payload_target": normalized.get(target_key),
                },
            )

    if kind == "ESCALATE" and not _has_human_recipient(recipients):
        raise ProtocolV1Error(
            "ESCALATE requires a human recipient",
            error_type="HUMAN_RECIPIENT_REQUIRED",
        )
    if kind == "ESCALATE":
        existing = conn.execute(
            "SELECT msg_id FROM debate_human_packets WHERE topic_id=? "
            "AND protocol_generation=?",
            (topic_id, int(state["transition_version"])),
        ).fetchone()
        if existing is not None:
            raise ProtocolV1Error(
                "ESCALATE packet already exists for this protocol generation",
                error_type="ESCALATE_DUPLICATE",
                details={"msg_id": existing["msg_id"]},
            )

    return {
        "protocol_version": PROTOCOL_VERSION,
        "round_no": round_no,
        "body_mode": mode,
        "payload": normalized,
        "payload_json": json.dumps(
            normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ),
        "phase_before": phase,
    }


def _phase_deadline(state: dict[str, Any], now: datetime | None = None) -> str:
    base = now or datetime.now(timezone.utc)
    timeout = int(state.get("phase_timeout_seconds") or 300)
    return (base + timedelta(seconds=timeout)).isoformat().replace("+00:00", "Z")


def record_post(
    conn: sqlite3.Connection,
    *,
    topic_id: str,
    role: str,
    kind: str,
    msg_id: str,
    semantic: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Apply deterministic micro-state changes after a valid INSERT."""
    if semantic is None:
        return get_protocol_state(conn, topic_id)
    state = get_protocol_state(conn, topic_id)
    if state is None:
        raise ProtocolV1Error(
            "protocol state disappeared during post",
            error_type="PROTOCOL_STATE_MISSING",
        )
    now = _now_iso()
    if kind == "CLAIM" and state["phase"] == "BLIND_CLAIM":
        conn.execute(
            "INSERT INTO debate_blind_commits "
            "(topic_id,role,msg_id,round_no,committed_at,released_at) "
            "VALUES (?,?,?,?,?,NULL)",
            (topic_id, role, msg_id, int(state["round_no"]), now),
        )
        count = conn.execute(
            "SELECT COUNT(*) FROM debate_blind_commits WHERE topic_id=?",
            (topic_id,),
        ).fetchone()[0]
        if count == len(state["blind_roles"]):
            conn.execute(
                "UPDATE debate_blind_commits SET released_at=? "
                "WHERE topic_id=? AND released_at IS NULL",
                (now, topic_id),
            )
            conn.execute(
                "UPDATE debate_protocol_state SET phase='DEBATE', "
                "blind_barrier_state='released', transition_version=transition_version+1, "
                "phase_deadline_at=?, updated_at=? WHERE topic_id=?",
                (_phase_deadline(state), now, topic_id),
            )
    elif kind == "VERIFY":
        current_round = int(state["round_no"])
        unresolved = conn.execute(
            "SELECT COUNT(*) FROM debate_messages c "
            "WHERE c.topic_id=? AND c.protocol_version=? "
            "AND c.round_no=? AND c.kind='CHALLENGE' "
            "AND NOT EXISTS (SELECT 1 FROM debate_messages d "
            " WHERE d.topic_id=c.topic_id AND d.reply_to=c.msg_id "
            " AND d.kind IN ('REBUT','CONCEDE'))",
            (topic_id, PROTOCOL_VERSION, current_round),
        ).fetchone()[0]
        result = semantic["payload"].get("result")
        if result == "verified" and unresolved == 0:
            conn.execute(
                "UPDATE debate_protocol_state SET phase='ADJUDICATE', "
                "transition_version=transition_version+1,phase_deadline_at=?,updated_at=? "
                "WHERE topic_id=?",
                (_phase_deadline(state), now, topic_id),
            )
        elif current_round >= int(state["max_rounds"]):
            conn.execute(
                "UPDATE debate_protocol_state SET phase='STALEMATE', "
                "stalemate_reason='round_cap_unresolved', "
                "transition_version=transition_version+1,phase_deadline_at=NULL,updated_at=? "
                "WHERE topic_id=?",
                (now, topic_id),
            )
        else:
            conn.execute(
                "UPDATE debate_protocol_state SET round_no=round_no+1, "
                "transition_version=transition_version+1,phase_deadline_at=?,updated_at=? "
                "WHERE topic_id=?",
                (_phase_deadline(state), now, topic_id),
            )
    elif kind == "ESCALATE":
        generation = int(state["transition_version"])
        conn.execute(
            "INSERT INTO debate_human_packets "
            "(topic_id,protocol_generation,msg_id,state,exact_human_action,payload_json,created_at,resolved_at) "
            "VALUES (?,?,?,'open',?,?,?,NULL)",
            (
                topic_id,
                generation,
                msg_id,
                semantic["payload"]["exact_human_action"],
                semantic["payload_json"],
                now,
            ),
        )
        conn.execute(
            "UPDATE debate_protocol_state SET phase='ESCALATED', "
            "transition_version=transition_version+1,phase_deadline_at=NULL,updated_at=? "
            "WHERE topic_id=?",
            (now, topic_id),
        )
    return get_protocol_state(conn, topic_id)


def visibility_sql(
    *, alias: str, viewer_role: str | None, control_plane: bool = False
) -> tuple[str, list[Any]]:
    """Return the canonical blind-commit SQL predicate."""
    if control_plane:
        return "1=1", []
    role = str(viewer_role or "")
    return (
        "NOT EXISTS (SELECT 1 FROM debate_blind_commits bc "
        f"WHERE bc.msg_id={alias}.msg_id AND bc.released_at IS NULL "
        "AND bc.role <> ?)",
        [role],
    )


def visible_message_ids(
    conn: sqlite3.Connection,
    *,
    topic_ids: Sequence[str],
    viewer_role: str | None,
    control_plane: bool = False,
) -> list[str]:
    if not topic_ids:
        return []
    ph = ",".join("?" for _ in topic_ids)
    predicate, predicate_params = visibility_sql(
        alias="m", viewer_role=viewer_role, control_plane=control_plane
    )
    rows = conn.execute(
        f"SELECT m.msg_id FROM debate_messages m WHERE m.topic_id IN ({ph}) "
        f"AND {predicate} ORDER BY m.ts,m.msg_id",
        [*topic_ids, *predicate_params],
    ).fetchall()
    return [str(row["msg_id"]) for row in rows]


def _normalized_position(row: sqlite3.Row) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if row["payload_json"]:
        try:
            parsed = json.loads(row["payload_json"])
            if isinstance(parsed, dict):
                payload = parsed
        except json.JSONDecodeError:
            payload = {}
    summary = payload.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        summary = " ".join(str(row["body"] or "").split())[:4000]
    return {
        "msg_id": row["msg_id"],
        "kind": row["kind"],
        "summary": summary,
        "evidence_refs": payload.get("evidence_refs", []),
    }


def prepare_order_swap(
    conn: sqlite3.Connection,
    *,
    topic_id: str,
    left_msg_id: str,
    right_msg_id: str,
) -> dict[str, Any]:
    state = get_protocol_state(conn, topic_id)
    if state is None or state["phase"] != "ADJUDICATE":
        raise ProtocolV1Error(
            "order-swap requires ADJUDICATE phase",
            error_type="WRONG_PHASE",
        )
    rows = conn.execute(
        "SELECT msg_id,topic_id,kind,body,payload_json FROM debate_messages "
        "WHERE topic_id=? AND msg_id IN (?,?)",
        (topic_id, left_msg_id, right_msg_id),
    ).fetchall()
    by_id = {row["msg_id"]: row for row in rows}
    if set(by_id) != {left_msg_id, right_msg_id} or left_msg_id == right_msg_id:
        raise ProtocolV1Error(
            "order-swap positions must be two distinct messages in the topic",
            error_type="INVALID_TARGET",
        )
    invalid_kinds = {
        str(row["kind"]) for row in rows if str(row["kind"]) not in {"CLAIM", "REBUT"}
    }
    if invalid_kinds:
        raise ProtocolV1Error(
            "order-swap accepts only CLAIM or REBUT positions",
            error_type="INVALID_TARGET",
            details={"invalid_kinds": sorted(invalid_kinds)},
        )
    positions = {
        left_msg_id: _normalized_position(by_id[left_msg_id]),
        right_msg_id: _normalized_position(by_id[right_msg_id]),
    }
    now = _now_iso()
    projections: list[dict[str, Any]] = []
    for order_key, ordered in (
        ("AB", [left_msg_id, right_msg_id]),
        ("BA", [right_msg_id, left_msg_id]),
    ):
        projection_id = f"{topic_id}:{int(state['round_no'])}:{order_key}"
        normalized = {
            "protocol_version": PROTOCOL_VERSION,
            "topic_id": topic_id,
            "round_no": int(state["round_no"]),
            "positions": [positions[msg_id] for msg_id in ordered],
        }
        normalized_json = json.dumps(
            normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        existing = conn.execute(
            "SELECT left_msg_id,right_msg_id,normalized_json "
            "FROM debate_judge_projections "
            "WHERE topic_id=? AND round_no=? AND order_key=?",
            (topic_id, int(state["round_no"]), order_key),
        ).fetchone()
        if existing is not None and (
            existing["left_msg_id"] != ordered[0]
            or existing["right_msg_id"] != ordered[1]
            or existing["normalized_json"] != normalized_json
        ):
            raise ProtocolV1Error(
                "judge projection is immutable for topic/round/order",
                error_type="JUDGE_PROJECTION_CONFLICT",
                details={"order_key": order_key},
            )
        if existing is None:
            conn.execute(
                "INSERT INTO debate_judge_projections "
                "(projection_id,topic_id,round_no,order_key,left_msg_id,right_msg_id,"
                "normalized_json,verdict_json,judge_role,created_at,decided_at) "
                "VALUES (?,?,?,?,?,?,?,NULL,NULL,?,NULL)",
                (
                    projection_id,
                    topic_id,
                    int(state["round_no"]),
                    order_key,
                    ordered[0],
                    ordered[1],
                    normalized_json,
                    now,
                ),
            )
        projections.append(
            {"projection_id": projection_id, "order_key": order_key, **normalized}
        )
    return {"topic_id": topic_id, "projections": projections}


def record_order_swap_verdict(
    conn: sqlite3.Connection,
    *,
    projection_id: str,
    judge_role: str,
    verdict: Any,
) -> dict[str, Any]:
    payload = _json_dict(verdict, field="verdict_json")
    _require_nonempty_string(payload, "winner_msg_id")
    _require_nonempty_string(payload, "decision")
    row = conn.execute(
        "SELECT * FROM debate_judge_projections WHERE projection_id=?",
        (projection_id,),
    ).fetchone()
    if row is None:
        raise ProtocolV1Error(
            "unknown judge projection",
            error_type="JUDGE_PROJECTION_NOT_FOUND",
        )
    if payload["winner_msg_id"] not in {row["left_msg_id"], row["right_msg_id"]}:
        raise ProtocolV1Error(
            "winner_msg_id is outside the projection",
            error_type="INVALID_TARGET",
        )
    now = _now_iso()
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    if row["verdict_json"]:
        if row["verdict_json"] != encoded or row["judge_role"] != judge_role:
            raise ProtocolV1Error(
                "projection verdict is immutable",
                error_type="JUDGE_VERDICT_CONFLICT",
            )
    else:
        state = get_protocol_state(conn, str(row["topic_id"]))
        if state is None or state["phase"] != "ADJUDICATE":
            raise ProtocolV1Error(
                "new judge verdict requires ADJUDICATE phase",
                error_type="WRONG_PHASE",
                details={"phase": state.get("phase") if state else None},
            )
        peer_role = conn.execute(
            "SELECT judge_role FROM debate_judge_projections "
            "WHERE topic_id=? AND round_no=? AND verdict_json IS NOT NULL "
            "LIMIT 1",
            (row["topic_id"], row["round_no"]),
        ).fetchone()
        if peer_role is not None and peer_role["judge_role"] != judge_role:
            raise ProtocolV1Error(
                "AB and BA verdicts require the same judge role",
                error_type="JUDGE_ROLE_MISMATCH",
            )
        conn.execute(
            "UPDATE debate_judge_projections SET verdict_json=?,judge_role=?,decided_at=? "
            "WHERE projection_id=?",
            (encoded, judge_role, now, projection_id),
        )
    peers = conn.execute(
        "SELECT projection_id,order_key,verdict_json FROM debate_judge_projections "
        "WHERE topic_id=? AND round_no=? ORDER BY order_key",
        (row["topic_id"], row["round_no"]),
    ).fetchall()
    complete = len(peers) == 2 and all(peer["verdict_json"] for peer in peers)
    stable: bool | None = None
    if complete:
        verdicts = [json.loads(peer["verdict_json"]) for peer in peers]
        stable = (
            verdicts[0]["winner_msg_id"] == verdicts[1]["winner_msg_id"]
            and verdicts[0]["decision"] == verdicts[1]["decision"]
        )
        if stable:
            conn.execute(
                "UPDATE debate_protocol_state SET phase='STOPPED',"
                "transition_version=transition_version+1,phase_deadline_at=NULL,updated_at=? "
                "WHERE topic_id=?",
                (now, row["topic_id"]),
            )
        else:
            conn.execute(
                "UPDATE debate_protocol_state SET phase='STALEMATE',"
                "stalemate_reason='judge_order_swap_disagreement',"
                "transition_version=transition_version+1,phase_deadline_at=NULL,updated_at=? "
                "WHERE topic_id=?",
                (now, row["topic_id"]),
            )
    return {
        "projection_id": projection_id,
        "topic_id": row["topic_id"],
        "complete": complete,
        "stable": stable,
        "protocol_state": get_protocol_state(conn, row["topic_id"]),
    }


def transition_expired_protocols(
    conn: sqlite3.Connection, *, now_iso: str | None = None
) -> list[dict[str, Any]]:
    """Move expired non-terminal phases to STALEMATE without agent action."""
    now = now_iso or _now_iso()
    rows = conn.execute(
        "SELECT topic_id,phase,phase_deadline_at FROM debate_protocol_state "
        "WHERE phase IN ('BLIND_CLAIM','DEBATE','ADJUDICATE') "
        "AND phase_deadline_at IS NOT NULL AND phase_deadline_at <= ?",
        (now,),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        reason = f"phase_timeout:{row['phase']}"
        cursor = conn.execute(
            "UPDATE debate_protocol_state SET phase='STALEMATE',stalemate_reason=?,"
            "transition_version=transition_version+1,phase_deadline_at=NULL,updated_at=? "
            "WHERE topic_id=? AND phase=?",
            (reason, now, row["topic_id"], row["phase"]),
        )
        if cursor.rowcount == 1:
            out.append({"topic_id": row["topic_id"], "reason": reason})
    return out


def _next_recovery_session(role: str, runtime: str, generation: int) -> str:
    prefix = "codex" if runtime.lower().startswith("codex") else "cc"
    safe = _SESSION_SAFE_RE.sub("_", role).strip("_") or "ROLE"
    return f"{prefix}-auto_{safe}_g{generation}"


def sweep_missing_roles(
    conn: sqlite3.Connection, *, topic_ids: Sequence[str] = ()
) -> list[dict[str, Any]]:
    """Rebind a missing role slot to a new same-role session generation.

    A merely quiet active binding is never declared dead here.  Missing active
    ownership is observable; dead worker processes are retired by the existing
    pid/create_time worker recovery path.  This prevents timeout-only false
    positives while eliminating permanent unbound wedges.
    """
    where = ""
    params: list[Any] = []
    if topic_ids:
        where = f"AND p.topic_id IN ({','.join('?' for _ in topic_ids)})"
        params.extend(topic_ids)
    topics = conn.execute(
        "SELECT p.topic_id,d.roles_json FROM debate_protocol_state p "
        "JOIN debates d ON d.topic_id=p.topic_id "
        "WHERE d.state='ACTIVE' AND p.phase NOT IN ('STOPPED','ESCALATED') "
        f"{where}",
        params,
    ).fetchall()
    now = _now_iso()
    actions: list[dict[str, Any]] = []
    for topic in topics:
        try:
            roster = json.loads(topic["roles_json"])
        except (TypeError, json.JSONDecodeError):
            roster = []
        roles = [
            str(entry.get("role"))
            for entry in roster
            if isinstance(entry, dict) and entry.get("role")
        ]
        for role in roles:
            if role.upper() in {"HUMAN", "OPERATOR"}:
                continue
            active = conn.execute(
                "SELECT 1 FROM debate_role_bindings "
                "WHERE topic_id=? AND role=? AND state='active' LIMIT 1",
                (topic["topic_id"], role),
            ).fetchone()
            if active is not None:
                continue
            last = conn.execute(
                "SELECT session_id,runtime,generation FROM debate_role_bindings "
                "WHERE topic_id=? AND role=? ORDER BY generation DESC LIMIT 1",
                (topic["topic_id"], role),
            ).fetchone()
            runtime = str(last["runtime"] if last else "claude")
            generation = int(last["generation"] if last else 0) + 1
            session_id = _next_recovery_session(role, runtime, generation)
            conn.execute(
                "INSERT INTO debate_role_bindings "
                "(topic_id,role,session_id,runtime,state,generation,created_at,"
                "updated_at,retired_at,reason,bound_by_role,bound_by_msg_id) "
                "VALUES (?,?,?,?,'active',?,?,?,NULL,?,NULL,NULL)",
                (
                    topic["topic_id"],
                    role,
                    session_id,
                    runtime,
                    generation,
                    now,
                    now,
                    "automatic same-role recovery: missing active binding",
                ),
            )
            if last is not None:
                cursor = conn.execute(
                    "SELECT last_processed_msg_id,last_processed_ts "
                    "FROM debate_signal_state WHERE topic_id=? AND role=? "
                    "AND session_id=?",
                    (topic["topic_id"], role, last["session_id"]),
                ).fetchone()
                if cursor is not None:
                    conn.execute(
                        "INSERT INTO debate_signal_state "
                        "(session_id,role,topic_id,last_processed_msg_id,"
                        "last_processed_ts,last_check_at) VALUES (?,?,?,?,?,?)",
                        (
                            session_id,
                            role,
                            topic["topic_id"],
                            cursor["last_processed_msg_id"],
                            cursor["last_processed_ts"],
                            now,
                        ),
                    )
            recovery_id = f"{topic['topic_id']}:{role}:{generation}"
            conn.execute(
                "INSERT OR IGNORE INTO debate_role_recovery_log "
                "(recovery_id,topic_id,role,old_session_id,new_session_id,generation,"
                "reason,created_at) VALUES (?,?,?,?,?,?,?,?)",
                (
                    recovery_id,
                    topic["topic_id"],
                    role,
                    last["session_id"] if last else None,
                    session_id,
                    generation,
                    "missing_active_binding",
                    now,
                ),
            )
            actions.append(
                {
                    "topic_id": topic["topic_id"],
                    "role": role,
                    "session_id": session_id,
                    "generation": generation,
                    "reason": "missing_active_binding",
                }
            )
    return actions


def adaptive_wait_decision(
    *,
    queue_depth: int,
    live_workers: int,
    worker_capacity: int,
    retry_attempt: int = 0,
    resource_blocked: bool = False,
    resource_interval: float = 0.0,
    idle_sweep_seconds: float = 30.0,
) -> dict[str, Any]:
    """Pure deterministic scheduler policy used by the resident pump."""
    if resource_blocked:
        return {
            "interval_seconds": max(1.0, float(resource_interval or 30.0)),
            "reason": "resource_blocked",
        }
    if retry_attempt > 0:
        index = min(retry_attempt - 1, len(_RETRY_BACKOFF_SECONDS) - 1)
        return {
            "interval_seconds": _RETRY_BACKOFF_SECONDS[index],
            "reason": "persisted_retry_backoff",
        }
    if queue_depth > 0 and (worker_capacity <= 0 or live_workers < worker_capacity):
        return {"interval_seconds": 0.0, "reason": "eligible_backlog"}
    if queue_depth > 0:
        return {"interval_seconds": 1.0, "reason": "capacity_wait"}
    if live_workers > 0:
        return {"interval_seconds": 1.0, "reason": "active_worker_lease"}
    return {
        "interval_seconds": max(1.0, float(idle_sweep_seconds)),
        "reason": "idle_crash_replay_sweep",
    }


def record_scheduler_decision(
    conn: sqlite3.Connection,
    *,
    interval_seconds: float,
    reason: str,
    queue_depth: int,
    live_workers: int,
    resource_tier: str = "",
) -> None:
    conn.execute(
        "INSERT INTO debate_scheduler_decisions "
        "(decided_at,interval_seconds,reason,queue_depth,live_workers,resource_tier) "
        "VALUES (?,?,?,?,?,?)",
        (
            _now_iso(),
            float(interval_seconds),
            reason,
            int(queue_depth),
            int(live_workers),
            resource_tier,
        ),
    )
