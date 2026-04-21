"""Premium runtime boundary and entitlement gate for sqlite-memory-mcp.

This module intentionally contains only public-core enforcement code:
- premium feature registry
- entitlement verification hooks
- audit/revoke checks
- guarded loading of private premium extensions

It does NOT contain private keys, customer entitlements, or premium logic.
"""

from __future__ import annotations

import base64
import hashlib
import importlib
import importlib.util
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

from db_utils import (
    MACHINE_ID,
    get_conn as _get_conn,
    json_loads,
    now_iso as _now,
    record_memory_event,
    setup_logger,
)
from premium_contract import build_mount_context

logger = setup_logger("sqlite-premium", "premium_runtime.log")

_CONFIG_PATH = Path(__file__).parent / "premium_security_config.json"

_DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": True,
    "mount_private_extensions": True,
    "private_entrypoint_env_var": "SQLITE_MEMORY_PREMIUM_ENTRYPOINT",
    "entitlement_inline_env_var": "SQLITE_MEMORY_PREMIUM_ENTITLEMENT_JSON",
    "entitlement_path_env_var": "SQLITE_MEMORY_PREMIUM_ENTITLEMENT_PATH",
    "public_key_env_var": "SQLITE_MEMORY_PREMIUM_PUBLIC_KEY",
    "owner_approval_env_var": "SQLITE_MEMORY_OWNER_APPROVAL",
    "record_allowed_events": True,
    "record_denied_events": True,
}

PREMIUM_FEATURES: dict[str, dict[str, Any]] = {
    "private_extension_runtime": {
        "tier": "premium",
        "requires_owner_approval": True,
        "description": "Allows loading premium extensions from an external private repo/runtime.",
    },
    "acl_rbac": {
        "tier": "premium",
        "requires_owner_approval": False,
        "description": "Team/client access control and role-based authorization.",
    },
    "multi_mailbox_ingestion": {
        "tier": "premium",
        "requires_owner_approval": False,
        "description": "Cross-mailbox ingestion and client-scoped communication indexing.",
    },
    "partner_digest": {
        "tier": "premium",
        "requires_owner_approval": False,
        "description": "Partner-grade digests and management-level summary pipelines.",
    },
    "advanced_ranking": {
        "tier": "premium",
        "requires_owner_approval": False,
        "description": "Advanced ranking and retrieval orchestration beyond the public baseline.",
    },
    "governance_audit": {
        "tier": "premium",
        "requires_owner_approval": False,
        "description": "Extended governance, entitlement, and premium audit workflows.",
    },
    "memory_action_snapshots": {
        "tier": "premium",
        "requires_owner_approval": False,
        "description": "Action snapshots and operational memory checkpoints over client work.",
    },
    "client_history_notes": {
        "tier": "premium",
        "requires_owner_approval": False,
        "description": "Client-scoped history notes layered on top of the shared memory core.",
    },
    "canonical_facts": {
        "tier": "premium",
        "requires_owner_approval": False,
        "description": "Human-approved canonical facts stored with premium provenance.",
    },
    "provenance_pointers": {
        "tier": "premium",
        "requires_owner_approval": False,
        "description": "Premium provenance pointers across mail, notes, facts, and action layers.",
    },
    "query_templates": {
        "tier": "premium",
        "requires_owner_approval": False,
        "description": "Predefined premium memory queries for partner/operator workflows.",
    },
    "human_approved_notes": {
        "tier": "premium",
        "requires_owner_approval": False,
        "description": "Promotion of human-approved task/note content into premium memory layers.",
    },
    "task_signal_extraction": {
        "tier": "premium",
        "requires_owner_approval": False,
        "description": "Extraction of commitments, blockers, deadlines, and task signals into memory objects.",
    },
    "custom_design_tab": {
        "tier": "premium",
        "requires_owner_approval": False,
        "description": "Premium Custom Design operator surface spanning premium memory rows in the task tray.",
    },
    "instant_briefing": {
        "tier": "premium",
        "requires_owner_approval": False,
        "description": "20-second executive/client briefing over facts, history, commitments, and recent communication.",
        "depends_on": [
            "partner_digest",
            "advanced_ranking",
            "query_templates",
            "client_history_notes",
            "canonical_facts",
            "memory_action_snapshots",
            "multi_mailbox_ingestion",
            "governance_audit",
            "task_signal_extraction",
        ],
    },
    "commitment_radar": {
        "tier": "premium",
        "requires_owner_approval": False,
        "description": "Priority radar over commitments, deadlines, blockers, and unresolved follow-ups.",
        "depends_on": [
            "task_signal_extraction",
            "query_templates",
            "multi_mailbox_ingestion",
            "advanced_ranking",
        ],
    },
    "client_memory_twin": {
        "tier": "premium",
        "requires_owner_approval": False,
        "description": "Live client memory twin built from history notes, facts, snapshots, and communication context.",
        "depends_on": [
            "client_history_notes",
            "canonical_facts",
            "memory_action_snapshots",
            "provenance_pointers",
            "multi_mailbox_ingestion",
        ],
    },
    "decision_ledger": {
        "tier": "premium",
        "requires_owner_approval": False,
        "description": "Unified decision ledger over governance decisions, provenance, and human-approved memory promotion.",
        "depends_on": [
            "governance_audit",
            "provenance_pointers",
            "human_approved_notes",
            "canonical_facts",
        ],
    },
    "chief_of_staff_queries": {
        "tier": "premium",
        "requires_owner_approval": False,
        "description": "Chief-of-staff style memory questions over risk, dependency, chronology, and unresolved work.",
        "depends_on": [
            "query_templates",
            "advanced_ranking",
            "task_signal_extraction",
            "governance_audit",
            "multi_mailbox_ingestion",
        ],
    },
    "team_digest": {
        "tier": "premium",
        "requires_owner_approval": False,
        "description": "Team-facing digest surface that extends partner digests into operator and management handoff views.",
        "depends_on": [
            "partner_digest",
            "advanced_ranking",
            "query_templates",
            "multi_mailbox_ingestion",
            "governance_audit",
        ],
    },
    "silence_drift_detection": {
        "tier": "premium",
        "requires_owner_approval": False,
        "description": "Detection of stale threads, communication silence, and operational drift before commitments slip.",
        "depends_on": [
            "multi_mailbox_ingestion",
            "task_signal_extraction",
            "advanced_ranking",
        ],
    },
    "cross_mailbox_context": {
        "tier": "premium",
        "requires_owner_approval": False,
        "description": "Unified context across multiple mailboxes and memory layers for the same client or scope.",
        "depends_on": [
            "multi_mailbox_ingestion",
            "client_history_notes",
            "canonical_facts",
            "memory_action_snapshots",
        ],
    },
}

PREMIUM_PACKS: dict[str, dict[str, Any]] = {
    "access_governance": {
        "label": "Access and Governance",
        "description": "ACL/RBAC control plane plus auditable premium governance workflows.",
        "features": ["acl_rbac", "governance_audit"],
    },
    "communication_context": {
        "label": "Communication Context",
        "description": "Cross-mailbox ingestion and unified premium context across client communication layers.",
        "features": ["multi_mailbox_ingestion", "cross_mailbox_context"],
    },
    "client_memory_twin": {
        "label": "Client Memory Twin",
        "description": "Action snapshots, client history, canonical facts, and promoted human-approved memory.",
        "features": ["client_memory_twin", "human_approved_notes"],
    },
    "briefing_suite": {
        "label": "Instant Briefing Suite",
        "description": "Executive/client briefing, team digests, and chief-of-staff query surfaces.",
        "features": ["instant_briefing", "team_digest", "chief_of_staff_queries"],
    },
    "commitment_radar": {
        "label": "Commitment Radar",
        "description": "Signals, commitments, blockers, deadlines, and drift detection under operator pressure.",
        "features": ["commitment_radar", "silence_drift_detection"],
    },
    "decision_ledger": {
        "label": "Decision Ledger",
        "description": "Decision trail, provenance, and explainable premium review history.",
        "features": ["decision_ledger", "provenance_pointers"],
    },
    "custom_design_surface": {
        "label": "Custom Design Surface",
        "description": "Premium operator tray surface with parameterized views and premium row orchestration.",
        "features": ["custom_design_tab"],
    },
}


class PremiumRuntimeError(RuntimeError):
    """Raised when premium runtime loading or evaluation fails unexpectedly."""


def load_premium_config() -> dict[str, Any]:
    """Load config with safe defaults when the file is absent or invalid."""
    try:
        return {
            **_DEFAULT_CONFIG,
            **json_loads(_CONFIG_PATH.read_text(encoding="utf-8")),
        }
    except (FileNotFoundError, ValueError, OSError):
        logger.warning(
            "premium_security_config.json missing or invalid; using safe defaults"
        )
        return dict(_DEFAULT_CONFIG)


def _normalize_string_items(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    values: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str):
            continue
        cleaned = item.strip()
        if not cleaned:
            continue
        if cleaned in seen:
            continue
        seen.add(cleaned)
        values.append(cleaned)
    return values


def _expand_feature_dependencies(feature_ids: set[str]) -> set[str]:
    expanded = set(feature_ids)
    queue = list(feature_ids)
    while queue:
        feature_id = queue.pop()
        feature = PREMIUM_FEATURES.get(feature_id) or {}
        for dependency in _normalize_string_items(feature.get("depends_on", [])):
            if dependency in expanded or dependency not in PREMIUM_FEATURES:
                continue
            expanded.add(dependency)
            queue.append(dependency)
    return expanded


def resolve_entitlement_selection(entitlement: dict[str, Any]) -> dict[str, Any]:
    """Resolve pack + feature selection into the effective entitled feature set."""
    raw_feature_ids = _normalize_string_items(entitlement.get("features", []))
    raw_pack_ids = _normalize_string_items(entitlement.get("packs", []))
    wildcard_features = "*" in raw_feature_ids
    wildcard_packs = "*" in raw_pack_ids

    requested_features = [item for item in raw_feature_ids if item != "*"]
    requested_packs = [item for item in raw_pack_ids if item != "*"]
    unknown_features = sorted(
        feature_id
        for feature_id in requested_features
        if feature_id not in PREMIUM_FEATURES
    )
    unknown_packs = sorted(
        pack_id for pack_id in requested_packs if pack_id not in PREMIUM_PACKS
    )

    effective_pack_ids = (
        sorted(PREMIUM_PACKS.keys())
        if wildcard_packs
        else sorted(pack_id for pack_id in requested_packs if pack_id in PREMIUM_PACKS)
    )
    explicit_feature_ids = (
        sorted(PREMIUM_FEATURES.keys())
        if wildcard_features
        else sorted(
            feature_id
            for feature_id in requested_features
            if feature_id in PREMIUM_FEATURES
        )
    )

    effective_feature_ids = set(explicit_feature_ids)
    for pack_id in effective_pack_ids:
        effective_feature_ids.update(
            _normalize_string_items(PREMIUM_PACKS[pack_id].get("features", []))
        )
    effective_feature_ids = _expand_feature_dependencies(effective_feature_ids)
    has_selection = bool(
        wildcard_features
        or wildcard_packs
        or effective_feature_ids
        or effective_pack_ids
    )
    if has_selection:
        effective_feature_ids.add("private_extension_runtime")

    return {
        "selection_mode": (
            "packs_and_features"
            if effective_pack_ids and explicit_feature_ids
            else "packs"
            if effective_pack_ids
            else "features"
            if explicit_feature_ids or wildcard_features
            else "none"
        ),
        "requested_packs": requested_packs,
        "selected_packs": effective_pack_ids,
        "explicit_features": explicit_feature_ids,
        "effective_features": sorted(effective_feature_ids),
        "unknown_features": unknown_features,
        "unknown_packs": unknown_packs,
        "wildcard": bool(wildcard_features or wildcard_packs),
        "has_selection": has_selection,
    }


def build_mount_runtime_config(
    *,
    base_config: dict[str, Any] | None = None,
    selection: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Attach entitlement selection metadata to the mount context config."""
    payload = dict(base_config or load_premium_config())
    payload["_premium_selection"] = dict(selection or {})
    payload["_premium_pack_catalog"] = {
        pack_id: dict(pack) for pack_id, pack in PREMIUM_PACKS.items()
    }
    return payload


def _new_id(prefix: str) -> str:
    raw = f"{prefix}:{_now()}:{os.getpid()}:{MACHINE_ID}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _json_text(payload: Any) -> str | None:
    if payload is None:
        return None
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _hash_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _parse_iso(ts: str | None) -> str | None:
    if not ts:
        return None
    return ts.strip()


def _canonical_entitlement_payload(entitlement: dict[str, Any]) -> bytes:
    payload = dict(entitlement)
    payload.pop("signature", None)
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _load_public_key_value(config: dict[str, Any]) -> str | None:
    env_var = str(config.get("public_key_env_var") or "").strip()
    if not env_var:
        return None
    value = os.environ.get(env_var, "").strip()
    return value or None


def _load_entitlement(config: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    inline_env = str(config.get("entitlement_inline_env_var") or "").strip()
    if inline_env:
        inline_value = os.environ.get(inline_env, "").strip()
        if inline_value:
            return json.loads(inline_value), "env:inline"

    path_env = str(config.get("entitlement_path_env_var") or "").strip()
    if path_env:
        raw_path = os.environ.get(path_env, "").strip()
        if raw_path:
            path = Path(raw_path)
            return json.loads(path.read_text(encoding="utf-8")), str(path)

    return None, "missing"


def _decode_signature(value: str) -> bytes:
    try:
        return base64.b64decode(value.encode("utf-8"), validate=True)
    except Exception as exc:
        raise PremiumRuntimeError(f"Invalid signature encoding: {exc}") from exc


def _verify_entitlement_signature(
    entitlement: dict[str, Any],
    public_key_value: str | None,
) -> tuple[bool, str]:
    signature = entitlement.get("signature")
    if not isinstance(signature, dict):
        return False, "signature_missing"
    if str(signature.get("alg", "")).lower() != "ed25519":
        return False, "unsupported_signature_algorithm"
    if not public_key_value:
        return False, "public_key_missing"

    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PublicKey,
        )
    except ImportError:
        return False, "cryptography_missing"

    material = public_key_value.strip().encode("utf-8")
    try:
        if b"BEGIN PUBLIC KEY" in material:
            public_key = serialization.load_pem_public_key(material)
        else:
            public_key = Ed25519PublicKey.from_public_bytes(
                base64.b64decode(material, validate=True)
            )
        public_key.verify(
            _decode_signature(str(signature.get("value", ""))),
            _canonical_entitlement_payload(entitlement),
        )
        return True, "signature_valid"
    except Exception as exc:
        return False, f"signature_invalid:{exc.__class__.__name__}"


def _write_gate_audit(
    conn: sqlite3.Connection,
    *,
    feature_id: str,
    decision: str,
    reason: str,
    entitlement_id: str | None = None,
    customer_id: str | None = None,
    server_name: str | None = None,
    tool_name: str | None = None,
    actor_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    checked_at = _now()
    payload_json = _json_text(payload)
    conn.execute(
        "INSERT INTO premium_gate_audit ("
        "audit_id, feature_id, decision, reason, entitlement_id, customer_id, "
        "server_name, tool_name, actor_id, machine_id, checked_at, payload_json"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            _new_id("pg"),
            feature_id,
            decision,
            reason,
            entitlement_id,
            customer_id,
            server_name,
            tool_name,
            actor_id,
            MACHINE_ID,
            checked_at,
            payload_json,
        ),
    )
    event_type = f"premium_gate_{decision}"
    record_memory_event(
        conn,
        event_type=event_type,
        aggregate_kind="premium_feature",
        aggregate_id=feature_id,
        actor_type="system",
        actor_id=actor_id,
        tool_name=tool_name or "sqlite-premium",
        event_ts=checked_at,
        new_value={
            "feature_id": feature_id,
            "decision": decision,
            "reason": reason,
            "entitlement_id": entitlement_id,
            "customer_id": customer_id,
            "server_name": server_name,
        },
        payload=payload,
        source_kind="premium_runtime",
        source_ref=feature_id,
    )


def _is_revoked(
    conn: sqlite3.Connection,
    *,
    entitlement_id: str,
    feature_id: str,
) -> bool:
    row = conn.execute(
        "SELECT 1 FROM premium_revocations "
        "WHERE entitlement_id = ? AND active = 1 AND (feature_id IS NULL OR feature_id = ?) "
        "LIMIT 1",
        (entitlement_id, feature_id),
    ).fetchone()
    return bool(row)


def revoke_entitlement(
    conn: sqlite3.Connection,
    *,
    entitlement_id: str,
    feature_id: str | None = None,
    customer_id: str | None = None,
    reason: str = "",
    revoked_by: str | None = None,
) -> str:
    """Persist a local revocation entry.

    This is intentionally an internal helper for future admin/service use.
    """
    revocation_id = _new_id("rv")
    revoked_at = _now()
    conn.execute(
        "INSERT INTO premium_revocations ("
        "revocation_id, entitlement_id, feature_id, customer_id, reason, revoked_at, revoked_by, active"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
        (
            revocation_id,
            entitlement_id,
            feature_id,
            customer_id,
            reason or None,
            revoked_at,
            revoked_by,
        ),
    )
    record_memory_event(
        conn,
        event_type="premium_revoked",
        aggregate_kind="premium_entitlement",
        aggregate_id=entitlement_id,
        actor_type="system",
        actor_id=revoked_by,
        tool_name="sqlite-premium.revoke_entitlement",
        event_ts=revoked_at,
        new_value={
            "entitlement_id": entitlement_id,
            "feature_id": feature_id,
            "customer_id": customer_id,
            "reason": reason,
        },
        source_kind="premium_runtime",
        source_ref=entitlement_id,
    )
    return revocation_id


def evaluate_feature_gate(
    conn: sqlite3.Connection,
    *,
    feature_id: str,
    server_name: str | None = None,
    tool_name: str | None = None,
    actor_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate whether a premium feature may run on this machine.

    Deny by default unless a valid entitlement and owner approval are present.
    """
    config = load_premium_config()
    feature = PREMIUM_FEATURES.get(feature_id)
    if not feature:
        verdict = {
            "allowed": False,
            "decision": "denied",
            "reason": "unknown_feature",
            "feature_id": feature_id,
        }
        _write_gate_audit(
            conn,
            feature_id=feature_id,
            decision="denied",
            reason="unknown_feature",
            server_name=server_name,
            tool_name=tool_name,
            actor_id=actor_id,
            payload=payload,
        )
        return verdict

    if str(feature.get("tier", "premium")).lower() != "premium":
        return {
            "allowed": True,
            "decision": "allowed",
            "reason": "non_premium_feature",
            "feature_id": feature_id,
        }

    if not config.get("enabled", True):
        verdict = {
            "allowed": False,
            "decision": "denied",
            "reason": "premium_runtime_disabled",
            "feature_id": feature_id,
        }
        if config.get("record_denied_events", True):
            _write_gate_audit(
                conn,
                feature_id=feature_id,
                decision="denied",
                reason=verdict["reason"],
                server_name=server_name,
                tool_name=tool_name,
                actor_id=actor_id,
                payload=payload,
            )
        return verdict

    try:
        entitlement, source_ref = _load_entitlement(config)
    except Exception as exc:
        verdict = {
            "allowed": False,
            "decision": "denied",
            "reason": f"entitlement_load_failed:{exc.__class__.__name__}",
            "feature_id": feature_id,
        }
        if config.get("record_denied_events", True):
            _write_gate_audit(
                conn,
                feature_id=feature_id,
                decision="denied",
                reason=verdict["reason"],
                server_name=server_name,
                tool_name=tool_name,
                actor_id=actor_id,
                payload={**(payload or {}), "entitlement_source": source_ref},
            )
        return verdict

    if not entitlement:
        verdict = {
            "allowed": False,
            "decision": "denied",
            "reason": "entitlement_missing",
            "feature_id": feature_id,
        }
        if config.get("record_denied_events", True):
            _write_gate_audit(
                conn,
                feature_id=feature_id,
                decision="denied",
                reason=verdict["reason"],
                server_name=server_name,
                tool_name=tool_name,
                actor_id=actor_id,
                payload={**(payload or {}), "entitlement_source": source_ref},
            )
        return verdict

    entitlement_id = str(entitlement.get("entitlement_id") or "").strip()
    customer_id = str(entitlement.get("customer_id") or "").strip() or None
    if not entitlement_id:
        verdict = {
            "allowed": False,
            "decision": "denied",
            "reason": "entitlement_id_missing",
            "feature_id": feature_id,
        }
        if config.get("record_denied_events", True):
            _write_gate_audit(
                conn,
                feature_id=feature_id,
                decision="denied",
                reason=verdict["reason"],
                customer_id=customer_id,
                server_name=server_name,
                tool_name=tool_name,
                actor_id=actor_id,
                payload={**(payload or {}), "entitlement_source": source_ref},
            )
        return verdict

    sig_ok, sig_reason = _verify_entitlement_signature(
        entitlement,
        _load_public_key_value(config),
    )
    if not sig_ok:
        verdict = {
            "allowed": False,
            "decision": "denied",
            "reason": sig_reason,
            "feature_id": feature_id,
            "entitlement_id": entitlement_id,
            "customer_id": customer_id,
        }
        if config.get("record_denied_events", True):
            _write_gate_audit(
                conn,
                feature_id=feature_id,
                decision="denied",
                reason=sig_reason,
                entitlement_id=entitlement_id,
                customer_id=customer_id,
                server_name=server_name,
                tool_name=tool_name,
                actor_id=actor_id,
                payload={**(payload or {}), "entitlement_source": source_ref},
            )
        return verdict

    if _is_revoked(conn, entitlement_id=entitlement_id, feature_id=feature_id):
        verdict = {
            "allowed": False,
            "decision": "denied",
            "reason": "entitlement_revoked",
            "feature_id": feature_id,
            "entitlement_id": entitlement_id,
            "customer_id": customer_id,
        }
        if config.get("record_denied_events", True):
            _write_gate_audit(
                conn,
                feature_id=feature_id,
                decision="denied",
                reason=verdict["reason"],
                entitlement_id=entitlement_id,
                customer_id=customer_id,
                server_name=server_name,
                tool_name=tool_name,
                actor_id=actor_id,
                payload={**(payload or {}), "entitlement_source": source_ref},
            )
        return verdict

    selection = resolve_entitlement_selection(entitlement)
    if not selection.get("has_selection"):
        verdict = {
            "allowed": False,
            "decision": "denied",
            "reason": "entitlement_selection_missing",
            "feature_id": feature_id,
            "entitlement_id": entitlement_id,
            "customer_id": customer_id,
            "selection_mode": selection.get("selection_mode"),
        }
        if config.get("record_denied_events", True):
            _write_gate_audit(
                conn,
                feature_id=feature_id,
                decision="denied",
                reason=verdict["reason"],
                entitlement_id=entitlement_id,
                customer_id=customer_id,
                server_name=server_name,
                tool_name=tool_name,
                actor_id=actor_id,
                payload={
                    **(payload or {}),
                    "entitlement_source": source_ref,
                    "selection_mode": selection.get("selection_mode"),
                },
            )
        return verdict

    effective_features = set(selection.get("effective_features", []))
    if feature_id not in effective_features:
        verdict = {
            "allowed": False,
            "decision": "denied",
            "reason": "feature_not_entitled",
            "feature_id": feature_id,
            "entitlement_id": entitlement_id,
            "customer_id": customer_id,
            "selection_mode": selection.get("selection_mode"),
            "selected_packs": selection.get("selected_packs", []),
            "effective_features": selection.get("effective_features", []),
        }
        if config.get("record_denied_events", True):
            _write_gate_audit(
                conn,
                feature_id=feature_id,
                decision="denied",
                reason=verdict["reason"],
                entitlement_id=entitlement_id,
                customer_id=customer_id,
                server_name=server_name,
                tool_name=tool_name,
                actor_id=actor_id,
                payload={
                    **(payload or {}),
                    "entitlement_source": source_ref,
                    "selection_mode": selection.get("selection_mode"),
                    "selected_packs": selection.get("selected_packs", []),
                },
            )
        return verdict

    now_ts = _now()
    not_before = _parse_iso(entitlement.get("not_before"))
    expires_at = _parse_iso(entitlement.get("expires_at"))
    if not_before and now_ts < not_before:
        verdict = {
            "allowed": False,
            "decision": "denied",
            "reason": "entitlement_not_yet_valid",
            "feature_id": feature_id,
            "entitlement_id": entitlement_id,
            "customer_id": customer_id,
        }
        if config.get("record_denied_events", True):
            _write_gate_audit(
                conn,
                feature_id=feature_id,
                decision="denied",
                reason=verdict["reason"],
                entitlement_id=entitlement_id,
                customer_id=customer_id,
                server_name=server_name,
                tool_name=tool_name,
                actor_id=actor_id,
                payload=payload,
            )
        return verdict
    if expires_at and now_ts > expires_at:
        verdict = {
            "allowed": False,
            "decision": "denied",
            "reason": "entitlement_expired",
            "feature_id": feature_id,
            "entitlement_id": entitlement_id,
            "customer_id": customer_id,
        }
        if config.get("record_denied_events", True):
            _write_gate_audit(
                conn,
                feature_id=feature_id,
                decision="denied",
                reason=verdict["reason"],
                entitlement_id=entitlement_id,
                customer_id=customer_id,
                server_name=server_name,
                tool_name=tool_name,
                actor_id=actor_id,
                payload=payload,
            )
        return verdict

    machine_ids = entitlement.get("machine_ids", [])
    if machine_ids and MACHINE_ID not in machine_ids:
        verdict = {
            "allowed": False,
            "decision": "denied",
            "reason": "machine_not_entitled",
            "feature_id": feature_id,
            "entitlement_id": entitlement_id,
            "customer_id": customer_id,
        }
        if config.get("record_denied_events", True):
            _write_gate_audit(
                conn,
                feature_id=feature_id,
                decision="denied",
                reason=verdict["reason"],
                entitlement_id=entitlement_id,
                customer_id=customer_id,
                server_name=server_name,
                tool_name=tool_name,
                actor_id=actor_id,
                payload=payload,
            )
        return verdict

    if feature.get("requires_owner_approval"):
        approval_env = str(config.get("owner_approval_env_var") or "").strip()
        approval_value = (
            os.environ.get(approval_env, "").strip() if approval_env else ""
        )
        approval_hash = str(entitlement.get("owner_approval_sha256") or "").strip()
        if not approval_value or not approval_hash:
            verdict = {
                "allowed": False,
                "decision": "denied",
                "reason": "owner_approval_missing",
                "feature_id": feature_id,
                "entitlement_id": entitlement_id,
                "customer_id": customer_id,
            }
            if config.get("record_denied_events", True):
                _write_gate_audit(
                    conn,
                    feature_id=feature_id,
                    decision="denied",
                    reason=verdict["reason"],
                    entitlement_id=entitlement_id,
                    customer_id=customer_id,
                    server_name=server_name,
                    tool_name=tool_name,
                    actor_id=actor_id,
                    payload=payload,
                )
            return verdict
        if _hash_text(approval_value) != approval_hash:
            verdict = {
                "allowed": False,
                "decision": "denied",
                "reason": "owner_approval_invalid",
                "feature_id": feature_id,
                "entitlement_id": entitlement_id,
                "customer_id": customer_id,
            }
            if config.get("record_denied_events", True):
                _write_gate_audit(
                    conn,
                    feature_id=feature_id,
                    decision="denied",
                    reason=verdict["reason"],
                    entitlement_id=entitlement_id,
                    customer_id=customer_id,
                    server_name=server_name,
                    tool_name=tool_name,
                    actor_id=actor_id,
                    payload=payload,
                )
            return verdict

    verdict = {
        "allowed": True,
        "decision": "allowed",
        "reason": "entitlement_valid",
        "feature_id": feature_id,
        "entitlement_id": entitlement_id,
        "customer_id": customer_id,
        "selection_mode": selection.get("selection_mode"),
        "selected_packs": selection.get("selected_packs", []),
        "effective_features": selection.get("effective_features", []),
    }
    if config.get("record_allowed_events", True):
        _write_gate_audit(
            conn,
            feature_id=feature_id,
            decision="allowed",
            reason=verdict["reason"],
            entitlement_id=entitlement_id,
            customer_id=customer_id,
            server_name=server_name,
            tool_name=tool_name,
            actor_id=actor_id,
            payload={
                **(payload or {}),
                "selection_mode": selection.get("selection_mode"),
                "selected_packs": selection.get("selected_packs", []),
            },
        )
    return verdict


def _entrypoint_fingerprint(entrypoint: str) -> str:
    return _hash_text(entrypoint)


def _resolve_module_from_entrypoint(entrypoint: str):
    """Load premium module from module path or file path.

    Supported formats:
    - ``package.module``
    - ``package.module:register``
    - ``C:\\secure\\premium_runtime.py``
    - ``C:\\secure\\premium_runtime.py::register``
    """
    attr_name = None
    if "::" in entrypoint:
        path_text, attr_name = entrypoint.rsplit("::", 1)
        path = Path(path_text)
        if not path.exists():
            raise PremiumRuntimeError(f"Premium entrypoint path not found: {path}")
        module_name = (
            "_sqlite_memory_premium_"
            + hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:12]
        )
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise PremiumRuntimeError(f"Cannot load premium module spec from: {path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module, attr_name

    path = Path(entrypoint)
    if entrypoint.lower().endswith(".py") or path.exists():
        if not path.exists():
            raise PremiumRuntimeError(f"Premium entrypoint path not found: {path}")
        module_name = (
            "_sqlite_memory_premium_"
            + hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:12]
        )
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise PremiumRuntimeError(f"Cannot load premium module spec from: {path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module, None

    if ":" in entrypoint:
        module_name, attr_name = entrypoint.rsplit(":", 1)
        return importlib.import_module(module_name), attr_name
    return importlib.import_module(entrypoint), None


def _register_loaded_module(
    module: Any,
    mcp: Any,
    server_name: str,
    attr_name: str | None,
    *,
    mount_config: dict[str, Any] | None = None,
):
    mount_context = build_mount_context(
        server_name=server_name,
        feature_id="private_extension_runtime",
        machine_id=MACHINE_ID,
        config=build_mount_runtime_config(base_config=mount_config),
    )
    if attr_name:
        target = getattr(module, attr_name, None)
        if target is None:
            raise PremiumRuntimeError(
                f"Premium module {module!r} does not expose attribute {attr_name!r}"
            )
    elif hasattr(module, "register_premium_extensions"):
        target = getattr(module, "register_premium_extensions")
    elif hasattr(module, "register"):
        target = getattr(module, "register")
    elif hasattr(module, "mcp"):
        target = getattr(module, "mcp")
    else:
        raise PremiumRuntimeError(
            "Premium module must expose register_premium_extensions(), "
            "register(), or mcp"
        )

    if callable(target):
        try:
            return target(
                mcp,
                server_name=server_name,
                mount_context=mount_context,
            )
        except TypeError:
            try:
                return target(mcp, server_name=server_name)
            except TypeError:
                return target(mcp)
    if hasattr(mcp, "mount"):
        mcp.mount(target)
        return {"mounted": True}
    raise PremiumRuntimeError("Unsupported premium registration target")


def maybe_mount_premium_extensions(mcp: Any, *, server_name: str) -> dict[str, Any]:
    """Attempt to load private premium extensions behind the runtime gate.

    Safe default behavior:
    - no entrypoint configured -> do nothing
    - invalid/missing entitlement -> deny and keep OSS server running
    - load error -> audit failure and keep OSS server running
    """
    config = load_premium_config()
    if not config.get("enabled", True):
        return {"status": "disabled", "reason": "premium_runtime_disabled"}
    if not config.get("mount_private_extensions", True):
        return {"status": "disabled", "reason": "premium_mounting_disabled"}

    entry_env = str(config.get("private_entrypoint_env_var") or "").strip()
    entrypoint = os.environ.get(entry_env, "").strip() if entry_env else ""
    if not entrypoint:
        return {"status": "skipped", "reason": "no_private_entrypoint"}

    payload = {
        "server_name": server_name,
        "entrypoint_ref": _entrypoint_fingerprint(entrypoint),
    }
    with _get_conn() as conn:
        verdict = evaluate_feature_gate(
            conn,
            feature_id="private_extension_runtime",
            server_name=server_name,
            tool_name=f"{server_name}.premium_runtime",
            actor_id=server_name,
            payload=payload,
        )
        if not verdict.get("allowed"):
            logger.warning(
                "Premium runtime denied for %s: %s",
                server_name,
                verdict.get("reason"),
            )
            return {"status": "denied", **verdict}

    try:
        module, attr_name = _resolve_module_from_entrypoint(entrypoint)
        result = _register_loaded_module(
            module,
            mcp,
            server_name,
            attr_name,
            mount_config=build_mount_runtime_config(
                base_config=config,
                selection={
                    key: value
                    for key, value in verdict.items()
                    if key in {"selection_mode", "selected_packs", "effective_features"}
                },
            ),
        )
        logger.info("Premium runtime loaded for %s from %s", server_name, entry_env)
        with _get_conn() as conn:
            _write_gate_audit(
                conn,
                feature_id="private_extension_runtime",
                decision="load_success",
                reason="premium_extensions_loaded",
                server_name=server_name,
                tool_name=f"{server_name}.premium_runtime",
                actor_id=server_name,
                payload=payload,
            )
        return {"status": "loaded", "server_name": server_name, "result": result}
    except Exception as exc:
        logger.error(
            "Premium runtime load failed for %s: %s", server_name, exc, exc_info=True
        )
        with _get_conn() as conn:
            _write_gate_audit(
                conn,
                feature_id="private_extension_runtime",
                decision="load_failed",
                reason=f"load_failed:{exc.__class__.__name__}",
                server_name=server_name,
                tool_name=f"{server_name}.premium_runtime",
                actor_id=server_name,
                payload=payload,
            )
        return {
            "status": "load_failed",
            "server_name": server_name,
            "reason": str(exc),
        }
