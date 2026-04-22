"""Runtime state and per-tool feature gating for template premium tools."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from . import host_api

logger = host_api.setup_logger(
    "sqlite-memory-premium-template-runtime",
    "sqlite_memory_premium_template.log",
)


@dataclass(slots=True)
class RuntimeState:
    configured: bool = False
    server_name: str = "sqlite-memory-premium-template"
    contract_version: str = ""
    machine_id: str = ""
    host_runtime_version: str = ""
    installation_fingerprint: str = ""
    manifest_id: str = ""
    protection_phase: int = 1
    config: dict[str, Any] = field(default_factory=dict)


_STATE = RuntimeState()


def configure_runtime(
    *,
    server_name: str | None,
    mount_context: Any,
) -> RuntimeState:
    """Bind the host runtime context for subsequent premium tool calls."""
    if mount_context is None:
        raise RuntimeError("mount_context required")
    contract_version = getattr(mount_context, "contract_version", None)
    if contract_version != host_api.PREMIUM_RUNTIME_CONTRACT_VERSION:
        raise RuntimeError(
            "premium contract mismatch: "
            f"{contract_version} != {host_api.PREMIUM_RUNTIME_CONTRACT_VERSION}"
        )

    _STATE.configured = True
    _STATE.server_name = server_name or getattr(
        mount_context,
        "server_name",
        "sqlite-memory-premium-template",
    )
    _STATE.contract_version = contract_version
    _STATE.machine_id = getattr(mount_context, "machine_id", "")
    _STATE.host_runtime_version = getattr(mount_context, "host_runtime_version", "")
    _STATE.installation_fingerprint = getattr(
        mount_context,
        "installation_fingerprint",
        "",
    )
    _STATE.manifest_id = getattr(mount_context, "manifest_id", "")
    _STATE.protection_phase = max(
        int(getattr(mount_context, "protection_phase", 1) or 1),
        1,
    )
    _STATE.config = dict(getattr(mount_context, "config", {}) or {})
    return _STATE


def require_feature(
    feature_id: str,
    *,
    tool_name: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate a premium feature gate before placeholder tool logic runs."""
    if not _STATE.configured:
        raise RuntimeError("premium runtime not configured")
    with host_api.get_conn() as conn:
        return host_api.evaluate_feature_gate(
            conn,
            feature_id=feature_id,
            server_name=_STATE.server_name,
            tool_name=tool_name,
            actor_id=_STATE.server_name,
            payload=payload,
        )


def denied_response(feature_id: str, verdict: dict[str, Any]) -> str:
    """Return a stable JSON error payload when a feature gate denies access."""
    return json.dumps(
        {
            "error": "premium_access_denied",
            "feature_id": feature_id,
            "reason": verdict.get("reason"),
            "decision": verdict.get("decision"),
            "entitlement_id": verdict.get("entitlement_id"),
            "customer_id": verdict.get("customer_id"),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
