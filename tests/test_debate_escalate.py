"""Tests 38-40: debate_escalate behavior.

Per CONDUCTOR 2026-05-09T16:35 EEST EXECUTOR INSTRUCTION test plan.
"""

from __future__ import annotations

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from debate import DebateError, escalate, init_debate, transition_state
from schema import init_db


@pytest.fixture
def topic(tmp_path):
    db_path = str(tmp_path / "debate_escalate.db")
    init_db(db_path)
    c = sqlite3.connect(db_path, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    init_debate(
        c, topic_id="X1", title="escalate-tests",
        roles=[
            {"role": "CONDUCTOR", "session_id": "s-cond"},
            {"role": "EXECUTOR", "session_id": "s-exec"},
            {"role": "HUMAN", "session_id": "s-human"},
        ],
        created_by_role="CONDUCTOR",
    )
    transition_state(c, topic_id="X1", role="CONDUCTOR", new_state="ACTIVE")
    yield c, "X1"
    c.close()


def test_debate_escalate_writes_H_PING(topic):
    conn, t = topic
    out = escalate(
        conn, topic_id=t, role="EXECUTOR", reason="resolve_by passed",
    )
    row = conn.execute(
        "SELECT priority, kind FROM debate_messages WHERE msg_id = ?",
        (out["msg_id"],),
    ).fetchone()
    assert row["priority"] == "H"
    assert row["kind"] == "PING"


def test_debate_escalate_targets_HUMAN_by_default(topic):
    conn, t = topic
    out = escalate(
        conn, topic_id=t, role="EXECUTOR", reason="format violation",
    )
    body = conn.execute(
        "SELECT body FROM debate_messages WHERE msg_id = ?",
        (out["msg_id"],),
    ).fetchone()[0]
    assert "target=HUMAN" in body


def test_debate_escalate_includes_reason_in_body(topic):
    conn, t = topic
    reason = "contradictory DECISION observed"
    out = escalate(conn, topic_id=t, role="EXECUTOR", reason=reason)
    body = conn.execute(
        "SELECT body FROM debate_messages WHERE msg_id = ?",
        (out["msg_id"],),
    ).fetchone()[0]
    assert f"[ESCALATE:{reason}]" in body


def test_debate_escalate_rejects_empty_reason(topic):
    conn, t = topic
    with pytest.raises(DebateError, match="invalid_reason"):
        escalate(conn, topic_id=t, role="EXECUTOR", reason="   ")
