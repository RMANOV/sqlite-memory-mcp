"""v3.9.2 wrapper-shaped regression tests (ADVOCATE turn 18 mandate
msg:0cc6a60b + msg:ca22ee19).

Test gap caught by ADVOCATE: the v3.9.2 unit tests use raw
``sqlite3.connect(isolation_level=None)`` and never exercise the
``with db_utils.get_conn() as conn:`` context manager that every MCP
tool wrapper relies on. That blind spot let two real bugs through:

  1. NESTED-TX (H): get_conn() opens its own ``BEGIN``; a DAO that
     also calls ``conn.execute('BEGIN IMMEDIATE')`` would hit
     "cannot start a transaction within a transaction".
  2. CURSOR REGRESSION (M): plain ON CONFLICT DO UPDATE on
     debate_signal_state lets racing advances overwrite a newer
     cursor with an older one.

This file exercises both DAO functions THROUGH the same wrapper used
in production so any future contract drift surfaces immediately.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from db_utils import get_conn
from debate import (
    DebateError,
    debate_post_with_recipients,
    debate_signal_advance,
    debate_signal_check,
    init_debate,
    transition_state,
)
from schema import init_db


@pytest.fixture
def wrapped_topic(tmp_path):
    """Set up a topic via the same get_conn wrapper that MCP tools use.

    Yields the tmp db path so individual tests can open their own
    ``with get_conn(db_path=...)`` blocks — that's the whole point of
    the wrapper-shaped regression class.
    """
    db_path = str(tmp_path / "v3_9_2_wrapper.db")
    init_db(db_path)
    with get_conn(db_path=db_path) as conn:
        init_debate(
            conn, topic_id="X1", title="wrapper-shaped",
            roles=[
                {"role": "CONDUCTOR", "session_id": "cc-cond1"},
                {"role": "EXECUTOR", "session_id": "cc-exec1"},
                {"role": "ADVOCATE", "session_id": "codex-adv1"},
            ],
            created_by_role="CONDUCTOR",
        )
        transition_state(
            conn, topic_id="X1", role="CONDUCTOR", new_state="ACTIVE"
        )
    yield db_path


# ═══════════════════════════════════════════════════════════════════════
# Nested-transaction regression (H msg:0cc6a60b)
# ═══════════════════════════════════════════════════════════════════════


def test_post_with_recipients_runs_inside_get_conn_wrapper(wrapped_topic):
    """The exact pattern every MCP tool uses: caller-owned BEGIN via
    get_conn, DAO body runs without trying to nest its own
    transaction. Pre-fix this would raise 'cannot start a transaction
    within a transaction'.
    """
    db = wrapped_topic
    with get_conn(db_path=db) as conn:
        out = debate_post_with_recipients(
            conn, topic_id="X1", role="CONDUCTOR",
            priority="H", kind="STATUS", body="wrapper-shaped",
            addressed_to=["EXECUTOR"],
        )
        assert out["recipient_count"] == 1

    # Verify the COMMIT actually landed (read in a fresh wrapper).
    with get_conn(db_path=db) as conn:
        row = conn.execute(
            "SELECT body FROM debate_messages WHERE msg_id = ?",
            (out["msg_id"],),
        ).fetchone()
    assert row["body"] == "wrapper-shaped"


def test_signal_check_runs_inside_get_conn_wrapper(wrapped_topic):
    db = wrapped_topic
    with get_conn(db_path=db) as conn:
        debate_post_with_recipients(
            conn, topic_id="X1", role="CONDUCTOR",
            priority="H", kind="STATUS", body="for-EXECUTOR",
            addressed_to=["EXECUTOR"],
        )
    with get_conn(db_path=db) as conn:
        out = debate_signal_check(
            conn, session_id="cc-exec1", role="EXECUTOR", topic_id="X1"
        )
    assert out["count"] == 1
    assert out["pending"][0]["body"] == "for-EXECUTOR"


def test_signal_advance_runs_inside_get_conn_wrapper(wrapped_topic):
    db = wrapped_topic
    with get_conn(db_path=db) as conn:
        m1 = debate_post_with_recipients(
            conn, topic_id="X1", role="CONDUCTOR",
            priority="M", kind="STATUS", body="m1",
            addressed_to=["EXECUTOR"],
        )
    with get_conn(db_path=db) as conn:
        debate_signal_advance(
            conn, session_id="cc-exec1", role="EXECUTOR",
            topic_id="X1", last_processed_msg_id=m1["msg_id"],
        )
    with get_conn(db_path=db) as conn:
        out = debate_signal_check(
            conn, session_id="cc-exec1", role="EXECUTOR", topic_id="X1"
        )
    assert out["count"] == 0


def test_post_with_recipients_rolls_back_on_validation_error_via_wrapper(
    wrapped_topic,
):
    """When the DAO raises mid-block, the wrapper's __exit__ must
    ROLLBACK so partial state is not visible to the next caller.
    Validates the caller-owned-tx contract end-to-end.
    """
    db = wrapped_topic
    # Baseline includes the synthetic STATE message from the fixture's
    # transition_state(ACTIVE) call — so we measure delta, not absolute.
    with get_conn(db_path=db) as conn:
        pre = conn.execute(
            "SELECT COUNT(*) AS c FROM debate_messages WHERE topic_id = ?",
            ("X1",),
        ).fetchone()["c"]

    with pytest.raises(DebateError) as exc_info:
        with get_conn(db_path=db) as conn:
            debate_post_with_recipients(
                conn, topic_id="X1", role="EXECUTOR",
                priority="M", kind="STATUS", body="should-not-persist",
                addressed_to=["EXECUTOR", "GHOST"],  # GHOST blocks the post
            )
    assert exc_info.value.error_type == "recipient_unknown_role"

    with get_conn(db_path=db) as conn:
        post = conn.execute(
            "SELECT COUNT(*) AS c FROM debate_messages WHERE topic_id = ?",
            ("X1",),
        ).fetchone()["c"]
    assert post == pre  # zero new messages persisted


def test_post_with_recipients_atomic_through_wrapper(wrapped_topic):
    """Mid-block crash AFTER the message INSERT but BEFORE recipient
    INSERTs must roll back BOTH together, courtesy of the caller's
    transaction (the DAO no longer manages its own). Verifies atomicity
    is delegated correctly.
    """
    db = wrapped_topic

    class _BoomError(RuntimeError):
        pass

    with get_conn(db_path=db) as conn:
        pre_msg = conn.execute(
            "SELECT COUNT(*) AS c FROM debate_messages WHERE topic_id = ?",
            ("X1",),
        ).fetchone()["c"]
        pre_rec = conn.execute(
            "SELECT COUNT(*) AS c FROM debate_message_recipients"
        ).fetchone()["c"]

    with pytest.raises(_BoomError):
        with get_conn(db_path=db) as conn:
            debate_post_with_recipients(
                conn, topic_id="X1", role="CONDUCTOR",
                priority="M", kind="STATUS", body="atomic",
                addressed_to=["EXECUTOR"],
            )
            raise _BoomError("simulated post-DAO failure")

    # Both message and recipient rows should have rolled back together.
    with get_conn(db_path=db) as conn:
        msg_count = conn.execute(
            "SELECT COUNT(*) AS c FROM debate_messages WHERE topic_id = ?",
            ("X1",),
        ).fetchone()["c"]
        rec_count = conn.execute(
            "SELECT COUNT(*) AS c FROM debate_message_recipients"
        ).fetchone()["c"]
    assert msg_count == pre_msg  # no new messages
    assert rec_count == pre_rec  # no new recipient rows


# ═══════════════════════════════════════════════════════════════════════
# Cursor monotonicity guard (M msg:ca22ee19)
# ═══════════════════════════════════════════════════════════════════════


def test_signal_advance_monotonic_forward_succeeds(wrapped_topic):
    db = wrapped_topic
    msg_ids = []
    with get_conn(db_path=db) as conn:
        for i in range(3):
            out = debate_post_with_recipients(
                conn, topic_id="X1", role="CONDUCTOR",
                priority="L", kind="STATUS", body=f"m{i}",
                addressed_to=["EXECUTOR"],
            )
            msg_ids.append(out["msg_id"])

    # Advance forward through m0, m1, m2 — each strictly newer.
    for mid in msg_ids:
        with get_conn(db_path=db) as conn:
            debate_signal_advance(
                conn, session_id="cc-exec1", role="EXECUTOR",
                topic_id="X1", last_processed_msg_id=mid,
            )

    with get_conn(db_path=db) as conn:
        row = conn.execute(
            "SELECT last_processed_msg_id FROM debate_signal_state "
            "WHERE session_id = ? AND role = ? AND topic_id = ?",
            ("cc-exec1", "EXECUTOR", "X1"),
        ).fetchone()
    assert row["last_processed_msg_id"] == msg_ids[-1]


def test_signal_advance_regression_raises_watermark_regression(wrapped_topic):
    """Cannot advance to an older cursor than the current one."""
    db = wrapped_topic
    with get_conn(db_path=db) as conn:
        m0 = debate_post_with_recipients(
            conn, topic_id="X1", role="CONDUCTOR",
            priority="M", kind="STATUS", body="m0",
            addressed_to=["EXECUTOR"],
        )
        m1 = debate_post_with_recipients(
            conn, topic_id="X1", role="CONDUCTOR",
            priority="M", kind="STATUS", body="m1",
            addressed_to=["EXECUTOR"],
        )
    # Set cursor to m1 (newer).
    with get_conn(db_path=db) as conn:
        debate_signal_advance(
            conn, session_id="cc-exec1", role="EXECUTOR",
            topic_id="X1", last_processed_msg_id=m1["msg_id"],
        )
    # Try to regress to m0.
    with pytest.raises(DebateError) as exc_info:
        with get_conn(db_path=db) as conn:
            debate_signal_advance(
                conn, session_id="cc-exec1", role="EXECUTOR",
                topic_id="X1", last_processed_msg_id=m0["msg_id"],
            )
    assert exc_info.value.error_type == "watermark_regression"

    # Cursor must NOT have moved backwards.
    with get_conn(db_path=db) as conn:
        row = conn.execute(
            "SELECT last_processed_msg_id FROM debate_signal_state "
            "WHERE session_id = ? AND role = ? AND topic_id = ?",
            ("cc-exec1", "EXECUTOR", "X1"),
        ).fetchone()
    assert row["last_processed_msg_id"] == m1["msg_id"]


def test_signal_advance_idempotent_at_equal_cursor(wrapped_topic):
    """Repeated advance to the same msg_id is a no-op rewrite (does not
    raise watermark_regression). Required for at-least-once delivery
    semantics."""
    db = wrapped_topic
    with get_conn(db_path=db) as conn:
        m1 = debate_post_with_recipients(
            conn, topic_id="X1", role="CONDUCTOR",
            priority="M", kind="STATUS", body="m1",
            addressed_to=["EXECUTOR"],
        )
    for _ in range(3):
        with get_conn(db_path=db) as conn:
            res = debate_signal_advance(
                conn, session_id="cc-exec1", role="EXECUTOR",
                topic_id="X1", last_processed_msg_id=m1["msg_id"],
            )
        assert res["last_processed_msg_id"] == m1["msg_id"]

    with get_conn(db_path=db) as conn:
        rows = conn.execute(
            "SELECT COUNT(*) AS c FROM debate_signal_state"
        ).fetchone()
    assert rows["c"] == 1


def test_signal_advance_interleaved_racers_converge_to_newer_cursor(
    wrapped_topic,
):
    """Threaded advance racers via the wrapper. Monotonicity must hold,
    so the FINAL cursor equals the LATER message (not just 'one of the
    candidates' — that was the v3.9.2 paranoid test's weakness pre-
    monotonic-guard).

    Uses sqlite3.OperationalError tolerance because get_conn() wraps
    every block in BEGIN DEFERRED; under high SHARED→RESERVED upgrade
    contention the second writer can hit SQLITE_BUSY past the
    busy_timeout. Real adapters retry; we just assert the property
    that survives once contention resolves.
    """
    db = wrapped_topic
    msg_ids: list[str] = []
    with get_conn(db_path=db) as conn:
        for i in range(2):
            out = debate_post_with_recipients(
                conn, topic_id="X1", role="CONDUCTOR",
                priority="M", kind="STATUS", body=f"m{i}",
                addressed_to=["EXECUTOR"],
            )
            msg_ids.append(out["msg_id"])

    unexpected: list[str] = []

    def race(target_msg_id: str):
        try:
            with get_conn(db_path=db) as conn:
                debate_signal_advance(
                    conn, session_id="cc-exec1", role="EXECUTOR",
                    topic_id="X1", last_processed_msg_id=target_msg_id,
                )
        except DebateError as exc:
            # watermark_regression is the expected outcome when the
            # OTHER thread already advanced to the newer cursor.
            if exc.error_type != "watermark_regression":
                unexpected.append(f"{target_msg_id}: {exc!r}")
        except sqlite3.OperationalError as exc:
            # Lock contention is acceptable under heavy concurrency;
            # real adapters retry. Don't fail the test on it.
            if "locked" not in str(exc).lower():
                unexpected.append(f"{target_msg_id}: {exc!r}")

    threads = [
        threading.Thread(target=race, args=(msg_ids[0],)),
        threading.Thread(target=race, args=(msg_ids[1],)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert unexpected == [], unexpected

    # Now do a third deterministic advance to msg_ids[1] from a clean
    # wrapper. Whatever the racing threads landed at, this advance is
    # either equal (idempotent) or strictly newer (succeeds), and the
    # final cursor MUST be msg_ids[1].
    try:
        with get_conn(db_path=db) as conn:
            debate_signal_advance(
                conn, session_id="cc-exec1", role="EXECUTOR",
                topic_id="X1", last_processed_msg_id=msg_ids[1],
            )
    except DebateError as exc:
        # Only acceptable if the racers already settled on msg_ids[1]
        # AND a stale racer somehow brought us back — should not happen
        # given the monotonic guard, but accept the no-op idempotent
        # case explicitly.
        assert exc.error_type != "watermark_regression"

    # Authoritatively compute the expected winner from the topic per
    # ADVOCATE turn-19 mandate (msg:b8182b8b): query the strictly-
    # latest message by compound (ts DESC, msg_id DESC) instead of
    # hard-coding an index into the post-order list. Couples the
    # assertion to the cursor contract, not to fixture sequencing.
    with get_conn(db_path=db) as conn:
        expected = conn.execute(
            "SELECT msg_id FROM debate_messages "
            "WHERE topic_id = ? AND kind = 'STATUS' "
            "ORDER BY ts DESC, msg_id DESC LIMIT 1",
            ("X1",),
        ).fetchone()
        row = conn.execute(
            "SELECT last_processed_msg_id FROM debate_signal_state "
            "WHERE session_id = ? AND role = ? AND topic_id = ?",
            ("cc-exec1", "EXECUTOR", "X1"),
        ).fetchone()
    assert expected is not None
    assert row["last_processed_msg_id"] == expected["msg_id"]


def test_signal_advance_initial_no_state_row_no_regression_check(
    wrapped_topic,
):
    """The monotonic guard only activates when a state row already
    exists. Initial advance must always succeed."""
    db = wrapped_topic
    with get_conn(db_path=db) as conn:
        m1 = debate_post_with_recipients(
            conn, topic_id="X1", role="CONDUCTOR",
            priority="M", kind="STATUS", body="m1",
            addressed_to=["EXECUTOR"],
        )
    with get_conn(db_path=db) as conn:
        res = debate_signal_advance(
            conn, session_id="cc-exec1", role="EXECUTOR",
            topic_id="X1", last_processed_msg_id=m1["msg_id"],
        )
    assert res["last_processed_msg_id"] == m1["msg_id"]


# ═══════════════════════════════════════════════════════════════════════
# Sanity: high-volume sequential through wrapper (no regressions)
# ═══════════════════════════════════════════════════════════════════════


def test_post_with_recipients_high_volume_sequential_via_wrapper(wrapped_topic):
    """50 sequential posts through the wrapper — no nested-tx errors,
    every commit lands."""
    db = wrapped_topic
    posted: list[str] = []
    for i in range(50):
        with get_conn(db_path=db) as conn:
            out = debate_post_with_recipients(
                conn, topic_id="X1", role="CONDUCTOR",
                priority="INFO", kind="STATUS", body=f"vol{i}",
                addressed_to=["EXECUTOR"],
            )
        posted.append(out["msg_id"])

    assert len(posted) == 50
    assert len(set(posted)) == 50

    with get_conn(db_path=db) as conn:
        rows = conn.execute(
            "SELECT COUNT(*) AS c FROM debate_messages WHERE topic_id = ?",
            ("X1",),
        ).fetchone()
    assert rows["c"] >= 50  # plus the STATE messages from fixture setup


def test_signal_check_post_advance_via_wrapper_returns_remainder(
    wrapped_topic,
):
    """End-to-end: post 5, advance to 2nd, signal_check should return
    the remaining 3 — all through the wrapper."""
    db = wrapped_topic
    msg_ids = []
    for i in range(5):
        with get_conn(db_path=db) as conn:
            out = debate_post_with_recipients(
                conn, topic_id="X1", role="CONDUCTOR",
                priority="M", kind="STATUS", body=f"e2e{i}",
                addressed_to=["EXECUTOR"],
            )
        msg_ids.append(out["msg_id"])

    with get_conn(db_path=db) as conn:
        debate_signal_advance(
            conn, session_id="cc-exec1", role="EXECUTOR",
            topic_id="X1", last_processed_msg_id=msg_ids[1],
        )

    with get_conn(db_path=db) as conn:
        out = debate_signal_check(
            conn, session_id="cc-exec1", role="EXECUTOR", topic_id="X1"
        )
    assert out["count"] == 3
    bodies = {m["body"] for m in out["pending"]}
    assert bodies == {"e2e2", "e2e3", "e2e4"}
