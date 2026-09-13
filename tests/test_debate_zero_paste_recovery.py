"""Advocate-BLOCK recovery tests for REV 2.2 zero-paste delivery.

Covers the two critical lost-work defects and the concurrency high-risk the
adversarial audit raised against task 0d806934:

  critical #1 — a dead worker (retired claim, no reply) must be re-dispatched;
                the pump cursor must NOT advance past a non-terminal trigger.
  critical #2 — a fresh start with no state file must sweep the pre-existing
                backlog from epoch, not from now().
  high-risk #1 — machine-wide live-worker count survives a pump restart.

All tests use temp DBs; production memory.db is never touched.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOKS = os.path.join(REPO, "hooks")
for path in (REPO, HOOKS):
    if path not in sys.path:
        sys.path.insert(0, path)

from debate import (  # noqa: E402 - imported after local repo path bootstrap
    bind_role_session,
    claim_worker_session,
    debate_post_with_recipients,
    init_debate,
    prepare_wake_dry_run,
    recover_stale_worker_claims,
)
from schema import init_db  # noqa: E402 - imported after local repo path bootstrap


@pytest.fixture
def conn(tmp_path):
    db_path = str(tmp_path / "zero_paste.db")
    init_db(db_path)
    c = sqlite3.connect(db_path, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    yield c
    c.close()


def _seed(c, topic="ZP_RECOVERY"):
    init_debate(
        c,
        topic_id=topic,
        title="zero-paste recovery",
        roles=[
            {"role": "CONDUCTOR", "session_id": "cc-cond_x"},
            {"role": "WORKER", "session_id": "cc-worker_x"},
        ],
        created_by_role="CONDUCTOR",
    )
    # DAO init_debate does not seed bindings (the MCP layer does); bind the
    # worker role so claim_worker_session has an active parent binding.
    for role, sid in (("CONDUCTOR", "cc-cond_x"), ("WORKER", "cc-worker_x")):
        bind_role_session(
            conn=c,
            topic_id=topic,
            role=role,
            session_id=sid,
            reason="test seed",
        )
    return topic


def _post_addressed(c, topic="ZP_RECOVERY", *, body="do work"):
    out = debate_post_with_recipients(
        c,
        topic_id=topic,
        role="CONDUCTOR",
        priority="H",
        kind="Q",
        body=body,
        addressed_to=["WORKER"],
    )
    return out["msg_id"]


# ── critical #1: retired claim is re-dispatchable (requeue) ─────────────────


def test_retired_claim_reactivates_on_reclaim(conn):
    topic = _seed(conn)
    trigger = _post_addressed(conn)
    claim = claim_worker_session(
        conn,
        topic_id=topic,
        role="WORKER",
        parent_session_id="cc-worker_x",
        trigger_msg_id=trigger,
    )
    assert claim["state"] == "active"
    worker_sid = claim["worker_session_id"]

    # Worker dies without replying → orphan recovery retires the claim.
    conn.execute(
        "UPDATE debate_worker_claims SET heartbeat_at = '2000-01-01T00:00:00Z' "
        "WHERE worker_session_id = ?",
        (worker_sid,),
    )
    rec = recover_stale_worker_claims(
        conn,
        topic_id=topic,
        older_than_ts="2020-01-01T00:00:00Z",
        minimum_age_seconds=0,
    )
    assert rec["retired_count"] == 1
    row = conn.execute(
        "SELECT state FROM debate_worker_claims WHERE worker_session_id = ?",
        (worker_sid,),
    ).fetchone()
    assert row["state"] == "retired"

    # Re-dispatch must REACTIVATE the same claim, not bounce as duplicate.
    reclaim = claim_worker_session(
        conn,
        topic_id=topic,
        role="WORKER",
        parent_session_id="cc-worker_x",
        trigger_msg_id=trigger,
    )
    assert reclaim["state"] == "active"
    assert reclaim.get("reactivated") is True
    assert reclaim["no_action"] is False


def test_requeue_is_bounded_and_surfaces_exhaustion(conn, monkeypatch):
    monkeypatch.setenv("DEBATE_WORKER_MAX_REQUEUES", "2")
    topic = _seed(conn)
    trigger = _post_addressed(conn)
    claim = claim_worker_session(
        conn,
        topic_id=topic,
        role="WORKER",
        parent_session_id="cc-worker_x",
        trigger_msg_id=trigger,
    )
    worker_sid = claim["worker_session_id"]

    def _kill_and_reclaim():
        conn.execute(
            "UPDATE debate_worker_claims SET state = 'retired' "
            "WHERE worker_session_id = ?",
            (worker_sid,),
        )
        return claim_worker_session(
            conn,
            topic_id=topic,
            role="WORKER",
            parent_session_id="cc-worker_x",
            trigger_msg_id=trigger,
        )

    r1 = _kill_and_reclaim()
    assert r1.get("reactivated") is True
    r2 = _kill_and_reclaim()
    assert r2.get("reactivated") is True
    # Third reclaim exceeds max_requeues=2 → exhaustion surfaced, no reactivation.
    r3 = _kill_and_reclaim()
    assert r3.get("requeue_exhausted") is True
    assert r3["no_action"] is True
    assert r3["state"] == "retired"


# ── critical #1: cursor terminal semantics ─────────────────────────────────


def test_trigger_terminal_and_demand_semantics(conn, tmp_path, monkeypatch):
    """The pump's cursor gate and dispatch demand must agree with the DB:
    dispatched+active = in-flight (hold cursor, no re-spawn); retired+no-reply
    = re-dispatch; reply-exists = terminal."""
    import debate_pump

    topic = _seed(conn)
    trigger = _post_addressed(conn)
    claim = claim_worker_session(
        conn,
        topic_id=topic,
        role="WORKER",
        parent_session_id="cc-worker_x",
        trigger_msg_id=trigger,
    )
    worker_sid = claim["worker_session_id"]

    db_path = os.path.join(str(tmp_path), "zero_paste.db")
    monkeypatch.setattr(debate_pump, "DB_PATH", db_path)
    suppressed = {"CONDUCTOR"}

    # Active claim, no reply → in-flight: not terminal, and no NEW spawn needed.
    assert debate_pump._trigger_is_terminal(trigger, suppressed) is False
    assert debate_pump._estimate_worker_demand(trigger, suppressed) == 0

    # Worker dies (retired), still no reply → NOT terminal AND demand > 0.
    conn.execute(
        "UPDATE debate_worker_claims SET state = 'retired' WHERE worker_session_id = ?",
        (worker_sid,),
    )
    assert debate_pump._trigger_is_terminal(trigger, suppressed) is False
    assert debate_pump._estimate_worker_demand(trigger, suppressed) == 1

    # Worker posts a reply → terminal, demand drops to 0.
    debate_post_with_recipients(
        conn,
        topic_id=topic,
        role="WORKER",
        priority="M",
        kind="A",
        body="done",
        addressed_to=["CONDUCTOR"],
        reply_to=trigger,
        # Reply ownership: the worker is dead/retired, so the reply that makes
        # the trigger terminal comes from the bound parent session.
        author_session_id="cc-worker_x",
    )
    assert debate_pump._trigger_is_terminal(trigger, suppressed) is True
    assert debate_pump._estimate_worker_demand(trigger, suppressed) == 0


def test_completed_claim_is_terminal(conn, tmp_path, monkeypatch):
    import debate_pump

    topic = _seed(conn)
    trigger = _post_addressed(conn)
    claim = claim_worker_session(
        conn,
        topic_id=topic,
        role="WORKER",
        parent_session_id="cc-worker_x",
        trigger_msg_id=trigger,
    )
    conn.execute(
        "UPDATE debate_worker_claims SET state = 'completed', completed_at = ? "
        "WHERE worker_session_id = ?",
        ("2026-07-21T00:00:00Z", claim["worker_session_id"]),
    )
    monkeypatch.setattr(
        debate_pump, "DB_PATH", os.path.join(str(tmp_path), "zero_paste.db")
    )
    assert debate_pump._trigger_is_terminal(trigger, {"CONDUCTOR"}) is True
    assert debate_pump._estimate_worker_demand(trigger, {"CONDUCTOR"}) == 0


# ── critical #2: first-start backlog sweep from epoch ───────────────────────


def test_fetch_new_from_epoch_sees_pre_existing_backlog(conn, tmp_path, monkeypatch):
    import debate_pump

    _seed(conn)
    trigger = _post_addressed(conn)
    monkeypatch.setattr(
        debate_pump, "DB_PATH", os.path.join(str(tmp_path), "zero_paste.db")
    )

    # now()-anchored cursor (the OLD default) misses the pre-existing trigger.
    from db_utils import now_iso

    missed = debate_pump._fetch_new(now_iso(), "", [], ["Q"], 100)
    assert all(r["msg_id"] != trigger for r in missed)

    # epoch cursor (the fix for a missing/corrupt state file) sees it.
    seen = debate_pump._fetch_new("1970-01-01T00:00:00Z", "", [], ["Q"], 100)
    assert any(r["msg_id"] == trigger for r in seen)


# ── high-risk #1: machine-wide live worker count ────────────────────────────


def test_machine_live_worker_count_uses_db_not_process(conn, tmp_path, monkeypatch):
    import debate_pump

    topic = _seed(conn)
    trigger = _post_addressed(conn)
    claim = claim_worker_session(
        conn,
        topic_id=topic,
        role="WORKER",
        parent_session_id="cc-worker_x",
        trigger_msg_id=trigger,
    )
    monkeypatch.setattr(
        debate_pump, "DB_PATH", os.path.join(str(tmp_path), "zero_paste.db")
    )
    # Simulate a fresh pump process: CHILDREN empty, but a live worker exists
    # in the DB. The machine-wide count must find it via the spawn receipt.
    #
    # Patch the CLAIM-KEY helper — that is what the census calls. Patching the
    # bare-id helper used to pass here for the wrong reason: the census unions
    # whatever the stub returns, so a set holding one string counted as one
    # worker even though the census's own identity is the (topic, role, id)
    # triple. Same number, different reason, and it hid the real contract.
    monkeypatch.setattr(
        debate_pump,
        "_live_worker_claim_keys",
        lambda topic_id: {(topic_id, "WORKER", claim["worker_session_id"])},
    )
    assert debate_pump._machine_live_worker_count([topic]) == 1
    assert debate_pump._machine_live_worker_count([]) == 1  # resolves active topics


# ── critical #1 THROUGH THE REAL RESOLVER (advocate req #4) ─────────────────


def test_resolver_re_dispatches_after_worker_death(conn):
    """The real prepare_wake_dry_run resolver — not a mock — must stop
    suppressing a trigger once its worker died (retired claim, no reply).
    This is the seam the previous fix missed (advocate 2nd BLOCK #1)."""
    topic = _seed(conn)
    post = debate_post_with_recipients(
        conn,
        topic_id=topic,
        role="CONDUCTOR",
        priority="H",
        kind="Q",
        body="do work",
        addressed_to=["WORKER"],
    )
    trigger = post["msg_id"]
    action = "post_tool_use_wake"
    # The resolver validates schema_version → pass the real post response.
    tr = {"msg_id": trigger, "schema_version": post["schema_version"]}

    # 1st resolve → target dispatched (a wake_log 'dispatched' row is written
    # by the launcher; emulate that + the claim the launcher would make).
    first = prepare_wake_dry_run(conn, tool_response=tr, action=action)
    assert [t["result"] for t in first["targets"]] == ["dry_run"]
    claim = claim_worker_session(
        conn,
        topic_id=topic,
        role="WORKER",
        parent_session_id="cc-worker_x",
        trigger_msg_id=trigger,
    )
    conn.execute(
        "UPDATE debate_wake_log SET result = 'dispatched' "
        "WHERE trigger_msg_id = ? AND target_session_id = ?",
        (trigger, "cc-worker_x"),
    )

    # While the claim is active → resolver SUPPRESSES (no double spawn).
    active = prepare_wake_dry_run(conn, tool_response=tr, action=action)
    assert [t["result"] for t in active["targets"]] == ["suppressed"]

    # Worker dies → claim retired, still no reply.
    conn.execute(
        "UPDATE debate_worker_claims SET state = 'retired' WHERE worker_session_id = ?",
        (claim["worker_session_id"],),
    )
    # Resolver must now RE-DISPATCH (not suppress) — the load-bearing fix.
    # (Terminality AFTER a reply is the pump cursor's job — proven in
    # test_pump_holds_cursor_until_trigger_is_terminal — not the resolver's,
    # which only prevents double-spawn of a live/covered worker.)
    revived = prepare_wake_dry_run(conn, tool_response=tr, action=action)
    assert [t["result"] for t in revived["targets"]] != ["suppressed"]


# ── critical #2: unbound addressed role is NOT terminal ─────────────────────


def test_unbound_addressed_role_is_not_terminal(conn, tmp_path, monkeypatch):
    import debate_pump

    topic = "ZP_UNBOUND"
    init_debate(
        conn,
        topic_id=topic,
        title="unbound",
        roles=[
            {"role": "CONDUCTOR", "session_id": "cc-cond_u"},
            {"role": "GHOST", "session_id": "cc-ghost_u"},
        ],
        created_by_role="CONDUCTOR",
    )
    # Bind only CONDUCTOR; GHOST is addressed but has NO active binding.
    bind_role_session(
        conn=conn,
        topic_id=topic,
        role="CONDUCTOR",
        session_id="cc-cond_u",
        reason="seed",
    )
    trigger = debate_post_with_recipients(
        conn,
        topic_id=topic,
        role="CONDUCTOR",
        priority="H",
        kind="Q",
        body="anyone home?",
        addressed_to=["GHOST"],
    )["msg_id"]
    monkeypatch.setattr(
        debate_pump, "DB_PATH", os.path.join(str(tmp_path), "zero_paste.db")
    )
    # GHOST unbound → pending work → the cursor must NOT treat it as terminal.
    assert (
        debate_pump._has_unbound_addressed_recipient(conn, trigger, {"CONDUCTOR"})
        is True
    )
    assert debate_pump._trigger_is_terminal(trigger, {"CONDUCTOR"}) is False


def test_suppressed_only_recipient_is_terminal(conn, tmp_path, monkeypatch):
    """A message whose only recipient is a suppressed role (CONDUCTOR) is
    terminal for wake purposes — it must NOT wedge the cursor."""
    import debate_pump

    topic = _seed(conn)
    reply_like = debate_post_with_recipients(
        conn,
        topic_id=topic,
        role="WORKER",
        priority="M",
        kind="STATUS",
        body="fyi",
        addressed_to=["CONDUCTOR"],
    )["msg_id"]
    monkeypatch.setattr(
        debate_pump, "DB_PATH", os.path.join(str(tmp_path), "zero_paste.db")
    )
    assert (
        debate_pump._has_unbound_addressed_recipient(conn, reply_like, {"CONDUCTOR"})
        is False
    )
    assert debate_pump._trigger_is_terminal(reply_like, {"CONDUCTOR"}) is True


# ── critical #1 end-to-end: cursor holds until terminal (loop-level) ────────


def test_pump_holds_cursor_until_trigger_is_terminal(conn, tmp_path, monkeypatch):
    """Drive the real pump loop with --once against a temp DB. A dispatched-
    but-unanswered trigger must NOT advance the cursor; only after a terminal
    reply exists does the next scan advance past it. This is the airtight
    end-to-end proof of advocate BLOCK critical #1."""
    import sys as _sys

    import debate_pump

    topic = _seed(conn)
    trigger = _post_addressed(conn)
    db_path = os.path.join(str(tmp_path), "zero_paste.db")
    state_path = tmp_path / "pump_state.json"
    monkeypatch.setattr(debate_pump, "DB_PATH", db_path)
    monkeypatch.setattr(debate_pump, "STATE_PATH", state_path)
    monkeypatch.setattr(debate_pump, "HEARTBEAT_PATH", tmp_path / "hb.json")
    monkeypatch.setattr(debate_pump, "LOG_PATH", tmp_path / "pump.jsonl")
    monkeypatch.setattr(debate_pump, "STOP", False, raising=False)
    monkeypatch.setenv("DEBATE_RESOURCE_BUDGET", "off")
    monkeypatch.setattr(debate_pump, "IS_WINDOWS", False)  # skip OS singleton in-test

    # Dispatch: create the active worker claim + a 'dispatched' wake_log row,
    # exactly what the real launcher does — but post NO reply (worker still
    # in-flight / about to die).
    def fake_dispatch(row, suppressed_roles):
        c2 = sqlite3.connect(db_path, isolation_level=None)
        c2.row_factory = sqlite3.Row
        try:
            claim_worker_session(
                c2,
                topic_id=row["topic_id"],
                role="WORKER",
                parent_session_id="cc-worker_x",
                trigger_msg_id=row["msg_id"],
            )
        finally:
            c2.close()
        return 1

    monkeypatch.setattr(debate_pump, "_dispatch_row", fake_dispatch)
    monkeypatch.setattr(
        _sys,
        "argv",
        [
            "debate_pump.py",
            "--once",
            "--since",
            "1970-01-01T00:00:00Z",
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

    def _trigger_still_pending() -> bool:
        state = (
            json.loads(state_path.read_text(encoding="utf-8"))
            if state_path.exists()
            else {}
        )
        rows = debate_pump._fetch_new(
            state.get("last_ts", "1970-01-01T00:00:00Z"),
            state.get("last_msg_id", ""),
            [],
            ["Q"],
            100,
        )
        return any(r["msg_id"] == trigger for r in rows)

    assert debate_pump.main() == 0
    # In-flight (dispatched, no reply): the cursor must still regard the
    # trigger as pending — it was NOT skipped.
    assert _trigger_still_pending() is True

    # Worker replies → trigger becomes terminal.
    debate_post_with_recipients(
        conn,
        topic_id=topic,
        role="WORKER",
        priority="M",
        kind="A",
        body="done",
        addressed_to=["CONDUCTOR"],
        reply_to=trigger,
        # Reply ownership: attributed reply (bound parent of the WORKER role).
        author_session_id="cc-worker_x",
    )
    monkeypatch.setattr(debate_pump, "STOP", False, raising=False)
    assert debate_pump.main() == 0
    # Terminal now: the cursor has advanced past the trigger — no longer pending.
    assert _trigger_still_pending() is False
