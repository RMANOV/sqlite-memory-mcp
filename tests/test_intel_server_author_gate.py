"""Public author gate (reply ownership, contract a784b6952429 item C1).

Every public MCP writer tool must require ``author_session_id`` — the DAO's
bare-ledger compat path (single-binding resolution / unattributed rows) must
not be reachable through MCP.  Tools are exercised through their real
``_db_tool`` envelope against a temporary DB.
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
from debate import bind_role_session, init_debate, transition_state  # noqa: E402
from schema import init_db  # noqa: E402

TOPIC = "GATE1"
CONDUCTOR = "codex-cond1"
EXECUTOR = "codex-exec1"


@pytest.fixture
def gate_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "gate.db")
    init_db(db_path)
    seed = sqlite3.connect(db_path, isolation_level=None)
    seed.row_factory = sqlite3.Row
    seed.execute("PRAGMA foreign_keys = ON")
    init_debate(
        seed,
        topic_id=TOPIC,
        title="author gate",
        roles=[
            {"role": "CONDUCTOR", "session_id": CONDUCTOR},
            {"role": "EXECUTOR", "session_id": EXECUTOR},
        ],
        created_by_role="CONDUCTOR",
    )
    transition_state(seed, topic_id=TOPIC, role="CONDUCTOR", new_state="ACTIVE")
    for role, sid in (("CONDUCTOR", CONDUCTOR), ("EXECUTOR", EXECUTOR)):
        bind_role_session(
            seed, topic_id=TOPIC, role=role, session_id=sid, reason="primary"
        )
    seed.close()

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
    yield db_path


def _rows(db_path):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM debate_messages WHERE topic_id = ?", (TOPIC,)
        ).fetchone()[0]
    finally:
        conn.close()


def _provenance(db_path, msg_id):
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT author_session_id, provenance_class FROM debate_messages "
            "WHERE msg_id = ?",
            (msg_id,),
        ).fetchone()
        return tuple(row)
    finally:
        conn.close()


def _post_q(db_path):
    out = json.loads(
        intel_server.debate_post_with_recipients(
            topic_id=TOPIC,
            role="CONDUCTOR",
            priority="H",
            kind="Q",
            body="question",
            addressed_to_csv="EXECUTOR",
            author_session_id=CONDUCTOR,
        )
    )
    assert "msg_id" in out, out
    return out["msg_id"]


OODA_BODY = "OBSERVE: x\nORIENT: y\nDECIDE: z\nACT: w"


@pytest.mark.parametrize(
    "tool,kwargs",
    [
        (
            "debate_post",
            dict(
                topic_id=TOPIC, role="EXECUTOR", priority="H", kind="STATUS", body="x"
            ),
        ),
        (
            "debate_post_with_recipients",
            dict(
                topic_id=TOPIC,
                role="EXECUTOR",
                priority="H",
                kind="STATUS",
                body="x",
                addressed_to_csv="CONDUCTOR",
            ),
        ),
        ("debate_state", dict(topic_id=TOPIC, role="CONDUCTOR", new_state="RESOLVED")),
        ("debate_escalate", dict(topic_id=TOPIC, role="CONDUCTOR", reason="help")),
        ("debate_compact", dict(topic_id=TOPIC, role="CONDUCTOR", body=OODA_BODY)),
        (
            "debate_close_topic",
            dict(topic_id=TOPIC, role="CONDUCTOR", new_state="RESOLVED"),
        ),
    ],
)
def test_public_writer_without_author_is_rejected_with_zero_rows(gate_db, tool, kwargs):
    before = _rows(gate_db)

    out = json.loads(getattr(intel_server, tool)(**kwargs))

    assert out.get("error_type") == "author_session_required", out
    assert _rows(gate_db) == before


def test_advance_watermark_without_author_is_rejected(gate_db):
    msg_id = _post_q(gate_db)
    before = _rows(gate_db)

    out = json.loads(
        intel_server.debate_advance_watermark(
            topic_id=TOPIC, role="EXECUTOR", processed_up_to_msg_id=msg_id
        )
    )

    assert out.get("error_type") == "author_session_required", out
    assert _rows(gate_db) == before


def test_public_writer_with_bound_author_is_attributed(gate_db):
    msg_id = _post_q(gate_db)

    out = json.loads(
        intel_server.debate_post(
            topic_id=TOPIC,
            role="EXECUTOR",
            priority="H",
            kind="A",
            body="answer",
            reply_to=msg_id,
            author_session_id=EXECUTOR,
        )
    )

    assert "msg_id" in out, out
    assert _provenance(gate_db, out["msg_id"]) == (EXECUTOR, "parent")


def test_public_writer_with_unknown_author_is_rejected(gate_db):
    msg_id = _post_q(gate_db)

    out = json.loads(
        intel_server.debate_post(
            topic_id=TOPIC,
            role="EXECUTOR",
            priority="H",
            kind="A",
            body="answer",
            reply_to=msg_id,
            author_session_id="cc-outsider9999",
        )
    )

    assert out.get("error_type") == "ROLE_UNAVAILABLE", out


def test_no_public_tool_exposes_internal_unattributed():
    for name in (
        "debate_post",
        "debate_post_with_recipients",
        "debate_state",
        "debate_escalate",
        "debate_compact",
        "debate_advance_watermark",
        "debate_close_topic",
    ):
        import inspect

        params = inspect.signature(getattr(intel_server, name)).parameters
        assert "internal_unattributed" not in params, name
        assert "author_session_id" in params, name


def test_resolve_then_archive_by_the_same_conductor_is_attributed(gate_db):
    """Refutation round 2 (major): RESOLVED retires the role bindings in the
    same transaction, so the conductor that just resolved the topic must still
    be accepted as the lifecycle holder for the ARCHIVED step — through the
    public gate, one step, attributed as parent."""
    resolved = json.loads(
        intel_server.debate_state(
            topic_id=TOPIC,
            role="CONDUCTOR",
            new_state="RESOLVED",
            author_session_id=CONDUCTOR,
        )
    )
    assert resolved.get("new_state") == "RESOLVED", resolved
    assert resolved.get("retired_bindings", 0) >= 1

    for tool in ("debate_close_topic", "debate_state"):
        out = json.loads(
            getattr(intel_server, tool)(
                topic_id=TOPIC,
                role="CONDUCTOR",
                new_state="ARCHIVED",
                author_session_id=CONDUCTOR,
            )
        )
        if tool == "debate_close_topic":
            assert out.get("new_state") == "ARCHIVED", out
            assert _provenance(gate_db, out["transition_msg_id"]) == (
                CONDUCTOR,
                "parent",
            )
        else:
            # second call: already ARCHIVED — must not be an authorization error
            assert out.get("error_type") != "ROLE_UNAVAILABLE", out


def test_archive_by_an_outsider_or_other_role_holder_is_still_rejected(gate_db):
    json.loads(
        intel_server.debate_state(
            topic_id=TOPIC,
            role="CONDUCTOR",
            new_state="RESOLVED",
            author_session_id=CONDUCTOR,
        )
    )
    for author in ("cc-outsider9999", EXECUTOR):
        out = json.loads(
            intel_server.debate_close_topic(
                topic_id=TOPIC,
                role="CONDUCTOR",
                new_state="ARCHIVED",
                author_session_id=author,
            )
        )
        assert out.get("error_type") in (
            "ROLE_UNAVAILABLE",
            "provenance_unresolvable",
        ), out
