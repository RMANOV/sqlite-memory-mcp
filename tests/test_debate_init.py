"""Tests 7-12: debate_init happy paths + rejection cases.

Per CONDUCTOR 2026-05-09T16:35 EEST EXECUTOR INSTRUCTION test plan.
"""

from __future__ import annotations

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from debate import DebateError, get_debate, init_debate
from schema import init_db


@pytest.fixture
def conn(tmp_path):
    db_path = str(tmp_path / "debate_init.db")
    init_db(db_path)
    c = sqlite3.connect(db_path, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    yield c
    c.close()


_VALID_ROLES = [
    {"role": "CONDUCTOR", "session_id": "s-cond"},
    {"role": "EXECUTOR", "session_id": "s-exec"},
]


def test_debate_init_creates_INIT_state(conn):
    out = init_debate(
        conn,
        topic_id="X1",
        title="t",
        roles=_VALID_ROLES,
        created_by_role="CONDUCTOR",
    )
    assert out["state"] == "INIT"
    db_row = get_debate(conn, "X1")
    assert db_row["state"] == "INIT"
    assert db_row["created_by_role"] == "CONDUCTOR"
    assert db_row["roles"] == _VALID_ROLES


def test_debate_init_idempotent_same_roles(conn):
    init_debate(
        conn, topic_id="X2", title="t", roles=_VALID_ROLES,
        created_by_role="CONDUCTOR",
    )
    out = init_debate(
        conn, topic_id="X2", title="ignored-on-second-call",
        roles=_VALID_ROLES, created_by_role="CONDUCTOR",
    )
    assert out["topic_id"] == "X2"
    assert get_debate(conn, "X2")["title"] == "t"


def test_debate_init_rejects_different_roles_for_existing_topic(conn):
    init_debate(
        conn, topic_id="X3", title="t", roles=_VALID_ROLES,
        created_by_role="CONDUCTOR",
    )
    other_roles = [{"role": "ADVOCATE", "session_id": "s-adv"}]
    with pytest.raises(DebateError, match="topic_exists_with_different_roles"):
        init_debate(
            conn, topic_id="X3", title="t", roles=other_roles,
            created_by_role="ADVOCATE",
        )


def test_debate_init_rejects_invalid_topic_id(conn):
    with pytest.raises(DebateError, match="invalid_topic_id"):
        init_debate(
            conn, topic_id="lowercase", title="t", roles=_VALID_ROLES,
            created_by_role="CONDUCTOR",
        )


def test_debate_init_rejects_empty_roles(conn):
    with pytest.raises(DebateError, match="invalid_roles"):
        init_debate(
            conn, topic_id="X4", title="t", roles=[],
            created_by_role="CONDUCTOR",
        )


def test_debate_init_rejects_role_without_session_id(conn):
    with pytest.raises(DebateError, match="missing session_id"):
        init_debate(
            conn, topic_id="X5", title="t",
            roles=[{"role": "CONDUCTOR"}],
            created_by_role="CONDUCTOR",
        )


def test_debate_init_rejects_empty_title(conn):
    with pytest.raises(DebateError, match="invalid_title"):
        init_debate(
            conn, topic_id="X6", title="   ", roles=_VALID_ROLES,
            created_by_role="CONDUCTOR",
        )


def test_debate_init_resolve_by_optional(conn):
    out = init_debate(
        conn, topic_id="X7", title="t", roles=_VALID_ROLES,
        created_by_role="CONDUCTOR",
    )
    assert out["resolve_by"] is None
    out2 = init_debate(
        conn, topic_id="X8", title="t", roles=_VALID_ROLES,
        created_by_role="CONDUCTOR",
        resolve_by="2026-05-12T00:00Z",
    )
    assert out2["resolve_by"] == "2026-05-12T00:00Z"


def test_debate_init_rejects_invalid_resolve_by_format(conn):
    with pytest.raises(DebateError, match="invalid_iso_utc"):
        init_debate(
            conn, topic_id="X9", title="t", roles=_VALID_ROLES,
            created_by_role="CONDUCTOR", resolve_by="not-a-date",
        )


def test_debate_init_metadata_round_trip(conn):
    md = {"version": "v3.9.0", "trigger": "weekend code red"}
    init_debate(
        conn, topic_id="X10", title="t", roles=_VALID_ROLES,
        created_by_role="CONDUCTOR", metadata=md,
    )
    assert get_debate(conn, "X10")["metadata"] == md
