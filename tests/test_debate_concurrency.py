"""Tests 41-43: concurrency, atomicity, FK cascade.

Per CONDUCTOR 2026-05-09T16:35 EEST EXECUTOR INSTRUCTION test plan.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from debate import init_debate, post_message
from schema import init_db


@pytest.fixture
def db_path(tmp_path):
    p = str(tmp_path / "debate_concurrency.db")
    init_db(p)
    c = sqlite3.connect(p, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    init_debate(
        c, topic_id="X1", title="concurrency-tests",
        roles=[
            {"role": "CONDUCTOR", "session_id": "s-cond"},
            {"role": "EXECUTOR", "session_id": "s-exec"},
            {"role": "ADVOCATE", "session_id": "s-adv"},
            {"role": "HUMAN", "session_id": "s-h"},
        ],
        created_by_role="CONDUCTOR",
    )
    c.close()
    yield p


def test_concurrent_post_isolation_level_none_no_lock_errors(db_path):
    """4 threads × 50 messages each = 200 unique inserts under WAL."""
    errors: list[str] = []
    posted: list[str] = []
    posted_lock = threading.Lock()

    def worker(role: str):
        try:
            c = sqlite3.connect(db_path, isolation_level=None)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA foreign_keys = ON")
            try:
                for i in range(50):
                    out = post_message(
                        c, topic_id="X1", role=role,
                        priority="INFO", kind="STATUS",
                        body=f"{role}-msg{i}",
                    )
                    with posted_lock:
                        posted.append(out["msg_id"])
            finally:
                c.close()
        except Exception as exc:
            errors.append(f"{role}: {exc}")

    threads = [
        threading.Thread(target=worker, args=(r,))
        for r in ("CONDUCTOR", "EXECUTOR", "ADVOCATE", "HUMAN")
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"thread errors: {errors}"
    assert len(posted) == 200
    assert len(set(posted)) == 200, "msg_id uniqueness violated"


def test_post_atomicity_state_invalid_no_persisted_row(db_path):
    """Pre-INSERT validation rejects invalid STATE → no row hits debate_messages."""
    c = sqlite3.connect(db_path, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    try:
        pre = c.execute(
            "SELECT COUNT(*) FROM debate_messages WHERE topic_id = 'X1'"
        ).fetchone()[0]
        try:
            post_message(
                c, topic_id="X1", role="CONDUCTOR",
                priority="H", kind="STATE", body="ARCHIVED",
            )
        except Exception:
            pass
        post = c.execute(
            "SELECT COUNT(*) FROM debate_messages WHERE topic_id = 'X1'"
        ).fetchone()[0]
        assert pre == post
    finally:
        c.close()


def test_cascade_delete_topic_removes_messages_and_watermarks(db_path):
    """ON DELETE CASCADE: deleting a topic clears messages + watermarks."""
    c = sqlite3.connect(db_path, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    try:
        target = post_message(
            c, topic_id="X1", role="EXECUTOR",
            priority="INFO", kind="STATUS", body="seed",
        )
        post_message(
            c, topic_id="X1", role="EXECUTOR",
            priority="INFO", kind="WATERMARK", body=target["msg_id"],
        )

        msg_count = c.execute(
            "SELECT COUNT(*) FROM debate_messages WHERE topic_id = 'X1'"
        ).fetchone()[0]
        wm_count = c.execute(
            "SELECT COUNT(*) FROM debate_watermarks WHERE topic_id = 'X1'"
        ).fetchone()[0]
        assert msg_count >= 2 and wm_count == 1

        c.execute("DELETE FROM debates WHERE topic_id = 'X1'")

        msg_after = c.execute(
            "SELECT COUNT(*) FROM debate_messages WHERE topic_id = 'X1'"
        ).fetchone()[0]
        wm_after = c.execute(
            "SELECT COUNT(*) FROM debate_watermarks WHERE topic_id = 'X1'"
        ).fetchone()[0]
        assert msg_after == 0
        assert wm_after == 0
    finally:
        c.close()
