"""Durable targeted-delivery completion requires a vehicle or an explicit ack.

Incident (routing repair, lane R, 2026-08-26): an implementation-tagged PING
addressed to EXECUTOR + CONDUCTOR was classified
``implementation_requires_impl_vehicle`` (notify-only, no worker spawn). The
pump then regarded the trigger as terminal — ``_recipient_bindings`` yields no
worker binding for an implementation vehicle, so ``_trigger_is_terminal``
answered True — and ``_complete_pending_deliveries`` marked the durable queue
rows complete with ``dispatched_rows=0`` and ``launched_workers=0``. The
addressed executor only ever saw the PING through its own poll.

Contract pinned here for a targeted delivery of an implementation-tagged or
notify-only trigger: the queue row stays pending until EITHER

  (a) a worker vehicle was actually launched for that recipient (a worker
      claim / dispatched wake row exists), OR
  (b) the recipient's already-running primary session explicitly
      acknowledged it — its signal cursor (``debate_signal_advance``) or role
      watermark (``advance_watermark``) reached the trigger, or a reply from
      that recipient exists (terminal per the existing rules).

Also pinned: completion with a launched worker still works, terminal
triggers still complete, no double-dispatch, and suppressed-only recipients
keep their existing terminal semantics. Every test drives the pump's own
functions in-process against a temp DB; ``_dispatch_row`` is always replaced
so no real agent is ever spawned and the live database is never opened.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
HOOKS = REPO / "hooks"
for _path in (str(REPO), str(HOOKS)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import debate_pump  # noqa: E402 - imported after local repo path bootstrap
from debate import (  # noqa: E402 - imported after local repo path bootstrap
    _insert_wake_log,
    advance_watermark,
    bind_role_session,
    claim_worker_session,
    debate_post_with_recipients,
    debate_signal_advance,
    debate_signal_check,
    init_debate,
    transition_state,
)
from schema import init_db  # noqa: E402 - imported after local repo path bootstrap

TOPIC = "DELIVERY_ACK_T1"
EXECUTOR_SESSION = "cc-executor_ack1"
CONDUCTOR_SESSION = "cc-conductor_ack1"
WAKE_ACTION = "post_tool_use_wake"
SUPPRESSED = {"CONDUCTOR"}


@pytest.fixture
def pump_db(tmp_path, monkeypatch):
    """Real schema in a temp file; the pump module pointed at it."""
    db_path = tmp_path / "delivery_ack.db"
    init_db(str(db_path))  # never call init_db() without a path: default is live
    con = sqlite3.connect(db_path, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    init_debate(
        con,
        topic_id=TOPIC,
        title="delivery ack contract",
        roles=[
            {"role": "CONDUCTOR", "session_id": CONDUCTOR_SESSION},
            {"role": "EXECUTOR", "session_id": EXECUTOR_SESSION},
        ],
        created_by_role="CONDUCTOR",
    )
    transition_state(con, topic_id=TOPIC, role="CONDUCTOR", new_state="ACTIVE")
    for role, sid in (("CONDUCTOR", CONDUCTOR_SESSION), ("EXECUTOR", EXECUTOR_SESSION)):
        bind_role_session(
            con,
            topic_id=TOPIC,
            role=role,
            session_id=sid,
            runtime="cc",
            reason="delivery ack fixture",
        )

    monkeypatch.setattr(debate_pump, "DB_PATH", str(db_path))
    monkeypatch.setattr(debate_pump, "LOG_PATH", tmp_path / "pump.jsonl")
    monkeypatch.setattr(debate_pump, "STATE_PATH", tmp_path / "pump_state.json")
    monkeypatch.setattr(debate_pump, "HEARTBEAT_PATH", tmp_path / "hb.json")
    monkeypatch.setattr(debate_pump, "IS_WINDOWS", False)
    monkeypatch.setenv("DEBATE_RESOURCE_BUDGET", "off")
    monkeypatch.setenv("DEBATE_WAKE_ACTION_NAME", WAKE_ACTION)
    monkeypatch.setattr(debate_pump, "_WITHHELD_DELIVERY_LOGGED", {})
    try:
        yield con
    finally:
        con.close()


class _FakeDispatch:
    """Stand-in for ``_dispatch_row``: never spawns; optionally claims."""

    def __init__(self, db_path: str, *, claim: bool):
        self.db_path = db_path
        self.claim = claim
        self.calls: list[str] = []

    def __call__(self, row, suppressed_roles):
        self.calls.append(str(row["msg_id"]))
        if not self.claim:
            return 0
        c2 = sqlite3.connect(self.db_path, isolation_level=None)
        c2.row_factory = sqlite3.Row
        try:
            claim_worker_session(
                c2,
                topic_id=row["topic_id"],
                role="EXECUTOR",
                parent_session_id=EXECUTOR_SESSION,
                trigger_msg_id=row["msg_id"],
            )
        finally:
            c2.close()
        return 1


def _run_once(monkeypatch, dispatch, *, kinds: str = "Q,DECISION,PING") -> None:
    monkeypatch.setattr(debate_pump, "STOP", False, raising=False)
    monkeypatch.setattr(debate_pump, "_dispatch_row", dispatch)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "debate_pump.py",
            "--once",
            "--since",
            "1970-01-01T00:00:00Z",
            "--action-kind",
            kinds,
            "--max-workers-per-scan",
            "1",
            "--max-concurrent-workers",
            "1",
            "--message-claim-reclaim-seconds",
            "0",
            "--worker-claim-recovery-seconds",
            "0",
        ],
    )
    assert debate_pump.main() == 0


def _queue(con: sqlite3.Connection, msg_id: str) -> dict[str, str | None]:
    rows = con.execute(
        "SELECT recipient, completed_at FROM debate_delivery_queue "
        "WHERE msg_id = ? ORDER BY recipient",
        (msg_id,),
    ).fetchall()
    return {str(r["recipient"]): r["completed_at"] for r in rows}


def _events(event: str) -> list[dict]:
    path = Path(debate_pump.LOG_PATH)
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            item = json.loads(line)
            if item.get("event") == event:
                out.append(item)
    return out


def _post_impl_ping(con: sqlite3.Connection, body: str = "impl hand-off") -> str:
    return debate_post_with_recipients(
        con,
        topic_id=TOPIC,
        role="CONDUCTOR",
        priority="H",
        kind="PING",
        body=body,
        addressed_to=["EXECUTOR", "CONDUCTOR"],
        vehicle="implementation",
    )["msg_id"]


def _post_analysis_q(con: sqlite3.Connection, body: str = "analyse this") -> str:
    return debate_post_with_recipients(
        con,
        topic_id=TOPIC,
        role="CONDUCTOR",
        priority="H",
        kind="Q",
        body=body,
        addressed_to=["EXECUTOR"],
        vehicle="analysis",
    )["msg_id"]


def _executor_signal_ack(con: sqlite3.Connection, msg_id: str) -> None:
    """The executor's primary session reads its inbox and advances its cursor."""
    debate_signal_check(
        con, session_id=EXECUTOR_SESSION, role="EXECUTOR", topic_id=TOPIC
    )
    debate_signal_advance(
        con,
        session_id=EXECUTOR_SESSION,
        role="EXECUTOR",
        topic_id=TOPIC,
        last_processed_msg_id=msg_id,
    )


# ── the defect: notify-only impl trigger completed with nothing launched ─────


def test_impl_trigger_without_vehicle_stays_pending(pump_db, monkeypatch):
    con = pump_db
    trigger = _post_impl_ping(con)
    dispatch = _FakeDispatch(debate_pump.DB_PATH, claim=False)

    _run_once(monkeypatch, dispatch)

    # No worker vehicle exists for an implementation trigger and nobody
    # acknowledged it: the durable queue must remain pending for EXECUTOR.
    assert dispatch.calls == []
    queue = _queue(con, trigger)
    assert queue["EXECUTOR"] is None
    assert queue["CONDUCTOR"] is None
    assert _events("pump_targeted_delivery_completed") == []
    withheld = _events("pump_targeted_delivery_pending_no_vehicle")
    assert [e["msg_id"] for e in withheld] == [trigger]
    assert withheld[0]["pending_recipients"] == ["EXECUTOR"]
    batches = _events("scan_batch")
    assert batches and batches[-1]["launched_workers"] == 0


def test_withheld_completion_is_logged_once_per_pump_lifetime_not_per_scan(
    pump_db, monkeypatch
):
    con = pump_db
    trigger = _post_impl_ping(con)
    dispatch = _FakeDispatch(debate_pump.DB_PATH, claim=False)

    _run_once(monkeypatch, dispatch)
    _run_once(monkeypatch, dispatch)
    _run_once(monkeypatch, dispatch)

    assert _queue(con, trigger)["EXECUTOR"] is None
    # The row is re-examined on every scan, but the withheld receipt is not
    # re-emitted while the pending recipient set is unchanged.
    withheld = _events("pump_targeted_delivery_pending_no_vehicle")
    assert [e["msg_id"] for e in withheld] == [trigger]


def test_impl_trigger_completes_after_signal_cursor_ack(pump_db, monkeypatch):
    con = pump_db
    trigger = _post_impl_ping(con)
    dispatch = _FakeDispatch(debate_pump.DB_PATH, claim=False)

    _run_once(monkeypatch, dispatch)
    assert _queue(con, trigger)["EXECUTOR"] is None

    # The already-running executor session polls its inbox and advances its
    # (session, role, topic) cursor to the trigger: explicit acknowledgement.
    _executor_signal_ack(con, trigger)
    _run_once(monkeypatch, dispatch)

    queue = _queue(con, trigger)
    assert queue["EXECUTOR"] is not None
    assert queue["CONDUCTOR"] is not None
    completed = _events("pump_targeted_delivery_completed")
    assert [(e["msg_id"], e["recipients"]) for e in completed] == [(trigger, 2)]
    assert dispatch.calls == []


def test_impl_trigger_completes_after_role_watermark_ack(pump_db, monkeypatch):
    con = pump_db
    trigger = _post_impl_ping(con)
    dispatch = _FakeDispatch(debate_pump.DB_PATH, claim=False)

    _run_once(monkeypatch, dispatch)
    assert _queue(con, trigger)["EXECUTOR"] is None

    # A canonical WATERMARK post from the executor role is the other explicit
    # acknowledgement channel (debate_advance_watermark).
    advance_watermark(
        con, topic_id=TOPIC, role="EXECUTOR", processed_up_to_msg_id=trigger
    )
    _run_once(monkeypatch, dispatch)

    assert _queue(con, trigger)["EXECUTOR"] is not None
    assert [e["msg_id"] for e in _events("pump_targeted_delivery_completed")] == [
        trigger
    ]


def test_older_watermark_does_not_ack_a_newer_trigger(pump_db, monkeypatch):
    con = pump_db
    first = _post_impl_ping(con, body="first hand-off")
    second = _post_impl_ping(con, body="second hand-off")
    dispatch = _FakeDispatch(debate_pump.DB_PATH, claim=False)

    advance_watermark(
        con, topic_id=TOPIC, role="EXECUTOR", processed_up_to_msg_id=first
    )
    _run_once(monkeypatch, dispatch)

    assert _queue(con, first)["EXECUTOR"] is not None
    assert _queue(con, second)["EXECUTOR"] is None
    assert [e["msg_id"] for e in _events("pump_targeted_delivery_completed")] == [first]
    withheld = _events("pump_targeted_delivery_pending_no_vehicle")
    assert [e["msg_id"] for e in withheld] == [second]


def test_impl_trigger_completes_after_recipient_reply(pump_db, monkeypatch):
    con = pump_db
    trigger = _post_impl_ping(con)
    dispatch = _FakeDispatch(debate_pump.DB_PATH, claim=False)

    _run_once(monkeypatch, dispatch)
    assert _queue(con, trigger)["EXECUTOR"] is None

    debate_post_with_recipients(
        con,
        topic_id=TOPIC,
        role="EXECUTOR",
        priority="M",
        kind="STATUS",
        body="ACK, working on it",
        addressed_to=["CONDUCTOR"],
        reply_to=trigger,
        vehicle="implementation",
    )
    _run_once(monkeypatch, dispatch)

    assert _queue(con, trigger)["EXECUTOR"] is not None
    assert [e["msg_id"] for e in _events("pump_targeted_delivery_completed")] == [
        trigger
    ]


def test_notify_only_wake_result_does_not_complete_without_ack(pump_db, monkeypatch):
    """DEBATE_WAKE_ACTION=notify leaves result='notified' — a desktop signal,
    not a vehicle. The cursor may pass it (existing rule) but the durable
    queue must wait for an acknowledgement."""
    con = pump_db
    trigger = _post_analysis_q(con)
    _insert_wake_log(
        con,
        trigger_msg_id=trigger,
        topic_id=TOPIC,
        recipient="EXECUTOR",
        action=WAKE_ACTION,
        result="notified",
        target_role="EXECUTOR",
        target_session_id=EXECUTOR_SESSION,
        target_runtime="cc",
    )
    dispatch = _FakeDispatch(debate_pump.DB_PATH, claim=False)

    _run_once(monkeypatch, dispatch)

    assert dispatch.calls == []
    assert _queue(con, trigger)["EXECUTOR"] is None
    assert _events("pump_targeted_delivery_completed") == []
    withheld = _events("pump_targeted_delivery_pending_no_vehicle")
    assert [e["msg_id"] for e in withheld] == [trigger]

    _executor_signal_ack(con, trigger)
    _run_once(monkeypatch, dispatch)
    assert _queue(con, trigger)["EXECUTOR"] is not None


# ── regression pins: launched vehicles and terminal triggers still complete ──


def test_launched_worker_then_reply_completes_without_double_dispatch(
    pump_db, monkeypatch
):
    con = pump_db
    trigger = _post_analysis_q(con)
    dispatch = _FakeDispatch(debate_pump.DB_PATH, claim=True)

    # Scan 1: a worker vehicle is launched (claim active) — in-flight.
    _run_once(monkeypatch, dispatch)
    assert dispatch.calls == [trigger]
    assert _queue(con, trigger)["EXECUTOR"] is None
    assert _events("pump_targeted_delivery_pending_no_vehicle") == []

    # Scan 2: still in-flight — the live claim must not be re-dispatched.
    _run_once(monkeypatch, dispatch)
    assert dispatch.calls == [trigger]

    # The worker replies → terminal → the queue completes on the next scan.
    debate_post_with_recipients(
        con,
        topic_id=TOPIC,
        role="EXECUTOR",
        priority="M",
        kind="A",
        body="done",
        addressed_to=["CONDUCTOR"],
        reply_to=trigger,
    )
    _run_once(monkeypatch, dispatch)
    assert _queue(con, trigger)["EXECUTOR"] is not None
    assert [e["msg_id"] for e in _events("pump_targeted_delivery_completed")] == [
        trigger
    ]

    # Scan 4: completed rows are not re-fetched, re-dispatched or re-completed.
    _run_once(monkeypatch, dispatch)
    assert dispatch.calls == [trigger]
    assert len(_events("pump_targeted_delivery_completed")) == 1


def test_launched_worker_claim_counts_as_vehicle_for_acknowledgement(pump_db):
    """(a) of the contract, in isolation: a worker claim for the recipient is
    proof a vehicle was launched, independent of any reply or cursor."""
    con = pump_db
    trigger = _post_analysis_q(con)
    acknowledged, pending = debate_pump._delivery_acknowledged(trigger, SUPPRESSED)
    assert acknowledged is False
    assert pending == ["EXECUTOR"]

    claim_worker_session(
        con,
        topic_id=TOPIC,
        role="EXECUTOR",
        parent_session_id=EXECUTOR_SESSION,
        trigger_msg_id=trigger,
    )
    acknowledged, pending = debate_pump._delivery_acknowledged(trigger, SUPPRESSED)
    assert acknowledged is True
    assert pending == []


def test_suppressed_only_recipient_still_completes(pump_db, monkeypatch):
    """A reply-like message addressed only to the suppressed CONDUCTOR has no
    eligible recipient to acknowledge it: terminal, completes as before."""
    con = pump_db
    msg_id = debate_post_with_recipients(
        con,
        topic_id=TOPIC,
        role="EXECUTOR",
        priority="M",
        kind="Q",
        body="fyi conductor",
        addressed_to=["CONDUCTOR"],
    )["msg_id"]
    dispatch = _FakeDispatch(debate_pump.DB_PATH, claim=False)

    _run_once(monkeypatch, dispatch)

    assert dispatch.calls == []
    assert _queue(con, msg_id)["CONDUCTOR"] is not None
    assert [e["msg_id"] for e in _events("pump_targeted_delivery_completed")] == [
        msg_id
    ]
    assert _events("pump_targeted_delivery_pending_no_vehicle") == []
