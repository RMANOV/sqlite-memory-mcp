"""Flexible debate roster: add/swap/disable roles after debate_init.

Roles were historically frozen at debate_init. These regressions cover the
flexible-roster path (add_role_to_debate / debate_add_role) plus assert that
session swap (replace_active / rotate) and disable+reactivate are handled by
the existing v3.10 binding machinery, with every invariant preserved.

All DBs are temp fixtures — never the live coordination DB.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import db_utils
import intel_server
import premium_runtime
from debate import (
    DebateError,
    add_role_to_debate,
    bind_role_session,
    debate_post_with_recipients,
    debate_signal_check,
    init_debate,
    post_message,
    rotate_role_binding,
    transition_state,
)
from schema import init_db


@pytest.fixture
def topic(tmp_path):
    db_path = str(tmp_path / "flex_roles.db")
    init_db(db_path)
    c = sqlite3.connect(db_path, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    # Minimal frozen roster: only CONDUCTOR + EXECUTOR at init.
    init_debate(
        c,
        topic_id="FLEX1",
        title="flexible roster",
        roles=[
            {"role": "CONDUCTOR", "session_id": "codex-cond1"},
            {"role": "EXECUTOR", "session_id": "codex-exec1"},
        ],
        created_by_role="CONDUCTOR",
    )
    bind_role_session(
        c,
        topic_id="FLEX1",
        role="CONDUCTOR",
        session_id="codex-cond1",
        reason="seed conductor",
    )
    bind_role_session(
        c,
        topic_id="FLEX1",
        role="EXECUTOR",
        session_id="codex-exec1",
        reason="seed executor",
    )
    transition_state(c, topic_id="FLEX1", role="CONDUCTOR", new_state="ACTIVE")
    yield c, "FLEX1"
    c.close()


def _binding_state(conn, topic_id, role, session_id):
    row = conn.execute(
        "SELECT state FROM debate_role_bindings "
        "WHERE topic_id = ? AND role = ? AND session_id = ?",
        (topic_id, role, session_id),
    ).fetchone()
    return row["state"] if row else None


def _declared_roles(conn, topic_id):
    row = conn.execute(
        "SELECT roles_json FROM debates WHERE topic_id = ?", (topic_id,)
    ).fetchone()
    return {r["role"] for r in json.loads(row["roles_json"])}


def _active_count(conn, topic_id, role):
    return conn.execute(
        "SELECT COUNT(*) AS c FROM debate_role_bindings "
        "WHERE topic_id = ? AND role = ? AND state = 'active'",
        (topic_id, role),
    ).fetchone()["c"]


# ── (1) ADD-ROLE post-init, then post addressed to it ──────────────────────


def test_pre_change_bind_undeclared_role_was_rejected(topic):
    """Document the failure the operator hit: bind to an undeclared role."""
    conn, t = topic
    with pytest.raises(DebateError) as exc_info:
        bind_role_session(
            conn,
            topic_id=t,
            role="ADVOCATE",
            session_id="cc-adv1",
            reason="bind undeclared",
        )
    assert exc_info.value.error_type == "recipient_unknown_role"


def test_pre_change_reinit_with_added_role_was_rejected(topic):
    """Document the failure: re-init with an added role."""
    conn, t = topic
    with pytest.raises(DebateError) as exc_info:
        init_debate(
            conn,
            topic_id=t,
            title="flexible roster",
            roles=[
                {"role": "CONDUCTOR", "session_id": "codex-cond1"},
                {"role": "EXECUTOR", "session_id": "codex-exec1"},
                {"role": "ADVOCATE", "session_id": "cc-adv1"},
            ],
            created_by_role="CONDUCTOR",
        )
    assert "topic_exists_with_different_roles" in str(exc_info.value)


def test_add_role_then_post_addressed_to_it_is_delivered(topic):
    conn, t = topic

    out = add_role_to_debate(
        conn,
        topic_id=t,
        role="ADVOCATE",
        session_id="cc-adv1",
        reason="enable advocate mid-debate",
        bound_by_role="CONDUCTOR",
    )
    assert out["added_role"] is True
    assert out["state"] == "active"

    # roles_json now declares the new role (gates recipient validation).
    assert "ADVOCATE" in _declared_roles(conn, t)
    # binding is the runtime wake authority.
    assert _binding_state(conn, t, "ADVOCATE", "cc-adv1") == "active"

    # The real success criterion: a post addressed to the new role is accepted
    # AND delivered to the new session's inbox.
    msg = debate_post_with_recipients(
        conn,
        topic_id=t,
        role="CONDUCTOR",
        priority="H",
        kind="STATUS",
        body="advocate, weigh in",
        addressed_to=["ADVOCATE"],
    )
    pending = debate_signal_check(
        conn, session_id="cc-adv1", role="ADVOCATE", topic_id=t
    )
    assert msg["msg_id"] in [m["msg_id"] for m in pending["pending"]]


def test_add_role_writes_audit_fields_on_binding_row(topic):
    conn, t = topic
    add_role_to_debate(
        conn,
        topic_id=t,
        role="EXECUTOR2",
        session_id="codex-exec2",
        reason="add backup executor lane",
        bound_by_role="CONDUCTOR",
    )
    row = conn.execute(
        "SELECT reason, bound_by_role, generation, created_at, state "
        "FROM debate_role_bindings "
        "WHERE topic_id = ? AND role = ? AND session_id = ?",
        (t, "EXECUTOR2", "codex-exec2"),
    ).fetchone()
    # The binding row IS the audit artifact (no separate audit table).
    assert row["reason"] == "add backup executor lane"
    assert row["bound_by_role"] == "CONDUCTOR"
    assert row["generation"] == 1
    assert row["created_at"]
    assert row["state"] == "active"


def test_add_role_idempotent_when_same_session_already_owns(topic):
    conn, t = topic
    first = add_role_to_debate(
        conn,
        topic_id=t,
        role="ADVOCATE",
        session_id="cc-adv1",
        reason="add advocate",
    )
    second = add_role_to_debate(
        conn,
        topic_id=t,
        role="ADVOCATE",
        session_id="cc-adv1",
        reason="add advocate again",
    )
    assert first["added_role"] is True
    assert second["added_role"] is False
    assert second["state"] == "active"
    # No second declaration, no duplicate active.
    assert _active_count(conn, t, "ADVOCATE") == 1
    assert sorted(_declared_roles(conn, t)) == sorted(
        {"CONDUCTOR", "EXECUTOR", "ADVOCATE"}
    )


# ── (2) SWAP session for a role on limit exhaustion ─────────────────────────


def test_add_role_declared_different_active_owner_rejected_without_replace(topic):
    conn, t = topic
    add_role_to_debate(
        conn,
        topic_id=t,
        role="ADVOCATE",
        session_id="cc-adv1",
        reason="initial advocate",
    )
    with pytest.raises(DebateError) as exc_info:
        add_role_to_debate(
            conn,
            topic_id=t,
            role="ADVOCATE",
            session_id="cc-adv2",
            reason="second advocate without replace",
        )
    assert exc_info.value.error_type == "binding_duplicate_active"


def test_swap_active_session_via_replace_active_is_atomic(topic):
    """replace_active=True covers the limit-exhaustion swap: old retired, new
    active, no duplicate active owner — all in one transaction."""
    conn, t = topic
    out = bind_role_session(
        conn,
        topic_id=t,
        role="EXECUTOR",
        session_id="codex-exec2",
        reason="exec1 hit usage limit; swap in exec2",
        replace_active=True,
    )
    assert out["state"] == "active"
    assert out["retired_sessions"] == ["codex-exec1"]
    assert _binding_state(conn, t, "EXECUTOR", "codex-exec1") == "retired"
    assert _binding_state(conn, t, "EXECUTOR", "codex-exec2") == "active"
    # Invariant: exactly one active owner survives the swap.
    assert _active_count(conn, t, "EXECUTOR") == 1


def test_swap_via_add_role_replace_active_on_existing_role(topic):
    conn, t = topic
    add_role_to_debate(
        conn,
        topic_id=t,
        role="ADVOCATE",
        session_id="cc-adv1",
        reason="advocate v1",
    )
    out = add_role_to_debate(
        conn,
        topic_id=t,
        role="ADVOCATE",
        session_id="cc-adv2",
        reason="advocate v1 exhausted; swap to v2",
        replace_active=True,
    )
    assert out["state"] == "active"
    assert out["retired_sessions"] == ["cc-adv1"]
    assert out["added_role"] is False  # role already declared
    assert _active_count(conn, t, "ADVOCATE") == 1
    assert _binding_state(conn, t, "ADVOCATE", "cc-adv2") == "active"


def test_rotate_binding_swaps_owner_and_carries_cursor(topic):
    """rotate_role_binding is the operator-facing swap for exhausted sessions:
    atomic owner swap PLUS read-cursor continuity (head/copy/replay)."""
    conn, t = topic
    # Give exec1 a cursor by posting + advancing.
    msg = debate_post_with_recipients(
        conn,
        topic_id=t,
        role="CONDUCTOR",
        priority="M",
        kind="STATUS",
        body="cursor anchor",
        addressed_to=["EXECUTOR"],
    )
    out = rotate_role_binding(
        conn,
        topic_id=t,
        role="EXECUTOR",
        old_session_id="codex-exec1",
        new_session_id="codex-exec2",
        cursor_mode="head",
        reason="exec1 exhausted; rotate to exec2 at head",
    )
    assert out["new_session_id"] == "codex-exec2"
    assert _binding_state(conn, t, "EXECUTOR", "codex-exec1") == "retired"
    assert _binding_state(conn, t, "EXECUTOR", "codex-exec2") == "active"
    # head mode seeds the replacement's cursor to the latest message — the new
    # session does not re-process already-seen history.
    cursor = conn.execute(
        "SELECT last_processed_msg_id FROM debate_signal_state "
        "WHERE session_id = ? AND role = ? AND topic_id = ?",
        ("codex-exec2", "EXECUTOR", t),
    ).fetchone()
    assert cursor["last_processed_msg_id"] == msg["msg_id"]


# ── (3) ENABLE / DISABLE binding (retire/reactivate without losing history) ─


def test_disable_non_active_backup_then_reactivate(topic):
    conn, t = topic
    # Add a backup ADVOCATE owner, then stand up a diagnostic backup session.
    add_role_to_debate(
        conn,
        topic_id=t,
        role="ADVOCATE",
        session_id="cc-adv1",
        reason="primary advocate",
    )
    bind_role_session(
        conn,
        topic_id=t,
        role="ADVOCATE",
        session_id="cc-advbkp1",
        state="diagnostic",
        reason="standby backup",
    )
    # Disable the NON-active backup — no ownership gap, no override required.
    disabled = bind_role_session(
        conn,
        topic_id=t,
        role="ADVOCATE",
        session_id="cc-advbkp1",
        state="retired",
        reason="disable standby backup",
    )
    assert disabled["state"] == "retired"
    assert disabled["ownership_gap_override"] is False
    assert _binding_state(conn, t, "ADVOCATE", "cc-advbkp1") == "retired"
    # Primary is untouched.
    assert _binding_state(conn, t, "ADVOCATE", "cc-adv1") == "active"


def test_disable_active_owner_then_reactivate_keeps_history(topic):
    conn, t = topic
    add_role_to_debate(
        conn,
        topic_id=t,
        role="ADVOCATE",
        session_id="cc-adv1",
        reason="advocate online",
    )
    gen_before = conn.execute(
        "SELECT generation FROM debate_role_bindings "
        "WHERE topic_id = ? AND role = ? AND session_id = ?",
        (t, "ADVOCATE", "cc-adv1"),
    ).fetchone()["generation"]

    # Retiring the ACTIVE owner is an ownership gap -> requires CONDUCTOR
    # override DECISION.
    with pytest.raises(DebateError) as exc_info:
        bind_role_session(
            conn,
            topic_id=t,
            role="ADVOCATE",
            session_id="cc-adv1",
            state="retired",
            reason="disable active without override",
        )
    assert exc_info.value.error_type == "conductor_override_required"

    override = post_message(
        conn,
        topic_id=t,
        role="CONDUCTOR",
        priority="H",
        kind="DECISION",
        body="allow advocate to go offline",
    )
    bind_role_session(
        conn,
        topic_id=t,
        role="ADVOCATE",
        session_id="cc-adv1",
        state="retired",
        reason="advocate offline",
        conductor_override_msg_id=override["msg_id"],
    )
    assert _binding_state(conn, t, "ADVOCATE", "cc-adv1") == "retired"

    # Reactivate the same session: no other active owner -> no override needed,
    # generation bumped, history (older generations) preserved as rows.
    reactivated = bind_role_session(
        conn,
        topic_id=t,
        role="ADVOCATE",
        session_id="cc-adv1",
        state="active",
        reason="advocate back online",
    )
    assert reactivated["state"] == "active"
    assert reactivated["generation"] > gen_before
    assert _binding_state(conn, t, "ADVOCATE", "cc-adv1") == "active"
    assert _active_count(conn, t, "ADVOCATE") == 1


def test_add_role_reattaches_new_session_when_declared_role_has_no_active_owner(topic):
    """Declared role whose owner was retired -> add_role reattaches a fresh
    session as active (not a re-declaration, no roles_json duplication)."""
    conn, t = topic
    # Retire EXECUTOR's active owner via CONDUCTOR override.
    override = post_message(
        conn,
        topic_id=t,
        role="CONDUCTOR",
        priority="H",
        kind="DECISION",
        body="executor offline",
    )
    bind_role_session(
        conn,
        topic_id=t,
        role="EXECUTOR",
        session_id="codex-exec1",
        state="retired",
        reason="exec offline",
        conductor_override_msg_id=override["msg_id"],
    )
    assert _active_count(conn, t, "EXECUTOR") == 0

    out = add_role_to_debate(
        conn,
        topic_id=t,
        role="EXECUTOR",
        session_id="codex-exec2",
        reason="reattach replacement executor",
    )
    assert out["added_role"] is False  # role was already declared
    assert out["state"] == "active"
    assert _binding_state(conn, t, "EXECUTOR", "codex-exec2") == "active"
    assert _active_count(conn, t, "EXECUTOR") == 1
    # roles_json keeps a single EXECUTOR entry (no duplicate declaration).
    row = conn.execute(
        "SELECT roles_json FROM debates WHERE topic_id = ?", (t,)
    ).fetchone()
    exec_entries = [
        r for r in json.loads(row["roles_json"]) if r["role"] == "EXECUTOR"
    ]
    assert len(exec_entries) == 1


# ── (4) Invariant guards ────────────────────────────────────────────────────


def test_invariant_no_two_active_owners_for_a_role(topic):
    conn, t = topic
    add_role_to_debate(
        conn,
        topic_id=t,
        role="ADVOCATE",
        session_id="cc-adv1",
        reason="primary",
    )
    # Direct bind of a second session without replace_active is rejected.
    with pytest.raises(DebateError) as exc_info:
        bind_role_session(
            conn,
            topic_id=t,
            role="ADVOCATE",
            session_id="cc-adv2",
            reason="duplicate active",
        )
    assert exc_info.value.error_type == "binding_duplicate_active"
    assert _active_count(conn, t, "ADVOCATE") == 1


def test_invariant_retiring_active_owner_requires_conductor_override(topic):
    conn, t = topic
    with pytest.raises(DebateError) as exc_info:
        bind_role_session(
            conn,
            topic_id=t,
            role="EXECUTOR",
            session_id="codex-exec1",
            state="retired",
            reason="retire active executor",
        )
    assert exc_info.value.error_type == "conductor_override_required"
    assert _binding_state(conn, t, "EXECUTOR", "codex-exec1") == "active"


def test_add_role_rejects_invalid_role_and_unknown_topic(topic):
    conn, t = topic
    with pytest.raises(DebateError):
        add_role_to_debate(
            conn,
            topic_id=t,
            role="lowercase_bad",
            session_id="cc-adv1",
            reason="bad role",
        )
    with pytest.raises(DebateError) as exc_info:
        add_role_to_debate(
            conn,
            topic_id="NO_SUCH_TOPIC",
            role="ADVOCATE",
            session_id="cc-adv1",
            reason="missing topic",
        )
    assert exc_info.value.error_type == "topic_not_found"


# ── (5) Backcompat: original topics/roles unchanged ─────────────────────────


def test_backcompat_original_roles_and_bindings_intact_after_add(topic):
    conn, t = topic
    before_active = {
        (r["role"], r["session_id"])
        for r in conn.execute(
            "SELECT role, session_id FROM debate_role_bindings "
            "WHERE topic_id = ? AND state = 'active'",
            (t,),
        ).fetchall()
    }
    add_role_to_debate(
        conn,
        topic_id=t,
        role="ADVOCATE",
        session_id="cc-adv1",
        reason="add advocate",
    )
    after_active = {
        (r["role"], r["session_id"])
        for r in conn.execute(
            "SELECT role, session_id FROM debate_role_bindings "
            "WHERE topic_id = ? AND state = 'active'",
            (t,),
        ).fetchall()
    }
    # Original active bindings are a subset of the post-add set (only added to).
    assert before_active <= after_active
    assert ("ADVOCATE", "cc-adv1") in after_active
    # Original declared roles still present.
    assert {"CONDUCTOR", "EXECUTOR"} <= _declared_roles(conn, t)
    # Existing-role posting still works (no regression to declared roles).
    msg = debate_post_with_recipients(
        conn,
        topic_id=t,
        role="CONDUCTOR",
        priority="M",
        kind="STATUS",
        body="executor still addressable",
        addressed_to=["EXECUTOR"],
    )
    pending = debate_signal_check(
        conn, session_id="codex-exec1", role="EXECUTOR", topic_id=t
    )
    assert msg["msg_id"] in [m["msg_id"] for m in pending["pending"]]


# ── MCP wrapper surface (debate_add_role) ───────────────────────────────────


@pytest.fixture
def wrapper_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "memory.db")
    init_db(db_path)
    monkeypatch.setattr(
        intel_server, "_get_conn", lambda: db_utils.get_conn(db_path)
    )
    monkeypatch.setattr(
        intel_server,
        "_get_conn_immediate",
        lambda: db_utils.get_conn_immediate(db_path),
    )
    # debate_init's premium gate only fires on NEW-topic creation; clear any
    # ambient gate env so the fixture init is deterministic.
    for name in (
        "SQLITE_MEMORY_DEBATE_GATE_ENABLED",
        "SQLITE_MEMORY_DEBATE_GATE_DISABLED",
    ):
        monkeypatch.delenv(name, raising=False)
    intel_server.debate_init(
        topic_id="WRAP1",
        title="wrapper roster",
        roles_json=json.dumps(
            [
                {"role": "CONDUCTOR", "session_id": "codex-cond20260531"},
                {"role": "EXECUTOR", "session_id": "codex-exec20260531"},
            ]
        ),
        created_by_role="CONDUCTOR",
        metadata_json=json.dumps(
            {"priority_lane": "P2", "priority_reason": "wrapper add-role test"}
        ),
    )
    return db_path


def test_wrapper_add_role_then_addressed_post_delivers(wrapper_db):
    out = json.loads(
        intel_server.debate_add_role(
            topic_id="WRAP1",
            role="ADVOCATE",
            session_id="cc-adv20260531",
            reason="enable advocate via MCP",
            bound_by_role="CONDUCTOR",
        )
    )
    assert "error_type" not in out
    assert out["added_role"] is True
    assert out["role"] == "ADVOCATE"

    posted = json.loads(
        intel_server.debate_post_with_recipients(
            topic_id="WRAP1",
            role="CONDUCTOR",
            priority="H",
            kind="STATUS",
            body="advocate online?",
            addressed_to_csv="ADVOCATE",
        )
    )
    assert "error_type" not in posted

    checked = json.loads(
        intel_server.debate_signal_check(
            session_id="cc-adv20260531", role="ADVOCATE", topic_id="WRAP1"
        )
    )
    assert posted["msg_id"] in [m["msg_id"] for m in checked["pending"]]


def test_wrapper_add_role_existing_topic_never_gated(wrapper_db, monkeypatch):
    # Adding a role to an existing topic must not be lockout-able by the
    # premium gate (same posture as debate_bind_role).
    monkeypatch.setattr(
        premium_runtime,
        "load_premium_config",
        lambda: {
            **premium_runtime._DEFAULT_CONFIG,
            "debate_protocol_gate_enabled": True,
            "control_plane_required": False,
        },
    )
    out = json.loads(
        intel_server.debate_add_role(
            topic_id="WRAP1",
            role="ADVOCATE",
            session_id="cc-adv20260531",
            reason="add under enabled gate",
            bound_by_role="CONDUCTOR",
        )
    )
    assert "error_type" not in out
    assert out["role"] == "ADVOCATE"
    assert out["added_role"] is True
