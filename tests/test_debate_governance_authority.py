"""C3 A1 roster contract through the real public wrappers.

The retired-role case is intentional RED until the production gate is added.
All data and session identifiers below are synthetic; only temporary DBs are used.
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import intel_server  # noqa: E402
from schema import init_db  # noqa: E402

EXECUTOR_SESSION = "codex-c3a1exec01"
RECIPIENT_SESSION = "codex-c3a1exec02"
SNAPSHOT_TABLES = (
    "debates",
    "debate_role_bindings",
    "debate_messages",
    "debate_message_recipients",
    "debate_worker_claims",
)


@pytest.fixture
def governance_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "governance.db")
    init_db(db_path)

    @contextlib.contextmanager
    def factory():
        conn = sqlite3.connect(db_path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    monkeypatch.setattr(intel_server, "_get_conn", factory)
    monkeypatch.setattr(intel_server, "_get_conn_immediate", factory)
    monkeypatch.setattr(
        intel_server, "_signal_wake_after_commit", lambda: None, raising=False
    )
    for name in (
        "SQLITE_MEMORY_DEBATE_GATE_ENABLED",
        "SQLITE_MEMORY_DEBATE_GATE_DISABLED",
    ):
        monkeypatch.delenv(name, raising=False)
    return db_path


def _snapshot(db_path):
    """Read every row of the fixed allowlist; recipient rows need no topic_id."""
    conn = sqlite3.connect(db_path)
    try:
        return {
            table: sorted(
                (tuple(row) for row in conn.execute(f"SELECT * FROM {table}")),
                key=repr,
            )
            for table in SNAPSHOT_TABLES
        }
    finally:
        conn.close()


def _init(topic_id, roles):
    return json.loads(
        intel_server.debate_init(
            topic_id=topic_id,
            title="synthetic roster contract",
            roles_json=json.dumps(roles),
            created_by_role="EXECUTOR_1",
            metadata_json=json.dumps(
                {"priority_lane": "P2", "priority_reason": "synthetic roster test"}
            ),
        )
    )


def test_new_roster_rejects_retired_conductor_without_rows(governance_db):
    before = _snapshot(governance_db)
    assert all(not rows for rows in before.values()), before
    out = _init(
        "C3A1BAD",
        [
            {"role": "EXECUTOR_1", "session_id": EXECUTOR_SESSION},
            {"role": "EXECUTOR_2", "session_id": RECIPIENT_SESSION},
            {"role": "CONDUCTOR", "session_id": "codex-c3a1old01"},
        ],
    )
    after = _snapshot(governance_db)
    assert out.get("error_type") == "role_retired", {
        "actual_outcome": out,
        "before": before,
        "after": after,
    }
    assert after == before


def test_numbered_executor_legacy_topic_stays_available(governance_db):
    roles = [
        {"role": "ADVOCATE_CODEX", "session_id": "codex-c3a1adv01"},
        {"role": "EXECUTOR_1", "session_id": EXECUTOR_SESSION},
        {"role": "EXECUTOR_2", "session_id": RECIPIENT_SESSION},
    ]
    created = _init("C3A1GOOD", roles)
    assert "error_type" not in created, created
    assert created["roles"] == roles

    conn = sqlite3.connect(governance_db)
    try:
        stored = conn.execute(
            "SELECT roles_json FROM debates WHERE topic_id = ?", ("C3A1GOOD",)
        ).fetchone()
        assert stored is not None
        assert json.loads(stored[0]) == roles
        bindings = conn.execute(
            "SELECT role, session_id, state FROM debate_role_bindings "
            "WHERE topic_id = ?",
            ("C3A1GOOD",),
        ).fetchall()
        assert set(bindings) == {
            (entry["role"], entry["session_id"], "active") for entry in roles
        }
    finally:
        conn.close()

    activated = json.loads(
        intel_server.debate_state(
            topic_id="C3A1GOOD",
            role="EXECUTOR_1",
            new_state="ACTIVE",
            reason="synthetic availability guard",
            author_session_id=EXECUTOR_SESSION,
        )
    )
    assert "error_type" not in activated, activated
    conn = sqlite3.connect(governance_db)
    try:
        assert conn.execute(
            "SELECT state FROM debates WHERE topic_id = ?", ("C3A1GOOD",)
        ).fetchone() == ("ACTIVE",)
    finally:
        conn.close()

    body = "synthetic ordinary legacy availability"
    posted = json.loads(
        intel_server.debate_post_with_recipients(
            topic_id="C3A1GOOD",
            role="EXECUTOR_1",
            author_session_id=EXECUTOR_SESSION,
            priority="M",
            kind="STATUS",
            body=body,
            addressed_to_csv="EXECUTOR_2",
        )
    )
    assert "msg_id" in posted, posted
    readback = json.loads(
        intel_server.debate_read(topic_id="C3A1GOOD", role="EXECUTOR_2")
    )
    assert "error_type" not in readback, readback
    messages = [
        row for row in readback["messages"] if row["msg_id"] == posted["msg_id"]
    ]
    assert len(messages) == 1, readback
    assert messages[0]["body"] == body
    assert messages[0]["author_session_id"] == EXECUTOR_SESSION
