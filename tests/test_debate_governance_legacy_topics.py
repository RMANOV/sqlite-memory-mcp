"""C3 F0 follow-up: legacy topics without a stamped governance record.

Every topic created before the F0 foundation (and every hand-seeded
historical topic) has no ``governance`` key in its metadata, or an
unusable one.  A well-formed governance decision on such a topic must fail
closed with the typed ``authority_unconfigured`` refusal and zero writes,
never with a Python error.  The second test pins the current contract that
a DECISION carrying a NON-governance payload on an unpinned legacy topic is
refused (typed) instead of being posted with the payload silently dropped;
that behaviour is recorded as a decision item for ROOT, so this node is the
visible contract, not an accident.

Seeded rows are historical data shapes, never a grant, pin or spend.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

TOPIC = "C3F0_LEGACYMETA"
AUTHOR = "codex-f0legacy01"
RECIPIENT = "codex-f0legacy02"
WRITERS = ("debate_post", "debate_post_with_recipients")
TS = "2026-01-01T00:00:00Z"


@pytest.fixture
def api(tmp_path, monkeypatch):
    import db_utils
    import debate_wake_signal
    import intel_server
    from schema import init_db

    path = str(tmp_path / "legacy_meta.db")
    init_db(path)
    monkeypatch.setattr(intel_server, "_get_conn", lambda: db_utils.get_conn(path))
    monkeypatch.setattr(
        intel_server, "_get_conn_immediate", lambda: db_utils.get_conn_immediate(path)
    )
    monkeypatch.setattr(debate_wake_signal, "signal_wake", lambda: False)
    for name in ("SQLITE_MEMORY_DEBATE_GATE_ENABLED", "SQLITE_MEMORY_DEBATE_GATE_DISABLED"):
        monkeypatch.delenv(name, raising=False)

    def call(name, **kwargs):
        return json.loads(getattr(intel_server, name)(**kwargs))

    return call, path


def _seed_legacy_topic(path, metadata_json):
    """Historical ACTIVE topic with two attributed executors and no governance record."""
    roles = [
        {"role": "EXECUTOR_1", "session_id": AUTHOR},
        {"role": "EXECUTOR_2", "session_id": RECIPIENT},
    ]
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO debates (topic_id, title, state, created_at, created_by_role, "
            "roles_json, metadata_json) VALUES (?, ?, 'ACTIVE', ?, 'EXECUTOR_1', ?, ?)",
            (TOPIC, "historical topic without governance record", TS,
             json.dumps(roles), metadata_json),
        )
        for entry in roles:
            conn.execute(
                "INSERT INTO debate_role_bindings (topic_id, role, session_id, runtime, "
                "state, generation, created_at, updated_at, retired_at, reason) "
                "VALUES (?, ?, ?, 'codex', 'active', 1, ?, ?, NULL, 'historical binding')",
                (TOPIC, entry["role"], entry["session_id"], TS, TS),
            )


def _snapshot(path):
    """Full rows of every debate table, including FTS/queue/projection data."""
    with sqlite3.connect(path) as conn:
        names = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND (name='debates' OR name GLOB 'debate_*') ORDER BY name"
        )]
        result = {}
        for name in names:
            quoted = '"' + name.replace('"', '""') + '"'
            result[name] = sorted(
                (tuple(r) for r in conn.execute(f"SELECT * FROM {quoted}")), key=repr
            )
        return result


def _authorize_input():
    # Well-formed INPUT template; never an issued grant.
    return {
        "schema": "governance/v1", "type": "authorize", "action": "retire_binding",
        "topic_id": TOPIC, "target_role": "EXECUTOR_2",
        "target_session_id": RECIPIENT, "target_fingerprint": "a" * 64,
        "effect": {"state": "retired", "claims": "hold"},
        "expires_at": "2026-09-15T00:00:00Z", "nonce": "0011223344556677",
    }


def _post(call, writer, payload):
    kwargs = {"addressed_to_csv": "EXECUTOR_2"} if writer == "debate_post_with_recipients" else {}
    return call(
        writer, topic_id=TOPIC, role="EXECUTOR_1", priority="M", kind="DECISION",
        body="synthetic legacy-metadata probe", payload_json=payload,
        body_mode="structured", author_session_id=AUTHOR, **kwargs,
    )


@pytest.mark.parametrize("writer", WRITERS)
@pytest.mark.parametrize(
    "metadata_json",
    [None, "{}", '{"priority_lane": "P2"}', '{"governance": "not-an-object"}',
     '{"governance": {"mode": "legacy"}}'],
    ids=["null", "empty", "no_key", "non_object_key", "legacy_key"],
)
def test_well_formed_grant_on_topic_without_governance_record_fails_closed(
    api, writer, metadata_json
):
    call, path = api
    _seed_legacy_topic(path, metadata_json)
    before = _snapshot(path)
    out = _post(call, writer, json.dumps(_authorize_input()))
    assert out.get("error_type") == "authority_unconfigured", out
    assert "debate_governance_bootstrap_human" in json.dumps(out.get("details", {}))
    assert _snapshot(path) == before


@pytest.mark.parametrize("writer", WRITERS)
def test_decision_with_non_governance_payload_is_refused_typed(api, writer):
    """Decision item for ROOT (recorded 2026-09-14): pins the CURRENT contract.

    Before F0 the payload of a legacy DECISION was silently discarded at
    INSERT; now every non-empty DECISION payload is a governance candidate and
    an opaque one is refused with ``governance_schema_unsupported`` and zero
    rows.  If ROOT rules for the old silent drop instead, this node flips.
    """
    call, path = api
    _seed_legacy_topic(path, None)
    before = _snapshot(path)
    out = _post(call, writer, '{"ordinary": "opaque"}')
    assert out.get("error_type") == "governance_schema_unsupported", out
    assert _snapshot(path) == before


@pytest.mark.parametrize("writer", WRITERS)
@pytest.mark.parametrize(
    "metadata_json",
    [
        '{"governance": {"mode": "authority"}}',
        json.dumps({"governance": {
            "mode": "authority", "candidate_role": "ADVOCATE_CODEX",
            "authority_role": "ADVOCATE_CODEX", "authority_session_id": "codex-f0adv01",
            "authority_generation": 1, "authority_epoch": 1,
        }}),
    ],
    ids=["authority_mode_only", "complete_looking_record"],
)
def test_authority_looking_metadata_grants_nothing_in_f0(api, writer, metadata_json):
    """Review item M2: an authority-looking record seeded by hand is never a pin.

    F0 has no positive path even when metadata says mode=authority, so a
    well-formed authorize on such a topic is refused typed with
    governance_action_not_implemented and zero writes; the error name flips
    in F1 when the real pin/consume path exists.
    """
    call, path = api
    _seed_legacy_topic(path, metadata_json)
    before = _snapshot(path)
    out = _post(call, writer, json.dumps(_authorize_input()))
    assert out.get("error_type") == "governance_action_not_implemented", out
    assert _snapshot(path) == before


@pytest.mark.parametrize("writer", WRITERS)
def test_oversize_non_decision_governance_payload_is_refused_by_size(api, writer):
    """Review item M1: classification never parses beyond the byte bound.

    A STATUS payload that carries the governance marker but exceeds 65536
    UTF-8 bytes is a candidate by size alone and is refused as too large,
    with zero writes, instead of being parsed in full before classification.
    """
    call, path = api
    _seed_legacy_topic(path, None)
    before = _snapshot(path)
    raw = '{"schema": "governance/v1", "pad": "' + "я" * 40000 + '"}'
    assert len(raw.encode("utf-8")) > 65536
    kwargs = {"addressed_to_csv": "EXECUTOR_2"} if writer == "debate_post_with_recipients" else {}
    out = call(
        writer, topic_id=TOPIC, role="EXECUTOR_1", priority="M", kind="STATUS",
        body="synthetic oversize governance-marker probe", payload_json=raw,
        body_mode="structured", author_session_id=AUTHOR, **kwargs,
    )
    assert out.get("error_type") == "governance_payload_too_large", out
    assert _snapshot(path) == before


def test_caller_supplied_governance_object_is_refused_at_init(api):
    """Review item M3: the whole governance record is server-derived.

    Even a governance object with only non-authority keys is refused at
    debate_init (no silent overwrite), with zero rows.
    """
    call, path = api
    before = _snapshot(path)
    out = call(
        "debate_init", topic_id="C3F0_M3", title="caller governance object",
        roles_json=json.dumps([
            {"role": "EXECUTOR_1", "session_id": AUTHOR},
            {"role": "EXECUTOR_2", "session_id": RECIPIENT},
        ]),
        created_by_role="EXECUTOR_1",
        metadata_json=json.dumps({
            "priority_lane": "P2", "priority_reason": "synthetic",
            "governance": {"candidate_role": "ADVOCATE_CODEX"},
        }),
    )
    assert out.get("error_type") == "governance_server_field_supplied", out
    assert _snapshot(path) == before


@pytest.mark.parametrize("writer", WRITERS)
def test_v1_topic_refuses_stamped_looking_decision_before_storage(api, writer):
    """Review item M4 characterization (ROOT Q5 evidence): configured debate/v1.

    A DECISION carrying a complete, issuer-stamped-looking governance payload
    on a debate/v1 topic never reaches the legacy governance validator; the
    existing v1 preflight refuses the legacy kind (SEMANTIC_KIND_REQUIRED)
    and nothing is stored, so no v1 row can be mistaken for a server-stamped
    grant by shape alone.
    """
    call, path = api
    topic = "C3F0_V1STAMP"
    roles = [
        {"role": "EXECUTOR_1", "session_id": AUTHOR},
        {"role": "EXECUTOR_2", "session_id": RECIPIENT},
    ]
    created = call(
        "debate_init", topic_id=topic, title="v1 stamped-looking probe",
        roles_json=json.dumps(roles), created_by_role="EXECUTOR_1",
        metadata_json=json.dumps({"priority_lane": "P2", "priority_reason": "synthetic"}),
        protocol_version="debate/v1", blind_roles_csv="EXECUTOR_1,EXECUTOR_2",
    )
    assert "error_type" not in created, created
    state = call(
        "debate_state", topic_id=topic, role="EXECUTOR_1", new_state="ACTIVE",
        reason="synthetic", author_session_id=AUTHOR,
    )
    assert state.get("new_state") == "ACTIVE", state
    payload = dict(_authorize_input(), topic_id=topic)
    payload["issuer"] = {
        "topic_id": topic, "role": "ADVOCATE_CODEX", "session_id": "codex-f0adv01",
        "binding_generation": 1, "binding_fingerprint": "b" * 64, "authority_epoch": 1,
    }
    before = _snapshot(path)
    kwargs = {"addressed_to_csv": "EXECUTOR_2"} if writer == "debate_post_with_recipients" else {}
    out = call(
        writer, topic_id=topic, role="EXECUTOR_1", priority="M", kind="DECISION",
        body="synthetic stamped-looking probe", payload_json=json.dumps(payload),
        body_mode="structured", author_session_id=AUTHOR, **kwargs,
    )
    assert out.get("error_type") == "SEMANTIC_KIND_REQUIRED", out
    assert _snapshot(path) == before
