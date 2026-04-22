"""Bootstrap premium registration for the private sqlite-memory runtime."""

from __future__ import annotations

from typing import Any

from . import acl_governance  # noqa: F401
from . import communication_memory  # noqa: F401
from .app import premium_mcp
from .runtime_state import configure_runtime
from .schema import init_private_schema
from .tray_extension import build_task_tray_extension as _build_task_tray_extension


def register_premium_extensions(
    mcp: Any,
    *,
    server_name: str | None = None,
    mount_context: Any | None = None,
) -> dict[str, Any]:
    """Mount placeholder premium MCP tools into the entitled host runtime."""
    state = configure_runtime(server_name=server_name, mount_context=mount_context)
    init_private_schema()
    selection = state.config.get("_premium_selection", {})

    feature_catalog = [
        "acl_rbac",
        "governance_audit",
        "multi_mailbox_ingestion",
        "custom_design_tab",
        "password_protected_views",
    ]

    if hasattr(mcp, "mount"):
        mcp.mount(premium_mcp)

    effective_features = selection.get("effective_features", [])
    if isinstance(effective_features, list) and effective_features:
        feature_list = [
            feature_id
            for feature_id in feature_catalog
            if feature_id in effective_features
        ]
    else:
        feature_list = list(feature_catalog)

    return {
        "mounted": True,
        "contract_version": state.contract_version,
        "host_runtime_version": state.host_runtime_version,
        "installation_fingerprint": state.installation_fingerprint,
        "manifest_id": state.manifest_id,
        "protection_phase": state.protection_phase,
        "extension_name": "sqlite-memory-mcp-premium-template",
        "packs": list(selection.get("selected_packs", [])),
        "selection_mode": str(selection.get("selection_mode") or "none"),
        "features": feature_list,
        "notes": (
            "Mounted template premium packs into "
            f"{state.server_name}; replace placeholder tools with real logic, "
            "including Custom Design and password-protected view surfaces"
        ),
    }


def build_task_tray_extension(
    *,
    server_name: str | None = None,
    mount_context: Any | None = None,
):
    """Expose the placeholder premium tray extension through the public contract."""
    configure_runtime(server_name=server_name, mount_context=mount_context)
    init_private_schema()
    return _build_task_tray_extension(
        server_name=server_name,
        mount_context=mount_context,
    )
