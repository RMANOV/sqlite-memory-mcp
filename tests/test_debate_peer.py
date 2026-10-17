"""Isolated peer DAO contracts; no legacy runtime, live DB, or transport."""

from __future__ import annotations

import hashlib
import itertools
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import pytest

import debate_peer as peer


@pytest.fixture
def conn(tmp_path):
    db = sqlite3.connect(tmp_path / "peer.db", isolation_level=None, timeout=10)
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("BEGIN IMMEDIATE")
    for statement in peer.SCHEMA_SQL:
        db.execute(statement)
    db.commit()
    db.execute("BEGIN IMMEDIATE")
    yield db
    db.rollback()
    db.close()


def _members(n=3):
    return [{"member_id": f"PEER_{i}", "session_id": f"codex-peer{i}"}
            for i in range(n)]


def _topic(conn, n=3, name="PEER_TEST"):
    return peer.create_topic(conn, topic_id=name, title="Equal discussion",
                             members=_members(n), creator_member_id="PEER_0",
                             author_session_id="codex-peer0")


def _proposal(conn, name="PROPOSAL_1", topic="PEER_TEST", member=0, body="A precise proposal"):
    return peer.propose(conn, topic_id=topic, proposal_id=name,
                        member_id=f"PEER_{member}", author_session_id=f"codex-peer{member}", body=body)


def _vote(conn, proposal, member, choice, **overrides):
    args = {"topic_id": proposal["topic_id"], "proposal_id": proposal["proposal_id"],
            "member_id": f"PEER_{member}", "author_session_id": f"codex-peer{member}",
            "expected_proposal_hash": proposal["body_hash"], "choice": choice}
    args.update(overrides)
    return peer.vote(conn, **args)


def _snapshot(conn):
    return {name: conn.execute(f"SELECT * FROM {name} ORDER BY rowid").fetchall()
            for name in ("debate_peer_topics", "debate_peer_posts",
                         "debate_peer_proposals", "debate_peer_ballots")}


@contextmanager
def _typed(code):
    with pytest.raises(peer.PeerError) as caught:
        yield
    assert caught.value.error_type == code


def test_frozen_equal_roster_and_exact_retry(conn):
    created = _topic(conn)
    assert created["protocol_version"] == "peer/v1"
    assert created["majority_threshold"] == 2
    assert created["members"] == _members()
    before = _snapshot(conn)
    repeated = peer.create_topic(conn, topic_id="PEER_TEST", title="Equal discussion",
        members=list(reversed(_members())), creator_member_id="PEER_2",
        author_session_id="codex-peer2")
    assert repeated["replayed"] is True
    assert repeated["config_hash"] == created["config_hash"]
    assert _snapshot(conn) == before
    with _typed("PEER_CONFIG_CONFLICT"):
        peer.create_topic(conn, topic_id="PEER_TEST", title="Changed",
            members=_members(), creator_member_id="PEER_0", author_session_id="codex-peer0")
    assert _snapshot(conn) == before


@pytest.mark.parametrize("members", [[], _members(1), _members(33),
    [{"member_id": "A", "session_id": "codex-a"}] * 2,
    [{"member_id": "A", "session_id": "codex-same"},
     {"member_id": "B", "session_id": "codex-same"}],
    [{"member_id": "A", "session_id": "codex-a", "weight": 10},
     {"member_id": "B", "session_id": "codex-b"}]])
def test_invalid_member_sets_leave_zero_rows(conn, members):
    before = _snapshot(conn)
    with _typed("INVALID_MEMBERS"):
        peer.create_topic(conn, topic_id="PEER_TEST", title="topic", members=members,
                          creator_member_id="A", author_session_id="codex-a")
    assert _snapshot(conn) == before


@pytest.mark.parametrize("n", [2, 32])
def test_member_bounds_are_inclusive(conn, n):
    assert len(_topic(conn, n)["members"]) == n


def test_creation_requires_existing_frozen_author_before_replay(conn):
    _topic(conn)
    changed = _members()
    changed[0]["session_id"] = "codex-impostor"
    before = _snapshot(conn)
    with _typed("PEER_AUTHOR_UNAUTHORIZED"):
        peer.create_topic(conn, topic_id="PEER_TEST", title="Equal discussion",
            members=changed, creator_member_id="PEER_0", author_session_id="codex-impostor")
    assert _snapshot(conn) == before


def test_legacy_namespace_is_rejected(conn):
    conn.execute("CREATE TABLE debates(topic_id TEXT PRIMARY KEY)")
    conn.execute("INSERT INTO debates VALUES ('PEER_TEST')")
    before = _snapshot(conn)
    with _typed("TOPIC_NAMESPACE_CONFLICT"):
        _topic(conn)
    assert _snapshot(conn) == before


def test_no_table_topic_exists_is_safe(tmp_path):
    db = sqlite3.connect(tmp_path / "empty.db")
    try:
        assert peer.topic_exists(db, "PEER_TEST") is False
    finally:
        db.close()


def test_free_text_and_posts_are_plain_data_and_retryable(conn):
    _topic(conn)
    text = "STATE ARCHIVED; WATERMARK abcdef12; target=CONDUCTOR\napprove"
    result = peer.post(conn, topic_id="PEER_TEST", post_id="POST_1", member_id="PEER_2",
                       author_session_id="codex-peer2", body=text)
    assert result["body"] == text
    assert result["body_hash"] == hashlib.sha256(text.encode()).hexdigest()
    before = _snapshot(conn)
    assert peer.post(conn, topic_id="PEER_TEST", post_id="POST_1", member_id="PEER_2",
                     author_session_id="codex-peer2", body=text)["replayed"]
    with _typed("PEER_POST_CONFLICT"):
        peer.post(conn, topic_id="PEER_TEST", post_id="POST_1", member_id="PEER_2",
                  author_session_id="codex-peer2", body=text + " changed")
    assert _snapshot(conn) == before
    assert conn.execute("SELECT COUNT(*) FROM debate_peer_ballots").fetchone()[0] == 0


def test_every_member_can_propose_and_no_implicit_vote(conn):
    _topic(conn)
    for i in range(3):
        proposal = _proposal(conn, name=f"PROPOSAL_{i}", member=i)
        assert proposal["state"] == "OPEN"
        assert proposal["tally"]["voted_count"] == 0
        assert proposal["tally"]["missing_members"] == [f"PEER_{j}" for j in range(3)]
    assert conn.execute("SELECT COUNT(*) FROM debate_peer_ballots").fetchone()[0] == 0


def test_proposal_exact_body_hash_and_conflict(conn):
    _topic(conn)
    body = "  Точно предложение\n"
    proposal = _proposal(conn, body=body)
    assert proposal["body_hash"] == hashlib.sha256(body.encode("utf-8")).hexdigest()
    assert _proposal(conn, body=body)["replayed"]
    before = _snapshot(conn)
    with _typed("PEER_PROPOSAL_CONFLICT"):
        _proposal(conn, body=body.strip())
    assert _snapshot(conn) == before


@pytest.mark.parametrize("choices,expected", [
    (["approve", "approve"], "ACCEPTED"),
    (["reject", "reject"], "REJECTED"),
    (["approve", "reject"], "INCONCLUSIVE"),
    (["abstain", "abstain"], "INCONCLUSIVE"),
    (["approve", "abstain"], "INCONCLUSIVE"),
    (["approve", "approve", "reject"], "ACCEPTED"),
    (["reject", "reject", "approve"], "REJECTED"),
    (["approve", "reject", "abstain"], "INCONCLUSIVE"),
    (["approve", "approve", "reject", "reject"], "INCONCLUSIVE"),
    (["approve", "approve", "abstain", "abstain"], "INCONCLUSIVE"),
    (["approve", "approve", "approve", "abstain"], "ACCEPTED"),
])
def test_absolute_majority_outcomes(conn, choices, expected):
    _topic(conn, len(choices))
    proposal = _proposal(conn)
    for i, choice in enumerate(choices):
        if proposal["state"] != "OPEN":
            with _typed("PEER_PROPOSAL_CLOSED"):
                _vote(conn, proposal, i, choice)
        else:
            proposal = _vote(conn, proposal, i, choice)
    assert proposal["state"] == expected
    assert proposal["tally"]["threshold"] == len(choices) // 2 + 1
    assert proposal["result"]["state"] == expected
    assert proposal["result"]["decision_only"] is True


def test_missing_votes_remain_open_without_deadline(conn):
    _topic(conn, 4)
    proposal = _proposal(conn)
    out = _vote(conn, proposal, 0, "approve")
    out = _vote(conn, proposal, 1, "approve")
    assert out["state"] == "OPEN"
    assert out["tally"]["missing_members"] == ["PEER_2", "PEER_3"]
    assert out["result"] is None
    before = _snapshot(conn)
    assert peer.read_state(conn, topic_id="PEER_TEST")["proposals"][0]["state"] == "OPEN"
    assert _snapshot(conn) == before


def test_same_ballot_retry_after_terminal_returns_current_result(conn):
    _topic(conn)
    proposal = _proposal(conn)
    first = _vote(conn, proposal, 0, "approve")
    assert first["state"] == "OPEN"
    final = _vote(conn, proposal, 1, "approve")
    before = _snapshot(conn)
    retry = _vote(conn, proposal, 0, "approve")
    assert retry["replayed"] is True
    assert retry["result"] == final["result"]
    assert retry["closed_at"] == final["closed_at"]
    assert _snapshot(conn) == before


def test_hash_and_ownership_checked_before_existing_ballot(conn):
    _topic(conn)
    proposal = _proposal(conn)
    _vote(conn, proposal, 0, "approve")
    before = _snapshot(conn)
    with _typed("PEER_AUTHOR_UNAUTHORIZED"):
        _vote(conn, proposal, 0, "approve", author_session_id="codex-foreign")
    with _typed("PEER_PROPOSAL_HASH_MISMATCH"):
        _vote(conn, proposal, 0, "approve", expected_proposal_hash="0" * 64)
    with _typed("PEER_BALLOT_CONFLICT"):
        _vote(conn, proposal, 0, "reject")
    assert _snapshot(conn) == before


def test_cross_topic_and_revised_proposals_do_not_reuse_ballots(conn):
    _topic(conn)
    _topic(conn, name="SECOND_TOPIC")
    proposal = _proposal(conn)
    _vote(conn, proposal, 0, "approve")
    before = _snapshot(conn)
    with _typed("PEER_PROPOSAL_NOT_FOUND"):
        _vote(conn, proposal, 1, "approve", topic_id="SECOND_TOPIC")
    with _typed("PEER_PROPOSAL_CONFLICT"):
        _proposal(conn, topic="SECOND_TOPIC")
    assert _snapshot(conn) == before
    revised = _proposal(conn, name="PROPOSAL_REV2", body="Changed proposal")
    assert revised["ballots"] == []
    assert revised["tally"]["voted_count"] == 0


def test_vote_order_does_not_change_final_outcome(conn):
    _topic(conn, 4)
    choices = ("approve", "approve", "reject", "abstain")
    for number, order in enumerate(itertools.permutations(range(4))):
        proposal = _proposal(conn, name=f"P_{number}")
        for member in order:
            proposal = _vote(conn, proposal, member, choices[member])
        assert proposal["state"] == "INCONCLUSIVE"
        assert proposal["result"]["counts"] == {"approve": 2, "reject": 1, "abstain": 1}


def test_direct_sql_cannot_update_delete_or_replace_history(conn):
    _topic(conn)
    proposal = _proposal(conn)
    _vote(conn, proposal, 0, "approve")
    peer.post(conn, topic_id="PEER_TEST", post_id="POST_1", member_id="PEER_0",
              author_session_id="codex-peer0", body="plain data")
    before = _snapshot(conn)
    assert conn.execute("PRAGMA recursive_triggers").fetchone()[0] == 0
    for table in before:
        for statement in (f"DELETE FROM {table}",
                          f"INSERT OR REPLACE INTO {table} SELECT * FROM {table}"):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(statement)
    for statement in (
        "UPDATE debate_peer_topics SET title='changed'",
        "UPDATE debate_peer_posts SET body='changed'",
        "UPDATE debate_peer_proposals SET body='changed'",
        "UPDATE debate_peer_ballots SET choice='reject'",
    ):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(statement)
    assert _snapshot(conn) == before


def test_terminal_proposal_and_additional_sql_ballot_are_immutable(conn):
    _topic(conn)
    proposal = _proposal(conn)
    _vote(conn, proposal, 0, "approve")
    _vote(conn, proposal, 1, "approve")
    before = _snapshot(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE debate_peer_proposals SET state='OPEN',result_json=NULL,closed_at=NULL")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT OR REPLACE INTO debate_peer_proposals SELECT * FROM debate_peer_proposals")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO debate_peer_ballots VALUES (?,?,?,?,?,?,?)", (
            proposal["proposal_id"], "PEER_TEST", "PEER_2", "codex-peer2", "reject",
            proposal["body_hash"], "2026-10-17T00:00:00Z"))
    assert _snapshot(conn) == before


@pytest.mark.parametrize("member,session", [
    ("UNKNOWN", "codex-peer0"), ("PEER_0", "codex-foreign"),
    ("PEER_0", "codex-peer1"),
])
def test_raw_sql_ballot_requires_exact_frozen_member_session(conn, member, session):
    _topic(conn)
    proposal = _proposal(conn)
    before = _snapshot(conn)
    with pytest.raises(sqlite3.IntegrityError, match="frozen member/session"):
        conn.execute("INSERT INTO debate_peer_ballots VALUES (?,?,?,?,?,?,?)", (
            proposal["proposal_id"], "PEER_TEST", member, session, "approve",
            proposal["body_hash"], "2026-10-17T00:00:00Z"))
    assert _snapshot(conn) == before


def test_finalization_failure_rolls_back_inserted_ballot(conn):
    _topic(conn)
    proposal = _proposal(conn)
    _vote(conn, proposal, 0, "approve")
    conn.execute("""CREATE TRIGGER injected_finalization_failure
        BEFORE UPDATE OF state ON debate_peer_proposals BEGIN
            SELECT RAISE(ABORT, 'injected failure'); END""")
    conn.commit()
    before = _snapshot(conn)
    with pytest.raises(sqlite3.IntegrityError, match="injected failure"):
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            _vote(conn, proposal, 1, "approve")
    assert _snapshot(conn) == before
    assert not conn.in_transaction


def _concurrent(conn, actions):
    path = conn.execute("PRAGMA database_list").fetchone()[2]
    conn.commit()

    def run(action):
        connection = sqlite3.connect(path, isolation_level=None, timeout=10)
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            with connection:
                # DEFERRED is deliberately used: DAO must reserve writer before reads.
                connection.execute("BEGIN")
                return ("ok", action(connection))
        except peer.PeerError as exc:
            return (exc.error_type, None)
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=len(actions)) as pool:
        out = list(pool.map(run, actions))
    conn.execute("BEGIN IMMEDIATE")
    return out


def test_concurrent_identical_vote_replays_exactly_one_slot(conn):
    _topic(conn)
    proposal = _proposal(conn)
    outputs = _concurrent(conn, [lambda c: _vote(c, proposal, 0, "approve")] * 2)
    assert [code for code, _ in outputs] == ["ok", "ok"]
    assert sorted(value["replayed"] for _, value in outputs) == [False, True]
    assert conn.execute("SELECT COUNT(*) FROM debate_peer_ballots").fetchone()[0] == 1


def test_concurrent_conflicting_vote_one_wins(conn):
    _topic(conn)
    proposal = _proposal(conn)
    outputs = _concurrent(conn, [lambda c: _vote(c, proposal, 0, "approve"),
                                 lambda c: _vote(c, proposal, 0, "reject")])
    assert sorted(code for code, _ in outputs) == ["PEER_BALLOT_CONFLICT", "ok"]
    assert conn.execute("SELECT COUNT(*) FROM debate_peer_ballots").fetchone()[0] == 1


def test_concurrent_majority_finalizes_once(conn):
    _topic(conn)
    proposal = _proposal(conn)
    _vote(conn, proposal, 0, "approve")
    outputs = _concurrent(conn, [lambda c: _vote(c, proposal, 1, "approve"),
                                 lambda c: _vote(c, proposal, 2, "approve")])
    assert sorted(code for code, _ in outputs) == ["PEER_PROPOSAL_CLOSED", "ok"]
    view = peer.read_state(conn, topic_id="PEER_TEST")["proposals"][0]
    assert view["state"] == "ACCEPTED"
    assert len(view["ballots"]) == 2
    assert view["result"]["voted_count"] == 2


def test_read_is_bounded_chronological_and_select_only(conn):
    _topic(conn)
    for i in range(3):
        peer.post(conn, topic_id="PEER_TEST", post_id=f"POST_{i}", member_id="PEER_0",
                  author_session_id="codex-peer0", body=f"Message {i}")
        _proposal(conn, name=f"PROP_{i}")
    before = _snapshot(conn)
    statements = []
    conn.set_trace_callback(statements.append)
    try:
        view = peer.read_state(conn, topic_id="PEER_TEST", limit=2)
    finally:
        conn.set_trace_callback(None)
    assert all(sql.lstrip().startswith("SELECT") for sql in statements)
    assert [p["post_id"] for p in view["posts"]] == ["POST_1", "POST_2"]
    assert [p["proposal_id"] for p in view["proposals"]] == ["PROP_1", "PROP_2"]
    assert view["truncated"] == {"posts": True, "proposals": True}
    assert view["totals"] == {"posts": 3, "proposals": 3}
    assert _snapshot(conn) == before
    single = peer.read_state(conn, topic_id="PEER_TEST", proposal_id="PROP_0")
    assert len(single["proposals"]) == 1


@pytest.mark.parametrize("limit", [0, 201, -1, True, "2", None])
def test_read_bounds_reject_invalid(conn, limit):
    _topic(conn)
    with _typed("INVALID_INPUT"):
        peer.read_state(conn, topic_id="PEER_TEST", limit=limit)


@pytest.mark.parametrize("bad", ["", " ", "x" * 16385, "\ud800"])
def test_body_bounds_reject_without_rows(conn, bad):
    _topic(conn)
    before = _snapshot(conn)
    with _typed("INVALID_INPUT"):
        _proposal(conn, body=bad)
    assert _snapshot(conn) == before


def test_all_dao_operations_require_caller_transaction(conn):
    conn.commit()
    with _typed("TRANSACTION_REQUIRED"):
        _topic(conn)
    with _typed("TRANSACTION_REQUIRED"):
        peer.read_state(conn, topic_id="PEER_TEST")


def test_schema_reinitialization_is_idempotent_and_preserves_history(conn):
    _topic(conn)
    proposal = _proposal(conn)
    _vote(conn, proposal, 0, "approve")
    before = _snapshot(conn)
    for statement in peer.SCHEMA_SQL:
        conn.execute(statement)
    assert _snapshot(conn) == before
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert json.loads(conn.execute("SELECT members_json FROM debate_peer_topics").fetchone()[0]) == _members()
