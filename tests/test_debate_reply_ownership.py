"""Reply ownership: a derived worker's terminal must not consume the bound
parent's independent right to answer (operator bug, 2026-08-22; dispatch
10f4c51b4997; primary contract a784b6952429; Phase A RED — revision 3.1).

Contract items (a784b6952429):
 C1 public authorization for EVERY kind, before duplicate/protocol
    classification; a missing id never creates a public (NULL,'unattributed')
    row; 'unattributed' only via the explicit trusted internal path;
 C2 atomic claim lifecycle: worker terminal completes its exact claim with its
    own msg_id in the same transaction; parent-final closes claims, blocks
    reclaim, never becomes a worker ack; signal_advance stays cursor machinery;
 C3 durability after reap;
 C4 schema integrity: pairing CHECK + immutability trigger, on the additive
    (live) path AND the pre-v1 rebuild path;
 C5 complete matrix (kinds, orderings, DECISION, rotated parent, typed errors);
 C6 concurrency with two connections and production BEGIN IMMEDIATE, zero
    collateral mutation for the loser;
 C7 migration / query contract (old writer shape → legacy; indexed lookup).

Pinned decisions (revision 3.1, after the second refutation):
 D1 missing author_session_id at the DAO → `author_session_required` for
    every kind, unless the caller passes internal_unattributed=True (trusted
    DAO-internal system rows only);
 D2 a worker whose claim is no longer active (completed by its own terminal,
    retired by parent-final) is NOT authorized → `ROLE_UNAVAILABLE` (auth-first);
 D3 a completed/retired worker may still advance its cursor for its own
    trigger idempotently (cursor machinery is not lifecycle);
 D4 a worker-authored post WITHOUT reply_to is classified by its single active
    claim and is non-terminal for slot purposes;
 D5 a parent-class terminal on a trigger closes it to new worker claims
    (`trigger_closed_by_parent`); a worker-class terminal consumes the worker
    slot for new claims (`worker_slot_consumed`).

Temporary DBs only (tmp_path); production DAO under test.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import debate  # noqa: E402
from debate import (  # noqa: E402
    DebateError,
    bind_role_session,
    claim_worker_session,
    debate_post_with_recipients,
    debate_signal_advance,
    init_debate,
    post_message,
    reap_worker_claims,
    recover_stale_worker_claims,
    rotate_role_binding,
    transition_state,
)
from schema import init_db  # noqa: E402

PARENT = "codex-exec1"
CONDUCTOR = "codex-cond1"
OTHER_ROLE_SESSION = "cc-adv1"
TERMINAL_KINDS = ("A", "STATUS")
PROVENANCE_COLUMNS = {"author_session_id", "provenance_class"}
ROLES = (
    ("CONDUCTOR", CONDUCTOR),
    ("EXECUTOR", PARENT),
    ("ADVOCATE", OTHER_ROLE_SESSION),
)


def _connect(db_path, timeout=5.0):
    c = sqlite3.connect(db_path, isolation_level=None, timeout=timeout)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return c


def _seed(conn, t):
    init_debate(
        conn,
        topic_id=t,
        title="reply ownership",
        roles=[{"role": r, "session_id": s} for r, s in ROLES]
        + [{"role": "EXECUTOR2", "session_id": "codex-exec2"}],  # unbound roster role
        created_by_role="CONDUCTOR",
    )
    transition_state(conn, topic_id=t, role="CONDUCTOR", new_state="ACTIVE")
    for role, session_id in ROLES:
        bind_role_session(
            conn, topic_id=t, role=role, session_id=session_id, reason="primary"
        )


@pytest.fixture
def topic(tmp_path):
    db_path = str(tmp_path / "reply_ownership.db")
    init_db(db_path)
    c = _connect(db_path)
    _seed(c, "RO1")
    yield c, "RO1"
    c.close()


def _trigger(conn, t, *, kind="Q", body="one-shot work", addressed="EXECUTOR", **kw):
    return debate_post_with_recipients(
        conn,
        topic_id=t,
        role="CONDUCTOR",
        priority="H",
        kind=kind,
        body=body,
        addressed_to=[addressed],
        author_session_id=CONDUCTOR,
        **kw,
    )["msg_id"]


def _claim(conn, t, trigger):
    return claim_worker_session(
        conn,
        topic_id=t,
        role="EXECUTOR",
        parent_session_id=PARENT,
        trigger_msg_id=trigger,
    )["worker_session_id"]


def _dispatch(conn, t, **kw):
    trigger = _trigger(conn, t, **kw)
    return trigger, _claim(conn, t, trigger)


def _terminal(
    conn, t, *, reply_to, author, kind="A", body="done", role="EXECUTOR", **kw
):
    return post_message(
        conn,
        topic_id=t,
        role=role,
        priority="H",
        kind=kind,
        body=body,
        reply_to=reply_to,
        author_session_id=author,
        **kw,
    )


def _advance(conn, t, worker, trigger):
    return debate_signal_advance(
        conn,
        session_id=worker,
        role="EXECUTOR",
        topic_id=t,
        last_processed_msg_id=trigger,
    )


def _rows(conn, t):
    return conn.execute(
        "SELECT COUNT(*) FROM debate_messages WHERE topic_id = ?", (t,)
    ).fetchone()[0]


def _provenance(conn, msg_id):
    row = conn.execute(
        "SELECT author_session_id, provenance_class FROM debate_messages WHERE msg_id = ?",
        (msg_id,),
    ).fetchone()
    return (row["author_session_id"], row["provenance_class"])


def _columns(conn):
    return {r["name"] for r in conn.execute("PRAGMA table_info(debate_messages)")}


def _claim_row(conn, t, worker):
    return conn.execute(
        "SELECT state, ack_msg_id, details_json FROM debate_worker_claims "
        "WHERE topic_id = ? AND worker_session_id = ?",
        (t, worker),
    ).fetchone()


def _reap(conn, t, worker):
    conn.execute(
        "UPDATE debate_worker_claims SET heartbeat_at = '2000-01-01T00:00:00Z' "
        "WHERE topic_id = ? AND worker_session_id = ?",
        (t, worker),
    )
    reap_worker_claims(conn, topic_id=t, older_than_ts="2001-01-01T00:00:00Z")
    return _claim_row(conn, t, worker)


def _raises(exc_info, error_type):
    assert exc_info.value.error_type == error_type, (
        f"expected {error_type}, got {exc_info.value.error_type}: {exc_info.value}"
    )


# ═════════════════════════════════════════════════════════════════════════════
# C1 — authorization for every kind, before the guard
# ═════════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("kind", TERMINAL_KINDS)
def test_worker_terminal_does_not_consume_parent_reply_right(topic, kind):
    conn, t = topic
    trigger, worker = _dispatch(conn, t)

    w = _terminal(conn, t, reply_to=trigger, author=worker, kind=kind)
    p = _terminal(conn, t, reply_to=trigger, author=PARENT, kind=kind, body="final")

    assert w["msg_id"] != p["msg_id"]
    assert _provenance(conn, w["msg_id"]) == (worker, "worker")
    assert _provenance(conn, p["msg_id"]) == (PARENT, "parent")


@pytest.mark.parametrize("kind", ["Q", "A", "STATUS", "DECISION", "PING"])
def test_missing_author_with_single_binding_resolves_to_parent_at_the_dao(topic, kind):
    # D1' (DAO compat, bare-ledger callers): on a NON-worker-scoped trigger a
    # missing author resolves to the role's single active binding and is
    # persisted as that session, class 'parent'.  The mandatory-author rule
    # for every kind is enforced by the PUBLIC writers (MCP tools / CLI) —
    # see tests/test_intel_server_author_gate.py.
    conn, t = topic
    trigger = _trigger(conn, t)
    reply_to = trigger if kind in ("A", "STATUS") else None

    msg = post_message(
        conn,
        topic_id=t,
        role="EXECUTOR",
        priority="H",
        kind=kind,
        body="bare ledger caller",
        reply_to=reply_to,
        standing=False if kind == "DECISION" else None,
    )

    assert _provenance(conn, msg["msg_id"]) == (PARENT, "parent")


def test_missing_author_on_claimed_trigger_is_rejected_even_after_reap(topic):
    conn, t = topic
    trigger, worker = _dispatch(conn, t)
    _terminal(conn, t, reply_to=trigger, author=worker)
    _reap(conn, t, worker)

    with pytest.raises(DebateError) as exc_info:
        _terminal(conn, t, reply_to=trigger, author=None, body="anonymous")
    _raises(exc_info, "author_session_required")


def test_internal_unattributed_path_is_explicit_and_never_takes_a_slot(topic):
    conn, t = topic
    trigger = _trigger(conn, t, body="plain question")

    note = _terminal(
        conn,
        t,
        reply_to=trigger,
        author=None,
        kind="STATUS",
        body="system note",
        internal_unattributed=True,
    )
    assert _provenance(conn, note["msg_id"]) == (None, "unattributed")

    worker = _claim(conn, t, trigger)
    w = _terminal(conn, t, reply_to=trigger, author=worker)
    assert _provenance(conn, w["msg_id"]) == (worker, "worker")
    p = _terminal(conn, t, reply_to=trigger, author=PARENT, body="final")
    assert _provenance(conn, p["msg_id"]) == (PARENT, "parent")


def test_unbound_roster_role_is_unattributed_and_never_an_owner(topic):
    # D1' bare-ledger compat: a roster role with NO active binding (pre-v3.10
    # topics) may still reply without an author — the row is (NULL,
    # 'unattributed') and never counts as an ownership slot.  An unknown
    # author id for that role is still rejected before anything else.
    conn, t = topic
    trigger = _trigger(conn, t, body="for executor2", addressed="EXECUTOR2")

    with pytest.raises(DebateError) as exc_info:
        _terminal(conn, t, reply_to=trigger, author="codex-exec2", role="EXECUTOR2")
    _raises(exc_info, "ROLE_UNAVAILABLE")
    msg = _terminal(conn, t, reply_to=trigger, author=None, role="EXECUTOR2")
    assert _provenance(conn, msg["msg_id"]) == (None, "unattributed")
    again = _terminal(
        conn, t, reply_to=trigger, author=None, role="EXECUTOR2", body="more"
    )
    assert again["msg_id"] != msg["msg_id"], "unattributed rows take no slot"


@pytest.mark.parametrize(
    "bad", ["cc-outsider9999", OTHER_ROLE_SESSION, "codex-exec1-W999"]
)
def test_unauthorized_author_rejects_before_the_guard(topic, bad):
    conn, t = topic
    trigger, worker = _dispatch(conn, t)
    before = _rows(conn, t)

    with pytest.raises(DebateError) as exc_info:
        _terminal(conn, t, reply_to=trigger, author=bad)
    _raises(exc_info, "ROLE_UNAVAILABLE")
    _terminal(conn, t, reply_to=trigger, author=worker)  # a terminal now exists
    with pytest.raises(DebateError) as exc_info:
        _terminal(conn, t, reply_to=trigger, author=bad)
    _raises(exc_info, "ROLE_UNAVAILABLE")
    assert _rows(conn, t) == before + 1


def test_retired_worker_claim_rejects(topic):
    conn, t = topic
    trigger, worker = _dispatch(conn, t)
    conn.execute(
        "UPDATE debate_worker_claims SET state = 'retired' "
        "WHERE topic_id = ? AND worker_session_id = ?",
        (t, worker),
    )

    with pytest.raises(DebateError) as exc_info:
        _terminal(conn, t, reply_to=trigger, author=worker)
    _raises(exc_info, "ROLE_UNAVAILABLE")


def test_worker_of_another_trigger_is_unresolvable(topic):
    conn, t = topic
    _t1, w1 = _dispatch(conn, t, body="first")
    t2, _w2 = _dispatch(conn, t, body="second")
    before = _rows(conn, t)

    with pytest.raises(DebateError) as exc_info:
        _terminal(conn, t, reply_to=t2, author=w1)
    _raises(exc_info, "provenance_unresolvable")
    assert _rows(conn, t) == before


def test_worker_post_without_reply_to_is_non_terminal_worker_class(topic):
    # D4: the legacy wake path may post progress without reply_to.
    conn, t = topic
    trigger, worker = _dispatch(conn, t)

    note = _terminal(
        conn, t, reply_to=None, author=worker, kind="STATUS", body="progress"
    )

    assert _provenance(conn, note["msg_id"]) == (worker, "worker")
    assert _claim_row(conn, t, worker)["state"] == "active"
    w = _terminal(conn, t, reply_to=trigger, author=worker)
    assert _claim_row(conn, t, worker)["ack_msg_id"] == w["msg_id"]


def test_worker_supplying_parent_id_is_classified_parent_documented_residual(topic):
    conn, t = topic
    trigger, _worker = _dispatch(conn, t)

    msg = _terminal(conn, t, reply_to=trigger, author=PARENT, body="impersonated")

    # Bearer-trust residual inside the local-DB boundary (no transport-bound
    # principal); MCP/session-layer binding is a separate follow-up lane.
    assert _provenance(conn, msg["msg_id"]) == (PARENT, "parent")


# ═════════════════════════════════════════════════════════════════════════════
# C2 — atomic claim lifecycle / parent-final; D2 error matrix
# ═════════════════════════════════════════════════════════════════════════════


def test_worker_terminal_completes_its_own_claim_atomically(topic):
    conn, t = topic
    trigger, worker = _dispatch(conn, t)

    w = _terminal(conn, t, reply_to=trigger, author=worker)

    row = _claim_row(conn, t, worker)
    assert row["state"] == "completed"
    assert row["ack_msg_id"] == w["msg_id"], "ack = the worker's OWN terminal"
    assert w["worker_claim"]["state"] == "completed"


def test_second_worker_terminal_rejects_as_unauthorized(topic):
    conn, t = topic
    trigger, worker = _dispatch(conn, t)
    _terminal(conn, t, reply_to=trigger, author=worker)

    with pytest.raises(DebateError) as exc_info:
        _terminal(conn, t, reply_to=trigger, author=worker, kind="STATUS", body="again")
    _raises(exc_info, "ROLE_UNAVAILABLE")


def test_second_parent_terminal_rejects(topic):
    conn, t = topic
    trigger, worker = _dispatch(conn, t)
    _terminal(conn, t, reply_to=trigger, author=worker)
    _terminal(conn, t, reply_to=trigger, author=PARENT, body="final")

    with pytest.raises(DebateError) as exc_info:
        _terminal(conn, t, reply_to=trigger, author=PARENT, body="again")
    _raises(exc_info, "terminal_reply_duplicate")


def test_parent_final_closes_claim_and_late_worker_is_unauthorized(topic):
    conn, t = topic
    trigger, worker = _dispatch(conn, t)

    p = _terminal(conn, t, reply_to=trigger, author=PARENT, body="parent final")

    row = _claim_row(conn, t, worker)
    assert row["state"] == "retired"
    assert row["ack_msg_id"] is None and row["ack_msg_id"] != p["msg_id"]
    assert "parent_final" in (row["details_json"] or "")
    with pytest.raises(DebateError) as exc_info:
        _terminal(conn, t, reply_to=trigger, author=worker, body="late")
    _raises(exc_info, "ROLE_UNAVAILABLE")


def test_new_worker_cannot_claim_after_parent_final(topic):
    conn, t = topic
    trigger = _trigger(conn, t)
    _terminal(conn, t, reply_to=trigger, author=PARENT, body="parent final")

    with pytest.raises(DebateError) as exc_info:
        _claim(conn, t, trigger)
    _raises(exc_info, "trigger_closed_by_parent")


def test_reclaim_after_parent_final_is_blocked(topic):
    conn, t = topic
    trigger, _worker = _dispatch(conn, t)
    _terminal(conn, t, reply_to=trigger, author=PARENT, body="parent final")

    with pytest.raises(DebateError) as exc_info:
        _claim(conn, t, trigger)
    _raises(exc_info, "trigger_closed_by_parent")


def test_new_claim_after_worker_terminal_is_blocked(topic):
    conn, t = topic
    trigger, worker = _dispatch(conn, t)
    _terminal(conn, t, reply_to=trigger, author=worker)

    with pytest.raises(DebateError) as exc_info:
        _claim(conn, t, trigger)
    _raises(exc_info, "worker_slot_consumed")


def test_completed_worker_may_still_advance_its_cursor_idempotently(topic):
    # D3: cursor machinery is not lifecycle; the terminal INSERT already
    # completed the claim and a later advance must neither fail nor change it.
    conn, t = topic
    trigger, worker = _dispatch(conn, t)
    w = _terminal(conn, t, reply_to=trigger, author=worker)
    before = tuple(_claim_row(conn, t, worker))
    assert before[0] == "completed" and before[1] == w["msg_id"]

    advanced = _advance(conn, t, worker, trigger)

    assert tuple(_claim_row(conn, t, worker)) == before
    assert advanced["worker_claim"]["ack_msg_id"] == w["msg_id"]


def test_stale_recovery_never_acks_a_non_worker_terminal(topic):
    conn, t = topic
    trigger, worker = _dispatch(conn, t)
    conn.execute(
        "INSERT INTO debate_messages (msg_id, topic_id, role, ts, priority, kind, "
        "reply_to, body, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            "legacyack001",
            t,
            "EXECUTOR",
            "2026-08-22T15:24:38.000000Z",
            "H",
            "A",
            trigger,
            "legacy terminal",
            "2026-08-22T15:24:38.000000Z",
        ),
    )
    conn.execute(
        "UPDATE debate_worker_claims SET heartbeat_at = '2000-01-01T00:00:00Z' "
        "WHERE topic_id = ? AND worker_session_id = ?",
        (t, worker),
    )

    recover_stale_worker_claims(
        conn, topic_id=t, older_than_ts="2001-01-01T00:00:00Z", minimum_age_seconds=0
    )

    row = _claim_row(conn, t, worker)
    assert row["state"] == "retired"
    assert row["ack_msg_id"] is None, (
        "a legacy (no-provenance) row is never a worker ack"
    )


# ═════════════════════════════════════════════════════════════════════════════
# C3 — durability after reap
# ═════════════════════════════════════════════════════════════════════════════


def test_worker_complete_reap_then_parent_accepted_once(topic):
    conn, t = topic
    trigger, worker = _dispatch(conn, t)
    w = _terminal(conn, t, reply_to=trigger, author=worker)
    assert _reap(conn, t, worker) is None, "completed claim is reaped"

    p = _terminal(conn, t, reply_to=trigger, author=PARENT, body="parent final")
    assert _provenance(conn, p["msg_id"]) == (PARENT, "parent")
    assert _provenance(conn, w["msg_id"]) == (worker, "worker")
    with pytest.raises(DebateError) as exc_info:
        _terminal(conn, t, reply_to=trigger, author=PARENT, body="again")
    _raises(exc_info, "terminal_reply_duplicate")


def test_parent_first_reap_then_second_parent_rejected(topic):
    conn, t = topic
    trigger, worker = _dispatch(conn, t)
    _terminal(conn, t, reply_to=trigger, author=PARENT, body="parent final")
    assert _reap(conn, t, worker) is None, "retired claim is reaped"

    with pytest.raises(DebateError) as exc_info:
        _terminal(conn, t, reply_to=trigger, author=PARENT, body="again")
    _raises(exc_info, "terminal_reply_duplicate")


def test_new_worker_cannot_claim_after_parent_final_even_after_reap(topic):
    conn, t = topic
    trigger, worker = _dispatch(conn, t)
    _terminal(conn, t, reply_to=trigger, author=PARENT, body="parent final")
    _reap(conn, t, worker)

    with pytest.raises(DebateError) as exc_info:
        _claim(conn, t, trigger)
    _raises(exc_info, "trigger_closed_by_parent")


def test_reap_log_is_indexed_for_ownership_lookup(topic):
    conn, _t = topic
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = 'idx_dwrl_owner'"
    ).fetchone()
    assert row is not None
    assert "debate_worker_reap_log" in row["sql"]
    assert "topic_id, role, trigger_msg_id" in row["sql"].replace("  ", " ")


# ═════════════════════════════════════════════════════════════════════════════
# C4 — schema integrity on the additive (live) path and the rebuild path
# ═════════════════════════════════════════════════════════════════════════════

BAD_PAIRS = [
    (None, "parent"),
    (None, "worker"),
    ("codex-exec1", "legacy"),
    ("codex-exec1", "unattributed"),
]


def _assert_integrity(conn, t, msg_id):
    for author, klass in BAD_PAIRS:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO debate_messages (msg_id, topic_id, role, ts, priority, kind, "
                "body, created_at, author_session_id, provenance_class) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    "pairing" + klass[:5],
                    t,
                    "EXECUTOR",
                    "2026-08-23T00:00:00Z",
                    "H",
                    "STATUS",
                    "x",
                    "2026-08-23T00:00:00Z",
                    author,
                    klass,
                ),
            )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO debate_messages (msg_id, topic_id, role, ts, priority, kind, "
            "body, created_at, provenance_class) VALUES (?,?,?,?,?,?,?,?,NULL)",
            (
                "nullprov0001",
                t,
                "EXECUTOR",
                "2026-08-23T00:00:00Z",
                "H",
                "STATUS",
                "x",
                "2026-08-23T00:00:00Z",
            ),
        )
    for column, value in (("provenance_class", "parent"), ("author_session_id", "x")):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                f"UPDATE debate_messages SET {column} = ? WHERE msg_id = ?",
                (value, msg_id),
            )


def test_fresh_db_pairing_check_and_immutability(topic):
    conn, t = topic
    trigger, worker = _dispatch(conn, t)
    w = _terminal(conn, t, reply_to=trigger, author=worker)

    _assert_integrity(conn, t, w["msg_id"])

    assert _provenance(conn, w["msg_id"]) == (worker, "worker")


PRE_PROVENANCE_V1_SHAPE = """
CREATE TABLE debates(
    topic_id TEXT PRIMARY KEY,title TEXT NOT NULL,state TEXT NOT NULL,
    created_at TEXT NOT NULL,created_by_role TEXT NOT NULL,resolve_by TEXT,
    archived_at TEXT,roles_json TEXT NOT NULL,metadata_json TEXT);
CREATE TABLE debate_messages(
    msg_id TEXT PRIMARY KEY,topic_id TEXT NOT NULL REFERENCES debates(topic_id),
    role TEXT NOT NULL,ts TEXT NOT NULL,priority TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN
      ('Q','A','STATUS','DECISION','PING','WATERMARK','STATE','COMPACTION',
       'CLAIM','CHALLENGE','EVIDENCE','REBUT','CONCEDE','VERIFY','DISSENT',
       'ESCALATE')),
    standing INTEGER,vehicle TEXT,reply_to TEXT REFERENCES debate_messages(msg_id),
    body TEXT NOT NULL,protocol_version TEXT,round_no INTEGER,
    body_mode TEXT,payload_json TEXT,created_at TEXT NOT NULL);
INSERT INTO debates VALUES(
  '{t}','legacy','ACTIVE','2026-01-01T00:00:00Z','OLD',NULL,NULL,'[]',NULL);
INSERT INTO debate_messages VALUES(
  '{m}','{t}','OLD','2026-01-01T00:00:00Z','H','STATUS',
  NULL,'analysis',NULL,'pre-migration row',NULL,NULL,NULL,NULL,'2026-01-01T00:00:00Z');
"""

PRE_V1_SHAPE = """
CREATE TABLE debates(
    topic_id TEXT PRIMARY KEY,title TEXT NOT NULL,state TEXT NOT NULL,
    created_at TEXT NOT NULL,created_by_role TEXT NOT NULL,resolve_by TEXT,
    archived_at TEXT,roles_json TEXT NOT NULL,metadata_json TEXT);
CREATE TABLE debate_messages(
    msg_id TEXT PRIMARY KEY,topic_id TEXT NOT NULL REFERENCES debates(topic_id),
    role TEXT NOT NULL,ts TEXT NOT NULL,priority TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN
      ('Q','A','STATUS','DECISION','PING','WATERMARK','STATE','COMPACTION')),
    standing INTEGER,vehicle TEXT,reply_to TEXT REFERENCES debate_messages(msg_id),
    body TEXT NOT NULL,created_at TEXT NOT NULL);
INSERT INTO debates VALUES(
  '{t}','legacy','ACTIVE','2026-01-01T00:00:00Z','OLD',NULL,NULL,'[]',NULL);
INSERT INTO debate_messages VALUES(
  '{m}','{t}','OLD','2026-01-01T00:00:00Z','H','Q',
  NULL,'analysis',NULL,'preserve me','2026-01-01T00:00:00Z');
"""


def _legacy_db(tmp_path, name, shape, t, m):
    path = tmp_path / name
    legacy = sqlite3.connect(path)
    legacy.executescript(shape.format(t=t, m=m))
    legacy.commit()
    legacy.close()
    return str(path)


def test_additive_migration_path_labels_legacy_and_enforces_integrity(tmp_path):
    # v1-shaped table WITHOUT the provenance columns = the live memory.db shape;
    # the rebuild must NOT trigger, the ALTER path is what runs.
    path = _legacy_db(
        tmp_path,
        "pre_provenance.db",
        PRE_PROVENANCE_V1_SHAPE,
        "LEGACY_PROV",
        "legacy0000bb",
    )

    init_db(path)
    conn = _connect(path)
    assert PROVENANCE_COLUMNS <= _columns(conn)
    first_info = [tuple(r) for r in conn.execute("PRAGMA table_info(debate_messages)")]
    assert _provenance(conn, "legacy0000bb") == (None, "legacy")
    _assert_integrity(conn, "LEGACY_PROV", "legacy0000bb")
    conn.close()

    init_db(path)  # idempotent re-run
    conn = _connect(path)
    assert [
        tuple(r) for r in conn.execute("PRAGMA table_info(debate_messages)")
    ] == first_info
    assert _provenance(conn, "legacy0000bb") == (None, "legacy")


def test_v1_rebuild_path_preserves_columns_and_integrity(tmp_path):
    path = _legacy_db(tmp_path, "pre_v1.db", PRE_V1_SHAPE, "LEGACY_V1", "legacy0000cc")

    init_db(path)
    init_db(path)

    conn = _connect(path)
    assert PROVENANCE_COLUMNS <= _columns(conn)
    assert _provenance(conn, "legacy0000cc") == (None, "legacy")
    _assert_integrity(conn, "LEGACY_V1", "legacy0000cc")


# ═════════════════════════════════════════════════════════════════════════════
# C5 — complete behaviour matrix
# ═════════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("worker_kind,parent_kind", [("A", "STATUS"), ("STATUS", "A")])
def test_cross_kind_terminal_parity(topic, worker_kind, parent_kind):
    conn, t = topic
    trigger, worker = _dispatch(conn, t)
    _terminal(conn, t, reply_to=trigger, author=worker, kind=worker_kind)
    _terminal(conn, t, reply_to=trigger, author=PARENT, kind=parent_kind, body="final")

    with pytest.raises(DebateError) as exc_info:
        _terminal(conn, t, reply_to=trigger, author=worker, kind=parent_kind)
    _raises(exc_info, "ROLE_UNAVAILABLE")
    with pytest.raises(DebateError) as exc_info:
        _terminal(conn, t, reply_to=trigger, author=PARENT, kind=worker_kind)
    _raises(exc_info, "terminal_reply_duplicate")


def test_plain_q_without_worker_keeps_multiple_parent_replies(topic):
    conn, t = topic
    trigger = _trigger(conn, t, body="plain question")

    first = _terminal(conn, t, reply_to=trigger, author=PARENT)
    second = _terminal(conn, t, reply_to=trigger, author=PARENT, body="more")
    assert first["msg_id"] != second["msg_id"]


def test_different_role_terminal_is_independent(topic):
    conn, t = topic
    trigger, worker = _dispatch(conn, t)
    _terminal(conn, t, reply_to=trigger, author=worker)

    advocate = _terminal(
        conn, t, reply_to=trigger, author=OTHER_ROLE_SESSION, role="ADVOCATE"
    )
    assert _provenance(conn, advocate["msg_id"]) == (OTHER_ROLE_SESSION, "parent")


def test_non_standing_decision_one_shot_unchanged(topic):
    conn, t = topic
    decision = _trigger(conn, t, kind="DECISION", body="one-shot", standing=False)

    _terminal(conn, t, reply_to=decision, author=PARENT)
    with pytest.raises(DebateError) as exc_info:
        _terminal(conn, t, reply_to=decision, author=PARENT, body="again")
    _raises(exc_info, "terminal_reply_duplicate")


def test_claimed_decision_worker_first_stays_role_level_one_shot(topic):
    conn, t = topic
    decision = _trigger(conn, t, kind="DECISION", body="one-shot", standing=False)
    worker = _claim(conn, t, decision)
    _terminal(conn, t, reply_to=decision, author=worker)

    with pytest.raises(DebateError) as exc_info:
        _terminal(conn, t, reply_to=decision, author=PARENT, body="parent")
    _raises(exc_info, "terminal_reply_duplicate")


def test_claimed_decision_parent_first_closes_claim_no_reactivation(topic):
    conn, t = topic
    decision = _trigger(conn, t, kind="DECISION", body="one-shot", standing=False)
    worker = _claim(conn, t, decision)
    _terminal(conn, t, reply_to=decision, author=PARENT, body="parent final")

    with pytest.raises(DebateError) as exc_info:
        _terminal(conn, t, reply_to=decision, author=worker, body="late worker")
    _raises(exc_info, "ROLE_UNAVAILABLE")
    assert _claim_row(conn, t, worker)["state"] == "retired"
    with pytest.raises(DebateError) as exc_info:
        _claim(conn, t, decision)
    _raises(exc_info, "trigger_closed_by_parent")


def test_retired_parent_binding_rejects_and_rotated_parent_accepts(topic):
    conn, t = topic
    trigger, worker = _dispatch(conn, t)
    _terminal(conn, t, reply_to=trigger, author=worker)
    rotate_role_binding(
        conn,
        topic_id=t,
        role="EXECUTOR",
        old_session_id=PARENT,
        new_session_id="codex-exec1b",
        cursor_mode="copy",
        reason="rotation under test",
    )

    with pytest.raises(DebateError) as exc_info:
        _terminal(conn, t, reply_to=trigger, author=PARENT, body="old parent")
    _raises(exc_info, "ROLE_UNAVAILABLE")
    p = _terminal(conn, t, reply_to=trigger, author="codex-exec1b", body="new parent")
    assert _provenance(conn, p["msg_id"]) == ("codex-exec1b", "parent")


def test_legacy_terminal_consumes_both_slots(topic):
    conn, t = topic
    trigger, worker = _dispatch(conn, t)
    conn.execute(
        "INSERT INTO debate_messages (msg_id, topic_id, role, ts, priority, kind, "
        "reply_to, body, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            "legacy0000aa",
            t,
            "EXECUTOR",
            "2026-08-22T15:24:38.000000Z",
            "H",
            "A",
            trigger,
            "legacy terminal (pre-migration)",
            "2026-08-22T15:24:38.000000Z",
        ),
    )
    assert _provenance(conn, "legacy0000aa") == (None, "legacy")

    for author in (PARENT, worker):
        with pytest.raises(DebateError) as exc_info:
            _terminal(conn, t, reply_to=trigger, author=author)
        _raises(exc_info, "terminal_reply_duplicate")


def test_rejected_posts_insert_zero_rows(topic):
    conn, t = topic
    trigger, worker = _dispatch(conn, t)
    before = _rows(conn, t)

    for author, err in (
        ("cc-outsider9999", "ROLE_UNAVAILABLE"),
        (None, "author_session_required"),
    ):
        with pytest.raises(DebateError) as exc_info:
            _terminal(conn, t, reply_to=trigger, author=author)
        _raises(exc_info, err)
    assert _rows(conn, t) == before


# ═════════════════════════════════════════════════════════════════════════════
# C6 — concurrency: two connections, production BEGIN IMMEDIATE discipline
# ═════════════════════════════════════════════════════════════════════════════

STATE_TABLES = {
    "debate_messages": "topic_id = ?",
    "debate_message_recipients": "msg_id IN (SELECT msg_id FROM debate_messages WHERE topic_id = ?)",
    "debate_delivery_queue": "msg_id IN (SELECT msg_id FROM debate_messages WHERE topic_id = ?)",
    "debate_worker_claims": "topic_id = ?",
    "debate_message_claims": "msg_id IN (SELECT msg_id FROM debate_messages WHERE topic_id = ?)",
    "debate_signal_state": "topic_id = ?",
    "debate_worker_reap_log": "topic_id = ?",
    "debate_worker_counters": "topic_id = ?",
    "debate_protocol_state": "topic_id = ?",
}


def _snapshot(conn, t):
    return {
        table: [
            tuple(r)
            for r in conn.execute(
                f"SELECT * FROM {table} WHERE {where} ORDER BY 1, 2", (t,)
            )
        ]
        for table, where in STATE_TABLES.items()
    }


@pytest.fixture
def file_topic(tmp_path):
    db_path = str(tmp_path / "concurrency.db")
    init_db(db_path)
    a = _connect(db_path, timeout=0.05)
    _seed(a, "RO2")
    b = _connect(db_path, timeout=0.05)
    yield a, b, "RO2"
    a.close()
    b.close()


def _post_addressed(conn, t, *, author, body, reply_to):
    return debate_post_with_recipients(
        conn,
        topic_id=t,
        role="EXECUTOR",
        priority="H",
        kind="A",
        body=body,
        addressed_to=["CONDUCTOR"],
        reply_to=reply_to,
        author_session_id=author,
    )


@pytest.mark.parametrize(
    "first,second,second_error",
    [
        ("worker", "worker", "ROLE_UNAVAILABLE"),
        ("parent", "parent", "terminal_reply_duplicate"),
        ("worker", "parent", None),
        ("parent", "worker", "ROLE_UNAVAILABLE"),
    ],
)
def test_concurrent_terminals_exactly_one_allowed_writer_per_slot(
    file_topic, first, second, second_error
):
    a, b, t = file_topic
    trigger, worker = _dispatch(a, t)
    ident = {"worker": worker, "parent": PARENT}
    statements = []
    b.set_trace_callback(statements.append)

    a.execute("BEGIN IMMEDIATE")
    first_msg = _post_addressed(
        a, t, author=ident[first], body="first", reply_to=trigger
    )
    snapshot_during = _snapshot(a, t)
    # Writer B under production discipline cannot even open its transaction.
    with pytest.raises(sqlite3.OperationalError):
        b.execute("BEGIN IMMEDIATE")
    assert b.in_transaction is False
    assert not [
        s
        for s in statements
        if s.strip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
    ]
    a.execute("COMMIT")
    assert _snapshot(b, t) == snapshot_during, "A's commit is exactly what A wrote"

    before = _snapshot(b, t)
    b.execute("BEGIN IMMEDIATE")
    if second_error is None:
        msg = _post_addressed(
            b, t, author=ident[second], body="second", reply_to=trigger
        )
        b.execute("COMMIT")
        assert _provenance(b, msg["msg_id"]) == (ident[second], second)
    else:
        with pytest.raises(DebateError) as exc_info:
            _post_addressed(b, t, author=ident[second], body="second", reply_to=trigger)
        b.execute("ROLLBACK")
        _raises(exc_info, second_error)
        assert _snapshot(b, t) == before, "the loser leaves zero collateral mutation"
    assert first_msg["msg_id"]


# ═════════════════════════════════════════════════════════════════════════════
# C7 — migration / query contract
# ═════════════════════════════════════════════════════════════════════════════


def test_old_explicit_column_insert_yields_legacy_and_consumes_both_slots(topic):
    conn, t = topic
    trigger, worker = _dispatch(conn, t)
    conn.execute(
        "INSERT INTO debate_messages (msg_id, topic_id, role, ts, priority, kind, "
        "standing, vehicle, reply_to, body, protocol_version, round_no, body_mode, "
        "payload_json, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "oldwriter001",
            t,
            "EXECUTOR",
            "2026-08-23T00:00:00Z",
            "H",
            "A",
            None,
            "analysis",
            trigger,
            "old writer",
            None,
            None,
            None,
            None,
            "2026-08-23T00:00:00Z",
        ),
    )

    assert _provenance(conn, "oldwriter001") == (None, "legacy")
    for author in (worker, PARENT):
        with pytest.raises(DebateError) as exc_info:
            _terminal(conn, t, reply_to=trigger, author=author)
        _raises(exc_info, "terminal_reply_duplicate")


def test_ownership_lookup_uses_reply_to_index_without_temp_sort_and_is_used(topic):
    conn, t = topic
    sql = getattr(debate, "OWNERSHIP_LOOKUP_SQL", None)
    assert sql, "OWNERSHIP_LOOKUP_SQL must be the single ownership query"
    plan = " | ".join(
        r[3]
        for r in conn.execute(
            "EXPLAIN QUERY PLAN " + sql, (t, "EXECUTOR", "abc123456789")
        )
    )
    assert "USING INDEX" in plan and "reply_to" in plan, plan
    assert "TEMP B-TREE" not in plan, plan

    trigger, worker = _dispatch(conn, t)
    executed = []
    conn.set_trace_callback(executed.append)
    _terminal(conn, t, reply_to=trigger, author=worker)
    conn.set_trace_callback(None)
    # sqlite3's trace callback reports the EXPANDED statement (parameters
    # substituted), so compare the shape, not the literal constant.
    shape = " ".join(sql.split("?")[0].split())  # text up to the first bind
    ran = [" ".join(s.split()) for s in executed]
    assert any(s.startswith(shape) and "ORDER BY" not in s for s in ran), (
        "the guard must run OWNERSHIP_LOOKUP_SQL (no ORDER BY)"
    )


# ═════════════════════════════════════════════════════════════════════════════
# Refutation round 2 (2026-08-23, workflow wf_7f16e820) — pinned fixes
# ═════════════════════════════════════════════════════════════════════════════


def test_signal_advance_never_writes_claim_lifecycle(topic):
    """signal_advance is cursor-only: with an ACTIVE claim and no own terminal
    it must neither complete nor retire the claim, even when a foreign
    terminal covers the trigger."""
    conn, t = topic
    trigger, worker = _dispatch(conn, t)
    _terminal(conn, t, reply_to=trigger, author=PARENT, body="parent answered first")
    # parent-final retired the claim atomically; reactivate nothing, just read
    before = tuple(_claim_row(conn, t, worker))
    assert before[0] == "retired"

    with conn:
        statements: list[str] = []
        conn.set_trace_callback(statements.append)
        try:
            advanced = _advance(conn, t, worker, trigger)
        finally:
            conn.set_trace_callback(None)
    assert tuple(_claim_row(conn, t, worker)) == before
    assert advanced["last_processed_msg_id"] == trigger
    writes = [
        s
        for s in statements
        if "debate_worker_claims" in s and s.lstrip().upper().startswith("UPDATE")
    ]
    assert writes == [], writes


def test_retired_worker_may_advance_only_its_own_trigger(topic):
    """D3 pinned for the RETIRED case (parent-final): own trigger OK, any
    other message still watermark_advance_unaddressed."""
    conn, t = topic
    trigger, worker = _dispatch(conn, t)
    _terminal(conn, t, reply_to=trigger, author=PARENT, body="final")
    assert _claim_row(conn, t, worker)["state"] == "retired"
    other = _trigger(conn, t, body="unrelated")

    assert _advance(conn, t, worker, trigger)["last_processed_msg_id"] == trigger
    with pytest.raises(DebateError) as exc_info:
        _advance(conn, t, worker, other)
    assert exc_info.value.error_type == "watermark_advance_unaddressed"


def test_stale_recovery_records_the_covering_class_and_ignores_unattributed(topic):
    """closed_by names the actual covering class; an unattributed terminal
    never covers a claim (consistent with the admission guard, so a retired
    claim can never be REQUEUEd behind a system note)."""
    conn, t = topic
    # (a) parent terminal covers → closed_by=covered_by_parent
    trigger_a, worker_a = _dispatch(conn, t)
    conn.execute(
        "UPDATE debate_worker_claims SET state='active', ack_msg_id=NULL WHERE worker_session_id=?",
        (worker_a,),
    )
    p = _terminal(conn, t, reply_to=trigger_a, author=PARENT, body="parent")
    assert _provenance(conn, p["msg_id"]) == (PARENT, "parent")
    # (b) unattributed system note on a claimed trigger → claim stays active
    trigger_b, worker_b = _dispatch(conn, t, body="second")
    _terminal(
        conn,
        t,
        reply_to=trigger_b,
        author=None,
        kind="STATUS",
        body="sys",
        internal_unattributed=True,
    )
    for w in (worker_a, worker_b):
        conn.execute(
            "UPDATE debate_worker_claims SET state='active', heartbeat_at='2000-01-01T00:00:00Z' "
            "WHERE topic_id=? AND worker_session_id=?",
            (t, w),
        )

    out = recover_stale_worker_claims(
        conn, topic_id=t, older_than_ts="2001-01-01T00:00:00Z", minimum_age_seconds=0
    )

    row_a = _claim_row(conn, t, worker_a)
    assert row_a["state"] == "retired" and row_a["ack_msg_id"] is None
    assert json.loads(row_a["details_json"])["closed_by"] == "covered_by_parent"
    row_b = _claim_row(conn, t, worker_b)
    assert row_b["state"] == "retired"
    assert json.loads(row_b["details_json"]).get("closed_by") != "legacy_terminal"
    assert json.loads(row_b["details_json"]).get("closed_by") is None
    assert {r["trigger_msg_id"] for r in out["retired"]} == {trigger_a, trigger_b}


def test_post_with_recipients_result_carries_provenance(topic):
    conn, t = topic
    trigger = _trigger(conn, t)
    out = debate_post_with_recipients(
        conn,
        topic_id=t,
        role="EXECUTOR",
        priority="H",
        kind="A",
        body="x",
        reply_to=trigger,
        addressed_to=["CONDUCTOR"],
        author_session_id=PARENT,
    )
    assert (out["author_session_id"], out["provenance_class"]) == (PARENT, "parent")
