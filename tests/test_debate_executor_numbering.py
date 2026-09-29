"""Executor numbers are never inherited; role EXECUTOR -> next free number.

Operator requirement 2026-09-29: an EXECUTOR_N number, once bound to a
session, is never re-issued to another session; a new session asks for
EXECUTOR and gets EXECUTOR_{max_ever+1}. All DBs are temp fixtures.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import db_utils
import intel_server
from debate import (
    DebateError,
    add_role_to_debate,
    bind_role_session,
    init_debate,
    rotate_role_binding,
    seed_initial_role_bindings,
    transition_state,
)
from debate_protocol_v1 import sweep_missing_roles
from schema import init_db


def _init(conn, topic_id, roles):
    init_debate(
        conn,
        topic_id=topic_id,
        title=topic_id,
        roles=roles,
        created_by_role="CONDUCTOR",
        require_numbered_executors=True,
    )


@pytest.fixture
def db(tmp_path):
    db_path = str(tmp_path / "exec_numbering.db")
    init_db(db_path)
    c = sqlite3.connect(db_path, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    _init(
        c,
        "DAY1",
        [
            {"role": "CONDUCTOR", "session_id": "codex-cond1"},
            {"role": "ADVOCATE", "session_id": "cc-advo1"},
        ],
    )
    for role, session in (("CONDUCTOR", "codex-cond1"), ("ADVOCATE", "cc-advo1")):
        bind_role_session(
            c, topic_id="DAY1", role=role, session_id=session, reason="seed"
        )
    transition_state(c, topic_id="DAY1", role="CONDUCTOR", new_state="ACTIVE")
    # History: E7 on DAY1 (active), E30 on an older, closed topic (retired).
    _init(c, "OLD", [{"role": "CONDUCTOR", "session_id": "codex-cond0"}])
    add_role_to_debate(
        c, topic_id="OLD", role="EXECUTOR_30", session_id="cc-exec30", reason="old"
    )
    transition_state(c, topic_id="OLD", role="CONDUCTOR", new_state="ACTIVE")
    transition_state(c, topic_id="OLD", role="CONDUCTOR", new_state="RESOLVED")
    add_role_to_debate(
        c, topic_id="DAY1", role="EXECUTOR_7", session_id="cc-exec7", reason="seed"
    )
    yield c, db_path
    c.close()


def test_next_free_is_max_over_all_topics_including_retired(db):
    conn, _ = db
    out = add_role_to_debate(
        conn, topic_id="DAY1", role="EXECUTOR", session_id="cc-new1", reason="join"
    )
    assert out["role"] == "EXECUTOR_31"
    assert out["display_label"] == "E31"
    assert "/rename E31" in out["pane_identity"]
    nxt = add_role_to_debate(
        conn,
        topic_id="DAY1",
        role="EXECUTOR_NEXT",
        session_id="cc-new2",
        reason="join",
    )
    assert nxt["role"] == "EXECUTOR_32"


def test_repeat_request_by_same_session_keeps_its_number(db):
    conn, _ = db
    first = add_role_to_debate(
        conn, topic_id="DAY1", role="EXECUTOR", session_id="cc-new1", reason="join"
    )
    again = add_role_to_debate(
        conn, topic_id="DAY1", role="EXECUTOR", session_id="cc-new1", reason="retry"
    )
    assert again["role"] == first["role"]
    assert again["added_role"] is False


def test_concurrent_allocations_get_distinct_numbers(db):
    _, db_path = db
    results: list[str] = []
    errors: list[BaseException] = []

    def register(session_id):
        try:
            with db_utils.get_conn_immediate(db_path) as conn:
                out = add_role_to_debate(
                    conn,
                    topic_id="DAY1",
                    role="EXECUTOR",
                    session_id=session_id,
                    reason="race",
                )
                results.append(out["role"])
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [
        threading.Thread(target=register, args=(f"cc-race{i}",)) for i in range(4)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert sorted(results) == sorted(f"EXECUTOR_{n}" for n in range(31, 35))


def test_same_session_carries_its_number_into_new_topic(db):
    conn, _ = db
    _init(conn, "DAY2", [{"role": "CONDUCTOR", "session_id": "codex-cond1"}])
    out = add_role_to_debate(
        conn, topic_id="DAY2", role="EXECUTOR_7", session_id="cc-exec7", reason="roll"
    )
    assert out["role"] == "EXECUTOR_7"
    assert out["display_label"] == "E7"
    bare = add_role_to_debate(
        conn, topic_id="DAY2", role="EXECUTOR", session_id="cc-exec30", reason="roll"
    )
    assert bare["role"] == "EXECUTOR_30"


def test_foreign_session_rejected_on_add(db):
    conn, _ = db
    _init(conn, "DAY2", [{"role": "CONDUCTOR", "session_id": "codex-cond1"}])
    with pytest.raises(DebateError) as exc:
        add_role_to_debate(
            conn,
            topic_id="DAY2",
            role="EXECUTOR_30",
            session_id="cc-xforeign",
            reason="x",
        )
    assert exc.value.error_type == "executor_number_not_inheritable"
    assert "request role EXECUTOR" in str(exc.value)
    roles = [
        r["role"]
        for r in json.loads(
            conn.execute(
                "SELECT roles_json FROM debates WHERE topic_id='DAY2'"
            ).fetchone()[0]
        )
    ]
    assert "EXECUTOR_30" not in roles


def test_foreign_session_rejected_on_bind_active_and_diagnostic(db):
    conn, _ = db
    for state in ("active", "diagnostic"):
        with pytest.raises(DebateError) as exc:
            bind_role_session(
                conn,
                topic_id="DAY1",
                role="EXECUTOR_7",
                session_id="cc-xforeign",
                state=state,
                reason="take over",
                replace_active=True,
            )
        assert exc.value.error_type == "executor_number_not_inheritable"


def test_foreign_session_rejected_on_init_roster(db):
    conn, _ = db
    with pytest.raises(DebateError) as exc:
        _init(
            conn,
            "DAY3",
            [
                {"role": "CONDUCTOR", "session_id": "codex-cond1"},
                {"role": "EXECUTOR_7", "session_id": "cc-xforeign"},
            ],
        )
    assert exc.value.error_type == "executor_number_not_inheritable"


def test_init_roster_bare_executor_allocates(db):
    conn, _ = db
    roles = [
        {"role": "CONDUCTOR", "session_id": "codex-cond1"},
        {"role": "EXECUTOR", "session_id": "cc-execa"},
        {"role": "EXECUTOR", "session_id": "cc-execb"},
    ]
    out = init_debate(
        conn,
        topic_id="DAY4",
        title="DAY4",
        roles=roles,
        created_by_role="CONDUCTOR",
        require_numbered_executors=True,
    )
    assert [r["role"] for r in out["roles"]] == [
        "CONDUCTOR",
        "EXECUTOR_31",
        "EXECUTOR_32",
    ]


def test_rotate_executor_to_new_session_rejected(db):
    conn, _ = db
    with pytest.raises(DebateError) as exc:
        rotate_role_binding(
            conn,
            topic_id="DAY1",
            role="EXECUTOR_7",
            old_session_id="cc-exec7",
            new_session_id="cc-exec7b",
            cursor_mode="copy",
            reason="exhausted",
        )
    assert exc.value.error_type == "executor_number_not_inheritable"


def test_retire_legacy_foreign_binding_still_allowed(db):
    conn, _ = db
    # Legacy row from before the rule: an older session also held EXECUTOR_7.
    conn.execute(
        "INSERT INTO debate_role_bindings (topic_id, role, session_id, runtime, "
        "state, generation, created_at, updated_at, reason) VALUES "
        "('DAY1', 'EXECUTOR_7', 'cc-legacy', 'claude', 'diagnostic', 0, "
        "'2000-01-01T00:00:00Z', '2000-01-01T00:00:00Z', 'legacy')"
    )
    out = bind_role_session(
        conn,
        topic_id="DAY1",
        role="EXECUTOR_7",
        session_id="cc-legacy",
        state="retired",
        reason="cleanup",
    )
    assert out["state"] == "retired"
    # The latest owner keeps the number; the legacy session cannot reclaim it.
    with pytest.raises(DebateError):
        bind_role_session(
            conn,
            topic_id="DAY1",
            role="EXECUTOR_7",
            session_id="cc-legacy",
            state="diagnostic",
            reason="reclaim",
        )


def test_advocate_rotation_still_works(db):
    conn, _ = db
    out = rotate_role_binding(
        conn,
        topic_id="DAY1",
        role="ADVOCATE",
        old_session_id="cc-advo1",
        new_session_id="cc-advo2",
        cursor_mode="copy",
        reason="handoff",
    )
    assert out["session_id"] == "cc-advo2"
    assert out["display_label"] == "ADV"


def test_wrapper_add_role_executor_returns_allocated_label(db, monkeypatch):
    _, db_path = db
    monkeypatch.setattr(intel_server, "_get_conn", lambda: db_utils.get_conn(db_path))
    monkeypatch.setattr(
        intel_server,
        "_get_conn_immediate",
        lambda: db_utils.get_conn_immediate(db_path),
    )
    out = json.loads(
        intel_server.debate_add_role(
            topic_id="DAY1",
            role="EXECUTOR",
            session_id="cc-wrap1",
            reason="join via MCP",
            bound_by_role="CONDUCTOR",
        )
    )
    assert out["role"] == "EXECUTOR_31"
    assert out["display_label"] == "E31"
    denied = json.loads(
        intel_server.debate_rotate_binding(
            topic_id="DAY1",
            role="EXECUTOR_7",
            old_session_id="cc-exec7",
            new_session_id="cc-wrap2",
            cursor_mode="head",
            reason="inherit",
        )
    )
    assert denied["error_type"] == "executor_number_not_inheritable"


def _roster(*pairs):
    return [{"role": r, "session_id": s} for r, s in pairs]


def test_init_retry_with_bare_executor_is_idempotent(db):
    conn, _ = db
    payload = (("CONDUCTOR", "codex-cond1"), ("EXECUTOR", "cc-new"))
    _init(conn, "RETRY", _roster(*payload))
    # Client retry after an MCP timeout: same JSON, no bindings seeded yet.
    again = init_debate(
        conn,
        topic_id="RETRY",
        title="RETRY",
        roles=_roster(*payload),
        created_by_role="CONDUCTOR",
        require_numbered_executors=True,
    )
    assert [r["role"] for r in again["roles"]] == ["CONDUCTOR", "EXECUTOR_31"]


def test_v1_blind_bare_executor_resolves_to_allocated_number(db):
    conn, _ = db
    out = init_debate(
        conn,
        topic_id="V1BLIND",
        title="V1BLIND",
        roles=_roster(("CONDUCTOR", "codex-cond1"), ("EXECUTOR", "cc-execx1")),
        created_by_role="CONDUCTOR",
        require_numbered_executors=True,
        protocol_version="debate/v1",
        blind_roles=["CONDUCTOR", "EXECUTOR"],
    )
    assert out["roles"][1]["role"] == "EXECUTOR_31"
    assert "EXECUTOR_31" in json.dumps(out["protocol_state"])


def test_v1_sweep_never_reissues_executor_number(db):
    conn, _ = db
    roles = _roster(("CONDUCTOR", "codex-cond1"), ("EXECUTOR_7", "cc-exec7"))
    init_debate(
        conn,
        topic_id="V1SWEEP",
        title="V1SWEEP",
        roles=roles,
        created_by_role="CONDUCTOR",
        require_numbered_executors=True,
        protocol_version="debate/v1",
        blind_roles=["CONDUCTOR", "EXECUTOR_7"],
    )
    seed_initial_role_bindings(
        conn, topic_id="V1SWEEP", roles=roles, bound_by_role="CONDUCTOR", reason="t"
    )
    transition_state(conn, topic_id="V1SWEEP", role="CONDUCTOR", new_state="ACTIVE")
    conn.execute(
        "UPDATE debate_role_bindings SET state='retired' "
        "WHERE topic_id='V1SWEEP' AND role='EXECUTOR_7'"
    )
    actions = sweep_missing_roles(conn, topic_ids=["V1SWEEP"])
    assert all(a["role"] != "EXECUTOR_7" for a in actions)
    back = bind_role_session(
        conn,
        topic_id="V1SWEEP",
        role="EXECUTOR_7",
        session_id="cc-exec7",
        reason="owner rebinds",
        replace_active=True,
    )
    assert back["state"] == "active"


def test_pane_identity_hint_depends_on_uuid_tail(db):
    conn, _ = db
    plain = add_role_to_debate(
        conn, topic_id="DAY1", role="EXECUTOR", session_id="cc-exec_newpane", reason="x"
    )
    assert "no _<uuid8> tail" in plain["pane_identity"]
    tagged = add_role_to_debate(
        conn,
        topic_id="DAY1",
        role="EXECUTOR",
        session_id="cc-exec_1a2b3c4d",
        reason="x",
    )
    assert "statusline.py if" in tagged["pane_identity"]
