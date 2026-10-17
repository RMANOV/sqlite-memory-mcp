"""Peer/v1 must not inherit legacy authority, receipts, or migration effects.

Every database is a temporary fixture. No transport, resident service, or live
session is used. These tests deliberately exercise the public legacy DAOs as
well as MCP wrappers: protecting only the new happy path is insufficient.
"""

from __future__ import annotations

import inspect
import json
import socket
import sqlite3
import subprocess
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

import db_utils
import debate
import debate_peer as peer
import debate_protocol_v1 as v1
import intel_server
from schema import init_db


TOPIC = "PEER_BOUNDARY"
MEMBERS = [
    {"member_id": "CONDUCTOR", "session_id": "codex-peer_one"},
    {"member_id": "REVIEWER", "session_id": "codex-peer_two"},
    {"member_id": "THIRD", "session_id": "cc-peer_three"},
]
ROLES = [
    {"role": "CONDUCTOR", "session_id": "codex-old_conductor"},
    {"role": "EXECUTOR", "session_id": "cc-old_executor"},
    {"role": "ADVOCATE", "session_id": "codex-old_advocate"},
    {"role": "JUDGE", "session_id": "cc-old_judge"},
]


def _connect(path):
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _snapshot(conn, *, include_peer=True):
    """Compare logical rows, including FTS contents and all receipt/audit tables.

    FTS shadow pages may legitimately be repacked without changing indexed
    history; the virtual table itself is included instead of those pages.
    """
    tables = [
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND (name='debates' OR name LIKE 'debate_%') ORDER BY name"
        )
        if not row[0].startswith("debate_messages_fts_")
        and (include_peer or not row[0].startswith("debate_peer_"))
    ]
    return {
        name: sorted(
            (tuple(row) for row in conn.execute(f'SELECT * FROM "{name}"')),
            key=repr,
        )
        for name in tables
    }


def _create_peer(conn, topic_id=TOPIC):
    return peer.create_topic(
        conn,
        topic_id=topic_id,
        title="Equal discussion boundary",
        members=MEMBERS,
        creator_member_id="CONDUCTOR",
        author_session_id="codex-peer_one",
    )


def _seed_legacy(conn, topic_id, *, protocol=None):
    debate.init_debate(
        conn,
        topic_id=topic_id,
        title="Preserved legacy history",
        roles=ROLES,
        created_by_role="CONDUCTOR",
        protocol_version=protocol,
        blind_roles=["EXECUTOR", "ADVOCATE"] if protocol else None,
    )
    debate.seed_initial_role_bindings(
        conn, topic_id=topic_id, roles=ROLES, bound_by_role="CONDUCTOR"
    )
    debate.transition_state(
        conn, topic_id=topic_id, role="CONDUCTOR", new_state="ACTIVE",
        author_session_id="codex-old_conductor",
    )
    if protocol:
        debate.debate_post_with_recipients(
            conn, topic_id=topic_id, role="EXECUTOR", priority="M",
            kind="CLAIM", body="historyneedle blind position",
            addressed_to=["ADVOCATE"], protocol_version="debate/v1",
            body_mode="structured",
            payload_json={"summary": "historyneedle blind position",
                          "assumptions": [], "evidence_refs": []},
            author_session_id="cc-old_executor",
        )
        return
    question = debate.debate_post_with_recipients(
        conn, topic_id=topic_id, role="CONDUCTOR", priority="M", kind="Q",
        body="historyneedle old question", addressed_to=["ADVOCATE"],
        author_session_id="codex-old_conductor",
    )
    debate.post_message(
        conn, topic_id=topic_id, role="ADVOCATE", priority="M", kind="A",
        body="historyneedle attributed reply", reply_to=question["msg_id"],
        author_session_id="codex-old_advocate",
    )
    debate.advance_watermark(
        conn, topic_id=topic_id, role="CONDUCTOR",
        processed_up_to_msg_id=question["msg_id"],
        author_session_id="codex-old_conductor",
    )
    debate.debate_post_with_recipients(
        conn, topic_id=topic_id, role="CONDUCTOR", priority="M", kind="Q",
        body="historyneedle pending delivery", addressed_to=["ADVOCATE"],
        author_session_id="codex-old_conductor",
    )


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "boundary.db"
    init_db(str(path))
    db = _connect(path)
    db.execute("BEGIN IMMEDIATE")
    yield db
    db.rollback()
    db.close()


@pytest.fixture
def peer_conn(conn):
    _create_peer(conn)
    return conn


@pytest.fixture
def wrapper_db(tmp_path, monkeypatch):
    path = tmp_path / "wrapper.db"
    init_db(str(path))
    monkeypatch.setattr(intel_server, "_get_conn", lambda: db_utils.get_conn(str(path)))
    monkeypatch.setattr(
        intel_server, "_get_conn_immediate",
        lambda: db_utils.get_conn_immediate(str(path)),
    )
    monkeypatch.setattr(
        intel_server, "evaluate_debate_protocol_creation_gate",
        lambda *args, **kwargs: {"allowed": True, "reason": "test-only"},
    )
    return path


def _wrapper_init(**overrides):
    args = {
        "topic_id": TOPIC, "title": "Equal discussion boundary",
        "members_json": json.dumps(MEMBERS), "creator_member_id": "CONDUCTOR",
        "author_session_id": "codex-peer_one",
    }
    args.update(overrides)
    return json.loads(intel_server.debate_peer_init(**args))


def _file_snapshot(path):
    conn = _connect(path)
    try:
        return _snapshot(conn)
    finally:
        conn.close()


def test_creation_gate_blocks_only_new_peer_topics(wrapper_db, monkeypatch):
    calls = []

    def deny(conn, **kwargs):
        calls.append(kwargs)
        return {"allowed": False, "reason": "explicit-test-denial"}

    before = _file_snapshot(wrapper_db)
    monkeypatch.setattr(intel_server, "evaluate_debate_protocol_creation_gate", deny)
    denied = _wrapper_init()
    assert denied["error_type"] == "premium_gate_denied"
    assert _file_snapshot(wrapper_db) == before
    assert calls[0]["tool_name"] == "sqlite-intel.debate_peer_init"
    assert calls[0]["payload"]["protocol_version"] == "peer/v1"

    monkeypatch.setattr(
        intel_server, "evaluate_debate_protocol_creation_gate",
        lambda *args, **kwargs: {"allowed": True},
    )
    assert _wrapper_init()["replayed"] is False
    after_create = _file_snapshot(wrapper_db)
    monkeypatch.setattr(intel_server, "evaluate_debate_protocol_creation_gate", deny)
    assert _wrapper_init()["replayed"] is True
    assert len(calls) == 1
    assert _file_snapshot(wrapper_db) == after_create
    assert json.loads(intel_server.debate_peer_read(TOPIC))["topic"]["protocol_version"] == "peer/v1"


@pytest.mark.parametrize("members_json", ["{", "null", "{}", "[]"])
def test_wrapper_invalid_members_are_typed_and_do_not_create(wrapper_db, members_json):
    before = _file_snapshot(wrapper_db)
    out = _wrapper_init(members_json=members_json)
    assert out["error_type"] == "INVALID_MEMBERS"
    assert _file_snapshot(wrapper_db) == before


@pytest.mark.parametrize("operation", ["init", "post", "propose", "vote"])
def test_wrapper_replay_checks_exact_owner_before_returning_existing_data(
    wrapper_db, operation
):
    assert "error" not in _wrapper_init()
    shared = {"topic_id": TOPIC, "member_id": "CONDUCTOR",
              "author_session_id": "codex-peer_one"}
    post_args = {**shared, "post_id": "post-one", "body": "discussion"}
    proposal_args = {**shared, "proposal_id": "proposal-one", "body": "proposal"}
    assert "error" not in json.loads(intel_server.debate_peer_post(**post_args))
    proposal = json.loads(intel_server.debate_peer_propose(**proposal_args))
    vote_args = {**shared, "proposal_id": "proposal-one", "choice": "approve",
                 "expected_proposal_hash": proposal["body_hash"]}
    assert "error" not in json.loads(intel_server.debate_peer_vote(**vote_args))
    before = _file_snapshot(wrapper_db)
    if operation == "init":
        out = _wrapper_init(author_session_id="codex-forged_session")
    else:
        args = {"post": post_args, "propose": proposal_args, "vote": vote_args}[operation]
        out = json.loads(getattr(intel_server, f"debate_peer_{operation}")(
            **{**args, "author_session_id": "codex-forged_session"}
        ))
    assert out["error_type"] == "PEER_AUTHOR_UNAUTHORIZED"
    assert _file_snapshot(wrapper_db) == before


def test_wrapper_ballot_conflict_and_wrong_hash_remain_typed(wrapper_db):
    _wrapper_init()
    shared = {"topic_id": TOPIC, "member_id": "REVIEWER",
              "author_session_id": "codex-peer_two", "proposal_id": "proposal-one"}
    proposal = json.loads(intel_server.debate_peer_propose(**shared, body="exact wording"))
    vote_args = {**shared, "choice": "approve", "expected_proposal_hash": proposal["body_hash"]}
    assert "error" not in json.loads(intel_server.debate_peer_vote(**vote_args))
    before = _file_snapshot(wrapper_db)
    wrong_hash = json.loads(intel_server.debate_peer_vote(
        **{**vote_args, "expected_proposal_hash": "0" * 64}
    ))
    conflicting = json.loads(intel_server.debate_peer_vote(**{**vote_args, "choice": "reject"}))
    assert wrong_hash["error_type"] == "PEER_PROPOSAL_HASH_MISMATCH"
    assert conflicting["error_type"] == "PEER_BALLOT_CONFLICT"
    assert _file_snapshot(wrapper_db) == before


def test_wrapper_rolls_back_ballot_when_terminal_update_fails(wrapper_db):
    """An ABORT of the final UPDATE alone would leave the preceding ballot.

    The wrapper must roll back the whole managed transaction, including that
    otherwise-valid INSERT, before returning its serialized error.
    """
    _wrapper_init()
    shared = {"topic_id": TOPIC, "proposal_id": "atomic-proposal"}
    proposal = json.loads(intel_server.debate_peer_propose(
        **shared, member_id="CONDUCTOR", author_session_id="codex-peer_one",
        body="fault-injected majority transition",
    ))
    ballot = {**shared, "expected_proposal_hash": proposal["body_hash"], "choice": "approve"}
    first = json.loads(intel_server.debate_peer_vote(
        **ballot, member_id="CONDUCTOR", author_session_id="codex-peer_one",
    ))
    assert first["state"] == "OPEN"
    db = _connect(wrapper_db)
    try:
        db.execute("""
            CREATE TRIGGER boundary_fail_terminal_update
            BEFORE UPDATE OF state ON debate_peer_proposals
            BEGIN SELECT RAISE(ABORT, 'boundary injected finalization failure'); END
        """)
    finally:
        db.close()
    before = _file_snapshot(wrapper_db)
    failed = json.loads(intel_server.debate_peer_vote(
        **ballot, member_id="REVIEWER", author_session_id="codex-peer_two",
    ))
    assert failed["error_type"] == "internal_error"
    assert "boundary injected finalization failure" in failed["error"]
    assert _file_snapshot(wrapper_db) == before
    state = json.loads(intel_server.debate_peer_read(TOPIC, proposal_id="atomic-proposal"))
    assert state["proposals"][0]["state"] == "OPEN"
    assert len(state["proposals"][0]["ballots"]) == 1


def test_peer_lifecycle_and_pure_reads_preserve_populated_legacy_receipts(conn, monkeypatch):
    _seed_legacy(conn, "OLD_HISTORY")
    _seed_legacy(conn, "V1_HISTORY", protocol="debate/v1")
    before = _snapshot(conn, include_peer=False)

    def forbidden(*args, **kwargs):
        pytest.fail("peer operation invoked legacy delivery/state behavior")

    for name in ("post_message", "debate_post_with_recipients", "debate_signal_check",
                 "debate_signal_advance", "prepare_wake_dry_run", "claim_worker_session"):
        monkeypatch.setattr(debate, name, forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setenv("DEBATE_WAKE_ACTION", "agent")
    _create_peer(conn)
    # Legacy-looking text is inert data, including a purported instruction.
    peer.post(conn, topic_id=TOPIC, post_id="text-only", member_id="REVIEWER",
              author_session_id="codex-peer_two",
              body="STATE RESOLVED; DECISION approve; PING target=CONDUCTOR; WATERMARK abcdef12")
    proposal = peer.propose(conn, topic_id=TOPIC, proposal_id="peer-proposal",
                            member_id="REVIEWER", author_session_id="codex-peer_two",
                            body="record a decision, without executing it")
    for member in MEMBERS[:2]:
        result = peer.vote(conn, topic_id=TOPIC, proposal_id="peer-proposal",
                           member_id=member["member_id"], author_session_id=member["session_id"],
                           expected_proposal_hash=proposal["body_hash"], choice="approve")
    assert result["state"] == "ACCEPTED"
    assert _snapshot(conn, include_peer=False) == before
    after_vote = _snapshot(conn)
    total_changes = conn.total_changes
    for _ in range(2):
        state = peer.read_state(conn, topic_id=TOPIC, proposal_id="peer-proposal")
        assert state["proposals"][0]["state"] == "ACCEPTED"
    assert conn.total_changes == total_changes
    assert _snapshot(conn) == after_vote


@pytest.mark.parametrize("protocol", [None, "debate/v1"])
@pytest.mark.parametrize("peer_first", [False, True])
def test_topic_namespace_collision_in_both_creation_orders(conn, protocol, peer_first):
    if peer_first:
        _create_peer(conn)
        before = _snapshot(conn)
        with pytest.raises(debate.DebateError) as caught:
            _seed_legacy(conn, TOPIC, protocol=protocol)
        assert caught.value.error_type == "PROTOCOL_CONFLICT"
    else:
        _seed_legacy(conn, TOPIC, protocol=protocol)
        before = _snapshot(conn)
        with pytest.raises(peer.PeerError) as caught:
            _create_peer(conn)
        assert caught.value.error_type == "TOPIC_NAMESPACE_CONFLICT"
    assert _snapshot(conn) == before
    assert sum(conn.execute(f"SELECT COUNT(*) FROM {table} WHERE topic_id=?", (TOPIC,)).fetchone()[0]
               for table in ("debates", "debate_peer_topics")) == 1


def test_namespace_sql_guards_reject_bypass_and_legacy_rename(conn):
    _create_peer(conn)
    _seed_legacy(conn, "OLD_HISTORY")
    before = _snapshot(conn)
    attempts = [
        ("INSERT INTO debates SELECT ?,title,state,created_at,created_by_role,resolve_by,"
         "archived_at,roles_json,metadata_json FROM debates WHERE topic_id='OLD_HISTORY'", (TOPIC,)),
        ("INSERT INTO debate_peer_topics SELECT ?,protocol_version,title,members_json,"
         "config_hash,creator_member_id,creator_session_id,created_at "
         "FROM debate_peer_topics WHERE topic_id=?", ("OLD_HISTORY", TOPIC)),
        ("UPDATE debates SET topic_id=? WHERE topic_id='OLD_HISTORY'", (TOPIC,)),
    ]
    for sql, args in attempts:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(sql, args)
        assert _snapshot(conn) == before


@pytest.mark.parametrize("protocol", [None, "debate/v1"])
def test_competing_namespace_creators_persist_exactly_one_owner(tmp_path, protocol):
    path = tmp_path / "namespace-race.db"
    init_db(str(path))
    barrier = Barrier(2, timeout=30)

    def create(is_peer):
        db = _connect(path)
        db.execute("PRAGMA busy_timeout=30000")
        try:
            barrier.wait()
            db.execute("BEGIN IMMEDIATE")
            try:
                if is_peer:
                    _create_peer(db)
                else:
                    debate.init_debate(
                        db, topic_id=TOPIC, title="legacy contender", roles=ROLES,
                        created_by_role="CONDUCTOR", protocol_version=protocol,
                        blind_roles=["EXECUTOR", "ADVOCATE"] if protocol else None,
                    )
                db.commit()
                return "created"
            except (peer.PeerError, debate.DebateError) as exc:
                db.rollback()
                return exc.error_type
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(create, (True, False)))
    assert outcomes.count("created") == 1
    assert set(outcomes) <= {"created", "PROTOCOL_CONFLICT", "TOPIC_NAMESPACE_CONFLICT"}
    db = _connect(path)
    try:
        assert sum(db.execute(f"SELECT COUNT(*) FROM {table} WHERE topic_id=?", (TOPIC,)).fetchone()[0]
                   for table in ("debates", "debate_peer_topics")) == 1
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        db.close()


def _legacy_attempt(conn, operation):
    common = {"topic_id": TOPIC, "role": "CONDUCTOR"}
    if operation.startswith("post_"):
        kind = operation.removeprefix("post_")
        return debate.post_message(
            conn, **common, priority="H", kind=kind,
            body={"STATE": "RESOLVED", "WATERMARK": "abcdef12", "PING": "target=REVIEWER"}.get(kind, "legacy decision"),
            author_session_id="codex-peer_one",
        )
    actions = {
        "transition": lambda: debate.transition_state(conn, **common, new_state="RESOLVED"),
        "bind": lambda: debate.bind_role_session(conn, **common, session_id="codex-peer_one", reason="bind"),
        "retire": lambda: debate.bind_role_session(conn, **common, session_id="codex-peer_one", state="retired", reason="retire"),
        "add": lambda: debate.add_role_to_debate(conn, **common, session_id="codex-peer_one", reason="add"),
        "seed_bindings": lambda: debate.seed_initial_role_bindings(conn, topic_id=TOPIC, roles=ROLES, bound_by_role="CONDUCTOR"),
        "rotate": lambda: debate.rotate_role_binding(conn, **common, old_session_id="codex-peer_one", new_session_id="codex-replacement", cursor_mode="copy", reason="rotate"),
        "watermark": lambda: debate.advance_watermark(conn, **common, processed_up_to_msg_id="abcdef12"),
        "priority": lambda: debate.set_topic_priority(conn, **common, lane="P0", reason="override"),
        "signal_check": lambda: debate.debate_signal_check(conn, **common, session_id="codex-peer_one"),
        "signal_advance": lambda: debate.debate_signal_advance(conn, **common, session_id="codex-peer_one", last_processed_msg_id="abcdef12"),
        "claim": lambda: debate.claim_worker_session(conn, **common, parent_session_id="codex-peer_one", trigger_msg_id="abcdef12"),
        "worker_done": lambda: debate.worker_no_action(conn, **common, worker_session_id="codex-peer_one-W1", trigger_msg_id="abcdef12"),
        "recover": lambda: debate.recover_stale_worker_claims(conn, topic_id=TOPIC, older_than_ts="2020-01-01T00:00:00Z"),
        "reap": lambda: debate.reap_worker_claims(conn, topic_id=TOPIC, older_than_ts="2020-01-01T00:00:00Z"),
        "reclaim": lambda: debate.reclaim_stale_message_claims(conn, topic_id=TOPIC, older_than_ts="2020-01-01T00:00:00Z"),
        "addressed": lambda: debate.debate_post_with_recipients(conn, **common, priority="H", kind="DECISION", body="execute", addressed_to=["REVIEWER"]),
        "escalate": lambda: debate.escalate(conn, **common, reason="override", target_role="REVIEWER"),
        "compact": lambda: debate.compact(conn, **common, body="replace discussion"),
        "configure_v1": lambda: v1.configure_topic(conn, topic_id=TOPIC, declared_roles=["CONDUCTOR", "REVIEWER"], blind_roles=["CONDUCTOR", "REVIEWER"]),
        "judge_prepare": lambda: v1.prepare_order_swap(conn, topic_id=TOPIC, left_msg_id="abcdef12", right_msg_id="abcdef13"),
        "judge_verdict": lambda: v1.record_order_swap_verdict(conn, projection_id="peer-proposal", judge_role="CONDUCTOR", verdict={"winner_msg_id": "abcdef12", "decision": "accept"}),
        "sweep": lambda: v1.sweep_missing_roles(conn, topic_ids=[TOPIC]),
        "expire": lambda: v1.transition_expired_protocols(conn, now_iso="2099-01-01T00:00:00Z"),
        "wake_bad_schema": lambda: debate.prepare_wake_dry_run(conn, tool_response={"topic_id": TOPIC, "msg_id": "abcdef12", "schema_version": "unknown"}),
        "wake_unknown_trigger": lambda: debate.prepare_wake_dry_run(conn, tool_response={"topic_id": TOPIC, "msg_id": "abcdef12", "schema_version": debate.DEBATE_POST_RESPONSE_SCHEMA_VERSION}),
    }
    return actions[operation]()


@pytest.mark.parametrize("operation", [
    "post_STATE", "post_DECISION", "post_WATERMARK", "post_PING", "transition",
    "bind", "retire", "add", "seed_bindings", "rotate", "watermark", "priority",
    "signal_check", "signal_advance", "claim", "worker_done", "recover", "reap",
    "reclaim", "addressed", "escalate", "compact", "configure_v1", "judge_prepare",
    "judge_verdict", "sweep", "expire", "wake_bad_schema", "wake_unknown_trigger",
])
def test_legacy_mutator_matrix_cannot_write_into_peer_namespace(peer_conn, operation):
    before = _snapshot(peer_conn)
    changes = peer_conn.total_changes
    try:
        result = _legacy_attempt(peer_conn, operation)
    except (debate.DebateError, v1.ProtocolV1Error, sqlite3.IntegrityError):
        pass
    else:
        # Only protocol maintenance may harmlessly find nothing to do.
        assert operation in {"sweep", "expire"}
        assert result == []
    assert peer_conn.total_changes == changes
    assert _snapshot(peer_conn) == before


def test_legacy_wrapper_cannot_seed_bindings_for_existing_peer(wrapper_db, monkeypatch):
    _wrapper_init()
    before = _file_snapshot(wrapper_db)

    def gate_must_not_run(*args, **kwargs):
        pytest.fail("peer identity must be checked before legacy entitlement or binding seeding")

    monkeypatch.setattr(intel_server, "evaluate_debate_protocol_creation_gate", gate_must_not_run)
    result = json.loads(intel_server.debate_init(
        topic_id=TOPIC, title="shadow topic", roles_json=json.dumps(ROLES),
        created_by_role="CONDUCTOR",
    ))
    assert result["error_type"] == "PROTOCOL_CONFLICT"
    assert _file_snapshot(wrapper_db) == before


def test_additive_schema_reinit_preserves_legacy_v1_history_fts_and_peer_rows(tmp_path):
    path = tmp_path / "upgrade.db"
    init_db(str(path))
    db = _connect(path)
    try:
        db.execute("BEGIN IMMEDIATE")
        # Emulate a populated immediately preceding schema: remove only the
        # additive peer objects, retaining the existing legacy/v1 structures.
        peer_tables = [row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'debate_peer_%'"
        )]
        triggers = [row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' "
            "AND (name LIKE 'debate_peer_%' OR tbl_name LIKE 'debate_peer_%')"
        )]
        for name in triggers:
            db.execute(f'DROP TRIGGER "{name}"')
        for name in ("debate_peer_ballots", "debate_peer_proposals", "debate_peer_posts", "debate_peer_topics"):
            assert name in peer_tables
            db.execute(f'DROP TABLE "{name}"')
        _seed_legacy(db, "OLD_HISTORY")
        _seed_legacy(db, "V1_HISTORY", protocol="debate/v1")
        db.commit()
        before = _snapshot(db, include_peer=False)
        fts_before = [tuple(row) for row in db.execute(
            "SELECT msg_id,body FROM debate_messages_fts "
            "WHERE debate_messages_fts MATCH 'historyneedle' ORDER BY msg_id"
        )]
        assert len(fts_before) >= 4
        assert any(row[0] == "parent" for row in db.execute(
            "SELECT provenance_class FROM debate_messages"
        ))
        assert db.execute("SELECT COUNT(*) FROM debate_message_recipients").fetchone()[0] > 0
        assert db.execute("SELECT COUNT(*) FROM debate_delivery_queue").fetchone()[0] > 0
        init_db(str(path))
        assert _snapshot(db, include_peer=False) == before
        db.execute("BEGIN IMMEDIATE")
        _create_peer(db)
        db.commit()
        with_peer = _snapshot(db)
        init_db(str(path))
        assert _snapshot(db) == with_peer
        assert [tuple(row) for row in db.execute(
            "SELECT msg_id,body FROM debate_messages_fts "
            "WHERE debate_messages_fts MATCH 'historyneedle' ORDER BY msg_id"
        )] == fts_before
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        db.close()


@pytest.mark.parametrize("name", ["init", "post", "propose", "vote", "read"])
def test_peer_wrapper_public_signature_hides_connection(name):
    signature = inspect.signature(getattr(intel_server, f"debate_peer_{name}"))
    assert "conn" not in signature.parameters
    assert "topic_id" in signature.parameters
