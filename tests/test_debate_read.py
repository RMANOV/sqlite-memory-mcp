"""Tests 24-31: debate_read cursor + filter behavior.

Per CONDUCTOR 2026-05-09T16:35 EEST EXECUTOR INSTRUCTION test plan.
Compound-cursor + pagination cases also covered in test_debate_dao.py.
This file targets watermark resolution + filter combinations.
"""

from __future__ import annotations

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from debate import (
    DebateError,
    init_debate,
    post_message,
    read_messages,
)
from schema import init_db


@pytest.fixture
def topic(tmp_path):
    db_path = str(tmp_path / "debate_read.db")
    init_db(db_path)
    c = sqlite3.connect(db_path, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    init_debate(
        c, topic_id="X1", title="read-tests",
        roles=[
            {"role": "CONDUCTOR", "session_id": "s-cond"},
            {"role": "EXECUTOR", "session_id": "s-exec"},
            {"role": "ADVOCATE", "session_id": "s-adv"},
        ],
        created_by_role="CONDUCTOR",
    )
    yield c, "X1"
    c.close()


def _seed_n(conn, t, n: int, role="EXECUTOR", kind="STATUS", priority="INFO"):
    out = []
    for i in range(n):
        out.append(post_message(
            conn, topic_id=t, role=role,
            priority=priority, kind=kind, body=f"msg{i}",
        ))
    return out


def test_debate_read_uses_role_watermark(topic):
    conn, t = topic
    for body in ("early0", "early1", "early2"):
        post_message(
            conn, topic_id=t, role="EXECUTOR",
            priority="INFO", kind="STATUS", body=body,
        )
    target = post_message(
        conn, topic_id=t, role="EXECUTOR",
        priority="INFO", kind="STATUS", body="watermark anchor",
    )
    post_message(
        conn, topic_id=t, role="EXECUTOR",
        priority="INFO", kind="WATERMARK", body=target["msg_id"],
    )
    for body in ("later0", "later1"):
        post_message(
            conn, topic_id=t, role="EXECUTOR",
            priority="INFO", kind="STATUS", body=body,
        )
    out = read_messages(conn, topic_id=t, role="EXECUTOR")
    bodies = [m["body"] for m in out["messages"]]
    assert "later0" in bodies and "later1" in bodies
    assert "early0" not in bodies and "early1" not in bodies and "early2" not in bodies


def test_debate_read_since_msg_id_overrides_watermark(topic):
    conn, t = topic
    msgs = _seed_n(conn, t, 5)
    out = read_messages(
        conn, topic_id=t, role="EXECUTOR",
        since_msg_id=msgs[2]["msg_id"],
    )
    bodies = [m["body"] for m in out["messages"]]
    assert "msg3" in bodies and "msg4" in bodies
    assert "msg0" not in bodies


def test_debate_read_since_ts_overrides_watermark(topic):
    conn, t = topic
    _seed_n(conn, t, 3)
    out = read_messages(
        conn, topic_id=t, role="EXECUTOR",
        since_ts="2026-01-01T00:00:00Z",
    )
    assert out["count"] == 3


def test_debate_read_kind_filter(topic):
    conn, t = topic
    post_message(conn, topic_id=t, role="EXECUTOR", priority="INFO", kind="STATUS", body="s1")
    post_message(conn, topic_id=t, role="EXECUTOR", priority="H", kind="Q", body="q1")
    post_message(conn, topic_id=t, role="EXECUTOR", priority="INFO", kind="STATUS", body="s2")
    out = read_messages(
        conn, topic_id=t, role="EXECUTOR", kind_filter=["Q"],
    )
    assert out["count"] == 1
    assert out["messages"][0]["body"] == "q1"


def test_debate_read_priority_filter(topic):
    conn, t = topic
    post_message(conn, topic_id=t, role="EXECUTOR", priority="H", kind="Q", body="hi")
    post_message(conn, topic_id=t, role="EXECUTOR", priority="L", kind="Q", body="lo")
    out = read_messages(
        conn, topic_id=t, role="EXECUTOR", priority_filter=["H"],
    )
    assert out["count"] == 1
    assert out["messages"][0]["body"] == "hi"


def test_debate_read_returns_in_ts_ascending_order(topic):
    conn, t = topic
    _seed_n(conn, t, 5)
    out = read_messages(conn, topic_id=t, role="EXECUTOR")
    timestamps = [m["ts"] for m in out["messages"]]
    assert timestamps == sorted(timestamps)


def test_debate_read_does_not_auto_advance_watermark(topic):
    conn, t = topic
    _seed_n(conn, t, 3)
    read_messages(conn, topic_id=t, role="EXECUTOR")
    out2 = read_messages(conn, topic_id=t, role="EXECUTOR")
    assert out2["count"] == 3


def test_debate_read_unlimited_when_default(topic):
    """Default limit=200; small topics fit comfortably."""
    conn, t = topic
    _seed_n(conn, t, 7)
    out = read_messages(conn, topic_id=t, role="EXECUTOR")
    assert out["count"] == 7
    assert out["truncated"] is False
    assert out["limit"] == 200


def test_debate_read_empty_for_new_role_no_watermark(topic):
    """First read by a role with no watermark returns ALL messages from start."""
    conn, t = topic
    _seed_n(conn, t, 3)
    out = read_messages(conn, topic_id=t, role="ADVOCATE")
    assert out["count"] == 3


def test_debate_read_limit_caps_at_max(topic):
    """Caller-provided limit > MAX_READ_LIMIT is capped silently."""
    conn, t = topic
    _seed_n(conn, t, 4)
    out = read_messages(conn, topic_id=t, role="EXECUTOR", limit=99999)
    assert out["limit"] == 1000


def test_debate_read_rejects_negative_limit(topic):
    conn, t = topic
    with pytest.raises(DebateError, match="invalid_limit"):
        read_messages(conn, topic_id=t, role="EXECUTOR", limit=0)


def test_debate_read_rejects_unknown_topic(topic):
    conn, _ = topic
    with pytest.raises(DebateError, match="unknown_topic"):
        read_messages(conn, topic_id="NOPE", role="EXECUTOR")
