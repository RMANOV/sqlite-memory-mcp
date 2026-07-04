# Deterministic image for MCP registry builders (Glama, etc.).
# Builds ONLY the core knowledge-graph MCP server ("sqlite-kb"); no
# [gui]/[vector]/[dev] extras, so the sole runtime dependency is fastmcp.
FROM python:3.11-slim

WORKDIR /app
COPY . /app

# Core-only install (pulls fastmcp, nothing else) + a writable DB dir.
RUN pip install --no-cache-dir . \
    && mkdir -p /data

# Where the server persists its SQLite DB. Mount a volume here to keep
# state; the schema layer creates the file on first use.
# (db_utils resolves the DB from this env; default is ~/.claude/memory.)
ENV SQLITE_MEMORY_DB=/data/memory.db

# Canonical core stdio entrypoint.
# pyproject [project.scripts]: sqlite-memory-mcp = "server:main" -> mcp.run(transport="stdio")
CMD ["sqlite-memory-mcp"]
