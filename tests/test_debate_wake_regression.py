"""Regression tests for the ffb235d debate wake fixes.

Per ADVOCATE post-hoc gate 3fd85c9584e3 (MAJOR test-gap) routed via
CONDUCTOR 5704c38f83e7 to the EXECUTOR3 infra follow-up lane:
  (a) implementation-tagged trigger -> worker guard holds (targets == [])
      while the addressed ACTIVE binding resolves NOTIFY-ONLY, and the
      'impl_notified' audit row dedupes hook + pump rescans;
  (b) plain-post PING derives recipients from target= tokens strictly
      roster-only -- undeclared roles and session-id tokens are refused;
  (c) every resource-budget tier keeps PING in action_kinds so
      ESCALATE:WAKE pings survive machine-state downgrades.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from debate import (
    _insert_wake_log,
    bind_role_session,
    debate_post_with_recipients,
    init_debate,
    post_message,
    prepare_wake_dry_run,
    transition_state,
)
from schema import init_db

ROOT = Path(__file__).resolve().parent.parent

IMPL_SESSION = "cc-exec3test01"
WAKE_ACTION = "post_tool_use_wake"


def _load_hook_module(name: str, rel_path: str):
    spec = spec_from_file_location(name, ROOT / rel_path)
    module = module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def topic(tmp_path):
    db_path = str(tmp_path / "debate_wake_regression.db")
    init_db(db_path)
    c = sqlite3.connect(db_path, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    init_debate(
        c, topic_id="WR1", title="wake-regression-tests",
        roles=[
            {"role": "CONDUCTOR", "session_id": "s-cond"},
            {"role": "EXECUTOR3", "session_id": "s-exec3"},
        ],
        created_by_role="CONDUCTOR",
    )
    transition_state(c, topic_id="WR1", role="CONDUCTOR", new_state="ACTIVE")
    bind_role_session(
        c, topic_id="WR1", role="EXECUTOR3", session_id=IMPL_SESSION,
        runtime="cc", reason="regression fixture",
    )
    yield c, "WR1"
    c.close()


def test_impl_trigger_notify_only_and_rescan_dedupe(topic):
    conn, t = topic
    out = debate_post_with_recipients(
        conn, topic_id=t, role="CONDUCTOR", priority="H", kind="Q",
        body="impl hand-off: fix the widget on your branch",
        addressed_to=["EXECUTOR3"], vehicle="implementation",
    )
    tool_response = {
        "msg_id": out["msg_id"],
        "topic_id": t,
        "schema_version": out["schema_version"],
    }

    res = prepare_wake_dry_run(
        conn, tool_response=tool_response, action=WAKE_ACTION,
    )
    # Worker guard must survive the notify branch: no dispatchable targets.
    assert res["targets"] == []
    assert res["logs"][0]["result"] == "implementation_requires_impl_vehicle"
    # The addressed ACTIVE binding resolves as a notify-only target.
    assert [
        (n["target_role"], n["target_session_id"], n["result"])
        for n in res["notify_targets"]
    ] == [("EXECUTOR3", IMPL_SESSION, "impl_notify_only")]

    # hooks/debate_wake.py writes this row after notify-send; replicate it
    # and verify hook + pump rescans dedupe on (trigger, session, action).
    _insert_wake_log(
        conn, trigger_msg_id=out["msg_id"], topic_id=t,
        recipient="EXECUTOR3", action=WAKE_ACTION, result="impl_notified",
        target_role="EXECUTOR3", target_session_id=IMPL_SESSION,
        target_runtime="cc",
    )
    rescan = prepare_wake_dry_run(
        conn, tool_response=tool_response, action=WAKE_ACTION,
    )
    assert rescan["targets"] == []
    assert rescan["notify_targets"] == []


def test_ping_recipient_derivation_is_roster_only(topic):
    conn, t = topic
    out = post_message(
        conn, topic_id=t, role="CONDUCTOR", priority="H", kind="PING",
        body=(
            "[ESCALATE:WAKE] wake needed target=EXECUTOR3 target=executor3 "
            "target=BOGUSROLE target=cc-fakesession1234"
        ),
    )
    rows = conn.execute(
        "SELECT recipient, recipient_mode FROM debate_message_recipients "
        "WHERE msg_id = ? ORDER BY recipient",
        (out["msg_id"],),
    ).fetchall()
    # Case-folded roster role accepted exactly once; the undeclared role and
    # the session-id token are refused (no free-form fan-out, no injection).
    assert [(r["recipient"], r["recipient_mode"]) for r in rows] == [
        ("EXECUTOR3", "normal")
    ]


def test_ping_survives_every_budget_tier():
    module = _load_hook_module(
        "debate_resource_budget_ping_tiers_test",
        "hooks/debate_resource_budget.py",
    )

    def snapshot(temp_c: float) -> object:
        return module.ResourceSnapshot(
            mem_total_mib=32768,
            mem_available_mib=16000,
            swap_total_mib=8192,
            swap_free_mib=8192,
            cpu_count=8,
            load1=0.5,
            memory_full_avg10=0.0,
            max_temp_c=temp_c,
            live_agent_count=0,
        )

    cases = [
        (snapshot(106.0), "blocked"),
        (snapshot(85.0), "low"),
        (snapshot(75.0), "guarded"),
        (snapshot(60.0), "normal"),
    ]
    for snap, expected_tier in cases:
        budget = module.compute_debate_resource_budget(snap)
        assert budget.tier == expected_tier, (
            f"snapshot {snap.max_temp_c}C hit tier {budget.tier}, "
            f"expected {expected_tier}"
        )
        assert "PING" in budget.action_kinds, (
            f"tier {budget.tier} dropped PING from action_kinds"
        )
