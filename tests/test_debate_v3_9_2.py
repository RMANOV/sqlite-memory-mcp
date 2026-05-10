"""v3.9.2 — prompt-time inbox signaling test battery.

Coverage map (per CONDUCTOR canonical msg:b3a87f15 + amendments):
  - Schema & basic invariants             (4 tests)
  - debate_post_with_recipients (DAO)    (10 tests)
  - debate_signal_check (DAO)            (15 tests)
  - debate_signal_advance (DAO)           (8 tests)
  - DebateError + _debate_error_response   (8 tests)
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from debate import (
    APPROVED_RUNTIME_PREFIXES,
    DEFAULT_SIGNAL_LIMIT,
    MAX_SIGNAL_LIMIT,
    SESSION_ID_RE,
    DebateError,
    debate_post_with_recipients,
    debate_signal_advance,
    debate_signal_check,
    init_debate,
    transition_state,
)
from intel_server import _debate_error_response
from schema import init_db


# ── Fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def topic(tmp_path):
    db_path = str(tmp_path / "v3_9_2.db")
    init_db(db_path)
    c = sqlite3.connect(db_path, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    init_debate(
        c, topic_id="X1", title="v3.9.2 test topic",
        roles=[
            {"role": "CONDUCTOR", "session_id": "cc-cond1"},
            {"role": "EXECUTOR", "session_id": "cc-exec1"},
            {"role": "ADVOCATE", "session_id": "codex-adv1"},
        ],
        created_by_role="CONDUCTOR",
    )
    transition_state(c, topic_id="X1", role="CONDUCTOR", new_state="ACTIVE")
    yield c, "X1"
    c.close()


# ═══════════════════════════════════════════════════════════════════════
# Schema invariants
# ═══════════════════════════════════════════════════════════════════════


def test_schema_creates_recipients_and_signal_state_tables(topic):
    conn, _t = topic
    tables = {
        r["name"]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name LIKE 'debate_%'"
        ).fetchall()
    }
    assert "debate_message_recipients" in tables
    assert "debate_signal_state" in tables


def test_schema_recipients_has_compound_pk(topic):
    conn, _t = topic
    cols = conn.execute("PRAGMA table_info(debate_message_recipients)").fetchall()
    pk_cols = {c["name"] for c in cols if c["pk"] > 0}
    assert pk_cols == {"msg_id", "recipient"}


def test_schema_signal_state_has_compound_pk_and_no_msg_id_fk(topic):
    conn, _t = topic
    cols = conn.execute("PRAGMA table_info(debate_signal_state)").fetchall()
    pk_cols = {c["name"] for c in cols if c["pk"] > 0}
    assert pk_cols == {"session_id", "role", "topic_id"}
    fks = conn.execute(
        "PRAGMA foreign_key_list(debate_signal_state)"
    ).fetchall()
    fk_cols = {f["from"] for f in fks}
    assert "topic_id" in fk_cols
    # last_processed_msg_id intentionally has NO FK so cursor history
    # survives potential message hard-deletes (advance is gated by
    # recipient match in the DAO, not by FK).
    assert "last_processed_msg_id" not in fk_cols


def test_schema_indexes_present(topic):
    conn, _t = topic
    indexes = {
        r["name"]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()
    }
    assert {
        "idx_dmr_recipient",
        "idx_dss_role_topic",
        "idx_dss_last_check",
    } <= indexes


# ═══════════════════════════════════════════════════════════════════════
# debate_post_with_recipients
# ═══════════════════════════════════════════════════════════════════════


def test_post_with_recipients_happy_path_inserts_atomic_rows(topic):
    conn, t = topic
    out = debate_post_with_recipients(
        conn, topic_id=t, role="CONDUCTOR",
        priority="H", kind="STATUS", body="hello",
        addressed_to=["EXECUTOR", "cc-exec1"],
    )
    assert out["recipient_count"] == 2
    rows = conn.execute(
        "SELECT recipient FROM debate_message_recipients WHERE msg_id = ?",
        (out["msg_id"],),
    ).fetchall()
    assert {r["recipient"] for r in rows} == {"EXECUTOR", "cc-exec1"}


def test_post_with_recipients_empty_addressed_to_raises(topic):
    conn, t = topic
    with pytest.raises(DebateError) as exc_info:
        debate_post_with_recipients(
            conn, topic_id=t, role="EXECUTOR",
            priority="M", kind="STATUS", body="x", addressed_to=[],
        )
    assert exc_info.value.error_type == "recipient_empty"


def test_post_with_recipients_dedupes_role_silently(topic):
    """addressed_to=['EXECUTOR', 'EXECUTOR'] inserts 1 row, not 2."""
    conn, t = topic
    out = debate_post_with_recipients(
        conn, topic_id=t, role="CONDUCTOR",
        priority="M", kind="STATUS", body="x",
        addressed_to=["EXECUTOR", "EXECUTOR"],
    )
    assert out["recipient_count"] == 1
    rows = conn.execute(
        "SELECT COUNT(*) AS c FROM debate_message_recipients "
        "WHERE msg_id = ?",
        (out["msg_id"],),
    ).fetchone()
    assert rows["c"] == 1


def test_post_with_recipients_dedupes_mixed_preserve_order(topic):
    """['EXECUTOR', 'cc-exec1', 'EXECUTOR', 'cc-exec1'] → 2 rows."""
    conn, t = topic
    out = debate_post_with_recipients(
        conn, topic_id=t, role="CONDUCTOR",
        priority="M", kind="STATUS", body="x",
        addressed_to=["EXECUTOR", "cc-exec1", "EXECUTOR", "cc-exec1"],
    )
    assert out["recipient_count"] == 2


def test_post_with_recipients_unknown_role_raises(topic):
    conn, t = topic
    with pytest.raises(DebateError) as exc_info:
        debate_post_with_recipients(
            conn, topic_id=t, role="EXECUTOR",
            priority="M", kind="STATUS", body="x",
            addressed_to=["GHOST"],
        )
    assert exc_info.value.error_type == "recipient_unknown_role"


def test_post_with_recipients_invalid_session_id_raises(topic):
    conn, t = topic
    with pytest.raises(DebateError) as exc_info:
        debate_post_with_recipients(
            conn, topic_id=t, role="EXECUTOR",
            priority="M", kind="STATUS", body="x",
            addressed_to=["bad-prefix-x"],
        )
    assert exc_info.value.error_type == "recipient_invalid_session_id"


def test_post_with_recipients_atomic_rollback_on_invalid_recipient(topic):
    """Mixed valid+invalid addressed_to: zero rows persisted."""
    conn, t = topic
    pre_msg = conn.execute(
        "SELECT COUNT(*) AS c FROM debate_messages WHERE topic_id = ?",
        (t,),
    ).fetchone()["c"]
    pre_rec = conn.execute(
        "SELECT COUNT(*) AS c FROM debate_message_recipients"
    ).fetchone()["c"]
    with pytest.raises(DebateError):
        debate_post_with_recipients(
            conn, topic_id=t, role="EXECUTOR",
            priority="M", kind="STATUS", body="atomic-test",
            addressed_to=["EXECUTOR", "GHOST"],
        )
    assert (
        conn.execute(
            "SELECT COUNT(*) AS c FROM debate_messages WHERE topic_id = ?",
            (t,),
        ).fetchone()["c"]
        == pre_msg
    )
    assert (
        conn.execute(
            "SELECT COUNT(*) AS c FROM debate_message_recipients"
        ).fetchone()["c"]
        == pre_rec
    )


def test_post_with_recipients_archived_blocks_state_kind(topic):
    """Per amendment a08c61b3: ARCHIVED is terminal — even kind=STATE blocked."""
    conn, t = topic
    transition_state(conn, topic_id=t, role="CONDUCTOR", new_state="RESOLVED")
    transition_state(conn, topic_id=t, role="CONDUCTOR", new_state="ARCHIVED")
    with pytest.raises(DebateError) as exc_info:
        debate_post_with_recipients(
            conn, topic_id=t, role="CONDUCTOR",
            priority="H", kind="STATE", body="ANYTHING",
            addressed_to=["EXECUTOR"],
        )
    assert exc_info.value.error_type == "lifecycle_archived"


def test_post_with_recipients_resolved_blocks_non_state(topic):
    conn, t = topic
    transition_state(conn, topic_id=t, role="CONDUCTOR", new_state="RESOLVED")
    with pytest.raises(DebateError) as exc_info:
        debate_post_with_recipients(
            conn, topic_id=t, role="EXECUTOR",
            priority="M", kind="STATUS", body="late",
            addressed_to=["CONDUCTOR"],
        )
    assert exc_info.value.error_type == "lifecycle_resolved_non_state"


def test_post_with_recipients_unknown_topic_raises(topic):
    conn, _t = topic
    with pytest.raises(DebateError) as exc_info:
        debate_post_with_recipients(
            conn, topic_id="NOPE", role="EXECUTOR",
            priority="M", kind="STATUS", body="x",
            addressed_to=["EXECUTOR"],
        )
    assert exc_info.value.error_type == "topic_not_found"


# ═══════════════════════════════════════════════════════════════════════
# debate_signal_check
# ═══════════════════════════════════════════════════════════════════════


def test_signal_check_returns_only_addressed_messages(topic):
    conn, t = topic
    debate_post_with_recipients(
        conn, topic_id=t, role="CONDUCTOR",
        priority="M", kind="STATUS", body="for-EXECUTOR",
        addressed_to=["EXECUTOR"],
    )
    debate_post_with_recipients(
        conn, topic_id=t, role="EXECUTOR",
        priority="M", kind="STATUS", body="for-CONDUCTOR",
        addressed_to=["CONDUCTOR"],
    )
    out = debate_signal_check(
        conn, session_id="cc-exec1", role="EXECUTOR", topic_id=t
    )
    bodies = {m["body"] for m in out["pending"]}
    assert bodies == {"for-EXECUTOR"}


def test_signal_check_dedupes_role_and_session_id_match(topic):
    """Single message addressed to BOTH role + session_id → 1 row, not 2."""
    conn, t = topic
    debate_post_with_recipients(
        conn, topic_id=t, role="CONDUCTOR",
        priority="M", kind="STATUS", body="dual",
        addressed_to=["EXECUTOR", "cc-exec1"],
    )
    out = debate_signal_check(
        conn, session_id="cc-exec1", role="EXECUTOR", topic_id=t
    )
    assert out["count"] == 1
    assert len(out["pending"]) == 1


def test_signal_check_truncation_and_next_cursor(topic):
    conn, t = topic
    for i in range(5):
        debate_post_with_recipients(
            conn, topic_id=t, role="CONDUCTOR",
            priority="L", kind="STATUS", body=f"m{i}",
            addressed_to=["EXECUTOR"],
        )
    out = debate_signal_check(
        conn, session_id="cc-exec1", role="EXECUTOR",
        topic_id=t, limit=3,
    )
    assert out["count"] == 3
    assert out["truncated"] is True
    assert out["next_cursor"] is not None
    assert "ts" in out["next_cursor"] and "msg_id" in out["next_cursor"]


def test_signal_check_pagination_walk_returns_remainder(topic):
    conn, t = topic
    for i in range(5):
        debate_post_with_recipients(
            conn, topic_id=t, role="CONDUCTOR",
            priority="L", kind="STATUS", body=f"m{i}",
            addressed_to=["EXECUTOR"],
        )
    page1 = debate_signal_check(
        conn, session_id="cc-exec1", role="EXECUTOR",
        topic_id=t, limit=3,
    )
    page2 = debate_signal_check(
        conn, session_id="cc-exec1", role="EXECUTOR", topic_id=t,
        since_msg_id=page1["next_cursor"]["msg_id"], limit=3,
    )
    assert page2["count"] == 2
    assert not page2["truncated"]
    assert page2["next_cursor"] is None


def test_signal_check_max_priority_uses_correct_order(topic):
    conn, t = topic
    debate_post_with_recipients(
        conn, topic_id=t, role="CONDUCTOR", priority="L",
        kind="STATUS", body="low", addressed_to=["EXECUTOR"],
    )
    debate_post_with_recipients(
        conn, topic_id=t, role="CONDUCTOR", priority="H",
        kind="STATUS", body="high", addressed_to=["EXECUTOR"],
    )
    debate_post_with_recipients(
        conn, topic_id=t, role="CONDUCTOR", priority="M",
        kind="STATUS", body="mid", addressed_to=["EXECUTOR"],
    )
    out = debate_signal_check(
        conn, session_id="cc-exec1", role="EXECUTOR", topic_id=t
    )
    assert out["max_priority"] == "H"


def test_signal_check_empty_returns_none_max_priority(topic):
    conn, t = topic
    out = debate_signal_check(
        conn, session_id="cc-exec1", role="EXECUTOR", topic_id=t
    )
    assert out["count"] == 0
    assert out["max_priority"] is None
    assert out["next_cursor"] is None


def test_signal_check_limit_zero_raises_value_error(topic):
    conn, t = topic
    with pytest.raises(DebateError) as exc_info:
        debate_signal_check(
            conn, session_id="cc-exec1", role="EXECUTOR",
            topic_id=t, limit=0,
        )
    assert exc_info.value.error_type == "limit_out_of_range"


def test_signal_check_limit_negative_raises(topic):
    conn, t = topic
    with pytest.raises(DebateError) as exc_info:
        debate_signal_check(
            conn, session_id="cc-exec1", role="EXECUTOR",
            topic_id=t, limit=-5,
        )
    assert exc_info.value.error_type == "limit_out_of_range"


def test_signal_check_limit_over_max_raises(topic):
    conn, t = topic
    with pytest.raises(DebateError) as exc_info:
        debate_signal_check(
            conn, session_id="cc-exec1", role="EXECUTOR",
            topic_id=t, limit=MAX_SIGNAL_LIMIT + 1,
        )
    assert exc_info.value.error_type == "limit_out_of_range"


def test_signal_check_limit_at_max_accepted(topic):
    conn, t = topic
    out = debate_signal_check(
        conn, session_id="cc-exec1", role="EXECUTOR",
        topic_id=t, limit=MAX_SIGNAL_LIMIT,
    )
    assert out["limit"] == MAX_SIGNAL_LIMIT


def test_signal_check_limit_non_int_raises_type_error(topic):
    conn, t = topic
    for bad in ("200", 10.5, None, [200]):
        with pytest.raises(DebateError) as exc_info:
            debate_signal_check(
                conn, session_id="cc-exec1", role="EXECUTOR",
                topic_id=t, limit=bad,
            )
        assert exc_info.value.error_type == "limit_invalid_type", bad


def test_signal_check_limit_bool_rejected_despite_int_subclass(topic):
    """Python: isinstance(True, int) == True. Defensive bool guard."""
    conn, t = topic
    for bad in (True, False):
        with pytest.raises(DebateError) as exc_info:
            debate_signal_check(
                conn, session_id="cc-exec1", role="EXECUTOR",
                topic_id=t, limit=bad,
            )
        assert exc_info.value.error_type == "limit_invalid_type"


def test_signal_check_invalid_session_id_raises(topic):
    conn, t = topic
    with pytest.raises(DebateError) as exc_info:
        debate_signal_check(
            conn, session_id="bad-format", role="EXECUTOR",
            topic_id=t,
        )
    assert exc_info.value.error_type == "recipient_invalid_session_id"


def test_signal_check_unknown_role_raises(topic):
    conn, t = topic
    with pytest.raises(DebateError) as exc_info:
        debate_signal_check(
            conn, session_id="cc-exec1", role="GHOST", topic_id=t,
        )
    assert exc_info.value.error_type == "recipient_unknown_role"


def test_signal_check_uses_signal_state_cursor_when_no_explicit_args(topic):
    """After signal_advance, signal_check uses the persisted cursor."""
    conn, t = topic
    m1 = debate_post_with_recipients(
        conn, topic_id=t, role="CONDUCTOR",
        priority="M", kind="STATUS", body="m1",
        addressed_to=["EXECUTOR"],
    )
    debate_post_with_recipients(
        conn, topic_id=t, role="CONDUCTOR",
        priority="M", kind="STATUS", body="m2",
        addressed_to=["EXECUTOR"],
    )
    debate_signal_advance(
        conn, session_id="cc-exec1", role="EXECUTOR",
        topic_id=t, last_processed_msg_id=m1["msg_id"],
    )
    out = debate_signal_check(
        conn, session_id="cc-exec1", role="EXECUTOR", topic_id=t
    )
    assert out["count"] == 1
    assert out["pending"][0]["body"] == "m2"


# ═══════════════════════════════════════════════════════════════════════
# debate_signal_advance
# ═══════════════════════════════════════════════════════════════════════


def test_signal_advance_to_role_addressed_msg_succeeds(topic):
    conn, t = topic
    m1 = debate_post_with_recipients(
        conn, topic_id=t, role="CONDUCTOR",
        priority="M", kind="STATUS", body="m1",
        addressed_to=["EXECUTOR"],
    )
    res = debate_signal_advance(
        conn, session_id="cc-exec1", role="EXECUTOR",
        topic_id=t, last_processed_msg_id=m1["msg_id"],
    )
    assert res["last_processed_msg_id"] == m1["msg_id"]
    assert res["last_processed_ts"] == m1["ts"]


def test_signal_advance_to_session_id_addressed_msg_succeeds(topic):
    conn, t = topic
    m1 = debate_post_with_recipients(
        conn, topic_id=t, role="CONDUCTOR",
        priority="M", kind="STATUS", body="m1",
        addressed_to=["cc-exec1"],
    )
    res = debate_signal_advance(
        conn, session_id="cc-exec1", role="EXECUTOR",
        topic_id=t, last_processed_msg_id=m1["msg_id"],
    )
    assert res["last_processed_msg_id"] == m1["msg_id"]


def test_signal_advance_to_msg_addressed_to_BOTH_succeeds_once(topic):
    conn, t = topic
    m1 = debate_post_with_recipients(
        conn, topic_id=t, role="CONDUCTOR",
        priority="M", kind="STATUS", body="m1",
        addressed_to=["EXECUTOR", "cc-exec1"],
    )
    debate_signal_advance(
        conn, session_id="cc-exec1", role="EXECUTOR",
        topic_id=t, last_processed_msg_id=m1["msg_id"],
    )
    rows = conn.execute(
        "SELECT COUNT(*) AS c FROM debate_signal_state "
        "WHERE session_id = ? AND role = ? AND topic_id = ?",
        ("cc-exec1", "EXECUTOR", t),
    ).fetchone()
    assert rows["c"] == 1


def test_signal_advance_to_unaddressed_msg_raises(topic):
    """CRITICAL turn-12 fix: cannot skip past unprocessed addressed work."""
    conn, t = topic
    m1 = debate_post_with_recipients(
        conn, topic_id=t, role="EXECUTOR",
        priority="M", kind="STATUS", body="for-CONDUCTOR-only",
        addressed_to=["CONDUCTOR"],
    )
    with pytest.raises(DebateError) as exc_info:
        debate_signal_advance(
            conn, session_id="cc-exec1", role="EXECUTOR",
            topic_id=t, last_processed_msg_id=m1["msg_id"],
        )
    assert exc_info.value.error_type == "watermark_advance_unaddressed"


def test_signal_advance_unknown_msg_id_raises(topic):
    conn, t = topic
    with pytest.raises(DebateError) as exc_info:
        debate_signal_advance(
            conn, session_id="cc-exec1", role="EXECUTOR",
            topic_id=t, last_processed_msg_id="deadbeef",
        )
    assert exc_info.value.error_type == "watermark_msg_id_unknown"


def test_signal_advance_invalid_session_id_raises(topic):
    conn, t = topic
    m1 = debate_post_with_recipients(
        conn, topic_id=t, role="CONDUCTOR",
        priority="M", kind="STATUS", body="x",
        addressed_to=["EXECUTOR"],
    )
    with pytest.raises(DebateError) as exc_info:
        debate_signal_advance(
            conn, session_id="bad", role="EXECUTOR",
            topic_id=t, last_processed_msg_id=m1["msg_id"],
        )
    assert exc_info.value.error_type == "recipient_invalid_session_id"


def test_signal_advance_idempotent(topic):
    conn, t = topic
    m1 = debate_post_with_recipients(
        conn, topic_id=t, role="CONDUCTOR",
        priority="M", kind="STATUS", body="x",
        addressed_to=["EXECUTOR"],
    )
    for _ in range(3):
        debate_signal_advance(
            conn, session_id="cc-exec1", role="EXECUTOR",
            topic_id=t, last_processed_msg_id=m1["msg_id"],
        )
    rows = conn.execute(
        "SELECT COUNT(*) AS c FROM debate_signal_state"
    ).fetchone()
    assert rows["c"] == 1


def test_signal_advance_writes_both_cursor_columns(topic):
    """Compound (ts, msg_id) cursor — both columns must be set."""
    conn, t = topic
    m1 = debate_post_with_recipients(
        conn, topic_id=t, role="CONDUCTOR",
        priority="M", kind="STATUS", body="x",
        addressed_to=["EXECUTOR"],
    )
    debate_signal_advance(
        conn, session_id="cc-exec1", role="EXECUTOR",
        topic_id=t, last_processed_msg_id=m1["msg_id"],
    )
    row = conn.execute(
        "SELECT last_processed_msg_id, last_processed_ts, last_check_at "
        "FROM debate_signal_state WHERE session_id = ? AND role = ? "
        "AND topic_id = ?",
        ("cc-exec1", "EXECUTOR", t),
    ).fetchone()
    assert row["last_processed_msg_id"] == m1["msg_id"]
    assert row["last_processed_ts"] == m1["ts"]
    assert row["last_check_at"] is not None


# ═══════════════════════════════════════════════════════════════════════
# DebateError + _debate_error_response (error contract per msg:e0f47b29)
# ═══════════════════════════════════════════════════════════════════════


def test_DebateError_legacy_single_arg_default_error_type():
    e = DebateError("legacy")
    assert e.error_type == "debate_validation"


def test_DebateError_keyword_error_type_accepted():
    e = DebateError("v3.9.2", error_type="limit_out_of_range")
    assert e.error_type == "limit_out_of_range"


def test_DebateError_positional_error_type_raises_TypeError():
    """The leading `*` makes error_type keyword-only."""
    with pytest.raises(TypeError):
        DebateError("foo", "bar")  # second positional arg rejected


def test_debate_error_response_emits_specific_error_type():
    out = _debate_error_response(
        DebateError("boom", error_type="limit_out_of_range")
    )
    assert isinstance(out, str)
    parsed = json.loads(out)
    assert parsed["error_type"] == "limit_out_of_range"
    assert parsed["error"] == "boom"


def test_debate_error_response_legacy_default_emits_debate_validation():
    out = _debate_error_response(DebateError("legacy"))
    parsed = json.loads(out)
    assert parsed["error_type"] == "debate_validation"


def test_debate_error_response_raw_ValueError_stays_internal_error():
    """Raw stdlib exceptions MUST surface as 'internal_error', NOT
    silently get rebadged as 'debate_validation'."""
    out = _debate_error_response(ValueError("not a DebateError"))
    parsed = json.loads(out)
    assert parsed["error_type"] == "internal_error"


def test_debate_error_response_raw_RuntimeError_stays_internal_error():
    out = _debate_error_response(RuntimeError("oops"))
    parsed = json.loads(out)
    assert parsed["error_type"] == "internal_error"


def test_debate_error_response_returns_str_not_dict():
    """Wire contract preservation per amendment 7 (msg:e0f47b29)."""
    out = _debate_error_response(DebateError("foo"))
    assert isinstance(out, str)
    assert isinstance(json.loads(out), dict)


# ═══════════════════════════════════════════════════════════════════════
# Constants smoke
# ═══════════════════════════════════════════════════════════════════════


def test_constants_exposed():
    assert DEFAULT_SIGNAL_LIMIT == 200
    assert MAX_SIGNAL_LIMIT == 1000
    assert APPROVED_RUNTIME_PREFIXES == (
        "cc-", "codex-", "mcp-", "tray-", "human-",
    )


def test_session_id_re_minimum_suffix_length():
    """Spec: [a-zA-Z0-9_]{4,64} suffix. 3-char suffixes rejected."""
    assert SESSION_ID_RE.fullmatch("cc-abcd") is not None
    assert SESSION_ID_RE.fullmatch("cc-abc") is None
