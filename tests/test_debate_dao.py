"""DAO + atomicity + compound-cursor tests for Debate Protocol v2.

These tests address ADVOCATE turn 2 corrections per CONDUCTOR
msg:0a91f237 H+PING:
  1. Compound (ts, msg_id) cursor for read_messages — no message dropped
     when multiple messages share the same ts; legacy ts-only watermarks
     admit all same-ts messages on first compound-cursor read.
  2. Pre-INSERT validation in post_message — invalid kind-specific
     semantics raise BEFORE any row hits the table.
  3. RESOLVED gate blocks open Qs of ALL priorities (not just H);
     `[DEFERRED:` body prefix in A counts as resolution-equivalent.
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
    transition_state,
)
from schema import init_db


@pytest.fixture
def conn(tmp_path):
    db_path = str(tmp_path / "debate_dao.db")
    init_db(db_path)
    c = sqlite3.connect(db_path, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    yield c
    c.close()


def _seed_topic(c, topic_id="WEEKEND_CODE_RED_DAO"):
    init_debate(
        c,
        topic_id=topic_id,
        title="Weekend code red DAO test",
        roles=[
            {"role": "CONDUCTOR", "session_id": "s-cond"},
            {"role": "EXECUTOR", "session_id": "s-exec"},
            {"role": "ADVOCATE", "session_id": "s-adv"},
        ],
        created_by_role="CONDUCTOR",
    )
    return topic_id


# ── Test #1: legacy ts-only watermark admits same-ts messages ───────────


def test_debate_read_legacy_watermark_no_msg_id_admits_same_ts_messages(conn):
    """Compound cursor fix per msg:7e3c8f10.

    Insert 3 messages with the same forced ts, write a watermark with
    only ts (no msg_id), advance via compound cursor: first read returns
    all 3 (msg_id > '' is true for any non-empty hex id), second read
    after advancing watermark returns 0.
    """
    topic = _seed_topic(conn)
    ts_shared = "2026-05-09T18:00:00Z"
    for i, mid in enumerate(("aaaaaaaa", "bbbbbbbb", "cccccccc")):
        conn.execute(
            "INSERT INTO debate_messages (msg_id, topic_id, role, ts, "
            "priority, kind, body, created_at) "
            "VALUES (?, ?, 'EXECUTOR', ?, 'INFO', 'STATUS', ?, ?)",
            (mid, topic, ts_shared, f"msg{i}", ts_shared),
        )
    # Insert legacy watermark with only ts (msg_id NULL)
    conn.execute(
        "INSERT INTO debate_watermarks (topic_id, role, "
        "last_processed_msg_id, last_processed_ts, updated_at) "
        "VALUES (?, 'EXECUTOR', NULL, ?, ?)",
        (topic, "2026-05-09T17:59:00Z", ts_shared),
    )

    out = read_messages(conn, topic_id=topic, role="EXECUTOR")
    assert out["count"] == 3
    returned_ids = {m["msg_id"] for m in out["messages"]}
    assert returned_ids == {"aaaaaaaa", "bbbbbbbb", "cccccccc"}

    # Advance watermark to the latest msg_id at that ts; subsequent read
    # should be empty.
    conn.execute(
        "UPDATE debate_watermarks SET last_processed_msg_id = ?, "
        "last_processed_ts = ? WHERE topic_id = ? AND role = 'EXECUTOR'",
        ("cccccccc", ts_shared, topic),
    )
    out2 = read_messages(conn, topic_id=topic, role="EXECUTOR")
    assert out2["count"] == 0


def test_debate_read_pagination_truncated_returns_cursor(conn):
    """Default limit=200; over the cap → truncated=True + next_*_cursor set."""
    topic = _seed_topic(conn)
    for i in range(5):
        post_message(
            conn,
            topic_id=topic,
            role="EXECUTOR",
            priority="INFO",
            kind="STATUS",
            body=f"status {i}",
        )
    out = read_messages(conn, topic_id=topic, role="EXECUTOR", limit=2)
    assert out["count"] == 2
    assert out["truncated"] is True
    assert out["next_msg_id_cursor"] is not None
    assert out["next_ts_cursor"] is not None
    assert out["limit"] == 2


# ── Test #2: STATE invalid transition does not persist row ──────────────


def test_debate_post_state_invalid_transition_no_persist(conn):
    """INIT → RESOLVED is illegal; row must NOT be inserted."""
    topic = _seed_topic(conn)
    pre_count = conn.execute(
        "SELECT COUNT(*) FROM debate_messages WHERE topic_id = ?", (topic,)
    ).fetchone()[0]
    with pytest.raises(DebateError, match="invalid_transition"):
        post_message(
            conn,
            topic_id=topic,
            role="CONDUCTOR",
            priority="H",
            kind="STATE",
            body="RESOLVED",
        )
    post_count = conn.execute(
        "SELECT COUNT(*) FROM debate_messages WHERE topic_id = ?", (topic,)
    ).fetchone()[0]
    assert post_count == pre_count, (
        f"invalid STATE message persisted: {pre_count} → {post_count}"
    )
    assert conn.execute(
        "SELECT state FROM debates WHERE topic_id = ?", (topic,)
    ).fetchone()[0] == "INIT"


# ── Test #3: WATERMARK pointing to unknown msg_id does not persist ──────


def test_debate_post_watermark_unknown_no_persist(conn):
    """WATERMARK referencing a msg_id not in topic must raise pre-INSERT."""
    topic = _seed_topic(conn)
    pre_count = conn.execute(
        "SELECT COUNT(*) FROM debate_messages WHERE topic_id = ?", (topic,)
    ).fetchone()[0]
    with pytest.raises(DebateError, match="watermark_msg_not_in_topic"):
        post_message(
            conn,
            topic_id=topic,
            role="EXECUTOR",
            priority="INFO",
            kind="WATERMARK",
            body="deadbeef",  # 8-hex but not in topic
        )
    post_count = conn.execute(
        "SELECT COUNT(*) FROM debate_messages WHERE topic_id = ?", (topic,)
    ).fetchone()[0]
    assert post_count == pre_count
    assert conn.execute(
        "SELECT COUNT(*) FROM debate_watermarks WHERE topic_id = ?", (topic,)
    ).fetchone()[0] == 0


# ── Test #4: DECISION reply_to must point to a Q ────────────────────────


def test_debate_post_decision_non_Q_no_persist(conn):
    """DECISION with reply_to pointing at a non-Q parent must raise pre-INSERT."""
    topic = _seed_topic(conn)
    parent = post_message(
        conn,
        topic_id=topic,
        role="EXECUTOR",
        priority="INFO",
        kind="STATUS",
        body="just a status, not a question",
    )
    pre_count = conn.execute(
        "SELECT COUNT(*) FROM debate_messages WHERE topic_id = ?", (topic,)
    ).fetchone()[0]
    with pytest.raises(DebateError, match="decision_reply_to_must_be_Q"):
        post_message(
            conn,
            topic_id=topic,
            role="CONDUCTOR",
            priority="H",
            kind="DECISION",
            body="conclude something",
            reply_to=parent["msg_id"],
        )
    post_count = conn.execute(
        "SELECT COUNT(*) FROM debate_messages WHERE topic_id = ?", (topic,)
    ).fetchone()[0]
    assert post_count == pre_count


def test_debate_post_decision_with_Q_reply_persists(conn):
    """Counterpart: DECISION replying to Q should succeed."""
    topic = _seed_topic(conn)
    transition_state(conn, topic_id=topic, role="CONDUCTOR", new_state="ACTIVE")
    q = post_message(
        conn,
        topic_id=topic,
        role="ADVOCATE",
        priority="M",
        kind="Q",
        body="real question",
    )
    out = post_message(
        conn,
        topic_id=topic,
        role="CONDUCTOR",
        priority="H",
        kind="DECISION",
        body="answer",
        reply_to=q["msg_id"],
    )
    assert "msg_id" in out


# ── Test #5: RESOLVED gate covers M and L; DEFERRED escape works ────────


@pytest.mark.parametrize("priority", ["M", "L"])
def test_debate_state_RESOLVED_blocks_open_questions_at_priority(conn, priority):
    """ALL open Qs block RESOLVED, not just H-priority."""
    topic = _seed_topic(conn)
    transition_state(conn, topic_id=topic, role="CONDUCTOR", new_state="ACTIVE")
    post_message(
        conn,
        topic_id=topic,
        role="ADVOCATE",
        priority=priority,
        kind="Q",
        body=f"open {priority} question",
    )
    result = transition_state(
        conn, topic_id=topic, role="CONDUCTOR", new_state="RESOLVED"
    )
    assert result["new_state"] == "ACTIVE", (
        f"RESOLVED transition should be blocked by open {priority} Q"
    )
    assert result["blocking_questions"], "expected blocking_questions populated"
    assert result["blocking_questions"][0]["priority"] == priority


def test_debate_state_RESOLVED_admits_DEFERRED_marked_answers(conn):
    """A reply with body starting `[DEFERRED:` counts as matched answer."""
    topic = _seed_topic(conn)
    transition_state(conn, topic_id=topic, role="CONDUCTOR", new_state="ACTIVE")
    q = post_message(
        conn,
        topic_id=topic,
        role="ADVOCATE",
        priority="M",
        kind="Q",
        body="should we defer?",
    )
    post_message(
        conn,
        topic_id=topic,
        role="CONDUCTOR",
        priority="M",
        kind="A",
        body="[DEFERRED:post-weekend-retro] revisit Monday once team available",
        reply_to=q["msg_id"],
    )
    result = transition_state(
        conn, topic_id=topic, role="CONDUCTOR", new_state="RESOLVED"
    )
    assert result["new_state"] == "RESOLVED"
    assert result["blocking_questions"] == []


def test_debate_state_RESOLVED_blocks_unanswered_question_even_if_DEFERRED_unmark(
    conn,
):
    """If A exists but is NOT marked DEFERRED for an unresolved Q, must still block.

    This guards against agents using [DEFERRED:] only for some Qs and
    leaving others unanswered.
    """
    topic = _seed_topic(conn)
    transition_state(conn, topic_id=topic, role="CONDUCTOR", new_state="ACTIVE")
    q1 = post_message(
        conn,
        topic_id=topic,
        role="ADVOCATE",
        priority="M",
        kind="Q",
        body="q1",
    )
    post_message(
        conn,
        topic_id=topic,
        role="CONDUCTOR",
        priority="M",
        kind="A",
        body="[DEFERRED:later] q1 deferred",
        reply_to=q1["msg_id"],
    )
    # q2 remains unanswered
    post_message(
        conn,
        topic_id=topic,
        role="ADVOCATE",
        priority="L",
        kind="Q",
        body="q2 unanswered",
    )
    result = transition_state(
        conn, topic_id=topic, role="CONDUCTOR", new_state="RESOLVED"
    )
    assert result["new_state"] == "ACTIVE"
    assert len(result["blocking_questions"]) == 1
    assert result["blocking_questions"][0]["body"] == "q2 unanswered"


# ── Companion test: COMPACTION OODA structure required ──────────────────


def test_debate_post_compaction_missing_OODA_section_no_persist(conn):
    """COMPACTION body without all 4 OODA section markers raises pre-INSERT."""
    topic = _seed_topic(conn)
    transition_state(conn, topic_id=topic, role="CONDUCTOR", new_state="ACTIVE")
    pre_count = conn.execute(
        "SELECT COUNT(*) FROM debate_messages WHERE topic_id = ?", (topic,)
    ).fetchone()[0]
    with pytest.raises(DebateError, match="compaction_body_missing_OODA"):
        post_message(
            conn,
            topic_id=topic,
            role="ADVOCATE",
            priority="INFO",
            kind="COMPACTION",
            body="OBSERVE: facts.\nORIENT: context.\nDECIDE: open qs.",
            # missing ACT
        )
    post_count = conn.execute(
        "SELECT COUNT(*) FROM debate_messages WHERE topic_id = ?", (topic,)
    ).fetchone()[0]
    assert post_count == pre_count


def test_debate_post_compaction_with_full_OODA_persists(conn):
    """COMPACTION with all 4 sections succeeds."""
    topic = _seed_topic(conn)
    transition_state(conn, topic_id=topic, role="CONDUCTOR", new_state="ACTIVE")
    out = post_message(
        conn,
        topic_id=topic,
        role="ADVOCATE",
        priority="INFO",
        kind="COMPACTION",
        body=(
            "OBSERVE: ADVOCATE turn 2 found 3 issues.\n"
            "ORIENT: cursor + atomicity + RESOLVED gate are protocol-load-bearing.\n"
            "DECIDE: apply fixup before checkpoint #2.\n"
            "ACT: fixup commit shipped, await CONDUCTOR sign-off.\n"
        ),
    )
    assert "msg_id" in out
