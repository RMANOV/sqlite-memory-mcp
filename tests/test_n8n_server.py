"""Tests for the n8n MCP connector (n8n_server.py).

Covers the ADVOCATE-locked acceptance criteria (topic N8N_CONNECTOR_20260528):

* Exact deny-by-default allowlist (governance/debate/bridge/collab/delete/
  entity-mutation tools ABSENT) — proven by exact-equality on the live surface.
* Denied tool rejected (call to a non-allowlisted tool fails).
* Fail-closed bearer auth (missing/empty bearer -> refuse to build).
* HTTP auth reject (no/bad bearer -> 401) and accept (good bearer -> 200).
* No permissive CORS middleware.
* 127.0.0.1 binding default.
* Durable origin tagging of writes (memory_events actor_type='n8n', entity.origin).
* One read + one append happy path.

All tests use temp DBs via SQLITE_MEMORY_DB; the production memory.db is never
touched and no MCP server is started on a real port.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import sqlite3
import sys

import pytest
from fastmcp import Client

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from schema import init_db

BEARER = "test-bearer-token-xyz"

# Tools that MUST NOT be reachable through the n8n surface (locked BLOCK set).
FORBIDDEN_TOOLS = {
    # delete
    "delete_entities",
    "delete_observations",
    "delete_relations",
    # governance / intelligence
    "promote_candidate",
    "govern_fact",
    "reflect_apply",
    "audit_memory",
    "extract_candidate_claims",
    "build_context_pack",
    "assess_context",
    # entity mutations
    "merge_entities",
    "link_task_entity",
    "unlink_task_entity",
    # debate (sample)
    "debate_post",
    "debate_read",
    "debate_init",
    # bridge (sample)
    "bridge_push",
    "bridge_pull",
    "bridge_status",
    # collab (sample)
    "share_knowledge",
    "request_publish",
}


@pytest.fixture
def n8n(tmp_path, monkeypatch):
    """Import n8n_server bound to an isolated temp DB."""
    db_path = str(tmp_path / "n8n_test.db")
    init_db(db_path)
    monkeypatch.setenv("SQLITE_MEMORY_DB", db_path)
    monkeypatch.delenv("SQLITE_MEMORY_N8N_BEARER", raising=False)
    monkeypatch.delenv("SQLITE_MEMORY_N8N_PORT", raising=False)
    # Inject a remote bind via the (intentionally non-existent) host env var to
    # prove it has NO effect — the bind host is hardcoded, not env-driven.
    monkeypatch.setenv("SQLITE_MEMORY_N8N_HOST", "0.0.0.0")
    # Re-import so module-level DB_PATH/HOST/PORT are re-read.
    for mod in (
        "db_utils",
        "schema",
        "server",
        "session_server",
        "task_server",
        "n8n_server",
    ):
        sys.modules.pop(mod, None)
    mod = importlib.import_module("n8n_server")
    mod._DB_PATH = db_path  # noqa: SLF001 - convenience for assertions
    return mod


async def _list_tools(mcp):
    """List tools through the supported in-memory FastMCP client transport."""
    async with Client(mcp) as client:
        return await client.list_tools()


async def _call_tool(mcp, name: str, arguments: dict):
    """Invoke a tool through the supported in-memory FastMCP client transport."""
    async with Client(mcp) as client:
        return await client.call_tool(name, arguments)


def _list_tool_names(mcp) -> set[str]:
    return {t.name for t in asyncio.run(_list_tools(mcp))}


# ── Allowlist / surface ─────────────────────────────────────────────────────


def test_surface_exactly_equals_allowlist(n8n):
    """Exposed tool set is EXACTLY the frozen allowlist — nothing more, nothing less."""
    mcp = n8n.build_n8n_server(BEARER)
    names = _list_tool_names(mcp)
    assert names == set(n8n.ALLOWLIST), (
        f"surface drift: extra={names - set(n8n.ALLOWLIST)} "
        f"missing={set(n8n.ALLOWLIST) - names}"
    )
    assert len(names) == 14


def test_governance_and_dangerous_tools_absent(n8n):
    """No governance/debate/bridge/collab/delete/entity-mutation tool is exposed."""
    mcp = n8n.build_n8n_server(BEARER)
    names = _list_tool_names(mcp)
    leaked = names & FORBIDDEN_TOOLS
    assert leaked == set(), f"forbidden tools leaked onto n8n surface: {leaked}"
    # Belt-and-braces: nothing prefixed debate_/bridge_ and no delete_*.
    assert not any(n.startswith(("debate_", "bridge_")) for n in names)
    assert not any(n.startswith("delete_") for n in names)


def test_denied_tool_call_is_rejected(n8n):
    """Calling a non-allowlisted (denied) tool fails — it is not on the surface."""
    mcp = n8n.build_n8n_server(BEARER)
    with pytest.raises(Exception):
        asyncio.run(_call_tool(mcp, "delete_entities", {"entityNames": ["x"]}))
    with pytest.raises(Exception):
        asyncio.run(_call_tool(mcp, "promote_candidate", {}))


# ── Fail-closed bearer auth ─────────────────────────────────────────────────


def test_build_fails_closed_when_bearer_missing(n8n, monkeypatch):
    """No bearer env + no arg -> refuse to build (fail-closed)."""
    monkeypatch.delenv("SQLITE_MEMORY_N8N_BEARER", raising=False)
    with pytest.raises(RuntimeError):
        n8n.build_n8n_server()


def test_build_fails_closed_when_bearer_empty(n8n):
    """Empty / whitespace bearer is never a valid credential."""
    with pytest.raises(RuntimeError):
        n8n.build_n8n_server("")
    with pytest.raises(RuntimeError):
        n8n.build_n8n_server("   ")


def test_build_fails_closed_when_env_bearer_empty(n8n, monkeypatch):
    """Empty env var -> refuse to build even with no explicit arg."""
    monkeypatch.setenv("SQLITE_MEMORY_N8N_BEARER", "")
    with pytest.raises(RuntimeError):
        n8n.build_n8n_server()


def test_build_succeeds_with_env_bearer(n8n, monkeypatch):
    """A non-empty env bearer is accepted and yields the full allowlist."""
    monkeypatch.setenv("SQLITE_MEMORY_N8N_BEARER", BEARER)
    mcp = n8n.build_n8n_server()
    assert _list_tool_names(mcp) == set(n8n.ALLOWLIST)


# ── HTTP auth (ASGI, no real port) ──────────────────────────────────────────


def _http_status(app, headers, payload):
    """Drive the ASGI app once and return the HTTP status code."""
    import httpx
    from httpx import ASGITransport

    base_headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }

    async def go():
        async with app.router.lifespan_context(app):
            transport = ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://testserver",
                follow_redirects=True,
            ) as client:
                r = await client.post(
                    "/mcp", json=payload, headers={**base_headers, **headers}
                )
                return r.status_code

    return asyncio.run(go())


def test_http_rejects_missing_bearer(n8n):
    """No Authorization header -> 401."""
    app = n8n.build_n8n_server(BEARER).http_app()
    status = _http_status(app, {}, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert status == 401


def test_http_rejects_bad_bearer(n8n):
    """Wrong bearer token -> 401."""
    app = n8n.build_n8n_server(BEARER).http_app()
    status = _http_status(
        app,
        {"Authorization": "Bearer not-the-token"},
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    assert status == 401


def test_http_accepts_good_bearer(n8n):
    """Correct bearer + initialize -> 200."""
    app = n8n.build_n8n_server(BEARER).http_app()
    status = _http_status(
        app,
        {"Authorization": f"Bearer {BEARER}"},
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "n8n-test", "version": "1"},
            },
        },
    )
    assert status == 200


# ── CORS / binding posture ──────────────────────────────────────────────────


def test_no_permissive_cors_middleware(n8n):
    """No CORSMiddleware is installed by the n8n app."""
    app = n8n.build_n8n_server(BEARER).http_app()
    names = []
    for m in app.user_middleware:
        cls = getattr(m, "cls", None)
        names.append(cls.__name__ if cls is not None else type(m).__name__)
    assert not any("CORS" in name for name in names), (
        f"CORS middleware unexpectedly present: {names}"
    )


def test_host_is_hardcoded_loopback(n8n):
    """Bind host is the hardcoded loopback constant 127.0.0.1."""
    assert n8n.HOST == "127.0.0.1"


def test_host_env_override_has_no_effect(n8n):
    """Setting SQLITE_MEMORY_N8N_HOST=0.0.0.0 must NOT change the bind host.

    The fixture sets the env var to a remote bind target before import. Because
    the host is a hardcoded constant (no os.environ read), it stays loopback —
    proving there is no remote-bind capability via env injection.
    """
    assert os.environ.get("SQLITE_MEMORY_N8N_HOST") == "0.0.0.0"
    assert n8n.HOST == "127.0.0.1"


def test_assert_loopback_refuses_non_loopback_hosts(n8n):
    """The startup guard refuses every non-loopback bind host (fail-closed)."""
    for bad in ("0.0.0.0", "::", "192.168.1.10", "10.0.0.5", "example.com"):
        with pytest.raises(RuntimeError):
            n8n._assert_loopback(bad)  # noqa: SLF001


def test_assert_loopback_accepts_loopback_hosts(n8n):
    """The startup guard accepts the legitimate loopback spellings."""
    for good in ("127.0.0.1", "::1", "localhost"):
        assert n8n._assert_loopback(good) == good  # noqa: SLF001


def test_no_host_env_var_is_read_in_source(n8n):
    """Source must not READ any host env var — no remote-bind escape hatch.

    A host env var may be *mentioned* in comments/docstrings (explaining its
    deliberate absence), but it must never appear inside an ``os.environ`` /
    ``os.getenv`` read. AST-walk every env-read call and assert no host var.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(n8n))
    host_env_reads = []
    for node in ast.walk(tree):
        # os.environ.get("X") / os.getenv("X")
        if isinstance(node, ast.Call):
            func = node.func
            is_env_read = False
            if isinstance(func, ast.Attribute) and func.attr in ("get", "getenv"):
                target = func.value
                if (isinstance(target, ast.Attribute) and target.attr == "environ") or (
                    isinstance(target, ast.Name) and target.id == "os"
                ):
                    is_env_read = True
            if is_env_read and node.args and isinstance(node.args[0], ast.Constant):
                key = node.args[0].value
                if isinstance(key, str) and "HOST" in key.upper():
                    host_env_reads.append(key)
        # os.environ["X"]
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Attribute):
            if node.value.attr == "environ" and isinstance(node.slice, ast.Constant):
                key = node.slice.value
                if isinstance(key, str) and "HOST" in key.upper():
                    host_env_reads.append(key)
    assert host_env_reads == [], (
        f"n8n_server reads host env var(s) {host_env_reads}; bind host must be "
        "hardcoded with no env override."
    )


# ── Origin tagging / happy paths ────────────────────────────────────────────


def test_append_happy_path_and_durable_origin_tagging(n8n):
    """One append: entity created, entity.origin stamped, audit event recorded."""
    mcp = n8n.build_n8n_server(BEARER)
    result = asyncio.run(
        _call_tool(
            mcp,
            "create_entities",
            {
                "entities": [
                    {
                        "name": "N8N-Origin-Entity",
                        "entityType": "note",
                        "observations": ["created from an n8n workflow"],
                    }
                ],
                "source": "workflow-7",
                "request_context": {"node": "Set", "execution_id": "e1"},
            },
        )
    )
    payload = json.loads(result.structured_content["result"])
    assert payload["created"] == 1

    db_path = n8n._DB_PATH  # noqa: SLF001
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        # entities.origin durably stamped
        row = conn.execute(
            "SELECT origin FROM entities WHERE name = ?", ("N8N-Origin-Entity",)
        ).fetchone()
        assert row is not None
        assert row["origin"] == "n8n:workflow-7"

        # durable audit event in the causal ledger
        ev = conn.execute(
            "SELECT actor_type, actor_id, tool_name, payload_json "
            "FROM memory_events WHERE actor_type = 'n8n' "
            "AND tool_name = 'sqlite-n8n.create_entities'"
        ).fetchone()
        assert ev is not None
        assert ev["actor_type"] == "n8n"
        assert ev["actor_id"] == "n8n:workflow-7"
        ctx = json.loads(ev["payload_json"])
        assert ctx["request_context"]["execution_id"] == "e1"
    finally:
        conn.close()


def test_read_happy_path(n8n):
    """One read: appended entity is visible through read_graph."""
    mcp = n8n.build_n8n_server(BEARER)
    asyncio.run(
        _call_tool(
            mcp,
            "create_entities",
            {
                "entities": [
                    {
                        "name": "Readable-Entity",
                        "entityType": "note",
                        "observations": [],
                    }
                ],
                "source": "wf-read",
            },
        )
    )
    result = asyncio.run(_call_tool(mcp, "read_graph", {"offset": 0, "limit": 50}))
    assert "Readable-Entity" in str(result.structured_content)


def test_all_write_tools_audit_uniformly(n8n):
    """Every write tool (5) records an n8n-origin audit event (criterion #3)."""
    mcp = n8n.build_n8n_server(BEARER)
    # seed an entity for add_observations / create_relations
    asyncio.run(
        _call_tool(
            mcp,
            "create_entities",
            {
                "entities": [
                    {"name": "A", "entityType": "note", "observations": []},
                    {"name": "B", "entityType": "note", "observations": []},
                ],
                "source": "seed",
            },
        )
    )
    asyncio.run(
        _call_tool(
            mcp,
            "add_observations",
            {
                "observations": [{"entityName": "A", "contents": ["obs1"]}],
                "source": "wf",
            },
        )
    )
    asyncio.run(
        _call_tool(
            mcp,
            "create_relations",
            {
                "relations": [{"from": "A", "to": "B", "relationType": "links_to"}],
                "source": "wf",
            },
        )
    )
    create_res = asyncio.run(
        _call_tool(mcp, "create_task_or_note", {"title": "T1", "source": "wf"})
    )
    task_id = json.loads(create_res.structured_content["result"])["task_id"]
    asyncio.run(
        _call_tool(
            mcp, "update_task", {"task_id": task_id, "status": "done", "source": "wf"}
        )
    )

    conn = sqlite3.connect(n8n._DB_PATH)  # noqa: SLF001
    conn.row_factory = sqlite3.Row
    try:
        tools = {
            r["tool_name"]
            for r in conn.execute(
                "SELECT DISTINCT tool_name FROM memory_events WHERE actor_type = 'n8n'"
            ).fetchall()
        }
    finally:
        conn.close()
    expected = {
        "sqlite-n8n.create_entities",
        "sqlite-n8n.add_observations",
        "sqlite-n8n.create_relations",
        "sqlite-n8n.create_task_or_note",
        "sqlite-n8n.update_task",
    }
    assert expected <= tools, f"missing audit events for: {expected - tools}"
