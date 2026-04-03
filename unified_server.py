#!/usr/bin/env python3
"""Unified SQLite MCP Server — all 50 tools in a single process.

Consolidates the 7 domain servers (core, tasks, session, entity, intel,
bridge, collab) into one FastMCP instance using mount(). This avoids running
multiple redundant Python interpreter instances when a single all-in-one MCP
server is preferred.

All tools keep their original names (no prefix).
"""

from __future__ import annotations

from fastmcp import FastMCP

from db_utils import ensure_db_initialized, setup_logger

# ── Logging (file-only, NEVER stdout — breaks MCP stdio) ────────────────
logger = setup_logger("sqlite-unified", "unified_server.log")

# ── Import satellite server mcp objects ──────────────────────────────────
# Each module creates its own FastMCP instance and registers tools via
# @mcp.tool() at import time. We mount them all into a unified instance.

from server import (
    mcp as core_mcp,
)  # 9 tools: entity/obs/relation CRUD, search, read_graph
from task_server import (
    mcp as tasks_mcp,
)  # 6 tools: create/update/query/digest/archive/bump
from session_server import (
    mcp as session_mcp,
)  # 5 tools: save/recall/search/health/resume
from entity_server import (
    mcp as entity_mcp,
)  # 7 tools: link/unlink/get_links/tasks/suggest/overlap/merge
from intel_server import (
    mcp as intel_mcp,
)  # 8 tools: assess/clarify/answer/extract/promote/pack/impact/enrich
from bridge_server import mcp as bridge_mcp  # 6 tools: bridge push/pull/status/sync
from collab_server import (
    mcp as collab_mcp,
)  # 9 tools: collaboration/sharing/verification

# ── Unified server ───────────────────────────────────────────────────────

mcp = FastMCP(
    "sqlite-unified",
    instructions=(
        "Unified SQLite knowledge graph server with all tools: "
        "entity/observation/relation CRUD, FTS5 search, task management, "
        "session persistence, entity linking, intelligence v2, "
        "bridge sync, and knowledge collaboration. For tasks/notes, "
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

logger.info(
    "Unified server ready: %d mounted servers, tools from 7 domains",
    7,
)

if __name__ == "__main__":
    ensure_db_initialized()
    mcp.run(transport="stdio")
