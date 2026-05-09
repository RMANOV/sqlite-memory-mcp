"""Test 44: paranoid socket-blocked LLM-free proof.

Per CONDUCTOR 2026-05-09T16:35 EEST EXECUTOR INSTRUCTION test plan +
LLM-free differentiator from MemoryReflection_LLMFreeArchitecture KG
entity. The debate protocol must not open any network sockets in the
hot path. Empirical proof: monkey-patch socket.socket to raise on every
attempt, then exercise init/post/read/state/escalate/compact.
"""

from __future__ import annotations

import os
import socket
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from debate import (
    compact,
    escalate,
    init_debate,
    post_message,
    read_messages,
    transition_state,
)
from schema import init_db


@pytest.fixture
def db(tmp_path):
    db_path = str(tmp_path / "debate_paranoid.db")
    init_db(db_path)
    c = sqlite3.connect(db_path, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    yield c
    c.close()


def test_paranoid_socket_blocked_full_lifecycle(db):
    """Full v2 lifecycle with socket.socket raising on every attempt.

    Proves no LLM/network call hidden in any hot-path code: init →
    transition → post (Q + A + STATE + WATERMARK + COMPACTION) →
    escalate → read all complete with zero socket attempts.
    """
    orig_socket = socket.socket
    attempts: list = []

    def tripwire(*a, **kw):
        attempts.append(a)
        raise OSError("network blocked by paranoid test")

    socket.socket = tripwire
    try:
        init_debate(
            db, topic_id="PARANOID_LIFECYCLE", title="no network",
            roles=[
                {"role": "CONDUCTOR", "session_id": "s-c"},
                {"role": "EXECUTOR", "session_id": "s-e"},
                {"role": "ADVOCATE", "session_id": "s-a"},
            ],
            created_by_role="CONDUCTOR",
        )
        transition_state(
            db, topic_id="PARANOID_LIFECYCLE", role="CONDUCTOR",
            new_state="ACTIVE",
        )
        q = post_message(
            db, topic_id="PARANOID_LIFECYCLE", role="ADVOCATE",
            priority="H", kind="Q", body="open question",
        )
        post_message(
            db, topic_id="PARANOID_LIFECYCLE", role="EXECUTOR",
            priority="H", kind="A", body="answered", reply_to=q["msg_id"],
        )
        post_message(
            db, topic_id="PARANOID_LIFECYCLE", role="EXECUTOR",
            priority="INFO", kind="WATERMARK", body=q["msg_id"],
        )
        compact(
            db, topic_id="PARANOID_LIFECYCLE", role="ADVOCATE",
            body=(
                "OBSERVE: q+a closed.\nORIENT: ready to resolve.\n"
                "DECIDE: transition to RESOLVED.\nACT: caller transitions."
            ),
        )
        escalate(
            db, topic_id="PARANOID_LIFECYCLE", role="EXECUTOR",
            reason="test-only",
        )
        read_messages(
            db, topic_id="PARANOID_LIFECYCLE", role="EXECUTOR",
        )
        transition_state(
            db, topic_id="PARANOID_LIFECYCLE", role="CONDUCTOR",
            new_state="RESOLVED",
        )
        transition_state(
            db, topic_id="PARANOID_LIFECYCLE", role="CONDUCTOR",
            new_state="ARCHIVED",
        )
    finally:
        socket.socket = orig_socket

    assert attempts == [], f"unexpected socket attempts: {len(attempts)}"


def test_paranoid_no_network_imports_in_debate_module():
    import importlib

    forbidden = ("requests", "httpx", "urllib3", "anthropic", "openai")
    m = importlib.import_module("debate")
    for f in forbidden:
        assert f not in m.__dict__, f"debate.py references {f}"
