"""Tests 13-23: debate_post happy paths + rejection cases.

Per CONDUCTOR 2026-05-09T16:35 EEST EXECUTOR INSTRUCTION test plan.
Tests covering pre-INSERT validation atomicity (15-22) live in
test_debate_dao.py — this file covers the basic happy-path semantics.
"""

from __future__ import annotations

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from debate import (
    DebateError,
    MSG_ID_RE,
    get_debate,
    get_watermark,
    init_debate,
    post_message,
    transition_state,
)
from schema import init_db


@pytest.fixture
def topic(tmp_path):
    db_path = str(tmp_path / "debate_post.db")
    init_db(db_path)
    c = sqlite3.connect(db_path, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    init_debate(
        c,
        topic_id="X1",
        title="post-tests",
        roles=[
            {"role": "CONDUCTOR", "session_id": "s-cond"},
            {"role": "EXECUTOR", "session_id": "s-exec"},
        ],
        created_by_role="CONDUCTOR",
    )
    yield c, "X1"
    c.close()


def test_debate_post_basic_message(topic):
    conn, t = topic
    out = post_message(
        conn, topic_id=t, role="EXECUTOR",
        priority="INFO", kind="STATUS", body="alive",
    )
    assert MSG_ID_RE.fullmatch(out["msg_id"])
    assert out["topic_state"] == "INIT"
    row = conn.execute(
        "SELECT body, kind FROM debate_messages WHERE msg_id = ?",
        (out["msg_id"],),
    ).fetchone()
    assert row["body"] == "alive"
    assert row["kind"] == "STATUS"


def test_debate_post_generates_unique_msg_ids(topic):
    conn, t = topic
    seen = set()
    for i in range(50):
        out = post_message(
            conn, topic_id=t, role="EXECUTOR",
            priority="INFO", kind="STATUS", body=f"msg{i}",
        )
        seen.add(out["msg_id"])
    assert len(seen) == 50


def test_debate_post_rejects_unknown_role(topic):
    conn, t = topic
    with pytest.raises(DebateError, match="unknown_role_for_topic"):
        post_message(
            conn, topic_id=t, role="GHOST",
            priority="H", kind="Q", body="?",
        )


def test_debate_post_rejects_unknown_topic(topic):
    conn, _ = topic
    with pytest.raises(DebateError, match="unknown_topic"):
        post_message(
            conn, topic_id="NOPE", role="EXECUTOR",
            priority="H", kind="Q", body="?",
        )


def test_debate_post_rejects_invalid_priority(topic):
    conn, t = topic
    with pytest.raises(DebateError, match="invalid_priority"):
        post_message(
            conn, topic_id=t, role="EXECUTOR",
            priority="HIGH", kind="STATUS", body="x",
        )


def test_debate_post_rejects_invalid_kind(topic):
    conn, t = topic
    with pytest.raises(DebateError, match="invalid_kind"):
        post_message(
            conn, topic_id=t, role="EXECUTOR",
            priority="H", kind="QUESTION", body="x",
        )


def test_debate_post_rejects_empty_body(topic):
    conn, t = topic
    with pytest.raises(DebateError, match="invalid_body"):
        post_message(
            conn, topic_id=t, role="EXECUTOR",
            priority="H", kind="Q", body="",
        )


def test_debate_post_reply_to_must_exist_in_same_topic(topic):
    conn, t = topic
    with pytest.raises(DebateError, match="unknown_reply_to"):
        post_message(
            conn, topic_id=t, role="EXECUTOR",
            priority="H", kind="A", body="x", reply_to="deadbeef",
        )


def test_debate_post_reply_to_cross_topic_rejected(topic, tmp_path):
    conn, t = topic
    init_debate(
        conn, topic_id="X2", title="other",
        roles=[
            {"role": "CONDUCTOR", "session_id": "s-cond"},
            {"role": "EXECUTOR", "session_id": "s-exec"},
        ],
        created_by_role="CONDUCTOR",
    )
    other_q = post_message(
        conn, topic_id="X2", role="CONDUCTOR",
        priority="H", kind="Q", body="from-other-topic",
    )
    with pytest.raises(DebateError, match="reply_to_cross_topic"):
        post_message(
            conn, topic_id=t, role="EXECUTOR",
            priority="H", kind="A", body="x",
            reply_to=other_q["msg_id"],
        )


def test_debate_post_kind_STATE_triggers_transition(topic):
    conn, t = topic
    out = post_message(
        conn, topic_id=t, role="CONDUCTOR",
        priority="H", kind="STATE", body="ACTIVE",
    )
    assert out["topic_state"] == "ACTIVE"
    assert get_debate(conn, t)["state"] == "ACTIVE"


def test_debate_post_kind_WATERMARK_updates_table_with_msg_id_target(topic):
    conn, t = topic
    target = post_message(
        conn, topic_id=t, role="EXECUTOR",
        priority="INFO", kind="STATUS", body="target",
    )
    post_message(
        conn, topic_id=t, role="EXECUTOR",
        priority="INFO", kind="WATERMARK", body=target["msg_id"],
    )
    wm = get_watermark(conn, t, "EXECUTOR")
    assert wm is not None
    assert wm["last_processed_msg_id"] == target["msg_id"]


def test_debate_post_kind_WATERMARK_updates_with_iso_only(topic):
    conn, t = topic
    post_message(
        conn, topic_id=t, role="EXECUTOR",
        priority="INFO", kind="WATERMARK",
        body="2026-05-09T18:00:00Z",
    )
    wm = get_watermark(conn, t, "EXECUTOR")
    assert wm is not None
    assert wm["last_processed_msg_id"] is None
    assert wm["last_processed_ts"] == "2026-05-09T18:00:00Z"


def test_debate_post_blocked_when_state_ARCHIVED(topic):
    conn, t = topic
    transition_state(conn, topic_id=t, role="CONDUCTOR", new_state="ACTIVE")
    transition_state(conn, topic_id=t, role="CONDUCTOR", new_state="RESOLVED")
    transition_state(conn, topic_id=t, role="CONDUCTOR", new_state="ARCHIVED")
    with pytest.raises(DebateError, match="topic_archived_read_only"):
        post_message(
            conn, topic_id=t, role="EXECUTOR",
            priority="INFO", kind="STATUS", body="late write",
        )


def test_debate_post_blocked_when_state_RESOLVED(topic):
    conn, t = topic
    transition_state(conn, topic_id=t, role="CONDUCTOR", new_state="ACTIVE")
    transition_state(conn, topic_id=t, role="CONDUCTOR", new_state="RESOLVED")
    with pytest.raises(DebateError, match="topic_resolved_read_only"):
        post_message(
            conn, topic_id=t, role="EXECUTOR",
            priority="INFO", kind="STATUS", body="late write",
        )


def test_debate_post_STATE_allowed_in_RESOLVED_for_archive_transition(topic):
    """Even on RESOLVED, kind=STATE moving to ARCHIVED is permitted."""
    conn, t = topic
    transition_state(conn, topic_id=t, role="CONDUCTOR", new_state="ACTIVE")
    transition_state(conn, topic_id=t, role="CONDUCTOR", new_state="RESOLVED")
    out = post_message(
        conn, topic_id=t, role="CONDUCTOR",
        priority="H", kind="STATE", body="ARCHIVED",
    )
    assert out["topic_state"] == "ARCHIVED"
