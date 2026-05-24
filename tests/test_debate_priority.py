from __future__ import annotations

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from debate import (  # noqa: E402
    DebateError,
    debate_post_with_recipients,
    init_debate,
    list_open_debate_work,
    seed_initial_role_bindings,
    set_topic_priority,
    transition_state,
)
from schema import init_db  # noqa: E402


def _topic(conn: sqlite3.Connection, topic_id: str) -> None:
    roles = [
        {"role": "CONDUCTOR", "session_id": f"codex-cond_{topic_id.lower()}"},
        {"role": "EXECUTOR", "session_id": f"codex-exec_{topic_id.lower()}"},
        {"role": "ADVOCATE", "session_id": f"cc-adv_{topic_id.lower()}"},
    ]
    init_debate(
        conn,
        topic_id=topic_id,
        title=f"{topic_id} priority test",
        roles=roles,
        created_by_role="CONDUCTOR",
    )
    transition_state(conn, topic_id=topic_id, role="CONDUCTOR", new_state="ACTIVE")
    seed_initial_role_bindings(conn, topic_id=topic_id, roles=roles, bound_by_role="CONDUCTOR")


@pytest.fixture
def conn(tmp_path):
    db_path = str(tmp_path / "debate_priority.db")
    init_db(db_path)
    c = sqlite3.connect(db_path, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    _topic(c, "PRIORITY_A")
    _topic(c, "PRIORITY_B")
    yield c
    c.close()


def test_conductor_topic_lane_is_cross_topic_sort_authority(conn):
    debate_post_with_recipients(
        conn,
        topic_id="PRIORITY_A",
        role="ADVOCATE",
        priority="H",
        kind="Q",
        body="high message priority, but no conductor P0 lane",
        addressed_to=["EXECUTOR"],
    )
    set_topic_priority(
        conn,
        topic_id="PRIORITY_B",
        role="CONDUCTOR",
        lane="P0",
        reason="operator safety gate",
        next_action="finish safety gate first",
    )

    queue = list_open_debate_work(conn)

    assert [item["topic_id"] for item in queue["items"][:2]] == [
        "PRIORITY_B",
        "PRIORITY_A",
    ]
    assert queue["items"][0]["lane"] == "P0"
    assert "explicit_conductor_priority" in queue["items"][0]["reason_codes"]
    assert queue["items"][0]["next_action"] == "finish safety gate first"


def test_open_h_question_beats_default_open_topic_without_explicit_lane(conn):
    debate_post_with_recipients(
        conn,
        topic_id="PRIORITY_B",
        role="ADVOCATE",
        priority="H",
        kind="Q",
        body="needs executor answer",
        addressed_to=["EXECUTOR"],
    )

    queue = list_open_debate_work(conn)

    assert queue["items"][0]["topic_id"] == "PRIORITY_B"
    assert queue["items"][0]["lane"] == "P1"
    assert "open_h_question" in queue["items"][0]["reason_codes"]
    assert queue["items"][0]["next_action"].startswith("answer open H Q ")


def test_topic_priority_requires_conductor_role(conn):
    with pytest.raises(DebateError) as exc_info:
        set_topic_priority(
            conn,
            topic_id="PRIORITY_A",
            role="EXECUTOR",
            lane="P0",
            reason="executor cannot own global priority",
        )

    assert exc_info.value.error_type == "topic_priority_requires_conductor"


def test_work_queue_reports_missing_active_binding_as_p1(conn):
    conn.execute(
        "UPDATE debate_role_bindings SET state = 'retired' "
        "WHERE topic_id = 'PRIORITY_A' AND role = 'EXECUTOR'"
    )

    queue = list_open_debate_work(conn, topics=["PRIORITY_A"])

    assert queue["items"][0]["lane"] == "P1"
    assert queue["items"][0]["missing_active_roles"] == ["EXECUTOR"]
    assert "missing_active_role_binding" in queue["items"][0]["reason_codes"]
