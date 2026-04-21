"""Bootstrap premium registration for the private sqlite-memory runtime."""

from __future__ import annotations

from typing import Any

from . import acl_governance  # noqa: F401
from . import communication_memory  # noqa: F401
from .app import premium_mcp
from .runtime_state import configure_runtime
from .schema import init_private_schema


def register_premium_extensions(
    mcp: Any,
    *,
    server_name: str | None = None,
    mount_context: Any | None = None,
) -> dict[str, Any]:
    """Mount placeholder premium MCP tools into the entitled host runtime."""
    state = configure_runtime(server_name=server_name, mount_context=mount_context)
    init_private_schema()

    if hasattr(mcp, "mount"):
        mcp.mount(premium_mcp)

    return {
        "mounted": True,
        "contract_version": state.contract_version,
        "extension_name": "sqlite-memory-mcp-premium-template",
        "features": [
            "acl_rbac",
            "governance_audit",
            "multi_mailbox_ingestion",
        ],
        "notes": (
            "Mounted template premium packs into "
            f"{state.server_name}; replace placeholder tools with real logic"
        ),
    }
