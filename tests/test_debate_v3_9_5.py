"""v3.9.5 — STATE-body reason fold + strict regex validation.

Per CONDUCTOR canonical msg:c5e2e575 (original Fix A) + resolution
msg:2c22988a (strict regex form, option (b)).

Bug: ``debate_state(topic, new_state=RESOLVED, reason=...)`` failed
with ``topic_resolved_read_only`` because the dual-record pattern
attempted a separate STATUS write AFTER the state flip; once the
topic entered RESOLVED, the STATUS write was blocked by the
read-only gate. The reason text never persisted.

Fix: fold the reason into the STATE body itself (single-row design),
remove the separate STATUS post entirely, and add a ``_STATE_BODY_RE``
fullmatch guard in ``post_message`` so the canonical body shape is
strictly enforced (rejects prefix-junk, empty reasons, multi-line
content — the same prefix-acceptance defense as v3.9.3 WATERMARK
parser fixup msg:b246664b).

Two categories of test:
  - 4 functional regression tests covering the original spec P2
    (reason persisted in STATE body across all transitions; no
    deprecated separate STATUS row)
  - 6 strict-regex validation tests covering each REJECT case
    enumerated in the resolution DECISION (msg:2c22988a CONTRACT)
"""

from __future__ import annotations

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from debate import (
    DebateError,
    _STATE_BODY_RE,
    init_debate,
    post_message,
    transition_state,
)
from schema import init_db


@pytest.fixture
def topic(tmp_path):
    db = str(tmp_path / "v3_9_5.db")
    init_db(db)
    c = sqlite3.connect(db, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    init_debate(
        c, topic_id="X1", title="v3.9.5 lifecycle test",
        roles=[
            {"role": "CONDUCTOR", "session_id": "cc-cond1"},
            {"role": "EXECUTOR", "session_id": "cc-exec1"},
        ],
        created_by_role="CONDUCTOR",
    )
    yield c, "X1"
    c.close()


# ════════════════════════════════════════════════════════════════════
# 4 functional regression tests (msg:c5e2e575 P2)
# ════════════════════════════════════════════════════════════════════


def test_state_transition_with_reason_persists_in_state_body(topic):
    """ACTIVE transition carries the reason in the STATE body itself,
    NOT in a separate STATUS row. Pre-fix the reason was silently
    dropped; the STATE row stored only ``ACTIVE``."""
    conn, t = topic
    result = transition_state(
        conn, topic_id=t, role="CONDUCTOR",
        new_state="ACTIVE", reason="kickoff",
    )
    assert result["new_state"] == "ACTIVE"
    assert result["body"] == "ACTIVE [reason: kickoff]"

    # Persisted row matches the return value exactly.
    row = conn.execute(
        "SELECT body, kind FROM debate_messages WHERE msg_id = ?",
        (result["transition_msg_id"],),
    ).fetchone()
    assert row["kind"] == "STATE"
    assert row["body"] == "ACTIVE [reason: kickoff]"

    # No separate STATUS row carrying the deprecated dual-record literal.
    deprecated_count = conn.execute(
        "SELECT COUNT(*) FROM debate_messages "
        "WHERE body LIKE 'state transition reason:%'"
    ).fetchone()[0]
    assert deprecated_count == 0


def test_resolved_transition_with_reason_succeeds(topic):
    """Pre-fix this raised topic_resolved_read_only — the very bug
    ADVOCATE flagged in msg:dd9db924. The dual-record STATUS write
    landed AFTER the state flip and got rejected by the read-only
    gate. Post-fix: single STATE row, no follow-up write, succeeds."""
    conn, t = topic
    transition_state(
        conn, topic_id=t, role="CONDUCTOR", new_state="ACTIVE",
    )
    result = transition_state(
        conn, topic_id=t, role="CONDUCTOR",
        new_state="RESOLVED", reason="all_questions_answered",
    )
    assert result["new_state"] == "RESOLVED"
    assert result["body"] == "RESOLVED [reason: all_questions_answered]"
    row = conn.execute(
        "SELECT body FROM debate_messages WHERE msg_id = ?",
        (result["transition_msg_id"],),
    ).fetchone()
    assert row["body"] == "RESOLVED [reason: all_questions_answered]"


def test_archived_transition_with_reason_succeeds(topic):
    """Same failure mode as RESOLVED — ARCHIVED is also a read-only
    terminal state, so the deprecated dual-record path hit it too."""
    conn, t = topic
    transition_state(
        conn, topic_id=t, role="CONDUCTOR", new_state="ACTIVE",
    )
    transition_state(
        conn, topic_id=t, role="CONDUCTOR", new_state="RESOLVED",
    )
    result = transition_state(
        conn, topic_id=t, role="CONDUCTOR",
        new_state="ARCHIVED", reason="retention_complete",
    )
    assert result["new_state"] == "ARCHIVED"
    assert result["body"] == "ARCHIVED [reason: retention_complete]"
    row = conn.execute(
        "SELECT body FROM debate_messages WHERE msg_id = ?",
        (result["transition_msg_id"],),
    ).fetchone()
    assert row["body"] == "ARCHIVED [reason: retention_complete]"


def test_no_reason_transitions_unchanged(topic):
    """Legacy bare-state transitions (no reason supplied) MUST still
    persist the plain state name — the v3.9.5 fix is additive to the
    body shape, not a breaking change for callers that omit reason."""
    conn, t = topic
    r1 = transition_state(
        conn, topic_id=t, role="CONDUCTOR", new_state="ACTIVE",
    )
    assert r1["body"] == "ACTIVE"
    row1 = conn.execute(
        "SELECT body FROM debate_messages WHERE msg_id = ?",
        (r1["transition_msg_id"],),
    ).fetchone()
    assert row1["body"] == "ACTIVE"

    r2 = transition_state(
        conn, topic_id=t, role="CONDUCTOR", new_state="RESOLVED",
    )
    assert r2["body"] == "RESOLVED"
    row2 = conn.execute(
        "SELECT body FROM debate_messages WHERE msg_id = ?",
        (r2["transition_msg_id"],),
    ).fetchone()
    assert row2["body"] == "RESOLVED"


# ════════════════════════════════════════════════════════════════════
# 6 strict-regex validation tests (msg:2c22988a CONTRACT)
# ════════════════════════════════════════════════════════════════════


def test_state_body_rejects_prefix_junk(topic):
    """``ACTIVE trailing-junk`` must NOT pass — defends against the
    same prefix-acceptance class as v3.9.3 WATERMARK parser fixup
    (msg:b246664b). A naive leading-token parse would silently
    swallow the trailing chars; fullmatch refuses."""
    conn, t = topic
    with pytest.raises(DebateError) as exc_info:
        post_message(
            conn, topic_id=t, role="CONDUCTOR",
            priority="H", kind="STATE",
            body="ACTIVE trailing-junk",
        )
    assert exc_info.value.error_type == "invalid_state"


def test_state_body_rejects_empty_reason(topic):
    """``ACTIVE [reason:]`` is structurally wrong — the ``.+`` in the
    regex requires at least one char of reason content."""
    conn, t = topic
    with pytest.raises(DebateError) as exc_info:
        post_message(
            conn, topic_id=t, role="CONDUCTOR",
            priority="H", kind="STATE",
            body="ACTIVE [reason:]",
        )
    assert exc_info.value.error_type == "invalid_state"


def test_state_body_rejects_wrong_bracket_key(topic):
    """Only ``[reason: ...]`` is accepted as the enrichment shape.
    ``[transition_id: ...]`` or any other key fails fullmatch — keeps
    the storage shape grep-stable."""
    conn, t = topic
    with pytest.raises(DebateError) as exc_info:
        post_message(
            conn, topic_id=t, role="CONDUCTOR",
            priority="H", kind="STATE",
            body="ACTIVE [transition_id: x]",
        )
    assert exc_info.value.error_type == "invalid_state"


def test_state_body_rejects_typo_state_name(topic):
    """``AKTIVE [reason: x]`` — state-name typos must be rejected
    even when the reason suffix is well-formed. The alternation
    group locks the state-name set to VALID_STATES."""
    conn, t = topic
    with pytest.raises(DebateError) as exc_info:
        post_message(
            conn, topic_id=t, role="CONDUCTOR",
            priority="H", kind="STATE",
            body="AKTIVE [reason: kickoff]",
        )
    assert exc_info.value.error_type == "invalid_state"


def test_state_body_rejects_multi_line_reason(topic):
    """``.+`` excludes newlines by default — multi-line reasons would
    fan out across log surfaces unpredictably; rejecting them keeps
    each STATE transition a single grep-able row."""
    conn, t = topic
    with pytest.raises(DebateError) as exc_info:
        post_message(
            conn, topic_id=t, role="CONDUCTOR",
            priority="H", kind="STATE",
            body="ACTIVE [reason: line1\nline2]",
        )
    assert exc_info.value.error_type == "invalid_state"


def test_state_body_regex_canonical_and_inverse_shapes():
    """Positive coverage for the 8 accepted shapes (4 bare + 4 with
    reason) plus a clear set of NEGATIVE inverse shapes — exercises
    ``_STATE_BODY_RE`` directly so the contract is tested
    independently of post_message's transition-validation side
    effects.
    """
    # Accept: bare states + states with single-line reasons.
    for state in ("INIT", "ACTIVE", "RESOLVED", "ARCHIVED"):
        assert _STATE_BODY_RE.fullmatch(state) is not None, state
        assert (
            _STATE_BODY_RE.fullmatch(f"{state} [reason: brief]")
            is not None
        ), state

    # Reject inverse shapes (case-sensitive, bare-whitespace, missing
    # delimiter). Each illustrates a class of caller error the strict
    # regex catches.
    for bad in (
        "active",                  # lowercase state name
        "Active",                  # mixed case
        "ACTIVE[reason: x]",       # missing space before bracket
        "ACTIVE [reason: x]extra", # trailing content after closer
        "ACTIVE [reason:x]",       # missing space after colon (regex
                                   # requires `[reason: <text>]`)
    ):
        assert _STATE_BODY_RE.fullmatch(bad) is None, f"{bad!r} unexpectedly matched"
