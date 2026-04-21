"""Minimal private premium registration template.

Copy this file into a private repo and replace the placeholder tools with the
real premium implementation.
"""

from __future__ import annotations

import json

from fastmcp import FastMCP

from premium_contract import (
    PREMIUM_RUNTIME_CONTRACT_VERSION,
    PremiumMountContext,
    PremiumRegistrationResult,
)

premium_mcp = FastMCP(
    "sqlite-premium-template",
    instructions=(
        "Template MCP mounted from a separate private premium repo. "
        "Replace these placeholder tools with real premium-only features."
    ),
)


@premium_mcp.tool()
def premium_status() -> str:
    """Template-only tool proving that private premium mounting works."""
    return json.dumps(
        {
            "status": "template_loaded",
            "note": "Replace this template tool with real premium-only tools.",
        }
    )


def register_premium_extensions(
    mcp,
    *,
    server_name: str | None = None,
    mount_context: PremiumMountContext | None = None,
) -> PremiumRegistrationResult:
    """Mount a placeholder premium MCP into the host runtime.

    Real premium repos should keep the same outer contract and swap in their
    proprietary tools and workflows here.
    """
    if mount_context is None:
        raise RuntimeError("mount_context required")
    if mount_context.contract_version != PREMIUM_RUNTIME_CONTRACT_VERSION:
        raise RuntimeError(
            "premium contract mismatch: "
            f"{mount_context.contract_version} != {PREMIUM_RUNTIME_CONTRACT_VERSION}"
        )
    if hasattr(mcp, "mount"):
        mcp.mount(premium_mcp)
    return {
        "mounted": True,
        "contract_version": mount_context.contract_version,
        "extension_name": "sqlite-memory-premium-template",
        "features": ["template_status"],
        "notes": f"Mounted into {server_name or mount_context.server_name}",
    }
