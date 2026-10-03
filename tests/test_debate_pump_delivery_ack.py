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

  (a) a versioned exact successful-launch receipt matches that recipient and
      current binding/worker identity (a claim alone is only reservation), OR
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
import io
from importlib.util import module_from_spec, spec_from_file_location
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
    worker_no_action,
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
    monkeypatch.setattr(debate_pump, "_WITHHELD_DELIVERY_LOGGED", {}, raising=False)
    monkeypatch.setattr(debate_pump, "_WITHHELD_DELIVERY_SATURATED", False, raising=False)
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

    # Scan 1: fake dispatcher reserves a claim — no process-launch proof.
    _run_once(monkeypatch, dispatch)
    assert dispatch.calls == [trigger]
    assert _queue(con, trigger)["EXECUTOR"] is None
    assert _events("pump_targeted_delivery_pending_no_vehicle") == []

    # Scan 2: still in-flight — the live claim must not be re-dispatched.
    _run_once(monkeypatch, dispatch)
    assert dispatch.calls == [trigger]

    # A role reply is terminal under existing normal-role semantics.
    actual_worker = con.execute("SELECT worker_session_id FROM debate_worker_claims WHERE trigger_msg_id=?", (trigger,)).fetchone()[0]
    debate_post_with_recipients(
        con,
        topic_id=TOPIC,
        role="EXECUTOR",
        priority="M",
        kind="A",
        body="done",
        addressed_to=["CONDUCTOR"],
        reply_to=trigger,
        author_session_id=actual_worker,
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


DIAGNOSTIC_SESSION = "cc-diagnostic_ack1"


def _diagnostic_trigger(con, *, mixed=False):
    bind_role_session(con, topic_id=TOPIC, role="EXECUTOR", session_id=DIAGNOSTIC_SESSION,
                      state="diagnostic", runtime="cc", reason="synthetic diagnostic")
    return debate_post_with_recipients(con, topic_id=TOPIC, role="CONDUCTOR", priority="H",
        kind="PING", body="exact synthetic diagnostic", addressed_to=["EXECUTOR"] if mixed else [],
        diagnostic_to=[DIAGNOSTIC_SESSION], vehicle="implementation")["msg_id"]


@pytest.mark.parametrize("borrowed", ["watermark", "signal", "reply"])
def test_diagnostic_does_not_borrow_role_evidence(pump_db, monkeypatch, borrowed):
    con = pump_db
    trigger = _diagnostic_trigger(con)
    if borrowed == "watermark":
        parent_evidence = _post_impl_ping(con, "later genuinely addressed parent evidence")
        advance_watermark(con, topic_id=TOPIC, role="EXECUTOR", processed_up_to_msg_id=parent_evidence)
    elif borrowed == "signal":
        parent_evidence = _post_impl_ping(con, "later genuinely addressed parent evidence")
        _executor_signal_ack(con, parent_evidence)
    else:
        debate_post_with_recipients(con, topic_id=TOPIC, role="EXECUTOR", priority="M", kind="STATUS",
            body="same role not exact recipient", addressed_to=["CONDUCTOR"], reply_to=trigger, vehicle="implementation")
    before = [tuple(row) for row in con.execute("SELECT * FROM debate_signal_state WHERE session_id=?", (EXECUTOR_SESSION,))]
    dispatch = _FakeDispatch(debate_pump.DB_PATH, claim=False)
    assert debate_pump._trigger_is_terminal(trigger, SUPPRESSED) is True
    _run_once(monkeypatch, dispatch)
    assert dispatch.calls == []
    assert _queue(con, trigger)[DIAGNOSTIC_SESSION] is None
    assert before == [tuple(row) for row in con.execute("SELECT * FROM debate_signal_state WHERE session_id=?", (EXECUTOR_SESSION,))]


def test_diagnostic_exact_cursor_completes_without_spawn(pump_db, monkeypatch):
    con = pump_db
    trigger = _diagnostic_trigger(con)
    dispatch = _FakeDispatch(debate_pump.DB_PATH, claim=False)
    _run_once(monkeypatch, dispatch)
    assert _queue(con, trigger)[DIAGNOSTIC_SESSION] is None
    debate_signal_check(con, session_id=DIAGNOSTIC_SESSION, role="EXECUTOR", topic_id=TOPIC)
    debate_signal_advance(con, session_id=DIAGNOSTIC_SESSION, role="EXECUTOR", topic_id=TOPIC, last_processed_msg_id=trigger)
    _run_once(monkeypatch, dispatch)
    assert _queue(con, trigger)[DIAGNOSTIC_SESSION] is not None
    assert dispatch.calls == []


def test_mixed_targets_require_all_exact_acks(pump_db, monkeypatch):
    con = pump_db
    trigger = _diagnostic_trigger(con, mixed=True)
    advance_watermark(con, topic_id=TOPIC, role="EXECUTOR", processed_up_to_msg_id=trigger)
    dispatch = _FakeDispatch(debate_pump.DB_PATH, claim=False)
    _run_once(monkeypatch, dispatch)
    assert all(value is None for value in _queue(con, trigger).values())


def test_normal_role_unrelated_diagnostic_cursor_cannot_ack(pump_db, monkeypatch):
    con = pump_db
    _diagnostic_trigger(con)
    trigger = _post_impl_ping(con)
    diagnostic_evidence = debate_post_with_recipients(con, topic_id=TOPIC, role="CONDUCTOR", priority="H",
        kind="PING", body="later genuinely addressed diagnostic evidence", addressed_to=[],
        diagnostic_to=[DIAGNOSTIC_SESSION], vehicle="implementation")["msg_id"]
    debate_signal_check(con, session_id=DIAGNOSTIC_SESSION, role="EXECUTOR", topic_id=TOPIC)
    debate_signal_advance(con, session_id=DIAGNOSTIC_SESSION, role="EXECUTOR", topic_id=TOPIC, last_processed_msg_id=diagnostic_evidence)
    dispatch = _FakeDispatch(debate_pump.DB_PATH, claim=False)
    _run_once(monkeypatch, dispatch)
    assert _queue(con, trigger)["EXECUTOR"] is None


def test_normal_role_retired_parent_cursor_cannot_ack(pump_db, monkeypatch):
    con = pump_db
    trigger = _post_impl_ping(con)
    _executor_signal_ack(con, trigger)
    bind_role_session(con, topic_id=TOPIC, role="EXECUTOR", session_id="cc-newparent_ack1",
                      runtime="cc", reason="synthetic replacement", replace_active=True)
    dispatch = _FakeDispatch(debate_pump.DB_PATH, claim=False)
    _run_once(monkeypatch, dispatch)
    assert _queue(con, trigger)["EXECUTOR"] is None


def test_same_timestamp_watermark_does_not_cover_later_msg_id(pump_db, monkeypatch):
    con = pump_db
    triggers = [_post_impl_ping(con, "clock1"), _post_impl_ping(con, "clock2")]
    lower, higher = sorted(triggers)
    # Synthetic clock only: message and queue timestamp kept coherent.
    for trigger in triggers:
        con.execute("UPDATE debate_messages SET ts='2026-10-03T00:00:00Z' WHERE msg_id=?", (trigger,))
    advance_watermark(con, topic_id=TOPIC, role="EXECUTOR", processed_up_to_msg_id=lower)
    dispatch = _FakeDispatch(debate_pump.DB_PATH, claim=False)
    _run_once(monkeypatch, dispatch)
    assert _queue(con, lower)["EXECUTOR"] is not None
    assert _queue(con, higher)["EXECUTOR"] is None


def _notified(con, trigger):
    _insert_wake_log(con, trigger_msg_id=trigger, topic_id=TOPIC, recipient="EXECUTOR",
        action=WAKE_ACTION, result="notified", target_role="EXECUTOR", target_session_id=EXECUTOR_SESSION, target_runtime="cc")


def test_claim_reservation_without_launch_is_not_delivery(pump_db, monkeypatch):
    con = pump_db
    trigger = _post_analysis_q(con)
    claim_worker_session(con, topic_id=TOPIC, role="EXECUTOR", parent_session_id=EXECUTOR_SESSION, trigger_msg_id=trigger)
    _notified(con, trigger)  # Terminal notification is not successful process launch.
    dispatch = _FakeDispatch(debate_pump.DB_PATH, claim=False)
    assert debate_pump._trigger_is_terminal(trigger, SUPPRESSED)
    _run_once(monkeypatch, dispatch)
    assert dispatch.calls == []
    assert _queue(con, trigger)["EXECUTOR"] is None


@pytest.mark.parametrize("defect", [None, "completed", "unversioned", "wrong_generation", "wrong_parent", "wrong_worker", "retired_claim", "unconfirmed", "wrong_recipient", "wrong_topic", "wrong_trigger", "wrong_mode", "truthy_int", "truthy_string", "bool_version", "unsupported_version"])
def test_exact_versioned_receipt_consumption(pump_db, monkeypatch, defect):
    con = pump_db
    trigger = _post_analysis_q(con)
    claim = claim_worker_session(con, topic_id=TOPIC, role="EXECUTOR", parent_session_id=EXECUTOR_SESSION, trigger_msg_id=trigger)
    binding = con.execute("SELECT generation FROM debate_role_bindings WHERE topic_id=? AND role=? AND session_id=?", (TOPIC,"EXECUTOR",EXECUTOR_SESSION)).fetchone()
    details = {"routing_receipt_version":1,"launch_confirmed":True,"recipient":"EXECUTOR","recipient_mode":"normal",
               "binding_session_id":EXECUTOR_SESSION,"binding_generation":binding["generation"],"worker_session_id":claim["worker_session_id"]}
    if defect == "unversioned":
        details = {}
    elif defect == "wrong_generation":
        details["binding_generation"] += 1
    elif defect == "wrong_parent":
        details["binding_session_id"] = DIAGNOSTIC_SESSION
    elif defect == "wrong_worker":
        details["worker_session_id"] = "cc-wrongworker123"
    elif defect == "unconfirmed":
        details["launch_confirmed"] = False
    elif defect == "wrong_recipient":
        details["recipient"] = DIAGNOSTIC_SESSION
    elif defect == "retired_claim":
        bind_role_session(con, topic_id=TOPIC, role="EXECUTOR", session_id="cc-newparent_receipt1", runtime="cc", reason="public retire old claim", replace_active=True)
    elif defect == "completed":
        worker_no_action(con, topic_id=TOPIC, role="EXECUTOR", worker_session_id=claim["worker_session_id"], trigger_msg_id=trigger, reason="synthetic no work")
    elif defect == "wrong_mode":
        details["recipient_mode"] = "diagnostic"
    elif defect == "truthy_int":
        details["launch_confirmed"] = 1
    elif defect == "truthy_string":
        details["launch_confirmed"] = "true"
    elif defect == "bool_version":
        details["routing_receipt_version"] = True
    elif defect == "unsupported_version":
        details["routing_receipt_version"] = 2
    _notified(con, trigger)
    receipt_trigger = _post_analysis_q(con, "unrelated receipt trigger") if defect == "wrong_trigger" else trigger
    receipt_topic = "OTHER_SYNTHETIC_TOPIC" if defect == "wrong_topic" else TOPIC
    if defect == "wrong_topic":
        init_debate(con, topic_id=receipt_topic, title="unrelated synthetic topic", roles=[{"role":"CONDUCTOR","session_id":"s-othercond"}], created_by_role="CONDUCTOR")
    _insert_wake_log(con, trigger_msg_id=receipt_trigger, topic_id=receipt_topic, recipient="EXECUTOR", action="external_agent_spawn", result="real_spawn",
        target_role="EXECUTOR", target_session_id=claim["worker_session_id"], target_runtime="cc", binding_generation=binding["generation"], details=details)
    dispatch = _FakeDispatch(debate_pump.DB_PATH, claim=False)
    _run_once(monkeypatch, dispatch)
    if defect == "retired_claim":
        # Cursor terminal can hold new-owner work before the delivery gate.
        assert debate_pump._delivery_acknowledged(trigger, SUPPRESSED)[0] is False
    assert (_queue(con, trigger)["EXECUTOR"] is not None) is (defect in {None, "completed"})


def test_withheld_diagnostics_are_bounded_and_queue_remains_pending(pump_db, monkeypatch):
    con = pump_db
    trigger = _post_impl_ping(con)
    # Saturated diagnostic cache is deliberately synthetic; durable row real.
    debate_pump._WITHHELD_DELIVERY_LOGGED.update({f"synthetic-{i}":("EXECUTOR",) for i in range(1024)})
    dispatch = _FakeDispatch(debate_pump.DB_PATH, claim=False)
    _run_once(monkeypatch, dispatch)
    _run_once(monkeypatch, dispatch)
    assert len(debate_pump._WITHHELD_DELIVERY_LOGGED) <= 1024
    assert _queue(con, trigger)["EXECUTOR"] is None
    assert len(_events("pump_targeted_delivery_log_saturated")) == 1
    debate_pump._WITHHELD_DELIVERY_LOGGED.pop("synthetic-0")
    _run_once(monkeypatch, dispatch)  # Capacity returns; actual pending row recorded.
    assert len(debate_pump._WITHHELD_DELIVERY_LOGGED) == 1024
    another = _post_impl_ping(con, "second saturation cycle")
    _run_once(monkeypatch, dispatch)
    assert len(_events("pump_targeted_delivery_log_saturated")) == 2
    assert _queue(con, trigger)["EXECUTOR"] is None
    assert _queue(con, another)["EXECUTOR"] is None


@pytest.mark.parametrize("spawn_ok,rebind_race", [(True,False), (False,False), (True,True)])
def test_real_launcher_receipt_records_prelaunch_parent_and_actual_worker(pump_db, monkeypatch, tmp_path, spawn_ok, rebind_race):
    import db_utils
    import psutil
    con = pump_db
    trigger = _post_analysis_q(con)
    monkeypatch.setattr(db_utils, "DB_PATH", str(debate_pump.DB_PATH))
    monkeypatch.setenv("SQLITE_MEMORY_DB", str(debate_pump.DB_PATH))
    monkeypatch.setenv("DEBATE_WAKE_HOOK_LOG", str(tmp_path/"wake.jsonl"))
    monkeypatch.setenv("DEBATE_WAKE_AGENT_LOG_DIR", str(tmp_path/"agents"))
    monkeypatch.setenv("DEBATE_WAKE_DISABLE_FILE", str(tmp_path/"missing-disable"))
    monkeypatch.setenv("DEBATE_WAKE_ACTION", "agent")
    monkeypatch.setenv("DEBATE_WAKE_REMAINING", "1")
    spec = spec_from_file_location("a_receipt_producer_test", REPO/"hooks/debate_wake.py")
    wake = module_from_spec(spec)
    sys.modules[spec.name] = wake
    spec.loader.exec_module(wake)
    monkeypatch.setattr(wake.shutil, "which", lambda _command: "/usr/bin/false")
    class FakeProcess:
        pid = 12345
        def __init__(self, *args, **kwargs):
            if not spawn_ok:
                raise OSError("synthetic spawn denied")
            if rebind_race:
                bind_role_session(con, topic_id=TOPIC, role="EXECUTOR", session_id="cc-racedparent123", runtime="cc", reason="public Popen rebind before receipt", replace_active=True)
            self.stdin = io.BytesIO()
    monkeypatch.setattr(wake.subprocess, "Popen", FakeProcess)
    monkeypatch.setattr(psutil, "Process", lambda _pid: type("FakeIdentity", (), {"create_time":lambda _self:123.5})())
    response = {"msg_id":trigger,"topic_id":TOPIC,"schema_version":"debate_post_with_recipients.v1"}
    resolved = wake._handle_tool_response(response)
    generation = con.execute("SELECT generation FROM debate_role_bindings WHERE topic_id=? AND role=? AND session_id=?", (TOPIC,"EXECUTOR",EXECUTOR_SESSION)).fetchone()[0]
    if spawn_ok:
        wake._maybe_dispatch(response, resolved)
    else:
        with pytest.raises(OSError, match="synthetic spawn denied"):
            wake._maybe_dispatch(response, resolved)
    rows = con.execute("SELECT details_json FROM debate_wake_log WHERE trigger_msg_id=? AND result='real_spawn'", (trigger,)).fetchall()
    if not spawn_ok:
        assert rows == []
        return
    assert len(rows) == 1
    details = json.loads(rows[0][0])
    if rebind_race:
        assert details.get("launch_confirmed") is not True
        assert debate_pump._delivery_acknowledged(trigger, SUPPRESSED)[0] is False
        assert con.execute("SELECT state FROM debate_worker_claims WHERE trigger_msg_id=?", (trigger,)).fetchone()[0] == "retired"
        return
    claim = con.execute("SELECT worker_session_id,parent_session_id FROM debate_worker_claims WHERE trigger_msg_id=?", (trigger,)).fetchone()
    assert details["routing_receipt_version"] == 1
    assert details["launch_confirmed"] is True
    assert details["recipient"] == "EXECUTOR"
    assert details["recipient_mode"] == "normal"
    assert details["binding_session_id"] == EXECUTOR_SESSION == claim["parent_session_id"]
    assert details["binding_generation"] == generation
    assert details["worker_session_id"] == claim["worker_session_id"]
    assert details["worker_session_id"] != EXECUTOR_SESSION
    assert debate_pump._delivery_acknowledged(trigger, SUPPRESSED)[0] is True
    bind_role_session(con, topic_id=TOPIC, role="EXECUTOR", session_id="cc-afterlaunchparent123", runtime="cc", reason="public rebind after once-valid receipt", replace_active=True)
    assert con.execute("SELECT state FROM debate_worker_claims WHERE trigger_msg_id=?", (trigger,)).fetchone()[0] == "retired"
    assert debate_pump._delivery_acknowledged(trigger, SUPPRESSED)[0] is False
    assert _queue(con, trigger)["EXECUTOR"] is None


def test_diagnostic_direct_versioned_receipt_positive(pump_db, monkeypatch):
    con = pump_db
    bind_role_session(con, topic_id=TOPIC, role="EXECUTOR", session_id=DIAGNOSTIC_SESSION, state="diagnostic", runtime="cc", reason="synthetic direct analysis")
    trigger = debate_post_with_recipients(con, topic_id=TOPIC, role="CONDUCTOR", priority="H", kind="Q", body="direct diagnostic analysis", addressed_to=[], diagnostic_to=[DIAGNOSTIC_SESSION], vehicle="analysis")["msg_id"]
    _insert_wake_log(con, trigger_msg_id=trigger, topic_id=TOPIC, recipient=DIAGNOSTIC_SESSION, action=WAKE_ACTION, result="notified",
                     target_role="EXECUTOR", target_session_id=DIAGNOSTIC_SESSION, target_runtime="cc")
    binding = con.execute("SELECT generation FROM debate_role_bindings WHERE topic_id=? AND session_id=?", (TOPIC,DIAGNOSTIC_SESSION)).fetchone()
    details = {"routing_receipt_version":1,"launch_confirmed":True,"recipient":DIAGNOSTIC_SESSION,"recipient_mode":"diagnostic",
               "binding_session_id":DIAGNOSTIC_SESSION,"binding_generation":binding["generation"],"worker_session_id":DIAGNOSTIC_SESSION}
    _insert_wake_log(con, trigger_msg_id=trigger, topic_id=TOPIC, recipient=DIAGNOSTIC_SESSION, action="external_agent_spawn", result="real_spawn",
        target_role="EXECUTOR", target_session_id=DIAGNOSTIC_SESSION, target_runtime="cc", binding_generation=binding["generation"], details=details)
    dispatch = _FakeDispatch(debate_pump.DB_PATH, claim=False)
    _run_once(monkeypatch, dispatch)
    assert _queue(con, trigger)[DIAGNOSTIC_SESSION] is not None
    assert dispatch.calls == []


def test_diagnostic_does_not_borrow_same_role_actual_claim(pump_db, monkeypatch):
    con = pump_db
    bind_role_session(con, topic_id=TOPIC, role="EXECUTOR", session_id=DIAGNOSTIC_SESSION, state="diagnostic", runtime="cc", reason="synthetic mixed claim fixture")
    trigger = debate_post_with_recipients(con, topic_id=TOPIC, role="CONDUCTOR", priority="H", kind="Q", body="valid mixed analysis claim", addressed_to=["EXECUTOR"], diagnostic_to=[DIAGNOSTIC_SESSION], vehicle="analysis")["msg_id"]
    claim_worker_session(con, topic_id=TOPIC, role="EXECUTOR", parent_session_id=EXECUTOR_SESSION, trigger_msg_id=trigger)
    _notified(con, trigger)
    _insert_wake_log(con, trigger_msg_id=trigger, topic_id=TOPIC, recipient=DIAGNOSTIC_SESSION, action=WAKE_ACTION, result="notified",
                     target_role="EXECUTOR", target_session_id=DIAGNOSTIC_SESSION, target_runtime="cc")
    assert debate_pump._trigger_is_terminal(trigger, SUPPRESSED)
    dispatch = _FakeDispatch(debate_pump.DB_PATH, claim=False)
    _run_once(monkeypatch, dispatch)
    assert _queue(con, trigger)[DIAGNOSTIC_SESSION] is None
    assert dispatch.calls == []


@pytest.mark.parametrize("replace_in_gap", [False, True])
def test_completion_revalidates_current_receipt_at_write_boundary(
    pump_db, monkeypatch, replace_in_gap
):
    """A stale pre-write ACK must not complete delivery to a new generation."""
    con = pump_db
    trigger = _post_analysis_q(con)
    claim = claim_worker_session(
        con, topic_id=TOPIC, role="EXECUTOR",
        parent_session_id=EXECUTOR_SESSION, trigger_msg_id=trigger,
    )
    generation = con.execute(
        "SELECT generation FROM debate_role_bindings WHERE topic_id=? "
        "AND role='EXECUTOR' AND session_id=?",
        (TOPIC, EXECUTOR_SESSION),
    ).fetchone()[0]
    # Narrow consumer receipt fixture; production claim and public replacement
    # remain real. Existing producer tests independently prove receipt emission.
    _insert_wake_log(
        con, trigger_msg_id=trigger, topic_id=TOPIC, recipient="EXECUTOR",
        action="external_agent_spawn", result="real_spawn", target_role="EXECUTOR",
        target_session_id=claim["worker_session_id"], target_runtime="cc",
        binding_generation=generation,
        details={"routing_receipt_version": 1, "launch_confirmed": True,
                 "recipient": "EXECUTOR", "recipient_mode": "normal",
                 "binding_session_id": EXECUTOR_SESSION,
                 "binding_generation": generation,
                 "worker_session_id": claim["worker_session_id"]},
    )
    _notified(con, trigger)
    assert debate_pump._trigger_is_terminal(trigger, SUPPRESSED) is True
    assert debate_pump._delivery_acknowledged(trigger, SUPPRESSED)[0] is True
    original_complete = debate_pump._complete_pending_deliveries

    def completion_boundary(msg_id, **kwargs):
        assert kwargs["suppressed_roles"] == SUPPRESSED
        if replace_in_gap:
            bind_role_session(
                con, topic_id=TOPIC, role="EXECUTOR",
                session_id="cc-consumer_newparent1", runtime="cc",
                reason="public replacement immediately before queue write",
                replace_active=True,
            )
            assert con.execute(
                "SELECT state FROM debate_worker_claims WHERE worker_session_id=?",
                (claim["worker_session_id"],),
            ).fetchone()[0] == "retired"
            assert debate_pump._delivery_acknowledged(trigger, SUPPRESSED)[0] is False
        return original_complete(msg_id, **kwargs)

    monkeypatch.setattr(debate_pump, "_complete_pending_deliveries", completion_boundary)
    dispatch = _FakeDispatch(debate_pump.DB_PATH, claim=False)
    _run_once(monkeypatch, dispatch)
    assert dispatch.calls == []
    queue = _queue(con, trigger)
    assert queue
    if replace_in_gap:
        assert all(value is None for value in queue.values())
        assert _events("pump_targeted_delivery_completed") == []
    else:
        assert all(value is not None for value in queue.values())
        assert len(_events("pump_targeted_delivery_completed")) == 1


def test_guarded_completion_keeps_mixed_targets_pending(pump_db):
    con = pump_db
    trigger = _diagnostic_trigger(con, mixed=True)
    advance_watermark(con, topic_id=TOPIC, role="EXECUTOR", processed_up_to_msg_id=trigger)
    assert debate_pump._complete_pending_deliveries(
        trigger, suppressed_roles=SUPPRESSED
    ) == 0
    assert all(value is None for value in _queue(con, trigger).values())
    # Refusal released its owned write lock.
    con.execute("BEGIN IMMEDIATE")
    con.rollback()


def test_connection_ack_borrows_transaction_without_ending_it(pump_db):
    con = pump_db
    trigger = _post_impl_ping(con)
    _executor_signal_ack(con, trigger)
    statements = []
    con.execute("BEGIN IMMEDIATE")
    con.set_trace_callback(statements.append)
    try:
        assert debate_pump._delivery_acknowledged_on_connection(
            con, trigger, SUPPRESSED
        ) == (True, [])
        assert con.in_transaction is True
        assert not any(sql.split()[0].upper() in {"BEGIN", "COMMIT", "ROLLBACK"}
                       for sql in statements)
        assert con.execute("SELECT 1").fetchone()[0] == 1
    finally:
        con.set_trace_callback(None)
        con.rollback()


def test_guarded_completion_revalidation_holds_writer_lock(pump_db, monkeypatch):
    con = pump_db
    trigger = _post_impl_ping(con)
    _executor_signal_ack(con, trigger)
    original_ack = debate_pump._delivery_acknowledged_on_connection
    observed = []

    def locked_ack(owned, msg_id, suppressed_roles):
        assert owned.in_transaction is True
        contender = sqlite3.connect(debate_pump.DB_PATH, isolation_level=None, timeout=0)
        contender.row_factory = sqlite3.Row
        try:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                bind_role_session(
                    contender, topic_id=TOPIC, role="EXECUTOR",
                    session_id="cc-blocked_newparent1", runtime="cc",
                    reason="public concurrent writer during atomic ACK",
                    replace_active=True,
                )
        finally:
            contender.close()
        observed.append(owned)
        return original_ack(owned, msg_id, suppressed_roles)

    monkeypatch.setattr(debate_pump, "_delivery_acknowledged_on_connection", locked_ack)
    assert debate_pump._complete_pending_deliveries(
        trigger, suppressed_roles=SUPPRESSED
    ) > 0
    assert len(observed) == 1
    assert all(value is not None for value in _queue(con, trigger).values())
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        observed[0].execute("SELECT 1")
    con.execute("BEGIN IMMEDIATE")
    con.rollback()


def test_guarded_completion_sql_error_rolls_back_insert_and_releases_lock(pump_db):
    con = pump_db
    trigger = _post_impl_ping(con)
    _executor_signal_ack(con, trigger)
    # Synthetic SQLite failure, not a mocked DAO verdict or production schema.
    con.execute("DELETE FROM debate_delivery_queue WHERE msg_id=?", (trigger,))
    con.execute("CREATE TRIGGER synthetic_queue_abort BEFORE UPDATE ON debate_delivery_queue BEGIN SELECT RAISE(ABORT,'synthetic queue write refused'); END")
    try:
        with pytest.raises(sqlite3.IntegrityError, match="synthetic queue write refused"):
            debate_pump._complete_pending_deliveries(trigger, suppressed_roles=SUPPRESSED)
        assert _queue(con, trigger) == {}
        con.execute("BEGIN IMMEDIATE")
        con.rollback()
    finally:
        con.execute("DROP TRIGGER synthetic_queue_abort")
