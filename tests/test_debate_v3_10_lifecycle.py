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
    debate_post_with_recipients,
    debate_signal_check,
    init_debate,
    post_message,
    prepare_wake_dry_run,
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
