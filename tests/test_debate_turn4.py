"""Turn-4 fixup tests — 8 new tests per CONDUCTOR PING msg:d918f0a3.

Three corrections covered:
  1. Cursor precedence per msg:5a2f8c47 — explicit since_msg_id and
     since_ts override since_latest_compaction.
  2. Watermark canonical body becomes msg_id-only per msg:c39e7d18 —
     deprecated keyword form parsed but raises watermark_ts_mismatch
     when its ts disagrees with the looked-up row.
  3. (docs sync per msg:6e9b14c2 — covered in DEBATE_PROTOCOL.md)
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
    init_debate,
    post_message,
    read_messages,
    transition_state,
)
from schema import init_db


@pytest.fixture
def topic(tmp_path):
    db_path = str(tmp_path / "debate_turn4.db")
    init_db(db_path)
    c = sqlite3.connect(db_path, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    init_debate(
        c, topic_id="X1", title="turn-4-fixup",
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


def _seed_with_compaction(conn, topic_id):
    """Seed: 2 early STATUS msgs → 1 COMPACTION → 2 later STATUS msgs.
    Returns (early_msg_ids, compaction_msg_id, later_msg_ids)."""
    early = []
    for body in ("e1", "e2"):
        out = post_message(
            conn, topic_id=topic_id, role="EXECUTOR",
            priority="INFO", kind="STATUS", body=body,
        )
        early.append(out["msg_id"])
    comp = compact(
        conn, topic_id=topic_id, role="ADVOCATE",
        body=(
            "OBSERVE: 2 status msgs.\n"
            "ORIENT: noise.\n"
            "DECIDE: compact.\n"
            "ACT: bootstrap reference."
        ),
    )
    later = []
    for body in ("L1", "L2"):
        out = post_message(
            conn, topic_id=topic_id, role="EXECUTOR",
            priority="INFO", kind="STATUS", body=body,
        )
        later.append(out["msg_id"])
    return early, comp["msg_id"], later


# ── Fix 1: cursor precedence — 3 tests ──────────────────────────────────


def test_cursor_precedence_since_msg_id_beats_since_latest_compaction(topic):
    """When BOTH since_msg_id and since_latest_compaction=True are
    given, since_msg_id wins (explicit caller intent over bootstrap)."""
    conn, t = topic
    early, _comp_id, later = _seed_with_compaction(conn, t)
    out = read_messages(
        conn, topic_id=t, role="ADVOCATE",
        since_msg_id=early[0],
        since_latest_compaction=True,
    )
    bodies = [m["body"] for m in out["messages"]]
    # since_msg_id=early[0] → returns e2, COMPACTION, L1, L2 (everything
    # AFTER early[0]). Compaction-bootstrap would have returned only L1, L2.
    assert "e2" in bodies
    assert any(m["kind"] == "COMPACTION" for m in out["messages"])
    assert "L1" in bodies and "L2" in bodies
    # bootstrap_compaction_msg_id is None because precedence rule said
    # since_latest_compaction was NOT applied (explicit override).
    assert out["bootstrap_compaction_msg_id"] is None


def test_cursor_precedence_since_ts_beats_since_latest_compaction(topic):
    """When BOTH since_ts and since_latest_compaction=True are given,
    since_ts wins."""
    conn, t = topic
    _early, _comp_id, _later = _seed_with_compaction(conn, t)
    # since_ts in the past → all messages returned; compaction-bootstrap
    # would have returned only later-than-COMPACTION.
    out = read_messages(
        conn, topic_id=t, role="ADVOCATE",
        since_ts="2024-01-01T00:00:00Z",
        since_latest_compaction=True,
    )
    bodies = [m["body"] for m in out["messages"]]
    assert "e1" in bodies and "e2" in bodies
    assert "L1" in bodies and "L2" in bodies
    assert out["bootstrap_compaction_msg_id"] is None


def test_cursor_precedence_since_latest_compaction_beats_watermark(topic):
    """When since_latest_compaction=True and the role has an empty
    watermark, the COMPACTION cursor is used (watermark is the next
    fallback)."""
    conn, t = topic
    early, comp_id, later = _seed_with_compaction(conn, t)
    out = read_messages(
        conn, topic_id=t, role="ADVOCATE",
        since_latest_compaction=True,
    )
    bodies = [m["body"] for m in out["messages"]]
    assert "e1" not in bodies and "e2" not in bodies
    assert "L1" in bodies and "L2" in bodies
    assert out["bootstrap_compaction_msg_id"] == comp_id


# ── Fix 2: watermark security — 5 tests ────────────────────────────────


def test_watermark_msg_id_only_accepted(topic):
    """Canonical body form: raw msg_id, ts derived by DAO."""
    conn, t = topic
    target = post_message(
        conn, topic_id=t, role="EXECUTOR",
        priority="INFO", kind="STATUS", body="anchor",
    )
    out = post_message(
        conn, topic_id=t, role="EXECUTOR",
        priority="INFO", kind="WATERMARK",
        body=target["msg_id"],
    )
    assert out["msg_id"]
    row = conn.execute(
        "SELECT last_processed_msg_id, last_processed_ts "
        "FROM debate_watermarks WHERE topic_id=? AND role=?",
        (t, "EXECUTOR"),
    ).fetchone()
    assert row["last_processed_msg_id"] == target["msg_id"]
    assert row["last_processed_ts"] == target["ts"]


def test_watermark_old_form_with_matching_ts_accepted(topic):
    """Deprecated keyword form is still accepted IFF its ts matches the
    looked-up row (deprecation grace period)."""
    conn, t = topic
    target = post_message(
        conn, topic_id=t, role="EXECUTOR",
        priority="INFO", kind="STATUS", body="anchor-match",
    )
    body = (
        f"processed_up_to_ts={target['ts']} "
        f"processed_up_to_msg_id={target['msg_id']}"
    )
    out = post_message(
        conn, topic_id=t, role="EXECUTOR",
        priority="INFO", kind="WATERMARK", body=body,
    )
    assert out["msg_id"]
    row = conn.execute(
        "SELECT last_processed_msg_id, last_processed_ts "
        "FROM debate_watermarks WHERE topic_id=? AND role=?",
        (t, "EXECUTOR"),
    ).fetchone()
    assert row["last_processed_msg_id"] == target["msg_id"]
    assert row["last_processed_ts"] == target["ts"]


def test_watermark_old_form_with_mismatched_ts_rejected_compact(topic):
    """processed_up_to=<wrong-ts>:<msg_id> form is rejected with
    watermark_ts_mismatch."""
    conn, t = topic
    target = post_message(
        conn, topic_id=t, role="EXECUTOR",
        priority="INFO", kind="STATUS", body="anchor-tampered",
    )
    tampered_ts = "2024-01-01T00:00:00Z"
    body = f"processed_up_to={tampered_ts}:{target['msg_id']}"
    with pytest.raises(DebateError, match="watermark_ts_mismatch"):
        post_message(
            conn, topic_id=t, role="EXECUTOR",
            priority="INFO", kind="WATERMARK", body=body,
        )


def test_watermark_old_form_keyword_with_mismatched_ts_rejected(topic):
    """processed_up_to_ts=<wrong> processed_up_to_msg_id=<X> form is
    also rejected when ts disagrees."""
    conn, t = topic
    target = post_message(
        conn, topic_id=t, role="EXECUTOR",
        priority="INFO", kind="STATUS", body="anchor-tampered2",
    )
    body = (
        f"processed_up_to_ts=2099-12-31T23:59:59Z "
        f"processed_up_to_msg_id={target['msg_id']}"
    )
    with pytest.raises(DebateError, match="watermark_ts_mismatch"):
        post_message(
            conn, topic_id=t, role="EXECUTOR",
            priority="INFO", kind="WATERMARK", body=body,
        )


def test_advance_watermark_helper_writes_msg_id_only_form(topic):
    """advance_watermark must write the canonical msg_id-only body
    (turn-4 simplification per msg:c39e7d18)."""
    conn, t = topic
    target = post_message(
        conn, topic_id=t, role="EXECUTOR",
        priority="INFO", kind="STATUS", body="anchor-helper",
    )
    advance_watermark(
        conn, topic_id=t, role="EXECUTOR",
        processed_up_to_msg_id=target["msg_id"],
    )
    row = conn.execute(
        "SELECT body FROM debate_messages "
        "WHERE topic_id=? AND kind='WATERMARK' AND role='EXECUTOR' "
        "ORDER BY ts DESC LIMIT 1",
        (t,),
    ).fetchone()
    assert row["body"] == target["msg_id"]
    assert "processed_up_to" not in row["body"]
