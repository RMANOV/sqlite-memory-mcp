# Basic Usage Guide

Quick start examples for the SQLite Memory MCP Server.

## Prerequisites

1. Server is running via Claude Code MCP integration (see README for setup).
2. All tool calls below are made through Claude Code's MCP interface.

## Creating Entities

Create entities with initial observations:

```json
{
  "tool": "create_entities",
  "arguments": {
    "entities": [
      {
        "name": "FastMCP",
        "entityType": "Library",
        "observations": [
          "Python framework for building MCP servers",
          "Supports stdio and SSE transports",
          "Version 2.0+ required for this server"
        ]
      },
      {
        "name": "WAL Mode",
        "entityType": "Concept",
        "observations": [
          "SQLite Write-Ahead Logging journal mode",
          "Enables concurrent readers and writers",
          "Set via PRAGMA journal_mode=WAL"
        ]
      }
    ]
  }
}
```

## Adding Observations

Add new observations to an existing entity:

```json
{
  "tool": "add_observations",
  "arguments": {
    "observations": [
      {
        "entityName": "FastMCP",
        "contents": [
          "Has built-in tool decorator for defining MCP tools",
          "Handles JSON-RPC protocol automatically"
        ]
      }
    ]
  }
}
```

Duplicate observations are silently ignored -- safe to call multiple times.

## Creating Relations

Link entities with directed relations:

```json
{
  "tool": "create_relations",
  "arguments": {
    "relations": [
      {
        "from": "SQLite Memory MCP",
        "to": "FastMCP",
        "relationType": "depends_on"
      },
      {
        "from": "SQLite Memory MCP",
        "to": "WAL Mode",
        "relationType": "uses"
      }
    ]
  }
}
```

## Searching the Knowledge Graph

FTS5 BM25 ranked search across all entities and observations:

```json
{
  "tool": "search_nodes",
  "arguments": {
    "query": "concurrent"
  }
}
```

Results are ranked by relevance. Supports FTS5 query syntax:

- `"WAL mode"` -- exact phrase
- `sqlite OR postgres` -- boolean OR
- `bug*` -- prefix matching
- `name:FastMCP` -- column-specific search

## Retrieving Specific Entities

Fetch entities by exact name, including their observations and inter-relations:

```json
{
  "tool": "open_nodes",
  "arguments": {
    "names": ["FastMCP", "WAL Mode"]
  }
}
```

## Full Graph Dump

Get the entire knowledge graph:

```json
{
  "tool": "read_graph",
  "arguments": {}
}
```

## Deleting Data

Delete entities (cascades to observations and relations):

```json
{
  "tool": "delete_entities",
  "arguments": {
    "entityNames": ["WAL Mode"]
  }
}
```

Remove specific observations:

```json
{
  "tool": "delete_observations",
  "arguments": {
    "deletions": [
      {
        "entityName": "FastMCP",
        "observations": ["Version 2.0+ required for this server"]
      }
    ]
  }
}
```

Remove specific relations:

```json
{
  "tool": "delete_relations",
  "arguments": {
    "relations": [
      {
        "from": "SQLite Memory MCP",
        "to": "FastMCP",
        "relationType": "depends_on"
      }
    ]
  }
}
```

## Session Tracking

Save a session snapshot:

```json
{
  "tool": "session_save",
  "arguments": {
    "session_id": "abc-123-def",
    "project": "sqlite-memory-mcp",
    "summary": "Implemented FTS5 search. Fixed BM25 ranking for multi-word queries.",
    "active_files": ["server.py", "README.md"]
  }
}
```

Recall recent sessions:

```json
{
  "tool": "session_recall",
  "arguments": {
    "last_n": 3
  }
}
```

## Cross-Project Search

Search entities scoped to a specific project:

```json
{
  "tool": "search_by_project",
  "arguments": {
    "project": "sqlite-memory-mcp",
    "query": "FTS5"
  }
}
```

This returns only entities whose `project` field matches, ranked by FTS5 BM25 relevance.
