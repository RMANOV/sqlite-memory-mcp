"""Governance foundation for the debate ledger (C3 F0).

Standard library only.  This module never imports ``debate``, ``schema``,
``debate_roles`` or ``intel_server`` and performs no I/O at import time, so
hooks and tests can load it in isolation.  It owns canonical JSON,
domain-separated digests, the bounded strict payload decoder, migration
manifest normalization, the binding version fingerprint and the shared
legacy message serializer.

Nothing here grants authority.  Every function validates shape only; the
sole outcome for a structurally valid authorization on a topic without a
pinned authority is a typed refusal.  Historical or authority-looking text
is never promoted to a valid stored governance payload.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any, Mapping

GOVERNANCE_SCHEMA = "governance/v1"
MANIFEST_SCHEMA = "governance-migration/v1"
MAX_PAYLOAD_BYTES = 65536
MAX_MANIFEST_TARGETS = 128

DOMAIN_MANIFEST = "c3-manifest/v1"
DOMAIN_TARGET = "c3-target/v1"
DOMAIN_BINDING = "c3-binding/v1"
DOMAIN_TOPIC = "c3-topic/v1"

# Metadata fields under ``governance`` that only the server may write.
GOVERNANCE_SERVER_FIELDS = frozenset(
    {
        "mode",
        "authority_role",
        "authority_session_id",
        "authority_generation",
        "authority_epoch",
    }
)

AUTHORIZE_ACTIONS = frozenset(
    {
        "retire_binding",
        "replace_binding",
        "rotate_binding",
        "diagnostic_uncover",
        "diagnostic_post",
    }
)
DECISION_TYPES = frozenset(
    {"authorize", "approve_pin", "approve_manifest", "objection_ruling", "bootstrap_human"}
)
OBJECTION_KINDS = frozenset({"CHALLENGE", "REBUT", "CONCEDE", "ESCALATE"})
MANIFEST_CLAIM_POLICIES = frozenset({"hold", "retire"})
MANIFEST_KEYS = frozenset({"schema", "control_topic_id", "nonce", "targets"})
MANIFEST_TARGET_KEYS = frozenset(
    {
        "topic_id",
        "role",
        "session_id",
        "expected_generation",
        "expected_fingerprint",
        "action",
        "claims",
    }
)
_AUTHORIZE_INPUT_KEYS = frozenset(
    {
        "schema",
        "type",
        "action",
        "topic_id",
        "target_role",
        "target_session_id",
        "target_fingerprint",
        "effect",
        "expires_at",
        "nonce",
    }
)
_APPROVE_PIN_INPUT_KEYS = frozenset(
    {
        "schema",
        "type",
        "topic_id",
        "authority_role",
        "authority_session_id",
        "authority_generation",
        "expected_authority_epoch",
        "manifest_sha256",
        "expires_at",
        "nonce",
    }
)
_APPROVE_MANIFEST_INPUT_KEYS = frozenset(
    {"schema", "type", "control_topic_id", "manifest_sha256", "expires_at", "nonce"}
)
_OBJECTION_RULING_INPUT_KEYS = frozenset(
    {
        "schema",
        "type",
        "challenge_msg_id",
        "disposition",
        "paused_scope",
        "rationale",
        "evidence_refs",
    }
)
_ISSUER_KEYS = frozenset(
    {
        "topic_id",
        "role",
        "session_id",
        "binding_generation",
        "binding_fingerprint",
        "authority_epoch",
    }
)
_LEGACY_ENVELOPE_KEYS = ("protocol_version", "round_no", "body_mode", "payload_json")

_HEX16_RE = re.compile(r"[0-9a-f]{16}")
_HEX64_RE = re.compile(r"[0-9a-f]{64}")


class GovernanceError(Exception):
    """Typed validation failure; the DAO translates it to DebateError."""

    def __init__(
        self,
        error_type: str,
        message: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message or error_type)
        self.error_type = error_type
        self.details: dict[str, Any] = dict(details or {})


# ── Canonical form and digests ─────────────────────────────────────────────


def canonical_json(value: object) -> str:
    """Deterministic JSON text: sorted keys, compact separators, no NaN/inf."""
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def content_digest(domain: str, value: object) -> str:
    """SHA-256 over ``domain`` (ASCII) + NUL + canonical JSON (UTF-8)."""
    raw = domain.encode("ascii") + bytes([0]) + canonical_json(value).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


# ── Bounded strict decoder ─────────────────────────────────────────────────


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    obj: dict[str, Any] = {}
    for key, value in pairs:
        if key in obj:
            raise GovernanceError(
                "governance_duplicate_key", f"duplicate JSON key {key!r}", {"key": key}
            )
        obj[key] = value
    return obj


def _reject_constant(name: str) -> Any:
    raise GovernanceError(
        "governance_payload_invalid", f"non-finite JSON constant {name}", {"constant": name}
    )


def decode_governance_payload(raw: str) -> dict[str, Any]:
    """Decode a governance candidate strictly and without schema knowledge.

    Order matters and is a contract: the UTF-8 byte bound is measured on the
    text before parsing so an oversize payload can never be classified by its
    content; duplicate keys and non-finite constants are rejected during
    decoding; only a JSON object root is accepted.  Only decoder failures
    are translated; any other exception is a programming error and escapes.
    """
    if not isinstance(raw, str):
        raise GovernanceError("governance_payload_invalid", "payload must be JSON text")
    size = len(raw.encode("utf-8"))
    if size > MAX_PAYLOAD_BYTES:
        raise GovernanceError(
            "governance_payload_too_large",
            f"payload is {size} bytes; limit {MAX_PAYLOAD_BYTES}",
            {"bytes": size, "limit": MAX_PAYLOAD_BYTES},
        )
    try:
        value = json.loads(
            raw, object_pairs_hook=_reject_duplicate_keys, parse_constant=_reject_constant
        )
    except GovernanceError:
        raise
    except (json.JSONDecodeError, RecursionError):
        raise GovernanceError(
            "governance_payload_invalid", "payload is not valid JSON"
        ) from None
    if not isinstance(value, dict):
        raise GovernanceError(
            "governance_payload_invalid", "payload root must be a JSON object"
        )
    return value


# ── Migration manifest ─────────────────────────────────────────────────────


def _manifest_invalid(message: str, **details: Any) -> GovernanceError:
    return GovernanceError("manifest_invalid", message, details)


def normalize_migration_manifest(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Return the canonical manifest: exact keys, bounded, identity-sorted."""
    if not isinstance(manifest, Mapping):
        raise _manifest_invalid("manifest must be an object")
    if set(manifest) != MANIFEST_KEYS:
        raise _manifest_invalid(
            "manifest keys must be exactly schema/control_topic_id/nonce/targets",
            keys=sorted(str(key) for key in manifest),
        )
    if manifest["schema"] != MANIFEST_SCHEMA:
        raise _manifest_invalid("unsupported manifest schema", schema=manifest["schema"])
    control_topic_id = manifest["control_topic_id"]
    if not isinstance(control_topic_id, str) or not control_topic_id:
        raise _manifest_invalid("control_topic_id must be a non-empty string")
    nonce = manifest["nonce"]
    if not isinstance(nonce, str) or not _HEX16_RE.fullmatch(nonce):
        raise _manifest_invalid("nonce must be 16 lowercase hex characters")
    targets = manifest["targets"]
    if not isinstance(targets, list) or not targets:
        raise _manifest_invalid("targets must be a non-empty list")
    if len(targets) > MAX_MANIFEST_TARGETS:
        raise _manifest_invalid(
            "too many targets", count=len(targets), limit=MAX_MANIFEST_TARGETS
        )
    seen: set[tuple[str, str, str]] = set()
    normalized: list[dict[str, Any]] = []
    for index, target in enumerate(targets):
        if not isinstance(target, Mapping) or set(target) != MANIFEST_TARGET_KEYS:
            raise _manifest_invalid("target keys are not the exact contract", index=index)
        topic_id = target["topic_id"]
        role = target["role"]
        session_id = target["session_id"]
        for name, value in (
            ("topic_id", topic_id),
            ("role", role),
            ("session_id", session_id),
        ):
            if not isinstance(value, str) or not value:
                raise _manifest_invalid(
                    f"target {name} must be a non-empty string", index=index
                )
        generation = target["expected_generation"]
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
            raise _manifest_invalid(
                "expected_generation must be a positive integer", index=index
            )
        fingerprint = target["expected_fingerprint"]
        if not isinstance(fingerprint, str) or not _HEX64_RE.fullmatch(fingerprint):
            raise _manifest_invalid(
                "expected_fingerprint must be 64 hex characters", index=index
            )
        if target["action"] not in AUTHORIZE_ACTIONS:
            raise _manifest_invalid("unknown target action", index=index)
        if target["claims"] not in MANIFEST_CLAIM_POLICIES:
            raise _manifest_invalid("claims must be hold or retire", index=index)
        identity = (topic_id, role, session_id)
        if identity in seen:
            raise GovernanceError(
                "manifest_duplicate_target",
                "a migration target identity may occur only once",
                {"topic_id": topic_id, "role": role, "session_id": session_id},
            )
        seen.add(identity)
        normalized.append({key: target[key] for key in sorted(MANIFEST_TARGET_KEYS)})
    normalized.sort(key=lambda item: (item["topic_id"], item["role"], item["session_id"]))
    return {
        "schema": MANIFEST_SCHEMA,
        "control_topic_id": control_topic_id,
        "nonce": nonce,
        "targets": normalized,
    }


def validate_manifest_digest(manifest: Mapping[str, Any], digest: str) -> dict[str, Any]:
    """Normalize and require the approved canonical-manifest digest."""
    normalized = normalize_migration_manifest(manifest)
    actual = content_digest(DOMAIN_MANIFEST, normalized)
    if not isinstance(digest, str) or actual != digest:
        raise GovernanceError(
            "manifest_digest_mismatch",
            "canonical manifest digest does not match the approved digest",
            {"expected": digest, "actual": actual},
        )
    return normalized


def manifest_target_key(target: Mapping[str, Any]) -> str:
    """Per-target spend key: digest of one canonical target."""
    return content_digest(
        DOMAIN_TARGET, {key: target[key] for key in sorted(MANIFEST_TARGET_KEYS)}
    )


# ── Binding version ────────────────────────────────────────────────────────


@dataclasses.dataclass(frozen=True)
class BindingVersion:
    """Exact state of one debate_role_bindings row that a grant is bound to."""

    topic_id: str
    role: str
    session_id: str
    state: str
    generation: int
    updated_at: str


def binding_fingerprint(version: BindingVersion) -> str:
    return content_digest(DOMAIN_BINDING, dataclasses.asdict(version))


# ── Stored payload recognition and the shared serializer ───────────────────


def _is_issuer(value: Any) -> bool:
    return isinstance(value, Mapping) and _ISSUER_KEYS <= set(value)


def is_stored_governance_payload(payload_json: Any) -> bool:
    """True only for an exact, server-stamped governance/v1 stored form.

    Uses the same strict bounded decoder as posting (size, duplicate keys,
    non-finite constants, object root), so an oversize or ambiguous stored
    string is never promoted either.
    """
    if not isinstance(payload_json, str) or not payload_json:
        return False
    try:
        value = decode_governance_payload(payload_json)
    except GovernanceError:
        return False
    if value.get("schema") != GOVERNANCE_SCHEMA:
        return False
    kind = value.get("type")
    if kind == "authorize":
        return _AUTHORIZE_INPUT_KEYS <= set(value) and _is_issuer(value.get("issuer"))
    if kind in ("approve_pin", "approve_manifest", "objection_ruling", "bootstrap_human"):
        return {"expires_at", "nonce"} <= set(value) and _is_issuer(value.get("issuer"))
    if kind == "objection_act":
        return {"target", "evidence_refs"} <= set(value)
    return False


def serialize_debate_message(row: Mapping[str, Any]) -> dict[str, Any]:
    """One read shape for every public reader (read_messages, signal_check).

    debate/v1 rows are returned unchanged.  A legacy row keeps ``body_mode``
    and the canonical ``payload_json`` string only when the stored payload is
    a valid governance form; every other legacy row drops the v1 envelope
    exactly as before.  The input mapping is never mutated.
    """
    item = dict(row)
    if item.get("protocol_version") is not None:
        return item
    if is_stored_governance_payload(item.get("payload_json")):
        item.pop("protocol_version", None)
        item.pop("round_no", None)
        return item
    for key in _LEGACY_ENVELOPE_KEYS:
        item.pop(key, None)
    return item


# ── Legacy-topic governance candidates (post-time validation) ──────────────


def is_legacy_governance_candidate(kind: str, payload_json: Any) -> bool:
    """Every non-empty DECISION payload, or any payload with a governance/* schema.

    Classification never parses more than the strict decoder would accept: a
    payload over the byte bound that carries the governance marker is a
    candidate by size alone, so the bounded decoder rejects it as too large
    instead of this function parsing it in full.
    """
    if not isinstance(payload_json, str) or not payload_json.strip():
        return False
    if kind == "DECISION":
        return True
    if '"governance/' not in payload_json:
        return False
    if len(payload_json.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        return True
    try:
        value = json.loads(payload_json)
    except (json.JSONDecodeError, RecursionError):
        return False
    schema = value.get("schema") if isinstance(value, dict) else None
    return isinstance(schema, str) and schema.startswith("governance/")


def _require_exact_keys(
    payload: Mapping[str, Any], expected: frozenset[str], form: str
) -> None:
    keys = set(payload)
    if keys != expected:
        raise GovernanceError(
            "governance_payload_invalid",
            f"{form} payload keys are not the exact contract",
            {
                "form": form,
                "missing": sorted(expected - keys),
                "unexpected": sorted(keys - expected),
            },
        )


def _require_nonce_and_expiry(payload: Mapping[str, Any], form: str) -> None:
    nonce = payload.get("nonce")
    if not isinstance(nonce, str) or not _HEX16_RE.fullmatch(nonce):
        raise GovernanceError(
            "governance_payload_invalid", f"{form} nonce must be 16 hex characters"
        )
    expires_at = payload.get("expires_at")
    if not isinstance(expires_at, str) or not expires_at:
        # Shape check only in this phase; the timestamp is parsed and compared
        # against utc_now() where a grant is actually consumed (F1).
        raise GovernanceError(
            "governance_payload_invalid",
            f"{form} expires_at must be a non-empty string (UTC timestamp)",
        )


def _pinned_authority(conn: sqlite3.Connection, topic_id: str) -> dict[str, Any] | None:
    """Return the topic's pinned authority record, or None while in legacy mode.

    F0 has no pin operation, so this always resolves to None; F1's pin writes
    the record this reads.  A missing topic or unreadable metadata counts as
    unpinned (fail closed).  This is the only DB read in this module and it
    runs inside the caller's transaction.
    """
    row = conn.execute(
        "SELECT metadata_json FROM debates WHERE topic_id = ?", (topic_id,)
    ).fetchone()
    if row is None:
        return None
    raw = row[0]
    if not isinstance(raw, str) or not raw:
        return None
    try:
        metadata = json.loads(raw)
    except (json.JSONDecodeError, RecursionError):
        return None
    governance = metadata.get("governance") if isinstance(metadata, dict) else None
    if not isinstance(governance, dict) or governance.get("mode") != "authority":
        return None
    return governance


def validate_legacy_governance_post(
    conn: sqlite3.Connection,
    *,
    topic_id: str,
    role: str,
    kind: str,
    reply_to: str | None,
    payload_json: Any,
    body_mode: str | None,
    author_session_id: str | None,
    recipients: tuple[str, ...] | list[str] = (),
) -> dict[str, Any]:
    """Validate a governance candidate on an unconfigured (legacy) topic.

    ``conn`` is the caller's open write transaction (approved signature); in
    F0 it is read only to resolve the topic's governance mode.  Rejection
    order is the F0 contract: strict decode, body mode, exact schema,
    caller-supplied server fields, form/key shape.  A structurally valid form
    then requires a pinned authority; no topic can be pinned yet, so the
    result is a typed ``authority_unconfigured`` naming the bootstrap/pin
    path.  No positive path exists in F0 (F1 adds issuer stamping and
    persistence).
    """
    payload = decode_governance_payload(payload_json)
    mode = body_mode if body_mode not in (None, "") else "structured"
    if mode != "structured":
        raise GovernanceError(
            "governance_body_mode_invalid",
            "governance payloads require body_mode=structured",
            {"body_mode": body_mode},
        )
    schema = payload.get("schema")
    if schema != GOVERNANCE_SCHEMA:
        raise GovernanceError(
            "governance_schema_unsupported",
            f"unsupported governance schema {schema!r}",
            {"schema": schema, "supported": GOVERNANCE_SCHEMA},
        )
    if "issuer" in payload:
        raise GovernanceError(
            "governance_server_field_supplied",
            "issuer is stamped by the server and may not be supplied",
            {"field": "issuer"},
        )
    form = payload.get("type")
    if kind == "DECISION":
        if form not in DECISION_TYPES or form == "bootstrap_human":
            raise GovernanceError(
                "governance_payload_invalid",
                "unknown or reserved DECISION governance type",
                {"type": form},
            )
        if form == "authorize":
            _require_exact_keys(payload, _AUTHORIZE_INPUT_KEYS, form)
            if payload["action"] not in AUTHORIZE_ACTIONS:
                raise GovernanceError(
                    "governance_payload_invalid", "unknown authorize action"
                )
            if payload["topic_id"] != topic_id:
                raise GovernanceError(
                    "governance_payload_invalid",
                    "authorize topic_id must equal the posting topic",
                )
            if not isinstance(payload["effect"], Mapping):
                raise GovernanceError(
                    "governance_payload_invalid", "effect must be an object"
                )
            fingerprint = payload["target_fingerprint"]
            if not isinstance(fingerprint, str) or not _HEX64_RE.fullmatch(fingerprint):
                raise GovernanceError(
                    "governance_payload_invalid",
                    "target_fingerprint must be 64 hex characters",
                )
        elif form == "approve_pin":
            _require_exact_keys(payload, _APPROVE_PIN_INPUT_KEYS, form)
        elif form == "approve_manifest":
            _require_exact_keys(payload, _APPROVE_MANIFEST_INPUT_KEYS, form)
        else:
            _require_exact_keys(payload, _OBJECTION_RULING_INPUT_KEYS, form)
        if form != "objection_ruling":
            _require_nonce_and_expiry(payload, form)
    elif kind in OBJECTION_KINDS:
        if form != "objection_act":
            raise GovernanceError(
                "governance_payload_invalid",
                f"{kind} requires type objection_act",
                {"type": form},
            )
        if payload.get("target") != reply_to:
            raise GovernanceError(
                "governance_payload_invalid", "objection target must equal reply_to"
            )
        if not isinstance(payload.get("evidence_refs"), list):
            raise GovernanceError(
                "governance_payload_invalid", "evidence_refs must be a list"
            )
    else:
        raise GovernanceError(
            "governance_payload_invalid",
            f"kind {kind} cannot carry a governance payload",
            {"kind": kind},
        )
    if _pinned_authority(conn, topic_id) is None:
        raise GovernanceError(
            "authority_unconfigured",
            "no pinned governance authority on this topic; bootstrap a HUMAN "
            "binding and pin an authority before issuing governance decisions",
            {
                "topic_id": topic_id,
                "next": [
                    "debate_governance_inventory",
                    "debate_governance_bootstrap_human",
                    "debate_governance_pin",
                ],
            },
        )
    raise GovernanceError(
        "governance_action_not_implemented",
        "issuer stamping and persistence are not available in this phase",
        {"type": form},
    )


# ── Topic governance metadata (server-derived, candidate-only) ─────────────


def candidate_governance_metadata(
    roles: list[Mapping[str, Any]], governance_roles: frozenset[str]
) -> dict[str, Any]:
    """Legacy-mode metadata for a new topic; names a single candidate only.

    ``governance_roles`` is passed by the caller (debate.py owns the
    debate_roles import) so this module stays dependency-free.
    """
    names = [str(entry.get("role")) for entry in roles if isinstance(entry, Mapping)]
    candidates = [name for name in names if name in governance_roles]
    return {
        "mode": "legacy",
        "candidate_role": candidates[0] if len(candidates) == 1 else "",
        "authority_role": None,
        "authority_session_id": None,
        "authority_generation": None,
        "authority_epoch": 0,
    }


def reject_caller_governance(metadata: Mapping[str, Any] | None) -> None:
    """Refuse ANY caller-supplied ``governance`` key (never a raw pin).

    The whole record is server-derived; accepting a partial object and then
    overwriting it would be a silent fallback, so the key itself is refused
    whatever it contains.
    """
    if not isinstance(metadata, Mapping) or "governance" not in metadata:
        return
    governance = metadata.get("governance")
    supplied = (
        sorted(str(key) for key in governance) if isinstance(governance, Mapping) else []
    )
    raise GovernanceError(
        "governance_server_field_supplied",
        "the governance record is server-derived and may not be supplied by the caller",
        {"fields": supplied or ["governance"]},
    )
