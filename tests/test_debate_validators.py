"""Validator + regex unit tests for Debate Protocol v2.

Schema migration is verified here too because it's a precondition for
every downstream debate test.
"""

from __future__ import annotations

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from debate import (
    DebateError,
    MSG_ID_RE,
    VALID_KINDS,
    VALID_PRIORITIES,
    VALID_STATES,
    VALID_TRANSITIONS,
    new_msg_id,
    validate_iso_utc,
    validate_kind,
    validate_priority,
    validate_role,
    validate_state,
    validate_topic_id,
    validate_transition,
)
from schema import init_db


@pytest.fixture
def db(tmp_path):
    db_path = str(tmp_path / "debate_validators.db")
    init_db(db_path)
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


def test_schema_has_three_debate_tables(db):
    rows = db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'debate%'"
    ).fetchall()
    found = {r["name"] for r in rows}
    assert {"debates", "debate_messages", "debate_watermarks"} <= found


def test_schema_has_required_indexes(db):
    rows = db.execute(
        "SELECT name FROM sqlite_master WHERE type='index' "
        "AND (name LIKE 'idx_debates%' OR name LIKE 'idx_debmsg%')"
    ).fetchall()
    names = {r["name"] for r in rows}
    expected = {
        "idx_debates_state",
        "idx_debates_resolve_by",
        "idx_debmsg_topic_ts",
        "idx_debmsg_topic_kind",
        "idx_debmsg_topic_priority",
        "idx_debmsg_topic_role_ts",
        "idx_debmsg_reply_to",
    }
    assert expected <= names, f"missing: {expected - names}"


def test_schema_kind_check_includes_compaction(db):
    sql = db.execute(
        "SELECT sql FROM sqlite_master WHERE name='debate_messages'"
    ).fetchone()["sql"]
    for kind in VALID_KINDS:
        assert f"'{kind}'" in sql


@pytest.mark.parametrize(
    "topic_id",
    ["WEEKEND_CODE_RED_2026_05_09", "X1", "ABC_DEF_GHI", "AB", "ZZ_999_AB"],
)
def test_topic_id_regex_accepts_valid(topic_id):
    validate_topic_id(topic_id)


@pytest.mark.parametrize(
    "bad",
    ["foo", "1ABC", "ABC-DEF", "abc", "", "A", "with space", "Аб"],
)
def test_topic_id_regex_rejects_invalid(bad):
    with pytest.raises(DebateError):
        validate_topic_id(bad)


def test_msg_id_uniqueness_for_1000_generations():
    seen = {new_msg_id() for _ in range(1000)}
    assert len(seen) == 1000
    for s in seen:
        assert MSG_ID_RE.fullmatch(s)


@pytest.mark.parametrize(
    "ts",
    [
        "2026-05-09T16:35Z",
        "2026-05-09T16:35:00Z",
        "2026-05-09T16:35:00.123456Z",
        "2026-12-31T23:59:59Z",
    ],
)
def test_iso_utc_accepts_valid_formats(ts):
    validate_iso_utc(ts)


@pytest.mark.parametrize(
    "bad",
    [
        "2026-05-09 16:35",
        "2026-05-09T16:35",
        "2026-05-09T16:35+00:00",
        "not a timestamp",
        "",
        "2026/05/09T16:35Z",
    ],
)
def test_iso_utc_rejects_bad_formats(bad):
    with pytest.raises(DebateError):
        validate_iso_utc(bad)


@pytest.mark.parametrize("priority", VALID_PRIORITIES)
def test_priority_enum_accepts_all_valid(priority):
    validate_priority(priority)


@pytest.mark.parametrize("bad", ["HIGH", "h", "", "URGENT", " H"])
def test_priority_enum_rejects_invalid(bad):
    with pytest.raises(DebateError):
        validate_priority(bad)


@pytest.mark.parametrize("kind", VALID_KINDS)
def test_kind_enum_accepts_all_eight_valid(kind):
    validate_kind(kind)


def test_kind_enum_includes_compaction():
    assert "COMPACTION" in VALID_KINDS


@pytest.mark.parametrize("bad", ["QUESTION", "answer", "", " Q", "comp"])
def test_kind_enum_rejects_invalid(bad):
    with pytest.raises(DebateError):
        validate_kind(bad)


def test_valid_states_set():
    assert VALID_STATES == ("INIT", "ACTIVE", "RESOLVED", "ARCHIVED")


def test_state_transitions_are_acyclic_and_terminal():
    assert VALID_TRANSITIONS["INIT"] == {"ACTIVE"}
    assert VALID_TRANSITIONS["ACTIVE"] == {"RESOLVED"}
    assert VALID_TRANSITIONS["RESOLVED"] == {"ARCHIVED"}
    assert VALID_TRANSITIONS["ARCHIVED"] == set()


def test_validate_transition_accepts_legal_path():
    validate_transition("INIT", "ACTIVE")
    validate_transition("ACTIVE", "RESOLVED")
    validate_transition("RESOLVED", "ARCHIVED")


@pytest.mark.parametrize(
    "old,new",
    [
        ("INIT", "RESOLVED"),
        ("INIT", "ARCHIVED"),
        ("ACTIVE", "INIT"),
        ("RESOLVED", "ACTIVE"),
        ("ARCHIVED", "ACTIVE"),
        ("ARCHIVED", "INIT"),
    ],
)
def test_validate_transition_rejects_illegal(old, new):
    with pytest.raises(DebateError):
        validate_transition(old, new)


@pytest.mark.parametrize("role", ["CONDUCTOR", "EXECUTOR", "ADVOCATE", "HUMAN"])
def test_role_regex_accepts_valid(role):
    validate_role(role)


@pytest.mark.parametrize("bad", ["conductor", "1ROLE", "role-x", ""])
def test_role_regex_rejects_invalid(bad):
    with pytest.raises(DebateError):
        validate_role(bad)


def test_validate_state_rejects_unknown():
    with pytest.raises(DebateError):
        validate_state("ZOMBIE")
