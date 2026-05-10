"""v3.9.2 — paranoid socket-blocked + concurrency proofs.

Per CONDUCTOR canonical msg:7d4a91ec (Step 9): the new prompt-time
inbox signaling DAOs (debate_post_with_recipients, debate_signal_check,
debate_signal_advance) must (a) make zero network calls in the hot
path — empirical LLM-free proof — and (b) survive multi-thread
contention under WAL with isolation_level=None.
"""

from __future__ import annotations

import os
import socket
import sqlite3
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from db_utils import get_conn_immediate
from debate import (
    DebateError,
    debate_post_with_recipients,
    debate_signal_advance,
    debate_signal_check,
    init_debate,
    transition_state,
)
from schema import init_db


def test_paranoid_socket_blocked_v3_9_2_dao_paths(tmp_path):
    """Full v3.9.2 hot path with socket.socket raising on every attempt.

    Proves no LLM / network call sneaks into post_with_recipients,
    signal_check, or signal_advance.
    """
    db = tmp_path / "v3_9_2_paranoid.db"
    init_db(str(db))
    c = sqlite3.connect(str(db), isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    init_debate(
        c, topic_id="PARANOID", title="no network",
        roles=[
            {"role": "CONDUCTOR", "session_id": "cc-cond1"},
            {"role": "EXECUTOR", "session_id": "cc-exec1"},
        ],
        created_by_role="CONDUCTOR",
    )
    transition_state(
        c, topic_id="PARANOID", role="CONDUCTOR", new_state="ACTIVE"
    )

    orig = socket.socket
    attempts: list = []

    def tripwire(*a, **kw):
        attempts.append(a)
        raise OSError("network blocked by paranoid test")

    socket.socket = tripwire
    try:
        m1 = debate_post_with_recipients(
            c, topic_id="PARANOID", role="CONDUCTOR",
            priority="H", kind="STATUS", body="m1",
            addressed_to=["EXECUTOR", "cc-exec1"],
        )
        m2 = debate_post_with_recipients(
            c, topic_id="PARANOID", role="CONDUCTOR",
            priority="L", kind="STATUS", body="m2",
            addressed_to=["EXECUTOR"],
        )
        out = debate_signal_check(
            c, session_id="cc-exec1", role="EXECUTOR",
            topic_id="PARANOID",
        )
        assert out["count"] == 2
        assert out["max_priority"] == "H"

        debate_signal_advance(
            c, session_id="cc-exec1", role="EXECUTOR",
            topic_id="PARANOID", last_processed_msg_id=m1["msg_id"],
        )
        out2 = debate_signal_check(
            c, session_id="cc-exec1", role="EXECUTOR",
            topic_id="PARANOID",
        )
        assert out2["count"] == 1
        assert out2["pending"][0]["msg_id"] == m2["msg_id"]
    finally:
        socket.socket = orig
        c.close()

    # Zero socket attempts proves no hidden network/LLM call along the
    # entire v3.9.2 hot path.
    assert attempts == []


def test_concurrent_post_with_recipients_4_threads_x_50_messages(tmp_path):
    """200 atomic addressed-message inserts under WAL + isolation_level
    None must all land with no lock errors and no lost rows."""
    db_path = str(tmp_path / "v3_9_2_concurrency.db")
    init_db(db_path)
    setup = sqlite3.connect(db_path, isolation_level=None)
    setup.row_factory = sqlite3.Row
    setup.execute("PRAGMA foreign_keys = ON")
    init_debate(
        setup, topic_id="X1", title="concurrency-v3.9.2",
        roles=[
            {"role": "CONDUCTOR", "session_id": "cc-cond1"},
            {"role": "EXECUTOR", "session_id": "cc-exec1"},
            {"role": "ADVOCATE", "session_id": "codex-adv1"},
            {"role": "HUMAN", "session_id": "human-rmanov"},
        ],
        created_by_role="CONDUCTOR",
    )
    transition_state(
        setup, topic_id="X1", role="CONDUCTOR", new_state="ACTIVE"
    )
    setup.close()

    errors: list[str] = []
    posted: list[str] = []
    lock = threading.Lock()

    def worker(role: str, target: str):
        try:
            c = sqlite3.connect(db_path, isolation_level=None)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA foreign_keys = ON")
            c.execute("PRAGMA busy_timeout = 30000")
            try:
                for i in range(50):
                    out = debate_post_with_recipients(
                        c, topic_id="X1", role=role,
                        priority="INFO", kind="STATUS",
                        body=f"{role}-msg{i}",
                        addressed_to=[target],
                    )
                    with lock:
                        posted.append(out["msg_id"])
            finally:
                c.close()
        except Exception as exc:  # pragma: no cover — surface for debug
            with lock:
                errors.append(f"{role}: {exc!r}")

    threads = [
        threading.Thread(target=worker, args=("CONDUCTOR", "EXECUTOR")),
        threading.Thread(target=worker, args=("EXECUTOR", "CONDUCTOR")),
        threading.Thread(target=worker, args=("ADVOCATE", "EXECUTOR")),
        threading.Thread(target=worker, args=("HUMAN", "ADVOCATE")),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], errors
    assert len(posted) == 200
    assert len(set(posted)) == 200  # all msg_ids unique

    # Verify recipient table reflects 200 atomic inserts (1 row each
    # since each post used a single-recipient list).
    verify = sqlite3.connect(db_path)
    rows = verify.execute(
        "SELECT COUNT(*) FROM debate_message_recipients"
    ).fetchone()
    verify.close()
    assert rows[0] == 200


def test_concurrent_signal_advance_no_lost_updates(tmp_path):
    """Two threads racing signal_advance for the same (session_id, role,
    topic_id) compound cursor must converge to the strictly-newer
    candidate (per ts, msg_id), with the monotonic guard surfacing
    stale racers as DebateError(error_type='watermark_regression').

    Worker contract per ADVOCATE turn-19 mandate (msg:b8182b8b):
      - swallow watermark_regression (expected for the stale racer
        after the newer cursor lands)
      - fail loud on any other DebateError or unexpected exception

    Final assertion queries the topic for the strictly-latest message
    by compound (ts DESC, msg_id DESC) and asserts the persisted cursor
    matches it — precise winner, not "any of the candidates".
    """
    db_path = str(tmp_path / "v3_9_2_advance_race.db")
    init_db(db_path)
    setup = sqlite3.connect(db_path, isolation_level=None)
    setup.row_factory = sqlite3.Row
    setup.execute("PRAGMA foreign_keys = ON")
    init_debate(
        setup, topic_id="X1", title="advance-race",
        roles=[
            {"role": "CONDUCTOR", "session_id": "cc-cond1"},
            {"role": "EXECUTOR", "session_id": "cc-exec1"},
        ],
        created_by_role="CONDUCTOR",
    )
    transition_state(
        setup, topic_id="X1", role="CONDUCTOR", new_state="ACTIVE"
    )
    msg_ids: list[str] = []
    for i in range(2):
        out = debate_post_with_recipients(
            setup, topic_id="X1", role="CONDUCTOR",
            priority="M", kind="STATUS", body=f"m{i}",
            addressed_to=["EXECUTOR"],
        )
        msg_ids.append(out["msg_id"])
    setup.close()

    errors: list[str] = []
    regression_swallowed: list[str] = []

    def race(target_msg_id: str):
        # v3.9.3 amendment 2B (msg:3d3442cb): the DAO's race-safety
        # contract is wrapper-scoped. Each iteration must open its own
        # ``get_conn_immediate`` block so the SELECT-then-UPSERT in
        # debate_signal_advance is serialized against the other racer
        # via BEGIN IMMEDIATE. Raw ``sqlite3.connect(isolation_level=
        # None)`` would auto-commit each statement and let two racers
        # race the SELECT and overwrite each other — which is exactly
        # the failure mode the wrapper closes.
        for _ in range(20):
            try:
                with get_conn_immediate(db_path=db_path) as conn:
                    debate_signal_advance(
                        conn, session_id="cc-exec1", role="EXECUTOR",
                        topic_id="X1",
                        last_processed_msg_id=target_msg_id,
                    )
            except DebateError as exc:
                if exc.error_type == "watermark_regression":
                    # Expected: the OTHER racer already advanced to
                    # the newer cursor; this stale racer's older
                    # target is now correctly rejected. Future
                    # iterations would all regress identically.
                    regression_swallowed.append(target_msg_id)
                    break
                errors.append(f"{target_msg_id}: {exc!r}")
                break
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower():
                    errors.append(f"{target_msg_id}: {exc!r}")
                    break
                # Lock contention is acceptable under the wrapper's
                # serialization; retry the next iteration.
            except Exception as exc:  # pragma: no cover
                errors.append(f"{target_msg_id}: {exc!r}")
                break

    t1 = threading.Thread(target=race, args=(msg_ids[0],))
    t2 = threading.Thread(target=race, args=(msg_ids[1],))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    # Only NON-regression errors fail the test. Regressions are the
    # designed-by-spec outcome for the stale racer.
    assert errors == [], errors

    # Compute the expected winner authoritatively from the topic — the
    # strictly-latest message by compound (ts DESC, msg_id DESC) order
    # — instead of relying on insertion order which would couple the
    # test to a property of the fixture rather than the cursor contract.
    verify = sqlite3.connect(db_path)
    verify.row_factory = sqlite3.Row
    expected_winner = verify.execute(
        "SELECT msg_id FROM debate_messages "
        "WHERE topic_id = ? AND kind = 'STATUS' "
        "ORDER BY ts DESC, msg_id DESC LIMIT 1",
        ("X1",),
    ).fetchone()
    final = verify.execute(
        "SELECT last_processed_msg_id FROM debate_signal_state "
        "WHERE session_id = ? AND role = ? AND topic_id = ?",
        ("cc-exec1", "EXECUTOR", "X1"),
    ).fetchone()
    verify.close()

    assert expected_winner is not None
    assert final is not None
    assert final["last_processed_msg_id"] == expected_winner["msg_id"], (
        f"final cursor {final['last_processed_msg_id']!r} does not "
        f"match strictly-latest message {expected_winner['msg_id']!r}; "
        f"monotonic guard failed to enforce winner"
    )
