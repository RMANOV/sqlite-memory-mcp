"""v3.9.3 — paranoid hunt fixups regression battery.

Per CONDUCTOR canonical msg:eee989d0 step 9, 7 new regression tests
covering the spec amendments:

  1. since_ts boundary exclusion in read_messages
  2. since_ts boundary exclusion in debate_signal_check
  3. msg_id 8|12 width validator coverage (legacy + new)
  4. deterministic forced-collision retry (monkeypatch new_msg_id)
  5. probabilistic 10000-gen smoke (statistical regression)
  6. _validate_recipient(debate=None) signature backcompat (fetch path
     vs pass-through path)
  7. wrapper-scoped race-storm for signal_advance under
     get_conn_immediate (concurrency convergence under monotonic guard)

Plus body-strip / recipient-truncation / logger.info / get_conn_immediate
smoke tests since they share fixtures.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import debate
from db_utils import get_conn, get_conn_immediate
from debate import (
    DebateError,
    MSG_ID_RE,
    _validate_recipient,
    debate_post_with_recipients,
    debate_signal_advance,
    debate_signal_check,
    init_debate,
    new_msg_id,
    post_message,
    read_messages,
    transition_state,
    validate_msg_id,
)
from schema import init_db


# ── Fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def topic(tmp_path):
    db = str(tmp_path / "v3_9_3.db")
    init_db(db)
    c = sqlite3.connect(db, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    init_debate(
        c, topic_id="X1", title="v3.9.3",
        roles=[
            {"role": "CONDUCTOR", "session_id": "cc-cond1"},
            {"role": "EXECUTOR", "session_id": "cc-exec1"},
        ],
        created_by_role="CONDUCTOR",
    )
    transition_state(c, topic_id="X1", role="CONDUCTOR", new_state="ACTIVE")
    yield c, "X1"
    c.close()


@pytest.fixture
def wrapped_topic(tmp_path):
    db_path = str(tmp_path / "v3_9_3_wrapped.db")
    init_db(db_path)
    with get_conn_immediate(db_path=db_path) as conn:
        init_debate(
            conn, topic_id="X1", title="v3.9.3-wrapped",
            roles=[
                {"role": "CONDUCTOR", "session_id": "cc-cond1"},
                {"role": "EXECUTOR", "session_id": "cc-exec1"},
            ],
            created_by_role="CONDUCTOR",
        )
        transition_state(
            conn, topic_id="X1", role="CONDUCTOR", new_state="ACTIVE"
        )
    yield db_path


# ═══════════════════════════════════════════════════════════════════════
# Test 1+2: since_ts boundary exclusion (read_messages + signal_check)
# ═══════════════════════════════════════════════════════════════════════


def _seed_three_at_same_ts(conn, topic_id, ts):
    """Force 3 STATUS messages addressed to EXECUTOR with identical ts."""
    msg_ids = []
    for i in range(3):
        mid = new_msg_id()
        conn.execute(
            "INSERT INTO debate_messages "
            "(msg_id, topic_id, role, ts, priority, kind, body, created_at) "
            "VALUES (?, ?, 'CONDUCTOR', ?, 'INFO', 'STATUS', ?, ?)",
            (mid, topic_id, ts, f"m{i}", ts),
        )
        conn.execute(
            "INSERT INTO debate_message_recipients (msg_id, recipient) "
            "VALUES (?, ?)",
            (mid, "EXECUTOR"),
        )
        msg_ids.append(mid)
    return msg_ids


def test_read_messages_since_ts_strict_exclusive(topic):
    """Per msg:946bcff6 amendment 3: messages with ts == since_ts MUST
    NOT be re-emitted when only since_ts is supplied."""
    conn, t = topic
    shared_ts = "2026-01-15T12:00:00Z"
    _seed_three_at_same_ts(conn, t, shared_ts)
    out = read_messages(
        conn, topic_id=t, role="EXECUTOR",
        since_ts=shared_ts, kind_filter=["STATUS"],
    )
    assert out["count"] == 0


def test_debate_signal_check_since_ts_strict_exclusive(topic):
    """Same exclusivity contract as read_messages, applied via the
    m.-aliased branch in debate_signal_check."""
    conn, t = topic
    shared_ts = "2026-01-15T12:00:00Z"
    _seed_three_at_same_ts(conn, t, shared_ts)
    out = debate_signal_check(
        conn, session_id="cc-exec1", role="EXECUTOR",
        topic_id=t, since_ts=shared_ts,
    )
    assert out["count"] == 0


def test_read_messages_since_ts_past_returns_all(topic):
    """Sanity: a past since_ts STILL returns everything; the strict-
    exclusive change is only at the boundary."""
    conn, t = topic
    shared_ts = "2026-01-15T12:00:00Z"
    _seed_three_at_same_ts(conn, t, shared_ts)
    out = read_messages(
        conn, topic_id=t, role="EXECUTOR",
        since_ts="2024-01-01T00:00:00Z", kind_filter=["STATUS"],
    )
    assert out["count"] == 3


def test_compound_cursor_still_inclusive_via_since_msg_id(topic):
    """When since_msg_id supplied (compound cursor active), the OR
    branch fires and intra-ts ordering is preserved — NOT regressed
    by the bare 'ts > ?' branch."""
    conn, t = topic
    shared_ts = "2026-01-15T12:00:00Z"
    msg_ids = _seed_three_at_same_ts(conn, t, shared_ts)
    msg_ids_sorted = sorted(msg_ids)
    out = read_messages(
        conn, topic_id=t, role="EXECUTOR",
        since_msg_id=msg_ids_sorted[0], kind_filter=["STATUS"],
    )
    # Two messages at the SAME ts but lex-greater msg_id should still
    # come back via the compound branch.
    assert out["count"] == 2


# ═══════════════════════════════════════════════════════════════════════
# Test 3: msg_id 8|12 width validator
# ═══════════════════════════════════════════════════════════════════════


def test_msg_id_validator_accepts_legacy_8_char():
    """v3.9.0–v3.9.2 wrote 8-char IDs; the widened regex MUST keep
    them valid for backward compatibility."""
    for legacy in (
        "aabbccdd", "deadbeef", "12345678", "a8f3c192", "00000000",
        "ffffffff",
    ):
        assert MSG_ID_RE.fullmatch(legacy) is not None
        validate_msg_id(legacy)  # does not raise


def test_msg_id_validator_accepts_new_12_char():
    """v3.9.3+ writes 12-char IDs via secrets.token_hex(6)."""
    for new in (
        "aabbccdd1111", "deadbeefcafe", "0123456789ab",
        "ffffffff0000", "fedcba987654",
    ):
        assert MSG_ID_RE.fullmatch(new) is not None
        validate_msg_id(new)


def test_msg_id_validator_rejects_other_widths():
    """10-char or 16-char hex must NOT validate — the migration
    contract is exact-8 OR exact-12."""
    for bad in (
        "aabbccdd11", "aabbccdd111", "aabbccdd11111",
        "aabbccdd11111111", "aabb", "",
    ):
        with pytest.raises(DebateError):
            validate_msg_id(bad)


def test_new_msg_id_generates_12_char():
    """Generator emits 12 lowercase hex chars (secrets.token_hex(6))."""
    for _ in range(20):
        mid = new_msg_id()
        assert len(mid) == 12
        assert all(c in "0123456789abcdef" for c in mid)
        assert MSG_ID_RE.fullmatch(mid) is not None


# ═══════════════════════════════════════════════════════════════════════
# Test 4: deterministic forced-collision retry
# ═══════════════════════════════════════════════════════════════════════


def test_msg_id_collision_retry_deterministic(topic, monkeypatch):
    """Verify post_message's collision-retry loop deterministically
    consumes the pre-seeded IDs and emits the first non-colliding ID.

    Per msg:3d3442cb amendment 2C: probabilistic tests give statistical
    confidence but no guarantee. This test forces the retry path by
    monkey-patching the generator.
    """
    conn, t = topic
    fake_sequence = [
        "aabbccdd1111", "aabbccdd2222", "aabbccdd3333", "aabbccdd9999",
    ]
    # Pre-seed 3 of those into debate_messages to force collisions.
    for mid in fake_sequence[:3]:
        conn.execute(
            "INSERT INTO debate_messages "
            "(msg_id, topic_id, role, ts, priority, kind, body, created_at) "
            "VALUES (?, ?, 'CONDUCTOR', ?, 'INFO', 'STATUS', ?, ?)",
            (mid, t, "2026-01-15T12:00:00Z", "seed", "2026-01-15T12:00:00Z"),
        )
    seen = iter(fake_sequence)
    monkeypatch.setattr(debate, "new_msg_id", lambda: next(seen))

    out = post_message(
        conn, topic_id=t, role="CONDUCTOR",
        priority="M", kind="STATUS", body="retry-test",
    )
    # The retry loop must consume the 3 collisions and return the
    # fresh ID at index 3.
    assert out["msg_id"] == "aabbccdd9999"


def test_msg_id_probabilistic_smoke():
    """Secondary smoke: 10000 fresh generations must have zero
    collisions (statistical regression check on entropy)."""
    seen = set()
    for _ in range(10000):
        mid = new_msg_id()
        assert mid not in seen, mid
        seen.add(mid)


# ═══════════════════════════════════════════════════════════════════════
# Test 5: _validate_recipient(debate=None) signature backcompat
# ═══════════════════════════════════════════════════════════════════════


def test_validate_recipient_legacy_path_fetches_debate(topic):
    """When ``debate=None`` (legacy callers), the helper still SELECTs
    roles_json — preserved for backward compatibility."""
    conn, t = topic
    # Should not raise — CONDUCTOR is a declared role.
    _validate_recipient("CONDUCTOR", t, conn)
    _validate_recipient("CONDUCTOR", t, conn, debate=None)


def test_validate_recipient_passthrough_path_skips_select(topic):
    """When ``debate=<dict>`` is supplied, the helper MUST NOT issue a
    SELECT roles_json. Verified by deleting the topic row from the DB
    AFTER caching the debate dict — the pass-through path keeps
    working (proving it consumed the cached dict), while the legacy
    path (debate=None) raises topic_not_found.

    Connection.execute can't be monkey-patched in CPython's sqlite3
    module (read-only attribute); this DB-state proof is equivalent
    and more direct.
    """
    conn, t = topic
    debate_dict = debate.get_debate(conn, t)
    # Cascade delete the topic row; messages/recipients go too.
    conn.execute("DELETE FROM debates WHERE topic_id = ?", (t,))
    # Pass-through path: succeeds because debate_dict is consumed.
    _validate_recipient("CONDUCTOR", t, conn, debate=debate_dict)
    _validate_recipient("EXECUTOR", t, conn, debate=debate_dict)
    _validate_recipient("cc-cond1", t, conn, debate=debate_dict)
    # Legacy path: fetches fresh, sees no topic, raises.
    with pytest.raises(DebateError) as exc_info:
        _validate_recipient("CONDUCTOR", t, conn)
    assert exc_info.value.error_type == "topic_not_found"


def test_validate_recipient_truncates_long_recipient_in_error(topic):
    """Caller-supplied recipient strings can be arbitrary length; the
    error message MUST cap them at 64 chars to bound DoS / log flood."""
    conn, t = topic
    long_bad = "X" * 500  # uppercase, no dash → looks_like_role path
    with pytest.raises(DebateError) as exc_info:
        _validate_recipient(long_bad, t, conn)
    msg = str(exc_info.value)
    # The truncated repr should appear; the full 500-char string should NOT.
    assert "X" * 64 not in msg or len(msg) < 200
    # More precise: 500 X's would explode the message; 64 is the cap.
    assert msg.count("X") <= 70  # 64 + slack for repr quotes


# ═══════════════════════════════════════════════════════════════════════
# Test 6: wrapper-scoped race-storm for signal_advance
# ═══════════════════════════════════════════════════════════════════════


def test_signal_advance_race_storm_under_get_conn_immediate(wrapped_topic):
    """N threads × M iterations racing signal_advance via the
    BEGIN IMMEDIATE wrapper. Final cursor MUST be the strictly-newer
    msg_id; all observed errors MUST be either watermark_regression
    (designed by spec) OR SQLITE_BUSY-bubble (acceptable under heavy
    contention; real adapters retry).
    """
    db = wrapped_topic
    msg_ids: list[str] = []
    with get_conn_immediate(db_path=db) as conn:
        for i in range(4):
            out = debate_post_with_recipients(
                conn, topic_id="X1", role="CONDUCTOR",
                priority="M", kind="STATUS", body=f"m{i}",
                addressed_to=["EXECUTOR"],
            )
            msg_ids.append(out["msg_id"])

    unexpected: list[str] = []

    def race(target_msg_id: str):
        for _ in range(8):
            try:
                with get_conn_immediate(db_path=db) as conn:
                    debate_signal_advance(
                        conn, session_id="cc-exec1", role="EXECUTOR",
                        topic_id="X1",
                        last_processed_msg_id=target_msg_id,
                    )
            except DebateError as exc:
                if exc.error_type == "watermark_regression":
                    break  # racer is permanently stale; stop polling
                unexpected.append(f"{target_msg_id}: {exc!r}")
                break
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower():
                    unexpected.append(f"{target_msg_id}: {exc!r}")
                    break
                # acceptable — retry next iteration

    threads = [threading.Thread(target=race, args=(mid,)) for mid in msg_ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert unexpected == [], unexpected

    # Final cursor MUST be the strictly-latest message in the topic.
    with get_conn(db_path=db) as conn:
        winner = conn.execute(
            "SELECT msg_id FROM debate_messages "
            "WHERE topic_id = ? AND kind = 'STATUS' "
            "ORDER BY ts DESC, msg_id DESC LIMIT 1",
            ("X1",),
        ).fetchone()
        final = conn.execute(
            "SELECT last_processed_msg_id FROM debate_signal_state "
            "WHERE session_id = ? AND role = ? AND topic_id = ?",
            ("cc-exec1", "EXECUTOR", "X1"),
        ).fetchone()
    assert final is not None
    assert final["last_processed_msg_id"] == winner["msg_id"]


# ═══════════════════════════════════════════════════════════════════════
# Bonus: body.strip() validation
# ═══════════════════════════════════════════════════════════════════════


def test_post_message_rejects_whitespace_only_body(topic):
    """Per msg:76e96a96 P2.5: a body that .strip()s to empty has no
    semantic content and is rejected like an empty string."""
    conn, t = topic
    for ws in ("   ", "\t\n", " ", " \n\t \n "):
        with pytest.raises(DebateError, match="invalid_body"):
            post_message(
                conn, topic_id=t, role="CONDUCTOR",
                priority="M", kind="STATUS", body=ws,
            )


def test_post_message_accepts_body_with_internal_whitespace(topic):
    """Whitespace-only is rejected; whitespace AROUND content is OK."""
    conn, t = topic
    out = post_message(
        conn, topic_id=t, role="CONDUCTOR",
        priority="M", kind="STATUS", body="  ok  ",
    )
    assert "msg_id" in out
