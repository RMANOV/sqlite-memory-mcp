"""Role-watermark to active-session cursor reconciliation regressions.

The role transcript cursor and the addressed-inbox cursor have different
coverage.  Advancing the full transcript therefore implies that the active
primary session processed the addressed subset, but the inverse is not true.
"""

from __future__ import annotations

import sqlite3

import pytest

from debate import (
    bind_role_session,
    claim_worker_session,
    debate_post_with_recipients,
    debate_signal_advance,
    debate_signal_check,
    init_debate,
    post_message,
    seed_initial_role_bindings,
    transition_state,
)
from schema import init_db


@pytest.fixture
def topic(tmp_path):
    db_path = str(tmp_path / "cursor_reconciliation.db")
    init_db(db_path)
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    init_debate(
        conn,
        topic_id="CURSOR_RECONCILE",
        title="cursor reconciliation",
        roles=[
            {"role": "CONDUCTOR", "session_id": "cc-conductor"},
            {"role": "ADVOCATE", "session_id": "codex-advocate"},
        ],
        created_by_role="CONDUCTOR",
    )
    seed_initial_role_bindings(
        conn,
        topic_id="CURSOR_RECONCILE",
        roles=[
            {"role": "CONDUCTOR", "session_id": "cc-conductor"},
            {"role": "ADVOCATE", "session_id": "codex-advocate"},
        ],
        bound_by_role="CONDUCTOR",
    )
    transition_state(
        conn,
        topic_id="CURSOR_RECONCILE",
        role="CONDUCTOR",
        new_state="ACTIVE",
    )
    yield conn, "CURSOR_RECONCILE"
    conn.close()


def _address_conductor(conn, topic_id, body):
    return debate_post_with_recipients(
        conn,
        topic_id=topic_id,
        role="ADVOCATE",
        priority="H",
        kind="STATUS",
        body=body,
        addressed_to=["CONDUCTOR"],
    )


def _signal_cursor(conn, topic_id, session_id="cc-conductor"):
    return conn.execute(
        "SELECT last_processed_msg_id,last_processed_ts "
        "FROM debate_signal_state "
        "WHERE session_id=? AND role='CONDUCTOR' AND topic_id=?",
        (session_id, topic_id),
    ).fetchone()


def test_role_watermark_ack_reconciles_active_primary_signal_cursor(topic):
    conn, topic_id = topic
    addressed = _address_conductor(conn, topic_id, "onboarding reply")

    post_message(
        conn,
        topic_id=topic_id,
        role="CONDUCTOR",
        priority="INFO",
        kind="WATERMARK",
        body=addressed["msg_id"],
    )

    cursor = _signal_cursor(conn, topic_id)
    assert cursor["last_processed_msg_id"] == addressed["msg_id"]
    assert (
        debate_signal_check(
            conn,
            session_id="cc-conductor",
            role="CONDUCTOR",
            topic_id=topic_id,
        )["count"]
        == 0
    )


def test_signal_check_repairs_preexisting_watermark_drift_before_delivery(topic):
    conn, topic_id = topic
    addressed = _address_conductor(conn, topic_id, "legacy drift")
    conn.execute(
        "INSERT INTO debate_watermarks "
        "(topic_id,role,last_processed_msg_id,last_processed_ts,updated_at) "
        "VALUES (?,?,?,?,?)",
        (
            topic_id,
            "CONDUCTOR",
            addressed["msg_id"],
            addressed["ts"],
            addressed["ts"],
        ),
    )

    inbox = debate_signal_check(
        conn,
        session_id="cc-conductor",
        role="CONDUCTOR",
        topic_id=topic_id,
    )

    assert inbox["count"] == 0
    assert inbox["cursor_reconciled_from_watermark"] == addressed["msg_id"]
    assert (
        _signal_cursor(conn, topic_id)["last_processed_msg_id"] == addressed["msg_id"]
    )


def test_unaddressed_watermark_target_reconciles_only_addressed_subset(topic):
    conn, topic_id = topic
    addressed = _address_conductor(conn, topic_id, "addressed subset")
    unaddressed = post_message(
        conn,
        topic_id=topic_id,
        role="ADVOCATE",
        priority="INFO",
        kind="STATUS",
        body="ledger-only tail",
    )

    post_message(
        conn,
        topic_id=topic_id,
        role="CONDUCTOR",
        priority="INFO",
        kind="WATERMARK",
        body=unaddressed["msg_id"],
    )

    assert (
        _signal_cursor(conn, topic_id)["last_processed_msg_id"] == addressed["msg_id"]
    )


def test_role_watermark_does_not_consume_diagnostic_session_inbox(topic):
    conn, topic_id = topic
    bind_role_session(
        conn,
        topic_id=topic_id,
        role="CONDUCTOR",
        session_id="codex-cond_diag",
        state="diagnostic",
        reason="cursor isolation test",
    )
    diagnostic = debate_post_with_recipients(
        conn,
        topic_id=topic_id,
        role="ADVOCATE",
        priority="H",
        kind="STATUS",
        body="diagnostic-only",
        addressed_to=[],
        diagnostic_to=["codex-cond_diag"],
    )

    post_message(
        conn,
        topic_id=topic_id,
        role="CONDUCTOR",
        priority="INFO",
        kind="WATERMARK",
        body=diagnostic["msg_id"],
    )

    assert _signal_cursor(conn, topic_id, "codex-cond_diag") is None
    inbox = debate_signal_check(
        conn,
        session_id="codex-cond_diag",
        role="CONDUCTOR",
        topic_id=topic_id,
    )
    assert [row["msg_id"] for row in inbox["pending"]] == [diagnostic["msg_id"]]


def test_older_role_watermark_never_regresses_newer_signal_cursor(topic):
    conn, topic_id = topic
    first = _address_conductor(conn, topic_id, "first")
    second = _address_conductor(conn, topic_id, "second")
    inbox = debate_signal_check(
        conn,
        session_id="cc-conductor",
        role="CONDUCTOR",
        topic_id=topic_id,
    )
    assert [row["msg_id"] for row in inbox["pending"]] == [
        first["msg_id"],
        second["msg_id"],
    ]
    debate_signal_advance(
        conn,
        session_id="cc-conductor",
        role="CONDUCTOR",
        topic_id=topic_id,
        last_processed_msg_id=second["msg_id"],
    )

    post_message(
        conn,
        topic_id=topic_id,
        role="CONDUCTOR",
        priority="INFO",
        kind="WATERMARK",
        body=first["msg_id"],
    )

    assert _signal_cursor(conn, topic_id)["last_processed_msg_id"] == second["msg_id"]


def test_role_watermark_never_advances_derived_worker_cursor(topic):
    conn, topic_id = topic
    trigger = _address_conductor(conn, topic_id, "worker trigger")
    claim = claim_worker_session(
        conn,
        topic_id=topic_id,
        role="CONDUCTOR",
        parent_session_id="cc-conductor",
        trigger_msg_id=trigger["msg_id"],
    )

    post_message(
        conn,
        topic_id=topic_id,
        role="CONDUCTOR",
        priority="INFO",
        kind="WATERMARK",
        body=trigger["msg_id"],
    )

    assert _signal_cursor(conn, topic_id)["last_processed_msg_id"] == trigger["msg_id"]
    assert _signal_cursor(conn, topic_id, claim["worker_session_id"]) is None
