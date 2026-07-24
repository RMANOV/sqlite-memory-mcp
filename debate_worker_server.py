#!/usr/bin/env python3
"""Minimal MCP surface for autonomous debate wake workers.

The general unified server exposes roughly one hundred tools.  A bounded wake
worker needs only the six tools below; loading the wider surface adds startup
contention, tool-discovery ambiguity, and a much larger prompt.  Reuse the
canonical intel-server tool objects so transaction, validation, and
post-commit wake semantics stay identical to the public unified server.
"""

from __future__ import annotations

from fastmcp import FastMCP

from db_utils import ensure_db_initialized, setup_logger
from intel_server import (
    debate_binding_list,
    debate_post_with_recipients,
    debate_signal_advance,
    debate_signal_check,
    debate_worker_claim,
    debate_worker_no_action,
)


logger = setup_logger("sqlite-debate-worker", "debate_worker_server.log")

mcp = FastMCP(
    "sqlite-debate-worker",
    instructions=(
        "Bounded local debate worker surface. Read the addressed inbox, post at "
        "most one addressed response or record no-action, then advance the "
        "cursor exactly as instructed by the wake prompt."
    ),
)

_TOOLS = (
    debate_signal_check,
    debate_post_with_recipients,
    debate_signal_advance,
    debate_binding_list,
    debate_worker_claim,
    debate_worker_no_action,
)

for _tool in _TOOLS:
    mcp.add_tool(_tool)


def main() -> None:
    ensure_db_initialized()
    logger.info("Debate worker server ready: %d tools", len(_TOOLS))
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
