"""Test 45: end-to-end debate lifecycle.

Per CONDUCTOR 2026-05-09T16:35 EEST EXECUTOR INSTRUCTION test plan.
Bootstraps a full debate, exchanges 5 messages with reply chains,
advances watermarks per role, transitions through every state, and
verifies final invariants.
"""

from __future__ import annotations

import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from debate import (
    compact,
    get_debate,
    get_watermark,
    init_debate,
    post_message,
    read_messages,
    transition_state,
)
from schema import init_db


def test_e2e_full_lifecycle(tmp_path):
    db_path = str(tmp_path / "debate_e2e.db")
    init_db(db_path)
    c = sqlite3.connect(db_path, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    try:
        # Init
        init_debate(
            c, topic_id="E2E_DEMO", title="full lifecycle",
            roles=[
                {"role": "CONDUCTOR", "session_id": "sess-c"},
                {"role": "EXECUTOR", "session_id": "sess-e"},
                {"role": "ADVOCATE", "session_id": "sess-a"},
            ],
            created_by_role="CONDUCTOR",
        )
        assert get_debate(c, "E2E_DEMO")["state"] == "INIT"

        # Transition INIT → ACTIVE
        transition_state(
            c, topic_id="E2E_DEMO", role="CONDUCTOR", new_state="ACTIVE",
        )
        assert get_debate(c, "E2E_DEMO")["state"] == "ACTIVE"

        # Exchange: 1 Q (advocate H) + 1 A (executor) + 1 STATUS + 1 DECISION
        q = post_message(
            c, topic_id="E2E_DEMO", role="ADVOCATE",
            priority="H", kind="Q", body="cursor model robust?",
        )
        post_message(
            c, topic_id="E2E_DEMO", role="EXECUTOR",
            priority="H", kind="A", body="compound (ts, msg_id) — yes",
            reply_to=q["msg_id"],
        )
        post_message(
            c, topic_id="E2E_DEMO", role="EXECUTOR",
            priority="INFO", kind="STATUS", body="checkpoint #2 shipped",
        )
        post_message(
            c, topic_id="E2E_DEMO", role="CONDUCTOR",
            priority="H", kind="DECISION", body="proceed to resolve",
            reply_to=q["msg_id"],
        )

        # Compact (OODA)
        compact(
            c, topic_id="E2E_DEMO", role="ADVOCATE",
            body=(
                "OBSERVE: 1 Q answered, 1 STATUS, 1 DECISION.\n"
                "ORIENT: nothing blocks RESOLVED.\n"
                "DECIDE: proceed.\n"
                "ACT: CONDUCTOR transitions next."
            ),
        )

        # Watermark each role to latest
        out = read_messages(c, topic_id="E2E_DEMO", role="EXECUTOR")
        last = out["messages"][-1]["msg_id"]
        post_message(
            c, topic_id="E2E_DEMO", role="EXECUTOR",
            priority="INFO", kind="WATERMARK", body=last,
        )
        post_message(
            c, topic_id="E2E_DEMO", role="ADVOCATE",
            priority="INFO", kind="WATERMARK", body=last,
        )
        post_message(
            c, topic_id="E2E_DEMO", role="CONDUCTOR",
            priority="INFO", kind="WATERMARK", body=last,
        )

        # Verify watermarks advanced
        for role in ("EXECUTOR", "ADVOCATE", "CONDUCTOR"):
            wm = get_watermark(c, "E2E_DEMO", role)
            assert wm is not None
            assert wm["last_processed_msg_id"] == last

        # Transition ACTIVE → RESOLVED (now all Qs answered)
        out = transition_state(
            c, topic_id="E2E_DEMO", role="CONDUCTOR", new_state="RESOLVED",
        )
        assert out["new_state"] == "RESOLVED"
        assert out["blocking_questions"] == []

        # Transition RESOLVED → ARCHIVED
        out = transition_state(
            c, topic_id="E2E_DEMO", role="CONDUCTOR", new_state="ARCHIVED",
        )
        assert out["new_state"] == "ARCHIVED"
        debate = get_debate(c, "E2E_DEMO")
        assert debate["archived_at"] is not None

        # Final assertion: total message count
        msg_count = c.execute(
            "SELECT COUNT(*) FROM debate_messages WHERE topic_id = 'E2E_DEMO'"
        ).fetchone()[0]
        # 1 STATE (INIT→ACTIVE) + Q + A + STATUS + DECISION + COMPACTION
        # + 3 WATERMARKs + 1 STATE (ACTIVE→RESOLVED) + 1 STATE (RESOLVED→ARCHIVED) = 11
        assert msg_count == 11
    finally:
        c.close()
