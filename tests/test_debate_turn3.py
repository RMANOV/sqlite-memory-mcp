"""Turn-3 fixup tests — 9 new tests per CONDUCTOR PING msg:b29df5e8.

Three corrections covered:
  1. since_latest_compaction mode in debate_read (msg:9b7c3d28)
  2. WATERMARK body must carry msg_id + advance helper (msg:4c8a91be)
  3. Unknown since_msg_id raises DebateError (msg:7da13e9f)
"""

from __future__ import annotations

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from debate import (
    DebateError,
    advance_watermark,
    compact,
    get_watermark,
    init_debate,
    post_message,
    read_messages,
    transition_state,
)
from schema import init_db


@pytest.fixture
def topic(tmp_path):
    db_path = str(tmp_path / "debate_turn3.db")
    init_db(db_path)
    c = sqlite3.connect(db_path, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    init_debate(
        c, topic_id="X1", title="turn-3-fixup",
        roles=[
            {"role": "CONDUCTOR", "session_id": "s-c"},
            {"role": "EXECUTOR", "session_id": "s-e"},
            {"role": "ADVOCATE", "session_id": "s-a"},
        ],
        created_by_role="CONDUCTOR",
    )
    transition_state(c, topic_id="X1", role="CONDUCTOR", new_state="ACTIVE")
    yield c, "X1"
    c.close()


# ── Fix 1: since_latest_compaction mode ─────────────────────────────────


def test_debate_read_since_latest_compaction_finds_max_ts(topic):
    conn, t = topic
    for body in ("early1", "early2"):
        post_message(
            conn, topic_id=t, role="EXECUTOR",
            priority="INFO", kind="STATUS", body=body,
        )
    compact(
        conn, topic_id=t, role="ADVOCATE",
        body=(
            "OBSERVE: 2 status msgs.\n"
            "ORIENT: noise.\n"
            "DECIDE: compact.\n"
            "ACT: future readers bootstrap from here."
        ),
    )
    for body in ("after1", "after2"):
        post_message(
            conn, topic_id=t, role="EXECUTOR",
            priority="INFO", kind="STATUS", body=body,
        )

    out = read_messages(
        conn, topic_id=t, role="ADVOCATE",
        since_latest_compaction=True,
    )
    bodies = [m["body"] for m in out["messages"]]
    assert "early1" not in bodies
    assert "early2" not in bodies
    assert "after1" in bodies and "after2" in bodies
    assert out["bootstrap_compaction_msg_id"] is not None


def test_debate_read_since_latest_compaction_falls_back_when_none_exist(topic):
    """No COMPACTION → falls through to existing precedence (here: empty
    role watermark, so reads from start)."""
    conn, t = topic
    for body in ("a", "b", "c"):
        post_message(
            conn, topic_id=t, role="EXECUTOR",
            priority="INFO", kind="STATUS", body=body,
        )
    out = read_messages(
        conn, topic_id=t, role="ADVOCATE",
        since_latest_compaction=True,
    )
    assert out["count"] >= 3
    assert out["bootstrap_compaction_msg_id"] is None


def test_debate_read_since_latest_compaction_compound_tiebreak_when_multiple_same_ts(
    topic, tmp_path
):
    """When multiple COMPACTIONs share a ts, compound (ts DESC, msg_id
    DESC) tiebreak picks the lex-largest msg_id at that ts."""
    conn, t = topic
    # Use a past ts so the post_message after the loop (which stamps
    # current UTC) is strictly later than the tied COMPACTION ts.
    ts_shared = "2024-01-01T00:00:00Z"
    for mid in ("aaaaaaaa", "bbbbbbbb", "cccccccc"):
        body = (
            f"OBSERVE: comp {mid}.\n"
            "ORIENT: tied ts.\n"
            "DECIDE: pick max msg_id.\n"
            "ACT: tested."
        )
        conn.execute(
            "INSERT INTO debate_messages (msg_id, topic_id, role, ts, "
            "priority, kind, body, created_at) "
            "VALUES (?, ?, 'ADVOCATE', ?, 'INFO', 'COMPACTION', ?, ?)",
            (mid, t, ts_shared, body, ts_shared),
        )
    post_message(
        conn, topic_id=t, role="EXECUTOR",
        priority="INFO", kind="STATUS", body="after-compactions",
    )
    out = read_messages(
        conn, topic_id=t, role="ADVOCATE",
        since_latest_compaction=True,
    )
    assert out["bootstrap_compaction_msg_id"] == "cccccccc"
    bodies = [m["body"] for m in out["messages"]]
    assert "after-compactions" in bodies


# ── Fix 2: WATERMARK body must have msg_id + advance helper ─────────────


def test_watermark_body_must_have_msg_id_or_raises(topic):
    """ISO-only body without msg_id is rejected on POST."""
    conn, t = topic
    with pytest.raises(DebateError, match="invalid_watermark_body"):
        post_message(
            conn, topic_id=t, role="EXECUTOR",
            priority="INFO", kind="WATERMARK",
            body="2026-05-09T19:00:00Z",
        )


def test_watermark_advance_helper_writes_canonical_form(topic):
    """advance_watermark looks up ts from msg_id and writes both columns."""
    conn, t = topic
    target = post_message(
        conn, topic_id=t, role="EXECUTOR",
        priority="INFO", kind="STATUS", body="anchor",
    )
    advance_watermark(
        conn, topic_id=t, role="EXECUTOR",
        processed_up_to_msg_id=target["msg_id"],
    )
    wm = get_watermark(conn, t, "EXECUTOR")
    assert wm is not None
    assert wm["last_processed_msg_id"] == target["msg_id"]
    assert wm["last_processed_ts"] == target["ts"]


def test_compound_cursor_works_after_watermark_advance(topic):
    """After advance_watermark, subsequent reads correctly skip ≤ cursor."""
    conn, t = topic
    a = post_message(
        conn, topic_id=t, role="EXECUTOR",
        priority="INFO", kind="STATUS", body="msgA",
    )
    advance_watermark(
        conn, topic_id=t, role="EXECUTOR",
        processed_up_to_msg_id=a["msg_id"],
    )
    post_message(
        conn, topic_id=t, role="EXECUTOR",
        priority="INFO", kind="STATUS", body="msgB",
    )
    out = read_messages(conn, topic_id=t, role="EXECUTOR")
    bodies = [m["body"] for m in out["messages"]]
    assert "msgA" not in bodies
    assert "msgB" in bodies


# ── Fix 3: Unknown since_msg_id raises ───────────────────────────────


def test_debate_read_unknown_since_msg_id_raises(topic):
    conn, t = topic
    post_message(
        conn, topic_id=t, role="EXECUTOR",
        priority="INFO", kind="STATUS", body="real",
    )
    with pytest.raises(DebateError, match="unknown_since_msg_id"):
        read_messages(
            conn, topic_id=t, role="EXECUTOR",
            since_msg_id="deadbeef",
        )


def test_debate_read_unknown_since_msg_id_does_not_silently_return_full_history(
    topic,
):
    """Verify pre-fix behavior is gone: caller can no longer be misled
    by silent fall-through into thinking 'no new messages' equals
    'caught up' when actually since_msg_id was a typo."""
    conn, t = topic
    for body in ("m1", "m2", "m3"):
        post_message(
            conn, topic_id=t, role="EXECUTOR",
            priority="INFO", kind="STATUS", body=body,
        )
    with pytest.raises(DebateError):
        read_messages(
            conn, topic_id=t, role="EXECUTOR",
            since_msg_id="11111111",
        )


def test_debate_read_unknown_since_ts_returns_empty_or_after(topic):
    """since_ts pre-dating any message is allowed (returns matching window)."""
    conn, t = topic
    for body in ("m1", "m2"):
        post_message(
            conn, topic_id=t, role="EXECUTOR",
            priority="INFO", kind="STATUS", body=body,
        )
    out_pre = read_messages(
        conn, topic_id=t, role="EXECUTOR",
        since_ts="2024-01-01T00:00:00Z",
    )
    assert out_pre["count"] >= 2
    out_future = read_messages(
        conn, topic_id=t, role="EXECUTOR",
        since_ts="2099-01-01T00:00:00Z",
    )
    assert out_future["count"] == 0
