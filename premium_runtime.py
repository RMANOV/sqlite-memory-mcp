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
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import tomllib

from db_utils import (
    MACHINE_ID,
    get_conn as _get_conn,
    json_loads,
    now_iso as _now,
    record_memory_event,
    setup_logger,
)
from premium_contract import PREMIUM_RUNTIME_CONTRACT_VERSION, build_mount_context

logger = setup_logger("sqlite-premium", "premium_runtime.log")

_CONFIG_PATH = Path(__file__).parent / "premium_security_config.json"

_DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": True,
    "mount_private_extensions": True,
    "private_entrypoint_env_var": "SQLITE_MEMORY_PREMIUM_ENTRYPOINT",
    "entitlement_inline_env_var": "SQLITE_MEMORY_PREMIUM_ENTITLEMENT_JSON",
    "entitlement_path_env_var": "SQLITE_MEMORY_PREMIUM_ENTITLEMENT_PATH",
    "entitlement_url_env_var": "SQLITE_MEMORY_PREMIUM_ENTITLEMENT_URL",
    "public_key_env_var": "SQLITE_MEMORY_PREMIUM_PUBLIC_KEY",
    "artifact_manifest_inline_env_var": "SQLITE_MEMORY_PREMIUM_ARTIFACT_MANIFEST_JSON",
    "artifact_manifest_path_env_var": "SQLITE_MEMORY_PREMIUM_ARTIFACT_MANIFEST_PATH",
    "artifact_manifest_url_env_var": "SQLITE_MEMORY_PREMIUM_ARTIFACT_MANIFEST_URL",
    "artifact_public_key_env_var": "SQLITE_MEMORY_PREMIUM_ARTIFACT_PUBLIC_KEY",
    "require_artifact_manifest": False,
    "remote_headers_inline_env_var": "SQLITE_MEMORY_PREMIUM_REMOTE_HEADERS_JSON",
    "remote_headers_path_env_var": "SQLITE_MEMORY_PREMIUM_REMOTE_HEADERS_PATH",
    "remote_timeout_seconds": 5,
    "control_plane_inline_env_var": "SQLITE_MEMORY_PREMIUM_POLICY_JSON",
    "control_plane_path_env_var": "SQLITE_MEMORY_PREMIUM_POLICY_PATH",
    "control_plane_url_env_var": "SQLITE_MEMORY_PREMIUM_POLICY_URL",
    "control_plane_public_key_env_var": "SQLITE_MEMORY_PREMIUM_POLICY_PUBLIC_KEY",
    "control_plane_timeout_seconds": 5,
    "control_plane_cache_ttl_seconds": 21600,
    "control_plane_required": True,
    "allow_cached_control_plane": True,
    "max_offline_grace_seconds": 604800,
    "minimum_protection_phase": 1,
    "require_machine_binding": True,
    "installation_salt_env_var": "SQLITE_MEMORY_PREMIUM_INSTALLATION_SALT",
    "owner_approval_env_var": "SQLITE_MEMORY_OWNER_APPROVAL",
    "debate_protocol_gate_enabled": False,
    "debate_protocol_gate_enabled_env_var": "SQLITE_MEMORY_DEBATE_GATE_ENABLED",
    "debate_protocol_gate_disabled_env_var": "SQLITE_MEMORY_DEBATE_GATE_DISABLED",
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
    "debate_protocol": {
        "tier": "premium",
        "requires_owner_approval": False,
        "description": "Premium gate for creating new debate protocol topics.",
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
    "password_protected_views": {
        "tier": "premium",
        "requires_owner_approval": False,
        "description": "Password-protected premium operator views for especially sensitive tray surfaces.",
        "depends_on": ["custom_design_tab"],
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
    "protected_operator_surface": {
        "label": "Protected Operator Surface",
        "description": "Password-protected premium views layered onto the Custom Design operator surface.",
        "features": ["password_protected_views"],
    },
}


class PremiumRuntimeError(RuntimeError):
    """Raised when premium runtime loading or evaluation fails unexpectedly."""


def _load_host_runtime_version() -> str:
    try:
        payload = tomllib.loads(
            (Path(__file__).parent / "pyproject.toml").read_text(encoding="utf-8")
        )
        version = payload.get("project", {}).get("version")
        if isinstance(version, str) and version.strip():
            return version.strip()
    except Exception:
        logger.warning("Unable to resolve host runtime version from pyproject.toml")
    return "0.0.0"


HOST_RUNTIME_VERSION = _load_host_runtime_version()


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


def _truthy_env(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _config_env_flag(config: dict[str, Any], config_key: str) -> bool:
    env_name = str(config.get(config_key) or "").strip()
    return _truthy_env(os.environ.get(env_name)) if env_name else False


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
    manifest: dict[str, Any] | None = None,
    control_policy: dict[str, Any] | None = None,
    installation_fingerprint: str = "",
    protection_phase: int = 1,
) -> dict[str, Any]:
    """Attach entitlement selection metadata to the mount context config."""
    payload = dict(base_config or load_premium_config())
    payload["_premium_selection"] = dict(selection or {})
    payload["_premium_pack_catalog"] = {
        pack_id: dict(pack) for pack_id, pack in PREMIUM_PACKS.items()
    }
    payload["_premium_host_runtime_version"] = HOST_RUNTIME_VERSION
    payload["_premium_installation_fingerprint"] = installation_fingerprint or ""
    payload["_premium_protection_phase"] = int(protection_phase or 1)
    if manifest:
        payload["_premium_artifact_manifest"] = dict(manifest)
    if control_policy:
        payload["_premium_control_policy"] = dict(control_policy)
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


def _canonical_signed_payload(
    payload: dict[str, Any],
    *,
    signature_field: str = "signature",
) -> bytes:
    sanitized = dict(payload)
    sanitized.pop(signature_field, None)
    return json.dumps(
        sanitized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _load_env_var_value(config: dict[str, Any], config_key: str) -> str | None:
    env_var = str(config.get(config_key) or "").strip()
    if not env_var:
        return None
    value = os.environ.get(env_var, "").strip()
    return value or None


def _load_json_from_env_or_path(
    config: dict[str, Any],
    *,
    inline_key: str,
    path_key: str,
    url_key: str | None = None,
) -> tuple[dict[str, Any] | None, str]:
    inline_env = str(config.get(inline_key) or "").strip()
    if inline_env:
        inline_value = os.environ.get(inline_env, "").strip()
        if inline_value:
            return json.loads(inline_value), "env:inline"

    path_env = str(config.get(path_key) or "").strip()
    if path_env:
        raw_path = os.environ.get(path_env, "").strip()
        if raw_path:
            path = Path(raw_path)
            return json.loads(path.read_text(encoding="utf-8")), str(path)

    if url_key:
        url = _load_env_var_value(config, url_key)
        if url:
            headers = _load_remote_headers(config)
            timeout_seconds = int(config.get("remote_timeout_seconds") or 5)
            return (
                _load_remote_json(
                    url, timeout_seconds=timeout_seconds, headers=headers
                ),
                url,
            )

    return None, "missing"


def _load_entitlement(config: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    return _load_json_from_env_or_path(
        config,
        inline_key="entitlement_inline_env_var",
        path_key="entitlement_path_env_var",
        url_key="entitlement_url_env_var",
    )


def _load_artifact_manifest(
    config: dict[str, Any],
) -> tuple[dict[str, Any] | None, str]:
    return _load_json_from_env_or_path(
        config,
        inline_key="artifact_manifest_inline_env_var",
        path_key="artifact_manifest_path_env_var",
        url_key="artifact_manifest_url_env_var",
    )


def _load_remote_headers(config: dict[str, Any]) -> dict[str, str]:
    payload, _source = _load_json_from_env_or_path(
        config,
        inline_key="remote_headers_inline_env_var",
        path_key="remote_headers_path_env_var",
    )
    if not isinstance(payload, dict):
        return {}
    headers: dict[str, str] = {}
    for key, value in payload.items():
        if not isinstance(key, str) or not isinstance(value, str):
            continue
        header_key = key.strip()
        header_value = value.strip()
        if not header_key or not header_value:
            continue
        headers[header_key] = header_value
    return headers


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Disallow redirects on premium authority fetches.

    Premium entitlement / manifest / policy endpoints are pinned URLs backed by
    the owner's own control plane. An HTTP 3xx here is either misconfiguration or
    hijack — both must fail closed, never silently follow to another host.
    """

    def http_error_301(self, req, fp, code, msg, headers):
        raise PremiumRuntimeError(
            f"Remote premium fetch refused redirect {code} to {headers.get('Location', '?')!r}"
        )

    http_error_302 = http_error_301
    http_error_303 = http_error_301
    http_error_307 = http_error_301
    http_error_308 = http_error_301


_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirectHandler())


def _load_remote_json(
    url: str,
    *,
    timeout_seconds: int,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    if not url.lower().startswith("https://"):
        raise PremiumRuntimeError(
            f"Remote premium fetch requires HTTPS: {url!r}"
        )
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "sqlite-memory-mcp",
            **(headers or {}),
        },
    )
    with _NO_REDIRECT_OPENER.open(request, timeout=max(timeout_seconds, 1)) as response:
        body = response.read().decode("utf-8")
    return json.loads(body)


def _load_control_plane_document(
    config: dict[str, Any],
) -> tuple[dict[str, Any] | None, str]:
    payload, source_ref = _load_json_from_env_or_path(
        config,
        inline_key="control_plane_inline_env_var",
        path_key="control_plane_path_env_var",
    )
    if payload:
        return payload, source_ref

    url = _load_env_var_value(config, "control_plane_url_env_var")
    if url:
        timeout_seconds = int(config.get("control_plane_timeout_seconds") or 5)
        headers = _load_remote_headers(config)
        return _load_remote_json(
            url, timeout_seconds=timeout_seconds, headers=headers
        ), url
    return None, "missing"


def _decode_signature(value: str) -> bytes:
    try:
        return base64.b64decode(value.encode("utf-8"), validate=True)
    except Exception as exc:
        raise PremiumRuntimeError(f"Invalid signature encoding: {exc}") from exc


def _verify_signed_payload(
    payload: dict[str, Any],
    public_key_value: str | None,
) -> tuple[bool, str]:
    signature = payload.get("signature")
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
            _canonical_signed_payload(payload),
        )
        return True, "signature_valid"
    except Exception as exc:
        return False, f"signature_invalid:{exc.__class__.__name__}"


def _verify_entitlement_signature(
    entitlement: dict[str, Any],
    public_key_value: str | None,
) -> tuple[bool, str]:
    return _verify_signed_payload(entitlement, public_key_value)


def _parse_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _iso_to_epoch(ts: str | None) -> float | None:
    value = _parse_iso(ts)
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _epoch_to_iso(value: float) -> str:
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()


def _version_tuple(value: str | None) -> tuple[int, ...]:
    if not value:
        return (0,)
    parts: list[int] = []
    for chunk in str(value).replace("-", ".").split("."):
        if chunk.isdigit():
            parts.append(int(chunk))
            continue
        digits = "".join(ch for ch in chunk if ch.isdigit())
        if digits:
            parts.append(int(digits))
    return tuple(parts or [0])


def _version_in_range(
    current: str,
    *,
    minimum: str | None = None,
    maximum: str | None = None,
) -> bool:
    current_tuple = _version_tuple(current)
    if minimum and current_tuple < _version_tuple(minimum):
        return False
    if maximum and current_tuple > _version_tuple(maximum):
        return False
    return True


def _resolve_entrypoint_file(entrypoint: str) -> Path | None:
    path_text = entrypoint
    if "::" in path_text:
        path_text = path_text.rsplit("::", 1)[0]
    if ":" in path_text and not Path(path_text).exists():
        module_name = path_text.rsplit(":", 1)[0]
    else:
        module_name = path_text

    path = Path(path_text)
    if path_text.lower().endswith(".py") or path.exists():
        if path.exists():
            return path.resolve()
        return None

    try:
        spec = importlib.util.find_spec(module_name)
    except (ImportError, AttributeError, ValueError):
        return None
    origin = getattr(spec, "origin", None)
    if not origin or origin in {"built-in", "frozen"}:
        return None
    origin_path = Path(origin)
    if origin_path.exists():
        return origin_path.resolve()
    return None


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(65536)
            if not chunk:
                break
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _entrypoint_runtime_info(entrypoint: str) -> dict[str, Any]:
    entrypoint_file = _resolve_entrypoint_file(entrypoint)
    return {
        "entrypoint": entrypoint,
        "entrypoint_ref": _hash_text(entrypoint),
        "entrypoint_file": str(entrypoint_file) if entrypoint_file else None,
        "entrypoint_sha256": _hash_file(entrypoint_file) if entrypoint_file else None,
    }


def _installation_fingerprint(
    *,
    entrypoint_ref: str,
    manifest_id: str,
    config: dict[str, Any],
) -> str:
    salt = _load_env_var_value(config, "installation_salt_env_var") or ""
    material = "|".join(
        [
            MACHINE_ID,
            HOST_RUNTIME_VERSION,
            entrypoint_ref,
            manifest_id,
            salt,
        ]
    )
    return _hash_text(material)


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


def _store_artifact_manifest(
    conn: sqlite3.Connection,
    *,
    manifest: dict[str, Any],
    entrypoint_info: dict[str, Any],
) -> None:
    verified_at = _now()
    conn.execute(
        "INSERT OR REPLACE INTO premium_artifact_manifests ("
        "manifest_id, extension_name, entrypoint_ref, entrypoint_sha256, "
        "contract_version, build_id, customer_id, protection_phase, "
        "minimum_host_version, maximum_host_version, issued_at, expires_at, "
        "verified_at, payload_json"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            str(manifest.get("manifest_id") or _new_id("mf")),
            str(manifest.get("extension_name") or "sqlite-memory-mcp-premium"),
            str(
                manifest.get("entrypoint_ref")
                or entrypoint_info.get("entrypoint_ref")
                or ""
            ),
            str(
                manifest.get("entrypoint_sha256")
                or entrypoint_info.get("entrypoint_sha256")
                or ""
            ),
            str(manifest.get("contract_version") or PREMIUM_RUNTIME_CONTRACT_VERSION),
            str(manifest.get("build_id") or "") or None,
            str(manifest.get("customer_id") or "") or None,
            max(_parse_int(manifest.get("protection_phase"), 1), 1),
            str(manifest.get("minimum_host_version") or "") or None,
            str(manifest.get("maximum_host_version") or "") or None,
            _parse_iso(manifest.get("issued_at")),
            _parse_iso(manifest.get("expires_at")),
            verified_at,
            _json_text(manifest),
        ),
    )


def _control_scope_keys(customer_id: str | None) -> list[str]:
    keys: list[str] = ["global"]
    if customer_id:
        keys.insert(0, f"customer:{customer_id}")
    return keys


def _cache_control_plane_policy(
    conn: sqlite3.Connection,
    *,
    policy: dict[str, Any],
    source_ref: str,
    config: dict[str, Any],
) -> bool:
    """Persist a verified control-plane policy.

    Returns True on write, False if a rollback guard blocked the write.
    """
    ttl_seconds = max(
        _parse_int(policy.get("cache_ttl_seconds"), 0),
        _parse_int(config.get("control_plane_cache_ttl_seconds"), 0),
    )
    fetched_at = _now()
    cache_deadline = (
        _epoch_to_iso(time.time() + ttl_seconds) if ttl_seconds > 0 else fetched_at
    )
    scope_key = (
        f"customer:{policy.get('customer_id')}"
        if str(policy.get("customer_id") or "").strip()
        else "global"
    )

    # Rollback guard: reject incoming policies that are older than the cached one.
    # A signature alone is not enough — replay of an older, more-permissive policy
    # must not overwrite a newer stricter one.
    existing = conn.execute(
        "SELECT payload_json FROM premium_control_plane_cache "
        "WHERE scope_key = ? LIMIT 1",
        (scope_key,),
    ).fetchone()
    if existing:
        try:
            existing_policy = json_loads(existing["payload_json"])
        except Exception:
            existing_policy = None
        if isinstance(existing_policy, dict):
            existing_issued = _iso_to_epoch(existing_policy.get("issued_at"))
            incoming_issued = _iso_to_epoch(policy.get("issued_at"))
            if (
                existing_issued is not None
                and incoming_issued is not None
                and incoming_issued < existing_issued
            ):
                logger.warning(
                    "control_plane_rollback_rejected scope=%s cached_issued_at=%s "
                    "incoming_issued_at=%s source=%s",
                    scope_key,
                    existing_policy.get("issued_at"),
                    policy.get("issued_at"),
                    source_ref,
                )
                return False

    conn.execute(
        "INSERT OR REPLACE INTO premium_control_plane_cache ("
        "scope_key, policy_id, source_ref, fetched_at, expires_at, "
        "cache_deadline, payload_json"
        ") VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            scope_key,
            str(policy.get("policy_id") or _new_id("cp")),
            source_ref,
            fetched_at,
            _parse_iso(policy.get("expires_at")),
            cache_deadline,
            json.dumps(policy, ensure_ascii=False, sort_keys=True),
        ),
    )
    return True


def _load_cached_control_plane_policy(
    conn: sqlite3.Connection,
    *,
    customer_id: str | None,
    config: dict[str, Any],
) -> dict[str, Any] | None:
    # Re-verify signature on every cache read. The cache row lives in memory.db,
    # which the OSS server writes — it must not be trusted just because it was
    # once signed at write time. A direct DB tamper would otherwise bypass the moat.
    public_key_value = _load_env_var_value(
        config,
        "control_plane_public_key_env_var",
    )
    for scope_key in _control_scope_keys(customer_id):
        row = conn.execute(
            "SELECT scope_key, source_ref, fetched_at, expires_at, cache_deadline, payload_json "
            "FROM premium_control_plane_cache WHERE scope_key = ? LIMIT 1",
            (scope_key,),
        ).fetchone()
        if not row:
            continue
        try:
            payload = json_loads(row["payload_json"])
        except Exception:
            logger.warning(
                "control_plane_cache_unreadable scope=%s", row["scope_key"]
            )
            continue
        sig_ok, sig_reason = _verify_signed_payload(payload, public_key_value)
        if not sig_ok:
            logger.warning(
                "control_plane_cache_signature_rejected scope=%s reason=%s",
                row["scope_key"],
                sig_reason,
            )
            continue
        payload["_cache_scope_key"] = row["scope_key"]
        payload["_cache_source_ref"] = row["source_ref"]
        payload["_cache_fetched_at"] = row["fetched_at"]
        payload["_cache_expires_at"] = row["expires_at"]
        payload["_cache_deadline"] = row["cache_deadline"]
        return payload
    return None


def _policy_cache_is_usable(
    policy: dict[str, Any],
    *,
    config: dict[str, Any],
) -> bool:
    now_epoch = time.time()
    deadline_epoch = _iso_to_epoch(str(policy.get("_cache_deadline") or ""))
    expires_epoch = _iso_to_epoch(str(policy.get("_cache_expires_at") or ""))
    grace_seconds = max(_parse_int(config.get("max_offline_grace_seconds"), 0), 0)
    if expires_epoch and now_epoch > expires_epoch:
        return False
    if deadline_epoch is None:
        return True
    if now_epoch <= deadline_epoch:
        return True
    return now_epoch <= deadline_epoch + grace_seconds


def _resolve_control_plane_policy(
    conn: sqlite3.Connection,
    *,
    config: dict[str, Any],
    customer_id: str | None,
) -> dict[str, Any]:
    source_ref = "missing"
    load_reason = ""
    try:
        live_policy, source_ref = _load_control_plane_document(config)
    except Exception as exc:
        live_policy = None
        load_reason = f"control_plane_load_failed:{exc.__class__.__name__}"
    if live_policy:
        public_key_value = _load_env_var_value(
            config,
            "control_plane_public_key_env_var",
        )
        sig_ok, sig_reason = _verify_signed_payload(live_policy, public_key_value)
        if sig_ok:
            _cache_control_plane_policy(
                conn,
                policy=live_policy,
                source_ref=source_ref,
                config=config,
            )
            return {
                "status": "live",
                "policy": live_policy,
                "source_ref": source_ref,
                "reason": "control_plane_live",
            }
        load_reason = sig_reason

    if config.get("allow_cached_control_plane", True):
        cached_policy = _load_cached_control_plane_policy(
            conn, customer_id=customer_id, config=config
        )
        if cached_policy and _policy_cache_is_usable(cached_policy, config=config):
            return {
                "status": "cached",
                "policy": cached_policy,
                "source_ref": str(cached_policy.get("_cache_source_ref") or "cache"),
                "reason": load_reason or "control_plane_cached",
            }

    if config.get("control_plane_required", False):
        return {
            "status": "denied",
            "policy": None,
            "source_ref": source_ref,
            "reason": load_reason or "control_plane_missing",
        }
    return {
        "status": "missing",
        "policy": None,
        "source_ref": source_ref,
        "reason": load_reason or "control_plane_missing",
    }


def _evaluate_control_plane_rules(
    policy: dict[str, Any] | None,
    *,
    feature_id: str,
    entitlement: dict[str, Any],
    entitlement_id: str,
    customer_id: str | None,
    manifest_id: str,
    entrypoint_sha256: str | None,
    protection_phase: int,
) -> str | None:
    if not policy:
        return None

    revoked_entitlements = set(
        _normalize_string_items(policy.get("revoked_entitlement_ids"))
    )
    if entitlement_id in revoked_entitlements:
        return "control_plane_revoked_entitlement"

    revoked_customers = set(_normalize_string_items(policy.get("revoked_customer_ids")))
    if customer_id and customer_id in revoked_customers:
        return "control_plane_revoked_customer"

    denied_features = set(_normalize_string_items(policy.get("denied_features")))
    if feature_id in denied_features:
        return "control_plane_denied_feature"

    allowed_features = set(_normalize_string_items(policy.get("allowed_features")))
    if allowed_features and feature_id not in allowed_features:
        return "control_plane_feature_not_allowed"

    denied_hashes = set(_normalize_string_items(policy.get("denied_entrypoint_hashes")))
    if entrypoint_sha256 and entrypoint_sha256 in denied_hashes:
        return "control_plane_denied_artifact_hash"

    allowed_hashes = set(
        _normalize_string_items(policy.get("allowed_entrypoint_hashes"))
    )
    if (
        entrypoint_sha256
        and allowed_hashes
        and feature_id == "private_extension_runtime"
        and entrypoint_sha256 not in allowed_hashes
    ):
        return "control_plane_artifact_hash_not_allowed"

    allowed_manifest_ids = set(
        _normalize_string_items(policy.get("allowed_manifest_ids"))
    )
    if manifest_id and allowed_manifest_ids and manifest_id not in allowed_manifest_ids:
        return "control_plane_manifest_not_allowed"

    minimum_phase = max(_parse_int(policy.get("minimum_protection_phase"), 0), 0)
    if minimum_phase and protection_phase < minimum_phase:
        return "control_plane_minimum_protection_phase"

    if not _version_in_range(
        HOST_RUNTIME_VERSION,
        minimum=str(policy.get("minimum_host_version") or "") or None,
        maximum=str(policy.get("maximum_host_version") or "") or None,
    ):
        return "control_plane_host_version_mismatch"

    max_ttl_seconds = _parse_int(policy.get("max_entitlement_ttl_seconds"), 0)
    issued_epoch = _iso_to_epoch(
        str(entitlement.get("issued_at") or entitlement.get("not_before") or "")
    )
    expires_epoch = _iso_to_epoch(str(entitlement.get("expires_at") or ""))
    if (
        max_ttl_seconds > 0
        and issued_epoch is not None
        and expires_epoch is not None
        and expires_epoch - issued_epoch > max_ttl_seconds
    ):
        return "control_plane_entitlement_ttl_exceeded"
    return None


def _validate_artifact_manifest(
    manifest: dict[str, Any] | None,
    *,
    config: dict[str, Any],
    control_policy: dict[str, Any] | None,
    entrypoint_info: dict[str, Any] | None,
    customer_id: str | None,
    require_private_runtime_manifest: bool = False,
) -> tuple[dict[str, Any] | None, str | None, int]:
    minimum_phase = max(_parse_int(config.get("minimum_protection_phase"), 1), 1)
    if control_policy:
        minimum_phase = max(
            minimum_phase,
            max(_parse_int(control_policy.get("minimum_protection_phase"), 0), 0),
        )
    require_manifest = bool(
        require_private_runtime_manifest
        or config.get("require_artifact_manifest", False)
        or bool(control_policy and control_policy.get("require_artifact_manifest"))
    )
    if not manifest:
        if require_manifest:
            return None, "artifact_manifest_required", minimum_phase
        return None, None, minimum_phase

    public_key_value = _load_env_var_value(config, "artifact_public_key_env_var")
    sig_ok, sig_reason = _verify_signed_payload(manifest, public_key_value)
    if not sig_ok:
        return None, sig_reason, minimum_phase

    manifest_customer_id = str(manifest.get("customer_id") or "").strip() or None
    if manifest_customer_id and customer_id and manifest_customer_id != customer_id:
        return None, "artifact_manifest_customer_mismatch", minimum_phase

    contract_version = str(manifest.get("contract_version") or "").strip()
    if contract_version and contract_version != PREMIUM_RUNTIME_CONTRACT_VERSION:
        return None, "artifact_manifest_contract_mismatch", minimum_phase

    if not _version_in_range(
        HOST_RUNTIME_VERSION,
        minimum=str(manifest.get("minimum_host_version") or "") or None,
        maximum=str(manifest.get("maximum_host_version") or "") or None,
    ):
        return None, "artifact_manifest_host_version_mismatch", minimum_phase

    expires_at = _iso_to_epoch(str(manifest.get("expires_at") or ""))
    if expires_at is not None and time.time() > expires_at:
        return None, "artifact_manifest_expired", minimum_phase

    if entrypoint_info:
        manifest_entrypoint_ref = str(manifest.get("entrypoint_ref") or "").strip()
        if manifest_entrypoint_ref and manifest_entrypoint_ref != entrypoint_info.get(
            "entrypoint_ref"
        ):
            return None, "artifact_manifest_entrypoint_ref_mismatch", minimum_phase
        manifest_sha = str(manifest.get("entrypoint_sha256") or "").strip()
        runtime_sha = str(entrypoint_info.get("entrypoint_sha256") or "").strip()
        if manifest_sha and runtime_sha and manifest_sha != runtime_sha:
            return None, "artifact_manifest_hash_mismatch", minimum_phase
        if manifest_sha and not runtime_sha:
            return None, "artifact_manifest_hash_unverifiable", minimum_phase

    protection_phase = max(_parse_int(manifest.get("protection_phase"), 1), 1)
    if protection_phase < minimum_phase:
        return None, "artifact_manifest_protection_phase_too_low", protection_phase
    return manifest, None, protection_phase


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
    audit_payload = dict(payload or {})

    def _deny(reason: str, **extra: Any) -> dict[str, Any]:
        verdict = {
            "allowed": False,
            "decision": "denied",
            "reason": reason,
            "feature_id": feature_id,
            **extra,
        }
        if config.get("record_denied_events", True):
            _write_gate_audit(
                conn,
                feature_id=feature_id,
                decision="denied",
                reason=reason,
                entitlement_id=extra.get("entitlement_id"),
                customer_id=extra.get("customer_id"),
                server_name=server_name,
                tool_name=tool_name,
                actor_id=actor_id,
                payload=audit_payload,
            )
        return verdict

    feature = PREMIUM_FEATURES.get(feature_id)
    if not feature:
        _write_gate_audit(
            conn,
            feature_id=feature_id,
            decision="denied",
            reason="unknown_feature",
            server_name=server_name,
            tool_name=tool_name,
            actor_id=actor_id,
            payload=audit_payload,
        )
        return {
            "allowed": False,
            "decision": "denied",
            "reason": "unknown_feature",
            "feature_id": feature_id,
        }

    if str(feature.get("tier", "premium")).lower() != "premium":
        return {
            "allowed": True,
            "decision": "allowed",
            "reason": "non_premium_feature",
            "feature_id": feature_id,
        }

    if not config.get("enabled", True):
        return _deny("premium_runtime_disabled")

    source_ref = "missing"
    try:
        entitlement, source_ref = _load_entitlement(config)
    except Exception as exc:
        audit_payload["entitlement_source"] = source_ref
        return _deny(f"entitlement_load_failed:{exc.__class__.__name__}")

    if not entitlement:
        audit_payload["entitlement_source"] = source_ref
        return _deny("entitlement_missing")

    entitlement_id = str(entitlement.get("entitlement_id") or "").strip()
    customer_id = str(entitlement.get("customer_id") or "").strip() or None
    audit_payload["entitlement_source"] = source_ref
    audit_payload["entitlement_id"] = entitlement_id or None
    audit_payload["customer_id"] = customer_id
    if not entitlement_id:
        return _deny("entitlement_id_missing", customer_id=customer_id)

    sig_ok, sig_reason = _verify_entitlement_signature(
        entitlement,
        _load_env_var_value(config, "public_key_env_var"),
    )
    if not sig_ok:
        return _deny(
            sig_reason,
            entitlement_id=entitlement_id,
            customer_id=customer_id,
        )

    if _is_revoked(conn, entitlement_id=entitlement_id, feature_id=feature_id):
        return _deny(
            "entitlement_revoked",
            entitlement_id=entitlement_id,
            customer_id=customer_id,
        )

    selection = resolve_entitlement_selection(entitlement)
    audit_payload["selection_mode"] = selection.get("selection_mode")
    audit_payload["selected_packs"] = selection.get("selected_packs", [])
    if not selection.get("has_selection"):
        return _deny(
            "entitlement_selection_missing",
            entitlement_id=entitlement_id,
            customer_id=customer_id,
            selection_mode=selection.get("selection_mode"),
        )

    effective_features = set(selection.get("effective_features", []))
    if feature_id not in effective_features:
        return _deny(
            "feature_not_entitled",
            entitlement_id=entitlement_id,
            customer_id=customer_id,
            selection_mode=selection.get("selection_mode"),
            selected_packs=selection.get("selected_packs", []),
            effective_features=selection.get("effective_features", []),
        )

    now_epoch = time.time()
    not_before_epoch = _iso_to_epoch(entitlement.get("not_before"))
    expires_at_epoch = _iso_to_epoch(entitlement.get("expires_at"))
    if not_before_epoch is not None and now_epoch < not_before_epoch:
        return _deny(
            "entitlement_not_yet_valid",
            entitlement_id=entitlement_id,
            customer_id=customer_id,
        )
    if expires_at_epoch is not None and now_epoch > expires_at_epoch:
        return _deny(
            "entitlement_expired",
            entitlement_id=entitlement_id,
            customer_id=customer_id,
        )

    machine_ids = entitlement.get("machine_ids", [])
    if config.get("require_machine_binding", True) and not machine_ids:
        return _deny(
            "machine_binding_missing",
            entitlement_id=entitlement_id,
            customer_id=customer_id,
        )
    if machine_ids and MACHINE_ID not in machine_ids:
        return _deny(
            "machine_not_entitled",
            entitlement_id=entitlement_id,
            customer_id=customer_id,
        )

    control_resolution = _resolve_control_plane_policy(
        conn,
        config=config,
        customer_id=customer_id,
    )
    control_policy = control_resolution.get("policy")
    audit_payload["control_plane_status"] = control_resolution.get("status")
    audit_payload["control_plane_source"] = control_resolution.get("source_ref")
    if control_resolution.get("status") == "denied":
        return _deny(
            str(control_resolution.get("reason") or "control_plane_denied"),
            entitlement_id=entitlement_id,
            customer_id=customer_id,
        )

    entrypoint_info: dict[str, Any] | None = None
    if feature_id == "private_extension_runtime":
        raw_entrypoint = str(audit_payload.get("entrypoint") or "").strip()
        if raw_entrypoint:
            entrypoint_info = _entrypoint_runtime_info(raw_entrypoint)
            audit_payload.update(entrypoint_info)

    manifest_required_for_feature = feature_id == "private_extension_runtime" or bool(
        config.get("require_artifact_manifest", False)
        or bool(control_policy and control_policy.get("require_artifact_manifest"))
    )
    manifest = None
    manifest_source = "missing"
    if manifest_required_for_feature:
        try:
            manifest, manifest_source = _load_artifact_manifest(config)
        except Exception as exc:
            return _deny(
                f"artifact_manifest_load_failed:{exc.__class__.__name__}",
                entitlement_id=entitlement_id,
                customer_id=customer_id,
            )

    validated_manifest, manifest_reason, protection_phase = _validate_artifact_manifest(
        manifest,
        config=config,
        control_policy=control_policy,
        entrypoint_info=entrypoint_info,
        customer_id=customer_id,
        require_private_runtime_manifest=feature_id == "private_extension_runtime",
    )
    audit_payload["artifact_manifest_source"] = manifest_source
    audit_payload["protection_phase"] = protection_phase
    if manifest_reason:
        return _deny(
            manifest_reason,
            entitlement_id=entitlement_id,
            customer_id=customer_id,
        )

    manifest_id = (
        str(validated_manifest.get("manifest_id") or "").strip()
        if validated_manifest
        else ""
    )
    installation_fingerprint = _installation_fingerprint(
        entrypoint_ref=str(audit_payload.get("entrypoint_ref") or feature_id),
        manifest_id=manifest_id,
        config=config,
    )
    audit_payload["installation_fingerprint"] = installation_fingerprint

    if validated_manifest and entrypoint_info:
        _store_artifact_manifest(
            conn,
            manifest=validated_manifest,
            entrypoint_info=entrypoint_info,
        )

    control_reason = _evaluate_control_plane_rules(
        control_policy,
        feature_id=feature_id,
        entitlement=entitlement,
        entitlement_id=entitlement_id,
        customer_id=customer_id,
        manifest_id=manifest_id,
        entrypoint_sha256=(
            str(entrypoint_info.get("entrypoint_sha256") or "")
            if entrypoint_info
            else None
        ),
        protection_phase=protection_phase,
    )
    if control_reason:
        return _deny(
            control_reason,
            entitlement_id=entitlement_id,
            customer_id=customer_id,
        )

    if feature.get("requires_owner_approval"):
        approval_env = str(config.get("owner_approval_env_var") or "").strip()
        approval_value = (
            os.environ.get(approval_env, "").strip() if approval_env else ""
        )
        approval_hash = str(entitlement.get("owner_approval_sha256") or "").strip()
        if not approval_value or not approval_hash:
            return _deny(
                "owner_approval_missing",
                entitlement_id=entitlement_id,
                customer_id=customer_id,
            )
        if _hash_text(approval_value) != approval_hash:
            return _deny(
                "owner_approval_invalid",
                entitlement_id=entitlement_id,
                customer_id=customer_id,
            )

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
        "control_plane_status": control_resolution.get("status"),
        "control_plane_source": control_resolution.get("source_ref"),
        "manifest_id": manifest_id,
        "protection_phase": protection_phase,
        "installation_fingerprint": installation_fingerprint,
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
            payload=audit_payload,
        )
    return verdict


_DEBATE_PROTOCOL_GATE_FAIL_OPEN_PREFIXES = (
    "entitlement_load_failed:",
    "control_plane_load_failed:",
    "artifact_manifest_load_failed:",
)


def evaluate_debate_protocol_creation_gate(
    conn: sqlite3.Connection,
    *,
    server_name: str | None = "sqlite-intel",
    tool_name: str | None = "sqlite-intel.debate_init",
    actor_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate the optional premium gate for new debate topic creation.

    The gate is default-off so deploying the code cannot lock out new topics.
    When enabled, clear entitlement denials fail closed, while entitlement
    runtime/evaluator failures fail open and write a distinct audit row.
    """
    config = load_premium_config()
    feature_id = "debate_protocol"
    audit_payload = {
        "gate_scope": "debate_init_new_topic",
        **dict(payload or {}),
    }

    if _config_env_flag(config, "debate_protocol_gate_disabled_env_var"):
        return {
            "allowed": True,
            "decision": "skipped",
            "reason": "debate_protocol_gate_disabled",
            "feature_id": feature_id,
        }

    gate_enabled = bool(config.get("debate_protocol_gate_enabled")) or _config_env_flag(
        config, "debate_protocol_gate_enabled_env_var"
    )
    if not gate_enabled:
        return {
            "allowed": True,
            "decision": "skipped",
            "reason": "debate_protocol_gate_default_off",
            "feature_id": feature_id,
        }

    try:
        verdict = evaluate_feature_gate(
            conn,
            feature_id=feature_id,
            server_name=server_name,
            tool_name=tool_name,
            actor_id=actor_id,
            payload=audit_payload,
        )
    except Exception as exc:
        reason = f"debate_protocol_gate_fail_open:{exc.__class__.__name__}"
        logger.exception("debate_protocol gate evaluator failed open")
        # STEP-4 safety: the audit write must NEVER be able to flip a fail-OPEN
        # allow into a fail-CLOSED deny. If persisting the audit row itself
        # fails, log and continue — the allow verdict below is still returned.
        try:
            _write_gate_audit(
                conn,
                feature_id=feature_id,
                decision="allowed",
                reason=reason,
                server_name=server_name,
                tool_name=tool_name,
                actor_id=actor_id,
                payload={
                    **audit_payload,
                    "fail_open": True,
                    "failure_class": exc.__class__.__name__,
                },
            )
        except Exception:
            logger.warning(
                "debate_protocol fail-open audit write failed; "
                "returning allow regardless",
                exc_info=True,
            )
        return {
            "allowed": True,
            "decision": "allowed",
            "reason": reason,
            "feature_id": feature_id,
            "fail_open": True,
        }

    reason = str(verdict.get("reason") or "")
    if not verdict.get("allowed") and reason.startswith(
        _DEBATE_PROTOCOL_GATE_FAIL_OPEN_PREFIXES
    ):
        fail_open_reason = f"debate_protocol_gate_fail_open:{reason}"
        # STEP-4 safety: same anti-lockout guard as the evaluator-exception
        # branch — a load-failure fail-OPEN must never become a deny because
        # the audit row could not be written.
        try:
            _write_gate_audit(
                conn,
                feature_id=feature_id,
                decision="allowed",
                reason=fail_open_reason,
                entitlement_id=verdict.get("entitlement_id"),
                customer_id=verdict.get("customer_id"),
                server_name=server_name,
                tool_name=tool_name,
                actor_id=actor_id,
                payload={
                    **audit_payload,
                    "fail_open": True,
                    "source_reason": reason,
                },
            )
        except Exception:
            logger.warning(
                "debate_protocol fail-open (by-reason) audit write failed; "
                "returning allow regardless",
                exc_info=True,
            )
        return {
            **verdict,
            "allowed": True,
            "decision": "allowed",
            "reason": fail_open_reason,
            "fail_open": True,
        }

    return verdict


def _entrypoint_fingerprint(entrypoint: str) -> str:
    return str(_entrypoint_runtime_info(entrypoint).get("entrypoint_ref") or "")


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
    host_runtime_version: str = "",
    installation_fingerprint: str = "",
    manifest_id: str = "",
    protection_phase: int = 1,
):
    mount_context = build_mount_context(
        server_name=server_name,
        feature_id="private_extension_runtime",
        machine_id=MACHINE_ID,
        host_runtime_version=host_runtime_version or HOST_RUNTIME_VERSION,
        installation_fingerprint=installation_fingerprint,
        manifest_id=manifest_id,
        protection_phase=max(int(protection_phase or 1), 1),
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

    entrypoint_info = _entrypoint_runtime_info(entrypoint)
    payload = {
        "server_name": server_name,
        **entrypoint_info,
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

    manifest_payload: dict[str, Any] | None = None
    control_policy_payload: dict[str, Any] | None = None
    try:
        manifest_payload, _manifest_source = _load_artifact_manifest(config)
    except Exception:
        manifest_payload = None
    with _get_conn() as conn:
        control_resolution = _resolve_control_plane_policy(
            conn,
            config=config,
            customer_id=str(verdict.get("customer_id") or "") or None,
        )
    if isinstance(control_resolution.get("policy"), dict):
        control_policy_payload = dict(control_resolution["policy"])
    validated_manifest, _manifest_reason, protection_phase = (
        _validate_artifact_manifest(
            manifest_payload,
            config=config,
            control_policy=control_policy_payload,
            entrypoint_info=entrypoint_info,
            customer_id=str(verdict.get("customer_id") or "") or None,
        )
    )
    protection_phase = max(
        int(protection_phase or 1),
        int(verdict.get("protection_phase") or 1),
        1,
    )
    installation_fingerprint = str(verdict.get("installation_fingerprint") or "")
    manifest_id = str(verdict.get("manifest_id") or "")
    if not installation_fingerprint:
        installation_fingerprint = _installation_fingerprint(
            entrypoint_ref=str(entrypoint_info.get("entrypoint_ref") or ""),
            manifest_id=manifest_id,
            config=config,
        )

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
                manifest=validated_manifest,
                control_policy=control_policy_payload,
                installation_fingerprint=installation_fingerprint,
                protection_phase=max(int(protection_phase or 1), 1),
            ),
            host_runtime_version=HOST_RUNTIME_VERSION,
            installation_fingerprint=installation_fingerprint,
            manifest_id=manifest_id,
            protection_phase=max(int(protection_phase or 1), 1),
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
                payload={
                    **payload,
                    "manifest_id": manifest_id or None,
                    "protection_phase": max(int(protection_phase or 1), 1),
                    "installation_fingerprint": installation_fingerprint,
                },
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
