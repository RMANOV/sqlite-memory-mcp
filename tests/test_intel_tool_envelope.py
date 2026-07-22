"""Regression tests for the shared intel-server DB tool envelope."""

from __future__ import annotations

import inspect
import json

import intel_server


class _ConnectionContext:
    def __init__(self, events, connection):
        self.events = events
        self.connection = connection

    def __enter__(self):
        self.events.append("enter")
        return self.connection

    def __exit__(self, exc_type, exc, traceback):
        self.events.append("rollback" if exc_type else "commit")
        return False


def test_db_tool_hides_connection_and_commits_before_callback(monkeypatch):
    events = []
    connection = object()
    monkeypatch.setattr(
        intel_server,
        "_get_conn",
        lambda: _ConnectionContext(events, connection),
    )

    @intel_server._db_tool(after_commit=lambda: events.append("after_commit"))
    def example(conn, value: int = 3):
        assert conn is connection
        events.append("body")
        return {"value": value}

    assert list(inspect.signature(example).parameters) == ["value"]
    assert json.loads(example(7)) == {"value": 7}
    assert events == ["enter", "body", "commit", "after_commit"]


def test_db_tool_write_uses_immediate_and_maps_failure(monkeypatch):
    events = []
    connection = object()
    monkeypatch.setattr(
        intel_server,
        "_get_conn",
        lambda: (_ for _ in ()).throw(AssertionError("read factory used")),
    )
    monkeypatch.setattr(
        intel_server,
        "_get_conn_immediate",
        lambda: _ConnectionContext(events, connection),
    )

    @intel_server._db_tool(
        write=True,
        error_mapper=lambda exc: {"error": str(exc), "error_type": "mapped"},
        after_commit=lambda: events.append("must_not_run"),
    )
    def example(conn):
        assert conn is connection
        raise ValueError("boom")

    assert json.loads(example()) == {"error": "boom", "error_type": "mapped"}
    assert events == ["enter", "rollback"]


def test_registered_tool_signatures_do_not_expose_connection():
    for tool in (
        intel_server.assess_context,
        intel_server.reflect_status,
        intel_server.debate_post,
        intel_server.debate_signal_check,
    ):
        assert "conn" not in inspect.signature(tool).parameters
