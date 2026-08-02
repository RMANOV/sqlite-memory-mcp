#!/usr/bin/env python3
"""Unified SQLite MCP Server — all domain tools in a single process.

Consolidates the 7 domain servers (core, tasks, session, entity, intel,
bridge, collab) into one FastMCP instance using mount(). This avoids running
multiple redundant Python interpreter instances when a single all-in-one MCP
server is preferred.

All tools keep their original names (no prefix).
"""

from __future__ import annotations

from fastmcp import FastMCP

from db_utils import ensure_db_initialized, setup_logger
from premium_runtime import maybe_mount_premium_extensions

# ── Import satellite server mcp objects ──────────────────────────────────
# Each module creates its own FastMCP instance and registers tools via
# @mcp.tool() at import time. We mount them all into a unified instance.

from server import mcp as core_mcp
from task_server import mcp as tasks_mcp
from session_server import mcp as session_mcp
from entity_server import mcp as entity_mcp
from intel_server import mcp as intel_mcp
from bridge_server import mcp as bridge_mcp
from collab_server import mcp as collab_mcp

# ── Logging (file-only, NEVER stdout — breaks MCP stdio) ────────────────
logger = setup_logger("sqlite-unified", "unified_server.log")

# ── Unified server ───────────────────────────────────────────────────────

mcp = FastMCP(
    "sqlite-unified",
    instructions=(
        "Unified SQLite knowledge graph server with all tools: "
        "entity/observation/relation CRUD, FTS5 search, task management, "
        "session persistence, entity linking, intelligence v2, "
        "bridge sync, and knowledge collaboration. For tasks/notes, "
        "query_tasks returns live work by default: done/archived/cancelled rows are "
        "excluded unless you pass an explicit status or include_completed=True. "
        "use find_by_title when only a remembered phrase is known; it searches "
        "across tasks, notes, and entities over title/name, description, notes, observations, "
        "and project regardless of section, type, or project filters, with confidence gating; "
        "finished rows stay reachable but always rank below live work. "
        "put the main long-form body in description by default; "
        "use notes only for auxiliary/internal metadata."
    ),
)

# Mount all satellite servers without prefix — keeps original tool names
mcp.mount(core_mcp)
mcp.mount(tasks_mcp)
mcp.mount(session_mcp)
mcp.mount(entity_mcp)
mcp.mount(intel_mcp)
mcp.mount(bridge_mcp)
mcp.mount(collab_mcp)


def main() -> None:
    ensure_db_initialized()
    maybe_mount_premium_extensions(mcp, server_name="sqlite-unified")
    logger.info(
        "Unified server ready: %d mounted servers, tools from 7 domains",
        7,
    )
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
