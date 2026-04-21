"""Shared FastMCP app for template premium tools."""

from __future__ import annotations

from fastmcp import FastMCP

premium_mcp = FastMCP(
    "sqlite-memory-premium-template",
    instructions=(
        "Template private premium MCP runtime for sqlite-memory-mcp. "
        "Replace the placeholder tools with real premium-only capabilities."
    ),
)
