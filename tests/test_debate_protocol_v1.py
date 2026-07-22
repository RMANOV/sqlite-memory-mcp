"""Acceptance tests for the deterministic debate/v1 §7 server contract."""

from __future__ import annotations

import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest

from debate import (
    DebateError,
    debate_post_with_recipients,
    debate_signal_advance,
    debate_signal_check,
    init_debate,
    post_message,
    prepare_wake_dry_run,
    read_messages,
    seed_initial_role_bindings,
)
from debate_protocol_v1 import (
    adaptive_wait_decision,
    get_protocol_state,
    prepare_order_swap,
    record_order_swap_verdict,
    sweep_missing_roles,
    transition_expired_protocols,
)
from debate_read_dao import DebateReadDAO
from debate_retrieval import search_debate_context
from schema import init_db


ROLES = [
    {"role": "CONDUCTOR", "session_id": "cc-conductor"},
    {"role": "EXECUTOR", "session_id": "cc-executor"},
    {"role": "ADVOCATE", "session_id": "codex-advocate"},
    {"role": "JUDGE", "session_id": "cc-judge"},
    {"role": "OPERATOR", "session_id": "human-operator"},
]


@pytest.fixture()
def conn(tmp_path):
    path = tmp_path / "memory.db"
    init_db(str(path))
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    yield db
    db.close()


def _topic(conn, topic="S7_TEST", *, timeout=300):
    out = init_debate(
        conn,
        topic_id=topic,
        title="§7 deterministic test",
        roles=ROLES,
        created_by_role="CONDUCTOR",
        protocol_version="debate/v1",
        blind_roles=["EXECUTOR", "ADVOCATE"],
        max_rounds=3,
        phase_timeout_seconds=timeout,
    )
    seed_initial_role_bindings(
        conn,
        topic_id=topic,
        roles=ROLES,
        bound_by_role="CONDUCTOR",
        reason="test",
    )
    post_message(
        conn,
        topic_id=topic,
        role="CONDUCTOR",
        priority="INFO",
        kind="STATE",
        body="ACTIVE",
    )
    assert out["protocol_state"]["phase"] == "BLIND_CLAIM"
    return topic


def _claim(conn, topic, role, recipient, summary, *, priority="M"):
    return debate_post_with_recipients(
        conn,
        topic_id=topic,
        role=role,
        priority=priority,
        kind="CLAIM",
        body=summary,
        addressed_to=[recipient],
        protocol_version="debate/v1",
        body_mode="structured",
        payload_json={
            "summary": summary,
            "assumptions": [],
            "evidence_refs": [],
        },
        author_session_id=("cc-executor" if role == "EXECUTOR" else "codex-advocate"),
    )


def _release(conn, topic, *, priority="M"):
    left = _claim(conn, topic, "EXECUTOR", "ADVOCATE", "position A", priority=priority)
    right = _claim(conn, topic, "ADVOCATE", "EXECUTOR", "position B", priority=priority)
    assert get_protocol_state(conn, topic)["blind_barrier_state"] == "released"
    return left, right


def test_schema_has_typed_envelope_and_protocol_tables(conn):
    columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(debate_messages)")
    }
    assert {"protocol_version", "round_no", "body_mode", "payload_json"} <= columns
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {
        "debate_protocol_state",
        "debate_blind_commits",
        "debate_judge_projections",
        "debate_human_packets",
        "debate_role_recovery_log",
        "debate_scheduler_decisions",
    } <= tables


def test_legacy_message_table_migration_is_lossless(tmp_path):
    path = tmp_path / "legacy.db"
    legacy = sqlite3.connect(path)
    legacy.executescript(
        """
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
        CREATE TABLE debate_message_recipients(
            msg_id TEXT NOT NULL REFERENCES debate_messages(msg_id),
            recipient TEXT NOT NULL,recipient_mode TEXT NOT NULL DEFAULT 'normal',
            PRIMARY KEY(msg_id,recipient));
        INSERT INTO debates VALUES(
          'LEGACY_TEST','legacy','ACTIVE','2026-01-01T00:00:00Z','OLD',
          NULL,NULL,'[]',NULL);
        INSERT INTO debate_messages VALUES(
          'abcdef12','LEGACY_TEST','OLD','2026-01-01T00:00:00Z','H','Q',
          NULL,'analysis',NULL,'preserve me','2026-01-01T00:00:00Z');
        INSERT INTO debate_message_recipients VALUES('abcdef12','OLD','normal');
        """
    )
    legacy.commit()
    legacy.close()
    init_db(str(path))
    migrated = sqlite3.connect(path)
    migrated.row_factory = sqlite3.Row
    try:
        row = dict(
            migrated.execute(
                "SELECT * FROM debate_messages WHERE msg_id='abcdef12'"
            ).fetchone()
        )
        assert row["body"] == "preserve me"
        assert row["protocol_version"] is None
        assert tuple(
            migrated.execute(
                "SELECT msg_id,recipient FROM debate_message_recipients"
            ).fetchone()
        ) == ("abcdef12", "OLD")
        assert migrated.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        migrated.close()


def test_concurrent_blind_claim_same_role_commits_exactly_once(conn):
    topic = _topic(conn, "S7_RACE")
    conn.commit()
    db_path = conn.execute("PRAGMA database_list").fetchone()[2]

    def attempt(label):
        db = sqlite3.connect(db_path, isolation_level=None, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute("BEGIN IMMEDIATE")
            try:
                out = _claim(db, topic, "EXECUTOR", "ADVOCATE", label, priority="M")
                db.execute("COMMIT")
                return ("ok", out["msg_id"])
            except Exception as exc:
                db.execute("ROLLBACK")
                return (getattr(exc, "error_type", type(exc).__name__), str(exc))
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, ["race A", "race B"]))
    assert [result[0] for result in results].count("ok") == 1
    assert [result[0] for result in results].count("BLIND_CLAIM_DUPLICATE") == 1
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM debate_blind_commits "
            "WHERE topic_id=? AND role='EXECUTOR'",
            (topic,),
        ).fetchone()[0]
        == 1
    )


def test_invalid_semantic_post_is_zero_mutation(conn):
    topic = _topic(conn)
    before = conn.total_changes
    counts_before = tuple(
        conn.execute(
            "SELECT (SELECT COUNT(*) FROM debate_messages),"
            "(SELECT COUNT(*) FROM debate_blind_commits),"
            "(SELECT transition_version FROM debate_protocol_state WHERE topic_id=?)",
            (topic,),
        ).fetchone()
    )
    with pytest.raises(DebateError, match="summary") as caught:
        debate_post_with_recipients(
            conn,
            topic_id=topic,
            role="EXECUTOR",
            priority="H",
            kind="CLAIM",
            body="bad",
            addressed_to=["ADVOCATE"],
            protocol_version="debate/v1",
            payload_json={"assumptions": [], "evidence_refs": []},
            author_session_id="cc-executor",
        )
    assert caught.value.error_type == "INVALID_PAYLOAD"
    counts_after = tuple(
        conn.execute(
            "SELECT (SELECT COUNT(*) FROM debate_messages),"
            "(SELECT COUNT(*) FROM debate_blind_commits),"
            "(SELECT transition_version FROM debate_protocol_state WHERE topic_id=?)",
            (topic,),
        ).fetchone()
    )
    assert counts_after == counts_before
    assert conn.total_changes == before


def test_blind_commit_has_no_read_or_signal_leak_then_releases(conn):
    topic = _topic(conn)
    first = _claim(conn, topic, "EXECUTOR", "ADVOCATE", "secret first", priority="H")
    advocate_read = read_messages(conn, topic_id=topic, role="ADVOCATE", limit=100)
    assert first["msg_id"] not in {m["msg_id"] for m in advocate_read["messages"]}
    advocate_signal = debate_signal_check(
        conn,
        session_id="codex-advocate",
        role="ADVOCATE",
        topic_id=topic,
    )
    assert first["msg_id"] not in {m["msg_id"] for m in advocate_signal["pending"]}
    hybrid = search_debate_context(
        conn,
        query="secret first",
        topic_ids=[topic],
        target_role="ADVOCATE",
    )
    assert first["msg_id"] not in {m["msg_id"] for m in hybrid["results"]}
    wake = prepare_wake_dry_run(
        conn,
        tool_response={**first, "schema_version": "debate_post_with_recipients.v1"},
        action="test_blind_wake",
    )
    assert wake["targets"] == []
    assert wake["logs"][0]["result"] == "blind_commit_waiting"
    own_read = read_messages(conn, topic_id=topic, role="EXECUTOR", limit=100)
    assert first["msg_id"] in {m["msg_id"] for m in own_read["messages"]}

    second = _claim(conn, topic, "ADVOCATE", "EXECUTOR", "secret second", priority="H")
    released_read = read_messages(conn, topic_id=topic, role="ADVOCATE", limit=100)
    assert {first["msg_id"], second["msg_id"]} <= {
        m["msg_id"] for m in released_read["messages"]
    }


def test_released_blind_projection_replays_claim_passed_by_global_cursor(conn):
    topic = _topic(conn, "S7_REPLAY")
    first = _claim(conn, topic, "EXECUTOR", "ADVOCATE", "first hidden")
    ping = debate_post_with_recipients(
        conn,
        topic_id=topic,
        role="CONDUCTOR",
        priority="H",
        kind="PING",
        body="bootstrap independent advocate claim",
        addressed_to=["ADVOCATE"],
    )
    inbox = debate_signal_check(
        conn,
        session_id="codex-advocate",
        role="ADVOCATE",
        topic_id=topic,
    )
    assert [item["msg_id"] for item in inbox["pending"]] == [ping["msg_id"]]
    debate_signal_advance(
        conn,
        session_id="codex-advocate",
        role="ADVOCATE",
        topic_id=topic,
        last_processed_msg_id=ping["msg_id"],
    )
    _claim(conn, topic, "ADVOCATE", "EXECUTOR", "second releases barrier")
    conn.commit()

    pump_path = Path(__file__).resolve().parents[1] / "hooks" / "debate_pump.py"
    spec = spec_from_file_location("debate_pump_s7_replay", pump_path)
    pump = module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = pump
    spec.loader.exec_module(pump)
    pump.DB_PATH = Path(conn.execute("PRAGMA database_list").fetchone()[2])
    rows = pump._fetch_released_blind_replay(ping["ts"], ping["msg_id"], [topic], 10)
    assert [row["msg_id"] for row in rows] == [first["msg_id"]]


def test_protocol_topic_rejects_legacy_conversation_kind(conn):
    topic = _topic(conn)
    with pytest.raises(DebateError) as caught:
        post_message(
            conn,
            topic_id=topic,
            role="EXECUTOR",
            priority="M",
            kind="A",
            body="legacy bypass",
        )
    assert caught.value.error_type == "SEMANTIC_KIND_REQUIRED"


def test_stale_read_guard_uses_compound_session_cursor(conn):
    topic = _topic(conn)
    left, right = _release(conn, topic, priority="H")
    with pytest.raises(DebateError) as caught:
        debate_post_with_recipients(
            conn,
            topic_id=topic,
            role="EXECUTOR",
            priority="M",
            kind="CHALLENGE",
            body="challenge",
            addressed_to=["ADVOCATE"],
            reply_to=left["msg_id"],
            protocol_version="debate/v1",
            payload_json={
                "target": left["msg_id"],
                "challenge_type": "logic",
                "requested_disposition": "rebut",
            },
            author_session_id="cc-executor",
        )
    assert caught.value.error_type == "STALE_READ"
    assert caught.value.details["msg_id"] == right["msg_id"]

    with pytest.raises(DebateError) as undelivered:
        debate_signal_advance(
            conn,
            session_id="cc-executor",
            role="EXECUTOR",
            topic_id=topic,
            last_processed_msg_id=right["msg_id"],
        )
    assert undelivered.value.error_type == "signal_advance_not_delivered"

    inbox = debate_signal_check(
        conn,
        session_id="cc-executor",
        role="EXECUTOR",
        topic_id=topic,
    )
    assert inbox["pending"][-1]["msg_id"] == right["msg_id"]
    debate_signal_advance(
        conn,
        session_id="cc-executor",
        role="EXECUTOR",
        topic_id=topic,
        last_processed_msg_id=right["msg_id"],
    )
    posted = debate_post_with_recipients(
        conn,
        topic_id=topic,
        role="EXECUTOR",
        priority="M",
        kind="CHALLENGE",
        body="challenge",
        addressed_to=["ADVOCATE"],
        reply_to=left["msg_id"],
        protocol_version="debate/v1",
        payload_json={
            "target": left["msg_id"],
            "challenge_type": "logic",
            "requested_disposition": "rebut",
        },
        author_session_id="cc-executor",
    )
    assert posted["round_no"] == 1


def test_round_cap_stalemate_one_dissent_and_one_human_packet(conn):
    topic = _topic(conn)
    left, _right = _release(conn, topic)
    last_verify = None
    for expected_round in (1, 2, 3):
        last_verify = debate_post_with_recipients(
            conn,
            topic_id=topic,
            role="JUDGE",
            priority="M",
            kind="VERIFY",
            body=f"contested {expected_round}",
            addressed_to=["CONDUCTOR"],
            reply_to=left["msg_id"],
            protocol_version="debate/v1",
            payload_json={
                "target": left["msg_id"],
                "result": "contested",
                "checks": ["deterministic"],
            },
            author_session_id="cc-judge",
        )
        assert last_verify["round_no"] == expected_round
    assert get_protocol_state(conn, topic)["phase"] == "STALEMATE"

    dissent = debate_post_with_recipients(
        conn,
        topic_id=topic,
        role="ADVOCATE",
        priority="M",
        kind="DISSENT",
        body="minority report",
        addressed_to=["CONDUCTOR"],
        reply_to=last_verify["msg_id"],
        protocol_version="debate/v1",
        payload_json={
            "decision_target": last_verify["msg_id"],
            "unresolved_point": "material uncertainty",
            "strongest_evidence": "source-1",
        },
        author_session_id="codex-advocate",
    )
    assert dissent["protocol_state"]["round_no"] == 3
    with pytest.raises(DebateError) as duplicate:
        debate_post_with_recipients(
            conn,
            topic_id=topic,
            role="ADVOCATE",
            priority="M",
            kind="DISSENT",
            body="duplicate",
            addressed_to=["CONDUCTOR"],
            reply_to=last_verify["msg_id"],
            protocol_version="debate/v1",
            payload_json={
                "decision_target": last_verify["msg_id"],
                "unresolved_point": "again",
                "strongest_evidence": "source-2",
            },
            author_session_id="codex-advocate",
        )
    assert duplicate.value.error_type == "DISSENT_DUPLICATE"

    escalation = debate_post_with_recipients(
        conn,
        topic_id=topic,
        role="CONDUCTOR",
        priority="H",
        kind="ESCALATE",
        body="human decision required",
        addressed_to=["OPERATOR"],
        protocol_version="debate/v1",
        payload_json={
            "decision_question": "choose A or B",
            "options": ["A", "B"],
            "decisive_evidence": ["source-1"],
            "unresolved_point": "risk appetite",
            "consequence_by_option": ["A: speed", "B: caution"],
            "exact_human_action": "Select A or B in Waiting on Me",
        },
        author_session_id="cc-conductor",
    )
    packet = conn.execute(
        "SELECT * FROM debate_human_packets WHERE topic_id=?", (topic,)
    ).fetchone()
    assert packet["msg_id"] == escalation["msg_id"]
    assert get_protocol_state(conn, topic)["phase"] == "ESCALATED"
    conn.commit()
    db_path = conn.execute("PRAGMA database_list").fetchone()[2]
    tray = DebateReadDAO(db_path)
    try:
        waiting, _ = tray.waiting_section_a()
        assert escalation["msg_id"] in {item["msg_id"] for item in waiting}
        recent = tray.recent(24 * 365, None, None)
        assert escalation["msg_id"] in {item["msg_id"] for item in recent["items"]}
        topic_row = next(
            item for item in tray.topics()["topics"] if item["topic_id"] == topic
        )
        assert topic_row["phase"] == "ESCALATED"
        assert tray.topic_thread(topic)["protocol_state"]["round_no"] == 3
    finally:
        tray.close()


def test_order_swap_stable_verdict_stops_protocol(conn):
    topic = _topic(conn, "S7_JUDGE")
    left, right = _release(conn, topic)
    verified = debate_post_with_recipients(
        conn,
        topic_id=topic,
        role="JUDGE",
        priority="M",
        kind="VERIFY",
        body="verified",
        addressed_to=["CONDUCTOR"],
        reply_to=left["msg_id"],
        protocol_version="debate/v1",
        payload_json={"target": left["msg_id"], "result": "verified", "checks": ["x"]},
        author_session_id="cc-judge",
    )
    assert verified["protocol_state"]["phase"] == "ADJUDICATE"
    projections = prepare_order_swap(
        conn,
        topic_id=topic,
        left_msg_id=left["msg_id"],
        right_msg_id=right["msg_id"],
    )["projections"]
    assert [p["order_key"] for p in projections] == ["AB", "BA"]
    assert (
        prepare_order_swap(
            conn,
            topic_id=topic,
            left_msg_id=left["msg_id"],
            right_msg_id=right["msg_id"],
        )["projections"]
        == projections
    )
    with pytest.raises(Exception) as projection_conflict:
        prepare_order_swap(
            conn,
            topic_id=topic,
            left_msg_id=right["msg_id"],
            right_msg_id=left["msg_id"],
        )
    assert projection_conflict.value.error_type == "JUDGE_PROJECTION_CONFLICT"
    first = record_order_swap_verdict(
        conn,
        projection_id=projections[0]["projection_id"],
        judge_role="JUDGE",
        verdict={"winner_msg_id": left["msg_id"], "decision": "accept A"},
    )
    assert first["complete"] is False
    with pytest.raises(Exception) as judge_mismatch:
        record_order_swap_verdict(
            conn,
            projection_id=projections[1]["projection_id"],
            judge_role="ADVOCATE",
            verdict={"winner_msg_id": left["msg_id"], "decision": "accept A"},
        )
    assert judge_mismatch.value.error_type == "JUDGE_ROLE_MISMATCH"
    second = record_order_swap_verdict(
        conn,
        projection_id=projections[1]["projection_id"],
        judge_role="JUDGE",
        verdict={"winner_msg_id": left["msg_id"], "decision": "accept A"},
    )
    assert second["stable"] is True
    assert second["protocol_state"]["phase"] == "STOPPED"


def test_timeout_role_sweep_and_adaptive_scheduler_are_server_deterministic(conn):
    topic = _topic(conn, "S7_SWEEP")
    expired = transition_expired_protocols(conn, now_iso="9999-01-01T00:00:00Z")
    assert expired == [{"topic_id": topic, "reason": "phase_timeout:BLIND_CLAIM"}]
    state_msg = conn.execute(
        "SELECT msg_id,ts FROM debate_messages WHERE topic_id=? AND kind='STATE'",
        (topic,),
    ).fetchone()
    conn.execute(
        "INSERT INTO debate_signal_state "
        "(session_id,role,topic_id,last_processed_msg_id,last_processed_ts,last_check_at) "
        "VALUES ('codex-advocate','ADVOCATE',?,?,?,'2026-01-01T00:00:00Z')",
        (topic, state_msg["msg_id"], state_msg["ts"]),
    )
    conn.execute(
        "UPDATE debate_role_bindings SET state='retired',retired_at='2026-01-01T00:00:00Z' "
        "WHERE topic_id=? AND role='ADVOCATE' AND state='active'",
        (topic,),
    )
    actions = sweep_missing_roles(conn, topic_ids=[topic])
    assert actions == [
        {
            "topic_id": topic,
            "role": "ADVOCATE",
            "session_id": "codex-auto_ADVOCATE_g2",
            "generation": 2,
            "reason": "missing_active_binding",
        }
    ]
    active_roles = conn.execute(
        "SELECT role,COUNT(*) c FROM debate_role_bindings "
        "WHERE topic_id=? AND state='active' GROUP BY role",
        (topic,),
    ).fetchall()
    assert {row["role"]: row["c"] for row in active_roles}["ADVOCATE"] == 1
    copied_cursor = conn.execute(
        "SELECT last_processed_msg_id,last_processed_ts FROM debate_signal_state "
        "WHERE topic_id=? AND role='ADVOCATE' AND session_id='codex-auto_ADVOCATE_g2'",
        (topic,),
    ).fetchone()
    assert tuple(copied_cursor) == (state_msg["msg_id"], state_msg["ts"])
    assert adaptive_wait_decision(queue_depth=1, live_workers=0, worker_capacity=2) == {
        "interval_seconds": 0.0,
        "reason": "eligible_backlog",
    }
    assert [
        adaptive_wait_decision(
            queue_depth=0,
            live_workers=0,
            worker_capacity=2,
            retry_attempt=n,
        )["interval_seconds"]
        for n in range(1, 7)
    ] == [1.0, 2.0, 5.0, 10.0, 30.0, 30.0]
    assert adaptive_wait_decision(queue_depth=0, live_workers=0, worker_capacity=2) == {
        "interval_seconds": 30.0,
        "reason": "idle_crash_replay_sweep",
    }
