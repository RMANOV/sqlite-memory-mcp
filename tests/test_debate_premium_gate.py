from __future__ import annotations

import json
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import db_utils
import intel_server
import premium_runtime
from schema import init_db


_DEBATE_GATE_ENV_VARS = (
    "SQLITE_MEMORY_DEBATE_GATE_ENABLED",
    "SQLITE_MEMORY_DEBATE_GATE_DISABLED",
)


@pytest.fixture(autouse=True)
def _isolate_debate_gate_env(monkeypatch):
    # Default-off proofs are only valid if ambient env does not pre-enable the
    # gate. Clear the gate env vars before every test in this module.
    for name in _DEBATE_GATE_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def debate_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "memory.db")
    init_db(db_path)
    # Wrapper-shaped: route intel_server through the REAL db_utils wrappers
    # (explicit db_path => tmp only, never the live DB, and never
    # ensure_db_initialized). This exercises the production nested-tx +
    # caller-context-manager contract instead of a hand-rolled raw ctx.
    monkeypatch.setattr(intel_server, "_get_conn", lambda: db_utils.get_conn(db_path))
    monkeypatch.setattr(
        intel_server,
        "_get_conn_immediate",
        lambda: db_utils.get_conn_immediate(db_path),
    )
    return db_path


def _roles_json() -> str:
    return json.dumps(
        [
            {"role": "CONDUCTOR", "session_id": "codex-cond20260531"},
            {"role": "ADVOCATE", "session_id": "codex-adv20260531"},
            {"role": "EXECUTOR_1", "session_id": "codex-exec20260531"},
        ]
    )


def _priority_metadata_json() -> str:
    return json.dumps(
        {
            "priority_lane": "P2",
            "priority_reason": "test topic blocks premium gate validation",
        }
    )


def _debate_init(topic_id: str) -> dict[str, object]:
    return json.loads(
        intel_server.debate_init(
            topic_id=topic_id,
            title="Premium gate test",
            roles_json=_roles_json(),
            created_by_role="CONDUCTOR",
            metadata_json=_priority_metadata_json(),
        )
    )


def _enable_debate_gate(monkeypatch):
    monkeypatch.setattr(
        premium_runtime,
        "load_premium_config",
        lambda: {
            **premium_runtime._DEFAULT_CONFIG,
            "debate_protocol_gate_enabled": True,
            "control_plane_required": False,
        },
    )


def _grant_valid_debate_entitlement(monkeypatch):
    """Enable the gate AND install a valid debate_protocol entitlement.

    Mirrors the valid-entitlement pattern in test_premium_runtime.py: bind the
    entitlement to this machine, declare the feature, and stub signature
    verification so the allow path is reachable without real crypto material.
    """
    _enable_debate_gate(monkeypatch)
    entitlement = {
        "entitlement_id": "ent-debate-1",
        "customer_id": "cust-debate-1",
        "features": ["debate_protocol"],
        "machine_ids": [premium_runtime.MACHINE_ID],
        "signature": {"alg": "ed25519", "value": "unused"},
    }
    monkeypatch.setattr(
        premium_runtime,
        "_load_entitlement",
        lambda config: (entitlement, "test:inline"),
    )
    monkeypatch.setattr(
        premium_runtime,
        "_verify_entitlement_signature",
        lambda entitlement, public_key_value: (True, "signature_valid"),
    )


def test_debate_init_new_topic_succeeds_when_gate_default_off(debate_db):
    out = _debate_init("PREMIUM_GATE_DEFAULT_OFF")

    assert out["topic_id"] == "PREMIUM_GATE_DEFAULT_OFF"
    assert out["state"] == "INIT"
    assert len(out["seeded_bindings"]) == 3


def test_debate_init_new_topic_denies_when_gate_enabled_without_entitlement(
    debate_db, monkeypatch
):
    _enable_debate_gate(monkeypatch)

    out = _debate_init("PREMIUM_GATE_DENIED")

    assert out["error_type"] == "premium_gate_denied"
    assert out["gate"]["reason"] == "entitlement_missing"

    conn = sqlite3.connect(debate_db)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT 1 FROM debates WHERE topic_id = 'PREMIUM_GATE_DENIED'"
        ).fetchone()
        audit_rows = conn.execute(
            "SELECT decision, reason FROM premium_gate_audit "
            "WHERE feature_id = 'debate_protocol'"
        ).fetchall()
    finally:
        conn.close()
    assert row is None
    # Lock the fail-CLOSED audit contract: a clear deny MUST persist a
    # denied/entitlement_missing audit row (the deny returns from inside the
    # committed get_conn() block, so the row survives the transaction).
    assert any(
        r["decision"] == "denied" and r["reason"] == "entitlement_missing"
        for r in audit_rows
    )


def test_debate_init_existing_topic_idempotence_is_not_gated(debate_db, monkeypatch):
    created = _debate_init("PREMIUM_GATE_EXISTING")
    assert created["topic_id"] == "PREMIUM_GATE_EXISTING"
    _enable_debate_gate(monkeypatch)

    out = _debate_init("PREMIUM_GATE_EXISTING")

    assert out["topic_id"] == "PREMIUM_GATE_EXISTING"
    assert "error_type" not in out


def test_debate_existing_topic_post_and_signal_stay_ungated(debate_db, monkeypatch):
    created = _debate_init("PREMIUM_GATE_ROUTING")
    assert created["topic_id"] == "PREMIUM_GATE_ROUTING"
    _enable_debate_gate(monkeypatch)

    q = json.loads(
        intel_server.debate_post_with_recipients(
            topic_id="PREMIUM_GATE_ROUTING",
            role="CONDUCTOR",
            priority="M",
            kind="Q",
            body="CONDUCTOR -> ADVOCATE question",
            addressed_to_csv="ADVOCATE",
            author_session_id="codex-cond20260531",
        )
    )
    a = json.loads(
        intel_server.debate_post_with_recipients(
            topic_id="PREMIUM_GATE_ROUTING",
            role="ADVOCATE",
            priority="M",
            kind="A",
            body="ADVOCATE -> EXECUTOR_1 answer",
            addressed_to_csv="EXECUTOR_1",
            reply_to=q["msg_id"],
            author_session_id="codex-adv20260531",
        )
    )
    pending = json.loads(
        intel_server.debate_signal_check(
            session_id="codex-exec20260531",
            role="EXECUTOR_1",
            topic_id="PREMIUM_GATE_ROUTING",
            limit=20,
        )
    )

    assert a["recipient_count"] == 1
    assert [msg["msg_id"] for msg in pending["pending"]] == [a["msg_id"]]

    # Case (f): read + wake are UNGATED by construction (no gate call in their
    # entrypoints). Prove it under the ENABLED gate / no entitlement — the same
    # condition that DENIES a new topic must not gate these existing-topic ops.
    read_out = json.loads(
        intel_server.debate_read(
            topic_id="PREMIUM_GATE_ROUTING",
            role="EXECUTOR_1",
            limit=50,
        )
    )
    assert read_out.get("error_type") != "premium_gate_denied"
    # EXECUTOR_1 is a seeded binding, so the read succeeds and sees the routed msg.
    assert "error_type" not in read_out
    assert a["msg_id"] in {m["msg_id"] for m in read_out["messages"]}

    wake_response = {
        "msg_id": a["msg_id"],
        "topic_id": "PREMIUM_GATE_ROUTING",
        "schema_version": a["schema_version"],
    }
    wake_out = json.loads(intel_server.debate_wake_dry_run(json.dumps(wake_response)))
    assert wake_out.get("error_type") != "premium_gate_denied"
    # Real schema-matching response => wake resolves the EXECUTOR target and
    # the dry run succeeds (no premium gating in the wake path at all).
    assert "error_type" not in wake_out
    assert any(
        log.get("result") != "schema_mismatch" for log in wake_out.get("logs", [])
    )


def test_debate_bind_role_existing_topic_never_gated(debate_db, monkeypatch):
    # Spec's named highest-risk tool: debate_bind_role must NEVER be gated, even
    # with the gate ENABLED and no entitlement (which would deny a new topic).
    # Mid-flight role re-binding must not be lockout-able.
    created = _debate_init("PREMIUM_GATE_BINDROLE")
    assert created["topic_id"] == "PREMIUM_GATE_BINDROLE"
    _enable_debate_gate(monkeypatch)

    out = json.loads(
        intel_server.debate_bind_role(
            topic_id="PREMIUM_GATE_BINDROLE",
            role="ADVOCATE",
            session_id="codex-advrebind20260531",
            reason="rebind under enabled gate",
            bound_by_role="CONDUCTOR",
            replace_active=True,
        )
    )

    assert "error_type" not in out
    assert out.get("role") == "ADVOCATE"


def test_debate_init_new_topic_allowed_with_valid_entitlement(debate_db, monkeypatch):
    _grant_valid_debate_entitlement(monkeypatch)

    out = _debate_init("PREMIUM_GATE_ENTITLED")

    assert out["topic_id"] == "PREMIUM_GATE_ENTITLED"
    assert out["state"] == "INIT"
    assert "error_type" not in out

    conn = sqlite3.connect(debate_db)
    conn.row_factory = sqlite3.Row
    try:
        topic_row = conn.execute(
            "SELECT 1 FROM debates WHERE topic_id = 'PREMIUM_GATE_ENTITLED'"
        ).fetchone()
        audit_rows = conn.execute(
            "SELECT decision, reason FROM premium_gate_audit "
            "WHERE feature_id = 'debate_protocol'"
        ).fetchall()
    finally:
        conn.close()

    assert topic_row is not None
    # Valid-entitlement allow is audited as a normal entitlement allow, never as
    # a fail-open row.
    assert any(
        row["decision"] == "allowed" and row["reason"] == "entitlement_valid"
        for row in audit_rows
    )
    assert not any(
        str(row["reason"]).startswith("debate_protocol_gate_fail_open:")
        for row in audit_rows
    )


def test_debate_init_new_topic_fail_opens_when_evaluator_raises(debate_db, monkeypatch):
    # When the evaluator itself raises (not a clean deny verdict), the wrapper's
    # anti-lockout except branch must fail OPEN so the live protocol cannot brick
    # itself, and record a DISTINCT runtime-error audit row.
    _enable_debate_gate(monkeypatch)

    def _boom(*args, **kwargs):
        raise RuntimeError("evaluator exploded")

    monkeypatch.setattr(premium_runtime, "evaluate_feature_gate", _boom)

    out = _debate_init("PREMIUM_GATE_FAILOPEN")

    assert out["topic_id"] == "PREMIUM_GATE_FAILOPEN"
    assert out["state"] == "INIT"
    assert "error_type" not in out

    conn = sqlite3.connect(debate_db)
    conn.row_factory = sqlite3.Row
    try:
        topic_row = conn.execute(
            "SELECT 1 FROM debates WHERE topic_id = 'PREMIUM_GATE_FAILOPEN'"
        ).fetchone()
        audit_rows = conn.execute(
            "SELECT decision, reason FROM premium_gate_audit "
            "WHERE feature_id = 'debate_protocol'"
        ).fetchall()
    finally:
        conn.close()

    assert topic_row is not None
    # Exception branch reason is exactly the runtime-error class, with NO
    # entitlement_load_failed infix (that infix is the by-reason branch instead).
    assert any(
        row["decision"] == "allowed"
        and row["reason"] == "debate_protocol_gate_fail_open:RuntimeError"
        for row in audit_rows
    )
