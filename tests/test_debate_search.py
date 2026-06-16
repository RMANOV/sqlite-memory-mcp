"""B5: debate_search — read-only LIKE-over-body search (Option A).

Proves the contract of the ``intel_server.debate_search`` MCP tool:

  (a) SCOPED   — only matches within the given topic_id, never bleeds in
                 messages from other topics.
  (b) BOUNDED  — honours and clamps ``limit`` (1..500; <=0/non-int → 1).
  (c) SAFE     — ``%``, ``_``, single-quote and backslash in the query are
                 treated LITERALLY (parameterized; no wildcard injection,
                 no SQL error).
  (d) READ-ONLY — calling it mutates no row (row counts before == after).

Mirrors the wrapper-DB style of test_debate_flexible_roles.py: monkeypatch
``intel_server._get_conn`` / ``_get_conn_immediate`` onto a tmp DB, then call
the ``@mcp.tool()`` wrappers directly.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import db_utils  # noqa: E402
import intel_server  # noqa: E402
from schema import init_db  # noqa: E402


@pytest.fixture
def wrapper_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "memory.db")
    init_db(db_path)
    monkeypatch.setattr(
        intel_server, "_get_conn", lambda: db_utils.get_conn(db_path)
    )
    monkeypatch.setattr(
        intel_server,
        "_get_conn_immediate",
        lambda: db_utils.get_conn_immediate(db_path),
    )
    for name in (
        "SQLITE_MEMORY_DEBATE_GATE_ENABLED",
        "SQLITE_MEMORY_DEBATE_GATE_DISABLED",
    ):
        monkeypatch.delenv(name, raising=False)
    roles_json = json.dumps(
        [
            {"role": "CONDUCTOR", "session_id": "codex-cond20260531"},
            {"role": "EXECUTOR", "session_id": "codex-exec20260531"},
        ]
    )
    for topic_id, title in (("SRCH1", "search topic one"),
                            ("SRCH2", "search topic two")):
        out = json.loads(
            intel_server.debate_init(
                topic_id=topic_id,
                title=title,
                roles_json=roles_json,
                created_by_role="CONDUCTOR",
                metadata_json=json.dumps(
                    {"priority_lane": "P2",
                     "priority_reason": "debate_search B5 test"}
                ),
            )
        )
        assert "error_type" not in out, out
    return db_path


def _post(topic_id: str, body: str, *, role: str = "EXECUTOR",
          kind: str = "STATUS", priority: str = "INFO") -> dict:
    out = json.loads(
        intel_server.debate_post(
            topic_id=topic_id, role=role,
            priority=priority, kind=kind, body=body,
        )
    )
    assert "error_type" not in out, out
    return out


def _search(topic_id: str, query: str, limit: int = 50) -> dict:
    out = json.loads(intel_server.debate_search(topic_id, query, limit))
    return out


def _row_count(db_path: str) -> int:
    c = sqlite3.connect(db_path)
    try:
        return c.execute("SELECT COUNT(*) FROM debate_messages").fetchone()[0]
    finally:
        c.close()


# (a) SCOPED ─────────────────────────────────────────────────────────────
def test_search_is_scoped_to_topic_id(wrapper_db):
    _post("SRCH1", "alpha needle one")
    _post("SRCH1", "beta filler")
    _post("SRCH2", "alpha needle two")  # same token, OTHER topic

    out = _search("SRCH1", "needle")
    assert "error_type" not in out, out
    bodies = [m["body"] for m in out["messages"]]
    assert bodies == ["alpha needle one"]
    assert out["count"] == 1
    # the SRCH2 message with the same token must NOT leak in
    assert all(m["topic_id"] == "SRCH1" for m in out["messages"])
    assert "alpha needle two" not in bodies


def test_search_empty_query_matches_all_in_topic_only(wrapper_db):
    _post("SRCH1", "one")
    _post("SRCH1", "two")
    _post("SRCH2", "other-topic")

    out = _search("SRCH1", "")
    bodies = sorted(m["body"] for m in out["messages"])
    assert bodies == ["one", "two"]
    assert all(m["topic_id"] == "SRCH1" for m in out["messages"])


def test_search_returns_debate_read_column_shape(wrapper_db):
    posted = _post("SRCH1", "shape-check body")
    out = _search("SRCH1", "shape-check")
    assert out["count"] == 1
    msg = out["messages"][0]
    expected_keys = {
        "msg_id", "topic_id", "role", "ts", "priority",
        "kind", "reply_to", "standing", "body", "created_at",
    }
    assert set(msg.keys()) == expected_keys
    assert msg["msg_id"] == posted["msg_id"]


def test_search_orders_newest_first(wrapper_db):
    a = _post("SRCH1", "match a")
    b = _post("SRCH1", "match b")
    c = _post("SRCH1", "match c")
    out = _search("SRCH1", "match")
    ids = [m["msg_id"] for m in out["messages"]]
    # ORDER BY ts DESC, msg_id DESC → newest first
    assert ids == [c["msg_id"], b["msg_id"], a["msg_id"]]


# (b) BOUNDED ────────────────────────────────────────────────────────────
def test_search_respects_limit(wrapper_db):
    for i in range(10):
        _post("SRCH1", f"limited needle {i}")
    out = _search("SRCH1", "needle", limit=3)
    assert out["count"] == 3
    assert out["limit"] == 3
    assert len(out["messages"]) == 3


def test_search_clamps_limit_to_max_500(wrapper_db):
    _post("SRCH1", "cap needle")
    out = _search("SRCH1", "needle", limit=10_000)
    assert out["limit"] == 500


@pytest.mark.parametrize("bad", [0, -1, -999])
def test_search_clamps_nonpositive_limit_to_one(wrapper_db, bad):
    _post("SRCH1", "needle one")
    _post("SRCH1", "needle two")
    out = _search("SRCH1", "needle", limit=bad)
    assert out["limit"] == 1
    assert out["count"] == 1


# (c) SAFE / PARAMETERIZED ───────────────────────────────────────────────
def test_search_percent_is_literal_not_wildcard(wrapper_db):
    _post("SRCH1", "discount 50% off")
    _post("SRCH1", "no percent here")  # would match if % were a wildcard
    out = _search("SRCH1", "50%")
    bodies = [m["body"] for m in out["messages"]]
    assert bodies == ["discount 50% off"]
    assert out["count"] == 1


def test_search_underscore_is_literal_not_wildcard(wrapper_db):
    _post("SRCH1", "user_name field")
    _post("SRCH1", "userXname collision")  # matches if _ were single-char wild
    out = _search("SRCH1", "user_name")
    bodies = [m["body"] for m in out["messages"]]
    assert bodies == ["user_name field"]
    assert out["count"] == 1


def test_search_single_quote_is_safe(wrapper_db):
    _post("SRCH1", "O'Brien said hello")
    _post("SRCH1", "unrelated body")
    out = _search("SRCH1", "O'Brien")
    assert "error_type" not in out, out
    assert [m["body"] for m in out["messages"]] == ["O'Brien said hello"]


def test_search_backslash_is_literal(wrapper_db):
    _post("SRCH1", r"path C:\temp\file")
    _post("SRCH1", "no backslash present")
    out = _search("SRCH1", "C:\\temp")
    assert "error_type" not in out, out
    bodies = [m["body"] for m in out["messages"]]
    assert bodies == [r"path C:\temp\file"]


def test_search_backslash_query_does_not_error(wrapper_db):
    # A lone trailing backslash must escape cleanly, never raising.
    _post("SRCH1", "plain body")
    out = _search("SRCH1", "\\")
    assert "error_type" not in out, out
    # no body contains a literal backslash → zero matches, no error
    assert out["count"] == 0


def test_search_combined_special_chars_literal(wrapper_db):
    _post("SRCH1", r"weird %_\' token")
    _post("SRCH1", "anything else entirely")
    out = _search("SRCH1", r"%_\'")
    assert "error_type" not in out, out
    assert [m["body"] for m in out["messages"]] == [r"weird %_\' token"]


# (d) READ-ONLY ──────────────────────────────────────────────────────────
def test_search_is_read_only(wrapper_db):
    db_path = wrapper_db
    _post("SRCH1", "first body")
    _post("SRCH1", "second needle body")
    _post("SRCH2", "third body")

    before = _row_count(db_path)
    # exercise every branch: a match, a no-match, special chars, big limit
    _search("SRCH1", "needle")
    _search("SRCH1", "no-such-token")
    _search("SRCH1", "100%")
    _search("SRCH1", "", limit=10_000)
    _search("SRCH2", "body")
    after = _row_count(db_path)

    assert before == after, "debate_search must not mutate any row"


# error handling ─────────────────────────────────────────────────────────
def test_search_unknown_topic_returns_error(wrapper_db):
    out = _search("NOPE9", "anything")
    assert "error_type" in out, out


def test_search_invalid_topic_id_returns_error(wrapper_db):
    out = _search("bad id!!", "anything")
    assert "error_type" in out, out
