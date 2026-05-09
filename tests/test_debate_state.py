"""Tests 32-37: debate_state lifecycle transitions.

Per CONDUCTOR 2026-05-09T16:35 EEST EXECUTOR INSTRUCTION test plan.
Strict-gate semantics (RESOLVED blocks all open Qs) covered here +
DEFERRED admission tested in test_debate_dao.py.
"""

from __future__ import annotations

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from debate import (
    DebateError,
    get_debate,
    init_debate,
    post_message,
    transition_state,
)
from schema import init_db


@pytest.fixture
def topic(tmp_path):
    db_path = str(tmp_path / "debate_state.db")
    init_db(db_path)
    c = sqlite3.connect(db_path, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    init_debate(
        c, topic_id="X1", title="state-tests",
        roles=[
            {"role": "CONDUCTOR", "session_id": "s-cond"},
            {"role": "EXECUTOR", "session_id": "s-exec"},
            {"role": "ADVOCATE", "session_id": "s-adv"},
        ],
        created_by_role="CONDUCTOR",
    )
    yield c, "X1"
    c.close()


def test_debate_state_INIT_to_ACTIVE(topic):
    conn, t = topic
    out = transition_state(
        conn, topic_id=t, role="CONDUCTOR", new_state="ACTIVE",
    )
    assert out["old_state"] == "INIT"
    assert out["new_state"] == "ACTIVE"
    assert get_debate(conn, t)["state"] == "ACTIVE"


def test_debate_state_ACTIVE_to_RESOLVED_succeeds_when_all_Qs_have_A(topic):
    conn, t = topic
    transition_state(conn, topic_id=t, role="CONDUCTOR", new_state="ACTIVE")
    q = post_message(
        conn, topic_id=t, role="ADVOCATE",
        priority="H", kind="Q", body="any open?",
    )
    post_message(
        conn, topic_id=t, role="EXECUTOR",
        priority="H", kind="A", body="resolved", reply_to=q["msg_id"],
    )
    out = transition_state(
        conn, topic_id=t, role="CONDUCTOR", new_state="RESOLVED",
    )
    assert out["new_state"] == "RESOLVED"


def test_debate_state_ACTIVE_to_RESOLVED_blocked_by_open_Q(topic):
    conn, t = topic
    transition_state(conn, topic_id=t, role="CONDUCTOR", new_state="ACTIVE")
    post_message(
        conn, topic_id=t, role="ADVOCATE",
        priority="H", kind="Q", body="open H",
    )
    out = transition_state(
        conn, topic_id=t, role="CONDUCTOR", new_state="RESOLVED",
    )
    assert out["new_state"] == "ACTIVE"
    assert out["blocking_questions"], "expected blocking H Q"


def test_debate_state_RESOLVED_to_ARCHIVED_sets_archived_at(topic):
    conn, t = topic
    transition_state(conn, topic_id=t, role="CONDUCTOR", new_state="ACTIVE")
    transition_state(conn, topic_id=t, role="CONDUCTOR", new_state="RESOLVED")
    out = transition_state(
        conn, topic_id=t, role="CONDUCTOR", new_state="ARCHIVED",
    )
    assert out["new_state"] == "ARCHIVED"
    debate = get_debate(conn, t)
    assert debate["archived_at"] is not None
    assert debate["state"] == "ARCHIVED"


@pytest.mark.parametrize(
    "old,new",
    [
        ("INIT", "RESOLVED"),
        ("INIT", "ARCHIVED"),
        ("ACTIVE", "INIT"),
        ("RESOLVED", "ACTIVE"),
    ],
)
def test_debate_state_invalid_transition_rejected(topic, old, new):
    conn, t = topic
    if old == "ACTIVE":
        transition_state(conn, topic_id=t, role="CONDUCTOR", new_state="ACTIVE")
    elif old == "RESOLVED":
        transition_state(conn, topic_id=t, role="CONDUCTOR", new_state="ACTIVE")
        transition_state(conn, topic_id=t, role="CONDUCTOR", new_state="RESOLVED")
    with pytest.raises(DebateError, match="invalid_transition"):
        transition_state(conn, topic_id=t, role="CONDUCTOR", new_state=new)


def test_debate_state_writes_synthetic_STATE_message(topic):
    conn, t = topic
    out = transition_state(
        conn, topic_id=t, role="CONDUCTOR", new_state="ACTIVE",
    )
    msg_id = out["transition_msg_id"]
    row = conn.execute(
        "SELECT kind, body FROM debate_messages WHERE msg_id = ?", (msg_id,),
    ).fetchone()
    assert row["kind"] == "STATE"
    assert row["body"] == "ACTIVE"


def test_debate_state_unknown_topic(topic):
    conn, _ = topic
    with pytest.raises(DebateError, match="unknown_topic"):
        transition_state(
            conn, topic_id="NOPE", role="CONDUCTOR", new_state="ACTIVE",
        )


def test_debate_state_archived_terminal_no_further_transition(topic):
    conn, t = topic
    transition_state(conn, topic_id=t, role="CONDUCTOR", new_state="ACTIVE")
    transition_state(conn, topic_id=t, role="CONDUCTOR", new_state="RESOLVED")
    transition_state(conn, topic_id=t, role="CONDUCTOR", new_state="ARCHIVED")
    with pytest.raises(DebateError, match="invalid_transition"):
        transition_state(
            conn, topic_id=t, role="CONDUCTOR", new_state="RESOLVED",
        )
