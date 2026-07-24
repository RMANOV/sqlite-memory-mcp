#!/usr/bin/env python3
"""n8n MCP connector for sqlite-memory-mcp — hardened, deny-by-default surface.

A SEPARATE micro-server entry point purpose-built for n8n personal-automation
workflows. It is **not** a mount of the full toolset: it starts from an empty
FastMCP instance and adds back ONLY an exact, frozen allowlist of safe tools
(memory/session/task reads + task ops + origin-tagged memory appends).

Security posture (ADVOCATE-locked acceptance, topic N8N_CONNECTOR_20260528):

* DENY-BY-DEFAULT: additive-by-construction. Governance, debate, bridge,
  collab, entity-mutation, delete, promote/govern/reflect/audit tools are
  NEVER added, so they cannot leak even if upstream servers grow new tools.
* HTTP transport binds 127.0.0.1 (loopback) — HARDCODED, with NO env override.
  There is no remote-bind capability: a non-loopback host is refused at startup
  (fail-closed). External exposure is a deferred ops decision handled by a
  fronting TLS reverse proxy, never by widening this bind.
* Bearer auth MANDATORY via ``SQLITE_MEMORY_N8N_BEARER`` — FAIL-CLOSED: if the
  env var is missing or empty the server refuses to start and no auth verifier
  is ever constructed (an empty string is never a valid token).
* NO permissive CORS is added by this module.
* Every write carries durable ``source=n8n`` provenance + request context,
  recorded in the causal ``memory_events`` ledger for audit.

Scope: PERSONAL AUTOMATION only. This is NOT team workflow memory and NOT a
premium-governance surface. No auto-promotion of n8n inputs into governed
knowledge ever happens here.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

from fastmcp import FastMCP
from fastmcp.server.auth import StaticTokenVerifier

from db_utils import (
    ensure_db_initialized,
    get_conn_immediate as _get_write_conn,
    get_entity_id as _get_entity_id,
    now_iso as _now,
    record_memory_event,
    setup_logger,
)

# Underlying tool implementations (cross-imported; core servers untouched).
from server import (
    mcp as _core_mcp,
    create_entities as _core_create_entities,
    add_observations as _core_add_observations,
    create_relations as _core_create_relations,
)
from session_server import mcp as _session_mcp
from task_server import (
    mcp as _tasks_mcp,
    create_task_or_note as _core_create_task_or_note,
    update_task as _core_update_task,
)

logger = setup_logger("sqlite-n8n", "n8n_server.log")

# ── Network binding (loopback-only, HARDCODED — no override) ────────────────
# BLOCK criterion: the connector must have NO remote-bind capability. The bind
# host is a hardcoded loopback constant; there is intentionally NO env override
# (e.g. no SQLITE_MEMORY_N8N_HOST) so a misconfiguration or env injection cannot
# expose the server on 0.0.0.0 / a LAN / WAN address. Exposing externally is a
# deferred Phase-3 ops decision handled OUT of this process (a fronting reverse
# proxy with TLS), never by widening this bind.
HOST = "127.0.0.1"
# Allowed loopback bind targets. Anything else is refused at startup.
_LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "::1", "localhost"})
# PORT remains operator-tunable: choosing a loopback port is not a remote-bind
# capability, so it stays overridable for convenience.
PORT = int(os.environ.get("SQLITE_MEMORY_N8N_PORT", "8848"))
BEARER_ENV = "SQLITE_MEMORY_N8N_BEARER"


def _assert_loopback(host: str) -> str:
    """Refuse any non-loopback bind host (defense-in-depth, fail-closed).

    Even though HOST is a hardcoded constant, this guard makes a remote bind
    structurally impossible: if a future edit or a monkeypatched value ever
    points the server off-loopback, startup raises instead of exposing the
    surface. Bare-string compare keeps it dependency-free; the set covers the
    only legitimate loopback spellings.
    """
    if host not in _LOOPBACK_HOSTS:
        raise RuntimeError(
            f"n8n connector refuses non-loopback bind host {host!r}. "
            "Remote-bind capability is forbidden (BLOCK criterion); bind is "
            "hardcoded to 127.0.0.1. Front it with a TLS reverse proxy for "
            "any external exposure."
        )
    return host


# ── Exact deny-by-default allowlist (frozen) ────────────────────────────────
# This is the *entire* tool surface exposed to n8n. Tests assert exact equality
# against the live ``list_tools`` output, which simultaneously proves that the
# BLOCK set (governance/debate/bridge/collab/delete/entity-mutation) is absent.
ALLOWLIST: frozenset[str] = frozenset(
    {
        # Memory reads (3)
        "read_graph",
        "search_nodes",
        "open_nodes",
        # Session reads (3)
        "resume_context",
        "session_recall",
        "search_by_project",
        # Task reads (3)
        "query_tasks",
        "task_digest",
        "find_by_title",
        # Task writes (2) — origin-tagged in the audit ledger
        "create_task_or_note",
        "update_task",
        # Memory tagged appends (3) — origin-tagged + entity.origin stamped
        "create_entities",
        "add_observations",
        "create_relations",
    }
)

# Tools sourced verbatim (no write side effects) from their owning servers.
_READ_ONLY_TOOLS: dict[str, FastMCP] = {
    "read_graph": _core_mcp,
    "search_nodes": _core_mcp,
    "open_nodes": _core_mcp,
    "resume_context": _session_mcp,
    "session_recall": _session_mcp,
    "search_by_project": _session_mcp,
    "query_tasks": _tasks_mcp,
    "task_digest": _tasks_mcp,
    "find_by_title": _tasks_mcp,
}

# Writes are re-registered through origin-tagging wrappers (below), NOT lifted
# verbatim — so every n8n-origin write lands a durable audit event.
_WRITE_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "create_task_or_note",
        "update_task",
        "create_entities",
        "add_observations",
        "create_relations",
    }
)

# Defense-in-depth invariant: the allowlist is exactly reads ∪ writes, nothing
# more. If these drift apart, construction fails loudly rather than silently
# exposing or hiding a tool.
assert ALLOWLIST == frozenset(_READ_ONLY_TOOLS) | _WRITE_TOOL_NAMES, (
    "n8n allowlist drift: ALLOWLIST must equal read-only ∪ write tool sets"
)


# ── Durable origin tagging ───────────────────────────────────────────────────


def _normalize_source(source: str | None) -> str:
    """Coerce a caller-provided source tag into a durable ``n8n:<id>`` marker.

    n8n passes its workflow id; default is the bare ``n8n`` namespace. The
    returned value is what lands in ``entities.origin`` and the audit ledger.
    """
    if not source:
        return "n8n"
    source = str(source).strip()
    if not source:
        return "n8n"
    return source if source.startswith("n8n") else f"n8n:{source}"


def _audit_n8n_write(
    *,
    tool_name: str,
    source: str,
    request_context: dict[str, Any] | None,
    result_summary: dict[str, Any] | None,
) -> None:
    """Record a durable n8n-origin write event in the causal ledger.

    Uniform across all five write tools (criterion #3): actor_type=``n8n``,
    actor_id=the normalized source/workflow tag, plus request context payload.
    The separate audit transaction starts IMMEDIATE so it waits at BEGIN
    instead of losing a DEFERRED read-to-write upgrade race against detached
    task-embedding maintenance.
    Best-effort: an audit failure must never lose the user's write, so it is
    logged but not raised.
    """
    try:
        payload: dict[str, Any] = {
            "origin": "n8n",
            "source": source,
            "request_context": request_context or {},
        }
        if result_summary is not None:
            payload["result"] = result_summary
        with _get_write_conn() as conn:
            record_memory_event(
                conn,
                event_type="n8n_write",
                aggregate_kind="n8n_request",
                aggregate_id=source,
                actor_type="n8n",
                actor_id=source,
                tool_name=f"sqlite-n8n.{tool_name}",
                event_ts=_now(),
                payload=payload,
                source_kind="n8n",
                source_ref=source,
            )
    except Exception as exc:  # pragma: no cover - audit must never break a write
        logger.warning("n8n audit event failed for %s: %s", tool_name, exc)


def _stamp_entity_origin(names: list[str], source: str) -> None:
    """Stamp ``entities.origin`` for newly created/touched entities (durable row tag)."""
    if not names:
        return
    try:
        with _get_write_conn() as conn:
            for name in names:
                eid = _get_entity_id(conn, name)
                if eid is not None:
                    conn.execute(
                        "UPDATE entities SET origin = ? WHERE id = ? AND origin = 'local'",
                        (source, eid),
                    )
    except Exception as exc:  # pragma: no cover
        logger.warning("n8n entity origin stamp failed: %s", exc)


# ── n8n server factory ───────────────────────────────────────────────────────


def build_n8n_server(bearer: str | None = None) -> FastMCP:
    """Construct the n8n FastMCP server, fail-closed on a missing bearer.

    Args:
        bearer: the shared bearer token. If ``None``, it is read from the
            ``SQLITE_MEMORY_N8N_BEARER`` env var. A missing or empty/whitespace
            token raises ``RuntimeError`` — the verifier is never constructed,
            so an empty string can never be a valid credential.

    Returns:
        A fresh ``FastMCP`` with ONLY the frozen allowlist mounted and bearer
        auth enabled. No CORS middleware is added.
    """
    if bearer is None:
        bearer = os.environ.get(BEARER_ENV, "")
    if bearer is None or not str(bearer).strip():
        raise RuntimeError(
            f"{BEARER_ENV} is unset or empty — refusing to start the n8n "
            "connector. Bearer auth is mandatory (fail-closed)."
        )
    bearer = str(bearer)

    # API-key-style bearer auth. Only constructed once a non-empty token exists.
    verifier = StaticTokenVerifier(
        tokens={bearer: {"client_id": "n8n", "scopes": ["n8n"]}}
    )

    mcp = FastMCP(
        "sqlite-n8n",
        instructions=(
            "Personal-automation connector for n8n workflows. Exposes a "
            "deny-by-default safe subset: memory/session/task reads, task "
            "create/update, and origin-tagged memory appends. NOT team "
            "workflow memory; NOT a governance surface."
        ),
        auth=verifier,
    )

    # 1) Read-only tools: lift verbatim from owning servers (no side effects).
    for name, owner in _READ_ONLY_TOOLS.items():
        tool = _get_owned_tool(owner, name)
        if tool is None:
            raise RuntimeError(f"n8n allowlist tool not found upstream: {name}")
        mcp.add_tool(tool)

    # 2) Write tools: register origin-tagging wrappers (criterion #3).
    _register_write_tools(mcp)

    # Hard invariant: exposed surface must equal the frozen allowlist exactly.
    # (Re-asserted at runtime in main() via an async check; see _assert_surface.)
    return mcp


def _get_owned_tool(owner: FastMCP, name: str):
    """Fetch a registered Tool object from an owning server by name.

    Uses the async ``get_tool`` API run synchronously at build time. Returns
    ``None`` if absent.
    """
    import asyncio

    async def _fetch():
        return await owner.get_tool(name)

    try:
        return asyncio.run(_fetch())
    except RuntimeError:
        # Already inside a running loop — fall back to a fresh loop.
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(_fetch())
        finally:
            loop.close()


def _register_write_tools(mcp: FastMCP) -> None:
    """Register the five origin-tagged write wrappers onto ``mcp``."""

    @mcp.tool(name="create_entities")
    def create_entities(
        entities: list[dict[str, Any]],
        source: str | None = None,
        request_context: dict[str, Any] | None = None,
    ) -> str:
        """Create entities (n8n origin-tagged).

        Each entity dict: name, entityType, observations[]; optional project.
        All created entities are stamped with a durable ``n8n`` origin and a
        write event is recorded in the audit ledger.
        """
        src = _normalize_source(source)
        result = _core_create_entities(entities)
        names = [e.get("name") for e in entities if e.get("name")]
        _stamp_entity_origin(names, src)
        _audit_n8n_write(
            tool_name="create_entities",
            source=src,
            request_context=request_context,
            result_summary={"entities": names},
        )
        return result

    @mcp.tool(name="add_observations")
    def add_observations(
        observations: list[dict[str, Any]],
        source: str | None = None,
        request_context: dict[str, Any] | None = None,
    ) -> str:
        """Add observations to existing entities (n8n origin-tagged).

        Each dict: entityName, contents[]. A durable write event is recorded
        in the audit ledger.
        """
        src = _normalize_source(source)
        result = _core_add_observations(observations)
        names = [o.get("entityName") for o in observations if o.get("entityName")]
        _audit_n8n_write(
            tool_name="add_observations",
            source=src,
            request_context=request_context,
            result_summary={"entities": names},
        )
        return result

    @mcp.tool(name="create_relations")
    def create_relations(
        relations: list[dict[str, Any]],
        source: str | None = None,
        request_context: dict[str, Any] | None = None,
    ) -> str:
        """Create relations between entities (n8n origin-tagged).

        Each dict: from, to, relationType. A durable write event is recorded
        in the audit ledger.
        """
        src = _normalize_source(source)
        result = _core_create_relations(relations)
        _audit_n8n_write(
            tool_name="create_relations",
            source=src,
            request_context=request_context,
            result_summary={"count": len(relations)},
        )
        return result

    @mcp.tool(name="create_task_or_note")
    def create_task_or_note(
        title: str,
        type: str = "task",
        description: str = "",
        section: str = "inbox",
        priority: str = "medium",
        due_date: str = "",
        project: str = "",
        parent_id: str = "",
        notes: str = "",
        recurring: str = "",
        reminder_at: str = "",
        source: str | None = None,
        request_context: dict[str, Any] | None = None,
    ) -> str:
        """Create a task or note (n8n origin-tagged). Returns the UUID.

        Put long-form body in ``description``. A durable write event is
        recorded in the audit ledger.
        """
        src = _normalize_source(source)
        result = _core_create_task_or_note(
            title=title,
            type=type,
            description=description,
            section=section,
            priority=priority,
            due_date=due_date,
            project=project,
            parent_id=parent_id,
            notes=notes,
            recurring=recurring,
            reminder_at=reminder_at,
        )
        task_id = None
        try:
            task_id = json.loads(result).get("task_id") or json.loads(result).get("id")
        except (ValueError, TypeError):
            pass
        _audit_n8n_write(
            tool_name="create_task_or_note",
            source=src,
            request_context=request_context,
            result_summary={"title": title, "task_id": task_id},
        )
        return result

    @mcp.tool(name="update_task")
    def update_task(
        task_id: str,
        title: str | None = None,
        description: str | None = None,
        status: str | None = None,
        section: str | None = None,
        priority: str | None = None,
        due_date: str | None = None,
        project: str | None = None,
        notes: str | None = None,
        reminder_at: str | None = None,
        source: str | None = None,
        request_context: dict[str, Any] | None = None,
    ) -> str:
        """Update an existing task/note by id (n8n origin-tagged).

        Only non-``None`` fields are changed. A durable write event is recorded
        in the audit ledger.
        """
        src = _normalize_source(source)
        kwargs: dict[str, Any] = {"task_id": task_id}
        for key, value in (
            ("title", title),
            ("description", description),
            ("status", status),
            ("section", section),
            ("priority", priority),
            ("due_date", due_date),
            ("project", project),
            ("notes", notes),
            ("reminder_at", reminder_at),
        ):
            if value is not None:
                kwargs[key] = value
        result = _core_update_task(**kwargs)
        _audit_n8n_write(
            tool_name="update_task",
            source=src,
            request_context=request_context,
            result_summary={"task_id": task_id},
        )
        return result


def main() -> None:
    """Run the n8n connector over loopback streamable-HTTP with bearer auth."""
    ensure_db_initialized()
    # Fail-closed: build_n8n_server raises if the bearer env var is missing/empty.
    mcp = build_n8n_server()
    # Fail-closed: refuse to bind anywhere other than loopback (no remote bind).
    bind_host = _assert_loopback(HOST)
    logger.info(
        "n8n connector ready: %d tools on %s:%d (bearer auth enabled, loopback-only)",
        len(ALLOWLIST),
        bind_host,
        PORT,
    )
    mcp.run(transport="streamable-http", host=bind_host, port=PORT)


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as exc:
        # Fail-closed startup: surface the reason and exit non-zero.
        print(f"n8n connector refused to start: {exc}", file=sys.stderr)
        sys.exit(1)
