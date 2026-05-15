"""v3.10 role/session lifecycle and wake-orchestration invariants."""

from __future__ import annotations

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from debate import (
    DebateError,
    bind_role_session,
    claim_worker_session,
    debate_post_with_recipients,
    debate_signal_advance,
    debate_signal_check,
    init_debate,
    post_message,
    prepare_wake_dry_run,
    reclaim_stale_message_claims,
    reap_worker_claims,
    rotate_role_binding,
    transition_state,
)
from schema import init_db


@pytest.fixture
def topic(tmp_path):
    db_path = str(tmp_path / "v3_10.db")
    init_db(db_path)
    c = sqlite3.connect(db_path, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    init_debate(
        c,
        topic_id="X1",
        title="v3.10 lifecycle",
        roles=[
            {"role": "CONDUCTOR", "session_id": "codex-cond1"},
            {"role": "EXECUTOR", "session_id": "codex-exec1"},
            {"role": "ADVOCATE", "session_id": "cc-adv1"},
            {"role": "ADVOCATE_DEPUTY", "session_id": "cc-advdep1"},
        ],
        created_by_role="CONDUCTOR",
    )
    transition_state(c, topic_id="X1", role="CONDUCTOR", new_state="ACTIVE")
    yield c, "X1"
    c.close()


def _binding_state(conn, topic_id, role, session_id):
    row = conn.execute(
        "SELECT state FROM debate_role_bindings "
        "WHERE topic_id = ? AND role = ? AND session_id = ?",
        (topic_id, role, session_id),
    ).fetchone()
    return row["state"] if row else None


def test_debate_state_resolved_retires_active_bindings_atomically(topic):
    conn, t = topic
    bind_role_session(
        conn,
        topic_id=t,
        role="EXECUTOR",
        session_id="codex-exec1",
        reason="primary",
    )
    bind_role_session(
        conn,
        topic_id=t,
        role="EXECUTOR",
        session_id="codex-diag1",
        state="diagnostic",
        reason="diagnostic",
    )
    q = post_message(
        conn,
        topic_id=t,
        role="ADVOCATE",
        priority="H",
        kind="Q",
        body="block close",
    )

    blocked = transition_state(
        conn, topic_id=t, role="CONDUCTOR", new_state="RESOLVED"
    )
    assert blocked["new_state"] == "ACTIVE"
    assert _binding_state(conn, t, "EXECUTOR", "codex-exec1") == "active"

    post_message(
        conn,
        topic_id=t,
        role="EXECUTOR",
        priority="H",
        kind="A",
        body="answered",
        reply_to=q["msg_id"],
    )
    out = transition_state(
        conn, topic_id=t, role="CONDUCTOR", new_state="RESOLVED"
    )
    assert out["new_state"] == "RESOLVED"
    assert out["retired_bindings"] == 1
    assert _binding_state(conn, t, "EXECUTOR", "codex-exec1") == "retired"
    assert _binding_state(conn, t, "EXECUTOR", "codex-diag1") == "diagnostic"


def test_debate_state_archived_retires_diagnostic_bindings_atomically(topic):
    conn, t = topic
    bind_role_session(
        conn,
        topic_id=t,
        role="EXECUTOR",
        session_id="codex-diag1",
        state="diagnostic",
        reason="diagnostic",
    )
    transition_state(conn, topic_id=t, role="CONDUCTOR", new_state="RESOLVED")
    assert _binding_state(conn, t, "EXECUTOR", "codex-diag1") == "diagnostic"

    out = transition_state(
        conn, topic_id=t, role="CONDUCTOR", new_state="ARCHIVED"
    )
    assert out["new_state"] == "ARCHIVED"
    assert out["retired_bindings"] == 1
    assert _binding_state(conn, t, "EXECUTOR", "codex-diag1") == "retired"


def test_bind_role_rejects_ownership_gap_without_conductor_override(topic):
    conn, t = topic
    bind_role_session(
        conn,
        topic_id=t,
        role="EXECUTOR",
        session_id="codex-exec1",
        reason="primary",
    )
    with pytest.raises(DebateError) as exc_info:
        bind_role_session(
            conn,
            topic_id=t,
            role="EXECUTOR",
            session_id="codex-exec1",
            state="retired",
            reason="retire without replacement",
        )
    assert exc_info.value.error_type == "conductor_override_required"

    override = post_message(
        conn,
        topic_id=t,
        role="CONDUCTOR",
        priority="H",
        kind="DECISION",
        body="allow temporary ownership gap",
    )
    out = bind_role_session(
        conn,
        topic_id=t,
        role="EXECUTOR",
        session_id="codex-exec1",
        state="retired",
        reason="override retire",
        conductor_override_msg_id=override["msg_id"],
    )
    assert out["ownership_gap_override"] is True
    assert _binding_state(conn, t, "EXECUTOR", "codex-exec1") == "retired"


def test_rotate_requires_cursor_mode_and_copy_missing_cursor_warns(topic):
    conn, t = topic
    bind_role_session(
        conn,
        topic_id=t,
        role="EXECUTOR",
        session_id="codex-exec1",
        reason="primary",
    )
    with pytest.raises(DebateError) as exc_info:
        rotate_role_binding(
            conn,
            topic_id=t,
            role="EXECUTOR",
            old_session_id="codex-exec1",
            new_session_id="codex-exec2",
            cursor_mode="",
            reason="missing mode",
        )
    assert exc_info.value.error_type == "cursor_mode_invalid"

    out = rotate_role_binding(
        conn,
        topic_id=t,
        role="EXECUTOR",
        old_session_id="codex-exec1",
        new_session_id="codex-exec2",
        cursor_mode="copy",
        reason="handoff",
    )
    assert out["warning"] == "copy_source_cursor_missing"
    assert (
        conn.execute(
            "SELECT 1 FROM debate_signal_state "
            "WHERE topic_id = ? AND role = ? AND session_id = ?",
            (t, "EXECUTOR", "codex-exec2"),
        ).fetchone()
        is None
    )


def test_wake_adapter_signal_only_and_loop_suppressed(topic):
    conn, t = topic
    bind_role_session(
        conn,
        topic_id=t,
        role="EXECUTOR",
        session_id="codex-exec1",
        reason="primary",
    )
    post = debate_post_with_recipients(
        conn,
        topic_id=t,
        role="CONDUCTOR",
        priority="H",
        kind="STATUS",
        body="wake executor",
        addressed_to=["EXECUTOR"],
    )
    before = conn.execute(
        "SELECT COUNT(*) AS c FROM debate_messages WHERE topic_id = ?", (t,)
    ).fetchone()["c"]
    first = prepare_wake_dry_run(conn, tool_response=post)
    second = prepare_wake_dry_run(conn, tool_response=post)
    after = conn.execute(
        "SELECT COUNT(*) AS c FROM debate_messages WHERE topic_id = ?", (t,)
    ).fetchone()["c"]
    wake_logs = conn.execute(
        "SELECT COUNT(*) AS c FROM debate_wake_log "
        "WHERE trigger_msg_id = ? AND target_session_id = ?",
        (post["msg_id"], "codex-exec1"),
    ).fetchone()["c"]

    assert before == after
    assert first["targets"][0]["target_session_id"] == "codex-exec1"
    assert second["suppressed"] == 1
    assert wake_logs == 1


def test_unknown_tool_response_schema_fails_closed_and_logs_audit(topic):
    conn, _t = topic
    out = prepare_wake_dry_run(
        conn,
        tool_response={"msg_id": "deadbeef", "schema_version": "unknown"},
    )
    assert out["targets"] == []
    row = conn.execute(
        "SELECT result FROM debate_wake_log WHERE trigger_msg_id = ?",
        ("deadbeef",),
    ).fetchone()
    assert row["result"] == "schema_mismatch"


def test_direct_session_addressing_rejected_unless_diagnostic_binding(topic):
    conn, t = topic
    with pytest.raises(DebateError) as exc_info:
        debate_post_with_recipients(
            conn,
            topic_id=t,
            role="CONDUCTOR",
            priority="M",
            kind="STATUS",
            body="bad direct",
            addressed_to=["codex-diag1"],
        )
    assert (
        exc_info.value.error_type
        == "recipient_direct_session_requires_diagnostic"
    )

    bind_role_session(
        conn,
        topic_id=t,
        role="EXECUTOR",
        session_id="codex-diag1",
        state="diagnostic",
        reason="diagnostic",
    )
    out = debate_post_with_recipients(
        conn,
        topic_id=t,
        role="CONDUCTOR",
        priority="M",
        kind="STATUS",
        body="diagnostic direct",
        addressed_to=[],
        diagnostic_to=["codex-diag1"],
    )
    rec = conn.execute(
        "SELECT recipient_mode FROM debate_message_recipients "
        "WHERE msg_id = ? AND recipient = ?",
        (out["msg_id"], "codex-diag1"),
    ).fetchone()
    assert rec["recipient_mode"] == "diagnostic"


def test_stale_session_binding_does_not_receive_role_addressed_work(topic):
    conn, t = topic
    bind_role_session(
        conn,
        topic_id=t,
        role="EXECUTOR",
        session_id="codex-exec_new",
        reason="current owner",
    )
    debate_post_with_recipients(
        conn,
        topic_id=t,
        role="CONDUCTOR",
        priority="H",
        kind="STATUS",
        body="current only",
        addressed_to=["EXECUTOR"],
    )
    stale = debate_signal_check(
        conn, session_id="codex-exec_old", role="EXECUTOR", topic_id=t
    )
    current = debate_signal_check(
        conn, session_id="codex-exec_new", role="EXECUTOR", topic_id=t
    )
    assert stale["count"] == 0
    assert current["count"] == 1


def test_duplicate_active_primary_rejected_secondary_role_allowed(topic):
    conn, t = topic
    bind_role_session(
        conn,
        topic_id=t,
        role="ADVOCATE",
        session_id="cc-adv1",
        reason="primary",
    )
    with pytest.raises(DebateError) as exc_info:
        bind_role_session(
            conn,
            topic_id=t,
            role="ADVOCATE",
            session_id="cc-adv2",
            reason="duplicate primary",
        )
    assert exc_info.value.error_type == "binding_duplicate_active"

    out = bind_role_session(
        conn,
        topic_id=t,
        role="ADVOCATE_DEPUTY",
        session_id="cc-advdep1",
        reason="declared secondary role",
    )
    assert out["state"] == "active"


def test_worker_claims_allocate_distinct_workers_and_reuse_duplicate_trigger(topic):
    conn, t = topic
    bind_role_session(
        conn,
        topic_id=t,
        role="EXECUTOR",
        session_id="codex-exec1",
        reason="primary",
    )
    first_trigger = debate_post_with_recipients(
        conn,
        topic_id=t,
        role="CONDUCTOR",
        priority="H",
        kind="STATUS",
        body="first task",
        addressed_to=["EXECUTOR"],
    )
    second_trigger = debate_post_with_recipients(
        conn,
        topic_id=t,
        role="CONDUCTOR",
        priority="H",
        kind="STATUS",
        body="second task",
        addressed_to=["EXECUTOR"],
    )

    first = claim_worker_session(
        conn,
        topic_id=t,
        role="EXECUTOR",
        parent_session_id="codex-exec1",
        trigger_msg_id=first_trigger["msg_id"],
    )
    duplicate = claim_worker_session(
        conn,
        topic_id=t,
        role="EXECUTOR",
        parent_session_id="codex-exec1",
        trigger_msg_id=first_trigger["msg_id"],
    )
    second = claim_worker_session(
        conn,
        topic_id=t,
        role="EXECUTOR",
        parent_session_id="codex-exec1",
        trigger_msg_id=second_trigger["msg_id"],
    )

    assert first["worker_session_id"] == "codex-exec1-W1"
    assert duplicate["worker_session_id"] == first["worker_session_id"]
    assert duplicate["duplicate"] is True
    assert second["worker_session_id"] == "codex-exec1-W2"


def test_worker_signal_requires_claim_and_inherits_active_parent_binding(topic):
    conn, t = topic
    bind_role_session(
        conn,
        topic_id=t,
        role="EXECUTOR",
        session_id="codex-exec1",
        reason="primary",
    )
    trigger = debate_post_with_recipients(
        conn,
        topic_id=t,
        role="CONDUCTOR",
        priority="H",
        kind="STATUS",
        body="worker task",
        addressed_to=["EXECUTOR"],
    )
    claim = claim_worker_session(
        conn,
        topic_id=t,
        role="EXECUTOR",
        parent_session_id="codex-exec1",
        trigger_msg_id=trigger["msg_id"],
    )

    out = debate_signal_check(
        conn,
        session_id=claim["worker_session_id"],
        role="EXECUTOR",
        topic_id=t,
    )
    assert [m["msg_id"] for m in out["pending"]] == [trigger["msg_id"]]

    with pytest.raises(DebateError) as exc_info:
        debate_signal_check(
            conn,
            session_id="codex-exec1-W99",
            role="EXECUTOR",
            topic_id=t,
        )
    assert exc_info.value.error_type == "worker_claim_required"


def test_worker_cursor_advance_isolated_from_parent_and_other_workers(topic):
    conn, t = topic
    bind_role_session(
        conn,
        topic_id=t,
        role="EXECUTOR",
        session_id="codex-exec1",
        reason="primary",
    )
    first = debate_post_with_recipients(
        conn,
        topic_id=t,
        role="CONDUCTOR",
        priority="H",
        kind="STATUS",
        body="first task",
        addressed_to=["EXECUTOR"],
    )
    second = debate_post_with_recipients(
        conn,
        topic_id=t,
        role="CONDUCTOR",
        priority="H",
        kind="STATUS",
        body="second task",
        addressed_to=["EXECUTOR"],
    )
    w1 = claim_worker_session(
        conn,
        topic_id=t,
        role="EXECUTOR",
        parent_session_id="codex-exec1",
        trigger_msg_id=first["msg_id"],
    )
    w2 = claim_worker_session(
        conn,
        topic_id=t,
        role="EXECUTOR",
        parent_session_id="codex-exec1",
        trigger_msg_id=second["msg_id"],
    )

    debate_signal_advance(
        conn,
        session_id=w1["worker_session_id"],
        role="EXECUTOR",
        topic_id=t,
        last_processed_msg_id=first["msg_id"],
    )

    rows = conn.execute(
        "SELECT session_id, last_processed_msg_id FROM debate_signal_state "
        "WHERE topic_id = ? AND role = ?",
        (t, "EXECUTOR"),
    ).fetchall()
    cursors = {row["session_id"]: row["last_processed_msg_id"] for row in rows}
    assert cursors[w1["worker_session_id"]] == first["msg_id"]
    assert "codex-exec1" not in cursors
    assert w2["worker_session_id"] not in cursors


def test_worker_completion_reuses_claim_and_blocks_duplicate_terminal(topic):
    conn, t = topic
    bind_role_session(
        conn,
        topic_id=t,
        role="EXECUTOR",
        session_id="codex-exec1",
        reason="primary",
    )
    trigger = debate_post_with_recipients(
        conn,
        topic_id=t,
        role="CONDUCTOR",
        priority="H",
        kind="STATUS",
        body="one-shot work",
        addressed_to=["EXECUTOR"],
    )
    claim = claim_worker_session(
        conn,
        topic_id=t,
        role="EXECUTOR",
        parent_session_id="codex-exec1",
        trigger_msg_id=trigger["msg_id"],
    )
    post_message(
        conn,
        topic_id=t,
        role="EXECUTOR",
        priority="H",
        kind="A",
        body="done",
        reply_to=trigger["msg_id"],
    )
    advanced = debate_signal_advance(
        conn,
        session_id=claim["worker_session_id"],
        role="EXECUTOR",
        topic_id=t,
        last_processed_msg_id=trigger["msg_id"],
    )
    duplicate = claim_worker_session(
        conn,
        topic_id=t,
        role="EXECUTOR",
        parent_session_id="codex-exec1",
        trigger_msg_id=trigger["msg_id"],
    )

    assert advanced["worker_claim"]["state"] == "completed"
    assert duplicate["worker_session_id"] == claim["worker_session_id"]
    assert duplicate["no_action"] is True
    with pytest.raises(DebateError) as exc_info:
        post_message(
            conn,
            topic_id=t,
            role="EXECUTOR",
            priority="H",
            kind="STATUS",
            body="duplicate done",
            reply_to=trigger["msg_id"],
        )
    assert exc_info.value.error_type == "terminal_reply_duplicate"


def test_worker_reap_removes_completed_claim_and_leaves_audit(topic):
    conn, t = topic
    bind_role_session(
        conn,
        topic_id=t,
        role="EXECUTOR",
        session_id="codex-exec1",
        reason="primary",
    )
    trigger = debate_post_with_recipients(
        conn,
        topic_id=t,
        role="CONDUCTOR",
        priority="H",
        kind="STATUS",
        body="cleanup work",
        addressed_to=["EXECUTOR"],
    )
    claim = claim_worker_session(
        conn,
        topic_id=t,
        role="EXECUTOR",
        parent_session_id="codex-exec1",
        trigger_msg_id=trigger["msg_id"],
    )
    conn.execute(
        "UPDATE debate_worker_claims SET state = 'completed', heartbeat_at = ? "
        "WHERE worker_session_id = ?",
        ("2026-05-01T00:00:00Z", claim["worker_session_id"]),
    )

    out = reap_worker_claims(
        conn,
        topic_id=t,
        older_than_ts="2026-05-02T00:00:00Z",
    )
    remaining = conn.execute(
        "SELECT 1 FROM debate_worker_claims WHERE worker_session_id = ?",
        (claim["worker_session_id"],),
    ).fetchone()
    audit = conn.execute(
        "SELECT result FROM debate_worker_reap_log WHERE worker_session_id = ?",
        (claim["worker_session_id"],),
    ).fetchone()

    assert out["count"] == 1
    assert remaining is None
    assert audit["result"] == "reaped"


def test_nonstanding_decision_claim_survives_cursor_advance_until_terminal_reply(topic):
    conn, t = topic
    bind_role_session(
        conn,
        topic_id=t,
        role="EXECUTOR",
        session_id="codex-exec1",
        reason="primary",
    )
    task = debate_post_with_recipients(
        conn,
        topic_id=t,
        role="CONDUCTOR",
        priority="H",
        kind="DECISION",
        body="do one thing",
        addressed_to=["EXECUTOR"],
        standing=False,
    )

    first = debate_signal_check(
        conn, session_id="codex-exec1", role="EXECUTOR", topic_id=t
    )
    debate_signal_advance(
        conn,
        session_id="codex-exec1",
        role="EXECUTOR",
        topic_id=t,
        last_processed_msg_id=task["msg_id"],
    )
    still_pending = debate_signal_check(
        conn, session_id="codex-exec1", role="EXECUTOR", topic_id=t
    )
    post_message(
        conn,
        topic_id=t,
        role="EXECUTOR",
        priority="H",
        kind="A",
        body="done",
        reply_to=task["msg_id"],
    )
    gone = debate_signal_check(
        conn, session_id="codex-exec1", role="EXECUTOR", topic_id=t
    )

    assert task["msg_id"] in [m["msg_id"] for m in first["pending"]]
    assert [m["msg_id"] for m in still_pending["pending"]] == [task["msg_id"]]
    assert gone["pending"] == []


def test_standing_false_decision_is_not_resurfaced_as_mandate(topic):
    conn, t = topic
    bind_role_session(
        conn,
        topic_id=t,
        role="EXECUTOR",
        session_id="codex-exec1",
        reason="primary",
    )
    task = debate_post_with_recipients(
        conn,
        topic_id=t,
        role="CONDUCTOR",
        priority="H",
        kind="DECISION",
        body="one-shot",
        addressed_to=["EXECUTOR"],
        standing=False,
    )
    standing = debate_post_with_recipients(
        conn,
        topic_id=t,
        role="CONDUCTOR",
        priority="H",
        kind="DECISION",
        body="standing mandate",
        addressed_to=["EXECUTOR"],
        standing=True,
    )
    post_message(
        conn,
        topic_id=t,
        role="EXECUTOR",
        priority="H",
        kind="A",
        body="task done",
        reply_to=task["msg_id"],
    )
    debate_signal_advance(
        conn,
        session_id="codex-exec1",
        role="EXECUTOR",
        topic_id=t,
        last_processed_msg_id=standing["msg_id"],
    )

    out = debate_signal_check(
        conn, session_id="codex-exec1", role="EXECUTOR", topic_id=t
    )
    assert [m["msg_id"] for m in out["pending"]] == [standing["msg_id"]]


def test_stale_nonstanding_decision_claim_reclaim_allows_new_worker_owner(topic):
    conn, t = topic
    bind_role_session(
        conn,
        topic_id=t,
        role="EXECUTOR",
        session_id="codex-exec1",
        reason="primary",
    )
    task = debate_post_with_recipients(
        conn,
        topic_id=t,
        role="CONDUCTOR",
        priority="H",
        kind="DECISION",
        body="one-shot task",
        addressed_to=["EXECUTOR"],
        standing=False,
    )
    second_trigger = debate_post_with_recipients(
        conn,
        topic_id=t,
        role="CONDUCTOR",
        priority="H",
        kind="STATUS",
        body="second worker trigger",
        addressed_to=["EXECUTOR"],
    )
    w1 = claim_worker_session(
        conn,
        topic_id=t,
        role="EXECUTOR",
        parent_session_id="codex-exec1",
        trigger_msg_id=task["msg_id"],
    )
    w2 = claim_worker_session(
        conn,
        topic_id=t,
        role="EXECUTOR",
        parent_session_id="codex-exec1",
        trigger_msg_id=second_trigger["msg_id"],
    )

    first = debate_signal_check(
        conn,
        session_id=w1["worker_session_id"],
        role="EXECUTOR",
        topic_id=t,
    )
    blocked = debate_signal_check(
        conn,
        session_id=w2["worker_session_id"],
        role="EXECUTOR",
        topic_id=t,
    )
    conn.execute(
        "UPDATE debate_message_claims SET heartbeat_at = ? "
        "WHERE msg_id = ? AND role = ?",
        ("2026-05-01T00:00:00Z", task["msg_id"], "EXECUTOR"),
    )

    reclaimed = reclaim_stale_message_claims(
        conn,
        topic_id=t,
        older_than_ts="2026-05-02T00:00:00Z",
    )
    second = debate_signal_check(
        conn,
        session_id=w2["worker_session_id"],
        role="EXECUTOR",
        topic_id=t,
    )
    claim = conn.execute(
        "SELECT owner_session_id FROM debate_message_claims "
        "WHERE msg_id = ? AND role = ?",
        (task["msg_id"], "EXECUTOR"),
    ).fetchone()
    audit = conn.execute(
        "SELECT result FROM debate_message_claim_reclaim_log "
        "WHERE msg_id = ? AND role = ?",
        (task["msg_id"], "EXECUTOR"),
    ).fetchone()

    assert task["msg_id"] in [m["msg_id"] for m in first["pending"]]
    assert task["msg_id"] not in [m["msg_id"] for m in blocked["pending"]]
    assert reclaimed["reclaimed_count"] == 1
    assert reclaimed["completed_count"] == 0
    assert task["msg_id"] in [m["msg_id"] for m in second["pending"]]
    assert claim["owner_session_id"] == w2["worker_session_id"]
    assert audit["result"] == "reclaimed"


def test_stale_nonstanding_decision_claim_reclaim_completes_if_ack_exists(topic):
    conn, t = topic
    bind_role_session(
        conn,
        topic_id=t,
        role="EXECUTOR",
        session_id="codex-exec1",
        reason="primary",
    )
    task = debate_post_with_recipients(
        conn,
        topic_id=t,
        role="CONDUCTOR",
        priority="H",
        kind="DECISION",
        body="one-shot task",
        addressed_to=["EXECUTOR"],
        standing=False,
    )
    debate_signal_check(
        conn,
        session_id="codex-exec1",
        role="EXECUTOR",
        topic_id=t,
    )
    ack = post_message(
        conn,
        topic_id=t,
        role="EXECUTOR",
        priority="H",
        kind="A",
        body="done",
        reply_to=task["msg_id"],
    )
    conn.execute(
        "UPDATE debate_message_claims SET state = 'active', ack_msg_id = NULL, "
        "completed_at = NULL, heartbeat_at = ? "
        "WHERE msg_id = ? AND role = ?",
        ("2026-05-01T00:00:00Z", task["msg_id"], "EXECUTOR"),
    )

    out = reclaim_stale_message_claims(
        conn,
        topic_id=t,
        older_than_ts="2026-05-02T00:00:00Z",
    )
    claim = conn.execute(
        "SELECT state, ack_msg_id FROM debate_message_claims "
        "WHERE msg_id = ? AND role = ?",
        (task["msg_id"], "EXECUTOR"),
    ).fetchone()
    audit = conn.execute(
        "SELECT result FROM debate_message_claim_reclaim_log "
        "WHERE msg_id = ? AND role = ?",
        (task["msg_id"], "EXECUTOR"),
    ).fetchone()

    assert out["reclaimed_count"] == 0
    assert out["completed_count"] == 1
    assert claim["state"] == "done"
    assert claim["ack_msg_id"] == ack["msg_id"]
    assert audit["result"] == "completed_from_terminal"


def test_message_claim_reclaim_rejects_too_recent_cutoff(topic):
    conn, t = topic
    with pytest.raises(DebateError) as exc_info:
        reclaim_stale_message_claims(
            conn,
            topic_id=t,
            older_than_ts="9999-01-01T00:00:00Z",
            minimum_age_seconds=60,
        )
    assert exc_info.value.error_type == "message_claim_reclaim_cutoff_too_recent"


def test_legacy_null_decision_stays_standing_after_cursor_advance(topic):
    conn, t = topic
    bind_role_session(
        conn,
        topic_id=t,
        role="EXECUTOR",
        session_id="codex-exec1",
        reason="primary",
    )
    legacy = debate_post_with_recipients(
        conn,
        topic_id=t,
        role="CONDUCTOR",
        priority="H",
        kind="DECISION",
        body="legacy standing mandate",
        addressed_to=["EXECUTOR"],
    )
    transient = debate_post_with_recipients(
        conn,
        topic_id=t,
        role="CONDUCTOR",
        priority="M",
        kind="STATUS",
        body="cursor target",
        addressed_to=["EXECUTOR"],
    )
    stored = conn.execute(
        "SELECT standing FROM debate_messages WHERE msg_id = ?",
        (legacy["msg_id"],),
    ).fetchone()

    debate_signal_advance(
        conn,
        session_id="codex-exec1",
        role="EXECUTOR",
        topic_id=t,
        last_processed_msg_id=transient["msg_id"],
    )
    out = debate_signal_check(
        conn, session_id="codex-exec1", role="EXECUTOR", topic_id=t
    )

    assert stored["standing"] is None
    assert [m["msg_id"] for m in out["pending"]] == [legacy["msg_id"]]
