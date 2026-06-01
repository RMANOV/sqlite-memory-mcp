"""v3.12 vehicle-tagging + fail-closed implementation router.

Covers the operator-approved solution #5 (durable rule, code-enforced):

  * ``vehicle`` persists on debate messages and validates (bad value → typed
    ``invalid_vehicle`` DebateError pre-INSERT, no row written);
  * the wake/pump chokepoint REFUSES implementation-tagged work with the typed
    ``implementation_requires_impl_vehicle`` error and does NOT allocate a
    no-edit ``-W<n>`` worker (fail closed, not a renamed bounce);
  * ``analysis`` / ``review`` still flow to wake-workers as before;
  * backcompat — legacy messages with no vehicle behave as ``analysis``.

The guard lives at the deepest shared chokepoint (``claim_worker_session``,
exercised by the wake hook, the pump hook, and the direct
``debate_worker_claim`` MCP tool) plus a signal-only resolution seam in
``prepare_wake_dry_run``. Tests assert at the DAO level — they do not touch
``subprocess.Popen``; the absence of a worker-claim row is the proof that no
no-edit worker would be spawned.
"""

from __future__ import annotations

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from debate import (
    DEFAULT_VEHICLE,
    VALID_VEHICLES,
    WAKE_WORKER_VEHICLES,
    DebateError,
    bind_role_session,
    claim_worker_session,
    debate_post_with_recipients,
    init_debate,
    normalize_vehicle,
    post_message,
    prepare_wake_dry_run,
    transition_state,
    validate_vehicle,
)
from schema import init_db


@pytest.fixture
def topic(tmp_path):
    db_path = str(tmp_path / "vehicle.db")
    init_db(db_path)
    c = sqlite3.connect(db_path, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    init_debate(
        c,
        topic_id="X1",
        title="vehicle tagging",
        roles=[
            {"role": "CONDUCTOR", "session_id": "codex-cond1"},
            {"role": "EXECUTOR", "session_id": "codex-exec1"},
        ],
        created_by_role="CONDUCTOR",
    )
    transition_state(c, topic_id="X1", role="CONDUCTOR", new_state="ACTIVE")
    bind_role_session(
        c,
        topic_id="X1",
        role="EXECUTOR",
        session_id="codex-exec1",
        reason="primary",
    )
    yield c, "X1"
    c.close()


def _vehicle_col(conn, msg_id):
    return conn.execute(
        "SELECT vehicle FROM debate_messages WHERE msg_id = ?", (msg_id,)
    ).fetchone()["vehicle"]


def _worker_claim_count(conn):
    return conn.execute(
        "SELECT COUNT(*) AS c FROM debate_worker_claims"
    ).fetchone()["c"]


def _post(conn, topic_id, **kw):
    """Post a STATUS addressed to EXECUTOR with an optional vehicle."""
    return debate_post_with_recipients(
        conn,
        topic_id=topic_id,
        role="CONDUCTOR",
        priority="H",
        kind="STATUS",
        body=kw.pop("body", "wake executor"),
        addressed_to=["EXECUTOR"],
        **kw,
    )


# ── 1. vehicle persists + validates ──────────────────────────────────────


@pytest.mark.parametrize("vehicle", VALID_VEHICLES)
def test_vehicle_persists_each_valid_value(topic, vehicle):
    conn, t = topic
    out = post_message(
        conn,
        topic_id=t,
        role="EXECUTOR",
        priority="INFO",
        kind="STATUS",
        body="x",
        vehicle=vehicle,
    )
    assert _vehicle_col(conn, out["msg_id"]) == vehicle
    assert out["vehicle"] == vehicle


def test_bad_vehicle_value_rejected_pre_insert(topic):
    conn, t = topic
    before = conn.execute(
        "SELECT COUNT(*) AS c FROM debate_messages WHERE topic_id = ?", (t,)
    ).fetchone()["c"]
    with pytest.raises(DebateError) as ei:
        post_message(
            conn,
            topic_id=t,
            role="EXECUTOR",
            priority="INFO",
            kind="STATUS",
            body="x",
            vehicle="deploy",
        )
    assert ei.value.error_type == "invalid_vehicle"
    after = conn.execute(
        "SELECT COUNT(*) AS c FROM debate_messages WHERE topic_id = ?", (t,)
    ).fetchone()["c"]
    # Atomic: a rejected vehicle leaves NO row behind.
    assert before == after


def test_validate_vehicle_helper_typed_error():
    with pytest.raises(DebateError) as ei:
        validate_vehicle("nope")
    assert ei.value.error_type == "invalid_vehicle"
    for v in VALID_VEHICLES:
        validate_vehicle(v)  # no raise


def test_normalize_vehicle_defaults_and_validates():
    assert normalize_vehicle(None) == DEFAULT_VEHICLE
    assert normalize_vehicle("") == DEFAULT_VEHICLE
    assert normalize_vehicle("review") == "review"
    with pytest.raises(DebateError):
        normalize_vehicle("ship-it")


# ── 2. fail-closed router: implementation refused, NO worker spawned ──────


def test_claim_refuses_implementation_and_spawns_no_worker(topic):
    """The deepest shared chokepoint covering wake + pump + direct claim."""
    conn, t = topic
    impl = _post(conn, t, vehicle="implementation", body="apply the patch")
    assert impl["vehicle"] == "implementation"

    with pytest.raises(DebateError) as ei:
        claim_worker_session(
            conn,
            topic_id=t,
            role="EXECUTOR",
            parent_session_id="codex-exec1",
            trigger_msg_id=impl["msg_id"],
        )
    assert ei.value.error_type == "implementation_requires_impl_vehicle"
    # Fail CLOSED, not a renamed bounce: NO -W<n> worker claim was allocated.
    assert _worker_claim_count(conn) == 0


def test_dry_run_resolution_refuses_implementation_with_typed_audit(topic):
    conn, t = topic
    impl = _post(conn, t, vehicle="implementation", body="apply the patch")
    out = prepare_wake_dry_run(conn, tool_response=impl)
    # No wake targets resolved...
    assert out["targets"] == []
    assert out["suppressed"] == 0
    # ...and a typed audit row was written (debate_wake_log.result is TEXT).
    assert out["logs"], "expected an audit log row for the refusal"
    assert out["logs"][0]["result"] == "implementation_requires_impl_vehicle"
    # The hook path that consumes these targets has nothing to dispatch.
    assert _worker_claim_count(conn) == 0


def test_dry_run_refuses_implementation_for_session_diagnostic_target(topic):
    """The resolution seam (not the claim guard) is what makes fail-closed
    UNIVERSAL. For a session/diagnostic-addressed target, the wake hook's
    ``_claim_worker_target`` early-returns (recipient != role) and
    ``_launch_agent`` goes straight to subprocess.Popen WITHOUT calling
    ``claim_worker_session`` — so the claim guard alone would miss it. The
    dry-run guard fires *before* recipient resolution, returning zero targets
    for every addressing mode (role, session, diagnostic), so the hook has
    nothing to launch."""
    conn, t = topic
    # Diagnostic binding for a session that is NOT the role name.
    bind_role_session(
        conn,
        topic_id=t,
        role="EXECUTOR",
        session_id="codex-diag1",
        state="diagnostic",
        reason="diagnostic",
    )
    # Sanity: an analysis diagnostic-addressed message DOES resolve a target
    # whose recipient is the session_id (the path the claim guard misses).
    ana = debate_post_with_recipients(
        conn,
        topic_id=t,
        role="CONDUCTOR",
        priority="H",
        kind="STATUS",
        body="analyze (diagnostic)",
        addressed_to=["EXECUTOR"],
        diagnostic_to=["codex-diag1"],
        vehicle="analysis",
    )
    ana_out = prepare_wake_dry_run(conn, tool_response=ana)
    diag_targets = [
        tg for tg in ana_out["targets"] if tg.get("recipient") == "codex-diag1"
    ]
    assert diag_targets, "diagnostic session target should resolve for analysis"
    assert diag_targets[0]["recipient"] != diag_targets[0]["target_role"]

    # Now the same diagnostic addressing tagged implementation: refused
    # wholesale at the resolution seam — zero targets, typed audit row.
    impl = debate_post_with_recipients(
        conn,
        topic_id=t,
        role="CONDUCTOR",
        priority="H",
        kind="STATUS",
        body="apply the patch (diagnostic)",
        addressed_to=["EXECUTOR"],
        diagnostic_to=["codex-diag1"],
        vehicle="implementation",
    )
    impl_out = prepare_wake_dry_run(conn, tool_response=impl)
    assert impl_out["targets"] == []
    assert impl_out["logs"][0]["result"] == "implementation_requires_impl_vehicle"


# ── 3. analysis / review still flow to wake-workers ───────────────────────


@pytest.mark.parametrize("vehicle", WAKE_WORKER_VEHICLES)
def test_wake_worker_vehicles_resolve_and_claim(topic, vehicle):
    conn, t = topic
    post = _post(conn, t, vehicle=vehicle, body=f"{vehicle} this")
    out = prepare_wake_dry_run(conn, tool_response=post)
    assert out["targets"], f"{vehicle} should resolve a wake target"
    assert out["targets"][0]["target_session_id"] == "codex-exec1"
    assert out["targets"][0]["target_role"] == "EXECUTOR"

    claim = claim_worker_session(
        conn,
        topic_id=t,
        role="EXECUTOR",
        parent_session_id="codex-exec1",
        trigger_msg_id=post["msg_id"],
    )
    assert claim["worker_session_id"].startswith("codex-exec1-W")
    assert claim["no_action"] is False
    assert _worker_claim_count(conn) == 1


# ── 4. backcompat: legacy untagged behaves as analysis ────────────────────


def test_legacy_untagged_message_behaves_as_analysis(topic):
    conn, t = topic
    legacy = _post(conn, t, body="legacy untagged")
    # Stored NULL, surfaced as the default analysis vehicle.
    assert _vehicle_col(conn, legacy["msg_id"]) is None
    assert legacy["vehicle"] == DEFAULT_VEHICLE == "analysis"

    out = prepare_wake_dry_run(conn, tool_response=legacy)
    assert out["targets"], "untagged (analysis-default) must flow to a worker"

    claim = claim_worker_session(
        conn,
        topic_id=t,
        role="EXECUTOR",
        parent_session_id="codex-exec1",
        trigger_msg_id=legacy["msg_id"],
    )
    assert claim["worker_session_id"].startswith("codex-exec1-W")
    assert _worker_claim_count(conn) == 1


def test_legacy_row_with_db_null_vehicle_resolves(topic):
    """Simulate a pre-v3.12 row by NULLing the column post-insert, proving
    the router treats DB-NULL exactly as analysis (true on-disk backcompat)."""
    conn, t = topic
    post = _post(conn, t, vehicle="analysis", body="pre-migration row")
    conn.execute(
        "UPDATE debate_messages SET vehicle = NULL WHERE msg_id = ?",
        (post["msg_id"],),
    )
    assert _vehicle_col(conn, post["msg_id"]) is None
    out = prepare_wake_dry_run(conn, tool_response=post)
    assert out["targets"], "DB-NULL vehicle must resolve as analysis"
    claim = claim_worker_session(
        conn,
        topic_id=t,
        role="EXECUTOR",
        parent_session_id="codex-exec1",
        trigger_msg_id=post["msg_id"],
    )
    assert claim["worker_session_id"].startswith("codex-exec1-W")
