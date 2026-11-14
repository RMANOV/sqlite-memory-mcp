"""C3 F06-F08 reachable behavior RED through existing REAL public wrappers.

No new governance module is imported, and no SQL creates authority or spends.
Snapshots include all currently present debate tables, so an absent future
projection table cannot mask today's acceptance of invalid payloads.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

TOPIC = "C3F0_PUBLIC"
AUTHOR = "codex-f0exec01"
RECIPIENT = "codex-f0exec02"
WRITERS = ("debate_post", "debate_post_with_recipients")


@pytest.fixture
def api(tmp_path, monkeypatch):
    import db_utils
    import debate_wake_signal
    import intel_server
    from schema import init_db

    path = str(tmp_path / "public.db")
    init_db(path)
    monkeypatch.setattr(intel_server, "_get_conn", lambda: db_utils.get_conn(path))
    monkeypatch.setattr(
        intel_server, "_get_conn_immediate", lambda: db_utils.get_conn_immediate(path)
    )
    # The decorator captures _signal_wake_after_commit. Patch the signal it
    # imports, not a replacement callback the existing closure will never see.
    monkeypatch.setattr(debate_wake_signal, "signal_wake", lambda: False)
    for name in ("SQLITE_MEMORY_DEBATE_GATE_ENABLED", "SQLITE_MEMORY_DEBATE_GATE_DISABLED"):
        monkeypatch.delenv(name, raising=False)

    def call(name, **kwargs):
        return json.loads(getattr(intel_server, name)(**kwargs))

    return call, path


def _snapshot(path):
    """Full rows in the isolated DB, including FTS shadow/queue/projection data."""
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


def _roles(candidates=1):
    roles = [
        {"role": "EXECUTOR_1", "session_id": AUTHOR},
        {"role": "EXECUTOR_2", "session_id": RECIPIENT},
    ]
    if candidates:
        roles.append({"role": "ADVOCATE_CODEX", "session_id": "codex-f0adv01"})
    if candidates == 2:
        roles.append({"role": "ADVOCATE", "session_id": "cc-f0adv02"})
    return roles


def _metadata():
    return {
        "priority_lane": "P2", "priority_reason": "synthetic foundation test",
        "unrelated": {"label": "keep", "items": [1, "two"]},
    }


def _init(api, *, metadata=None, candidates=1, configured_v1=False):
    call, _ = api
    kwargs = {}
    if configured_v1:
        kwargs = {
            "protocol_version": "debate/v1",
            "blind_roles_csv": "EXECUTOR_1,EXECUTOR_2",
        }
    return call(
        "debate_init", topic_id=TOPIC, title="C3 synthetic public fixture",
        roles_json=json.dumps(_roles(candidates)), created_by_role="EXECUTOR_1",
        metadata_json=json.dumps(_metadata() if metadata is None else metadata),
        **kwargs,
    )


def _active(api, *, configured_v1=False):
    call, _ = api
    out = _init(api, configured_v1=configured_v1)
    assert "error_type" not in out, out
    state = call(
        "debate_state", topic_id=TOPIC, role="EXECUTOR_1", new_state="ACTIVE",
        reason="synthetic test", author_session_id=AUTHOR,
    )
    assert state.get("new_state") == "ACTIVE", state


def _post(api, writer, *, payload="", kind="DECISION", author=AUTHOR, mode="structured"):
    call, _ = api
    kwargs = {}
    if writer == "debate_post_with_recipients":
        kwargs["addressed_to_csv"] = "EXECUTOR_2"
    return call(
        writer, topic_id=TOPIC, role="EXECUTOR_1", priority="M", kind=kind,
        body="synthetic public validation probe", payload_json=payload,
        body_mode=mode, author_session_id=author, **kwargs,
    )


def _assert_rejected(api, expected_error, operation):
    _, path = api
    before = _snapshot(path)
    out = operation()
    after = _snapshot(path)
    assert out.get("error_type") == expected_error, {
        "result": out, "full_rows_unchanged": after == before,
    }
    assert after == before


RAW_PIN_FIELDS = [
    {"mode": "authority"},
    {"authority_session_id": "codex-f0adv01"},
    {"authority_generation": 7},
    {"authority_epoch": 9},
    {
        "mode": "authority", "authority_role": "ADVOCATE_CODEX",
        "authority_session_id": "codex-f0adv01",
        "authority_generation": 7, "authority_epoch": 9,
    },
]


@pytest.mark.parametrize("existing", [False, True], ids=["fresh", "same_roster_retry"])
@pytest.mark.parametrize("injected", RAW_PIN_FIELDS,
                         ids=["mode", "session", "generation", "epoch", "complete_pin"])
def test_f06_raw_pin_fields_reject_before_creation_or_idempotency(api, existing, injected):
    if existing:
        created = _init(api)
        assert "error_type" not in created, created
    metadata = dict(_metadata(), governance=injected)
    _assert_rejected(
        api, "governance_server_field_supplied",
        lambda: _init(api, metadata=metadata),
    )


@pytest.mark.parametrize("candidates", [0, 1, 2], ids=["zero", "one", "two"])
def test_f06_legitimate_candidate_topics_stay_unpinned_and_preserve_metadata(api, candidates):
    created = _init(api, candidates=candidates)
    assert "error_type" not in created, created
    assert created["roles"] == _roles(candidates)
    metadata = created["metadata"]
    assert metadata["unrelated"] == _metadata()["unrelated"]
    governance = metadata.get("governance", {})
    assert governance.get("mode") == "legacy", governance
    if candidates == 1:
        assert governance.get("candidate_role") == "ADVOCATE_CODEX"
    else:
        assert governance.get("candidate_role") in (None, "")
    for field in ("authority_role", "authority_session_id", "authority_generation"):
        assert governance.get(field) is None, governance
    assert governance.get("authority_epoch", 0) == 0


def _well_formed_authorization_input():
    # Rejected INPUT template, not an issued grant or valid authority fixture.
    return {
        "schema": "governance/v1", "type": "authorize", "action": "retire_binding",
        "topic_id": TOPIC, "target_role": "EXECUTOR_2",
        "target_session_id": RECIPIENT, "target_fingerprint": "a" * 64,
        "effect": {"state": "retired", "claims": "hold"},
        "expires_at": "2026-09-15T00:00:00Z", "nonce": "0011223344556677",
    }


def _invalid_payload(case):
    payload = _well_formed_authorization_input()
    mode = "structured"
    if case == "malformed":
        return '{"schema":', mode, "governance_payload_invalid"
    if case == "duplicate_root":
        raw = json.dumps(payload)
        return raw[:-1] + ',"schema":"governance/v2"}', mode, "governance_duplicate_key"
    if case == "duplicate_nested":
        raw = json.dumps(payload).replace(
            '"claims": "hold"', '"claims": "hold", "claims": "retire"'
        )
        assert raw.count('"claims"') == 2
        return raw, mode, "governance_duplicate_key"
    if case == "nonfinite":
        payload["nonce"] = float("nan")
        return json.dumps(payload), mode, "governance_payload_invalid"
    if case == "oversize":
        payload["nonce"] = "я" * 32768
        raw = json.dumps(payload, ensure_ascii=False)
        assert len(raw.encode("utf-8")) > 65536
        return raw, mode, "governance_payload_too_large"
    if case == "unknown_schema":
        payload["schema"] = "governance/v2"
        return json.dumps(payload), mode, "governance_schema_unsupported"
    if case == "supplied_issuer":
        payload["issuer"] = {
            "topic_id": TOPIC, "role": "ADVOCATE_CODEX",
            "session_id": "codex-f0adv01", "binding_generation": 1,
            "binding_fingerprint": "b" * 64, "authority_epoch": 1,
        }
        return json.dumps(payload), mode, "governance_server_field_supplied"
    if case == "nonobject":
        return "[]", mode, "governance_payload_invalid"
    assert case == "body_mode"
    return json.dumps(payload), "live_text", "governance_body_mode_invalid"


@pytest.mark.parametrize("writer", WRITERS)
@pytest.mark.parametrize("case", [
    "malformed", "duplicate_root", "duplicate_nested", "nonfinite", "oversize",
    "unknown_schema", "supplied_issuer", "nonobject", "body_mode",
])
def test_f07_both_public_writers_reject_invalid_candidates_without_rows(api, writer, case):
    _active(api)
    raw, mode, expected = _invalid_payload(case)
    _assert_rejected(
        api, expected, lambda: _post(api, writer, payload=raw, mode=mode)
    )


@pytest.mark.parametrize("writer", WRITERS)
@pytest.mark.parametrize(
    ("author", "expected"),
    [("", "author_session_required"), ("codex-f0outsider", "ROLE_UNAVAILABLE")],
    ids=["missing_author", "outsider"],
)
def test_f07_auth_first_precedes_malformed_governance_payload(api, writer, author, expected):
    _active(api)
    _assert_rejected(
        api, expected, lambda: _post(api, writer, payload="{bad", author=author)
    )


@pytest.mark.parametrize("writer", WRITERS)
@pytest.mark.parametrize(
    ("kind", "payload"),
    [("STATUS", ""), ("STATUS", '{"ordinary":"opaque"}'), ("Q", ""), ("DECISION", "")],
    ids=["status", "noncandidate_payload", "question", "plain_decision"],
)
def test_f08_ordinary_unpinned_legacy_work_preserves_read_shape(api, writer, kind, payload):
    _active(api)
    out = _post(api, writer, kind=kind, payload=payload)
    assert "error_type" not in out and out.get("msg_id"), out
    call, _ = api
    read = call("debate_read", topic_id=TOPIC, role="EXECUTOR_2", limit=100)
    rows = [m for m in read["messages"] if m["msg_id"] == out["msg_id"]]
    assert len(rows) == 1, read
    assert not {"protocol_version", "round_no", "body_mode", "payload_json"} & rows[0].keys()
    if writer == "debate_post_with_recipients":
        pending = call(
            "debate_signal_check", topic_id=TOPIC, role="EXECUTOR_2",
            session_id=RECIPIENT, limit=100,
        )
        exact = [m for m in pending["pending"] if m["msg_id"] == out["msg_id"]]
        assert len(exact) == 1, pending
        assert exact[0] == rows[0]


@pytest.mark.parametrize("writer", WRITERS)
def test_f08_legacy_semantic_kind_still_requires_protocol(api, writer):
    _active(api)
    _assert_rejected(
        api, "PROTOCOL_NOT_CONFIGURED", lambda: _post(api, writer, kind="CHALLENGE")
    )


@pytest.mark.parametrize("writer", WRITERS)
def test_f08_governance_candidate_does_not_bypass_configured_v1(api, writer):
    _active(api, configured_v1=True)
    _assert_rejected(
        api, "SEMANTIC_KIND_REQUIRED",
        lambda: _post(api, writer, payload=json.dumps(_well_formed_authorization_input())),
    )


def test_f08_unpinned_parent_can_claim_actual_addressed_work(api):
    _active(api)
    posted = _post(api, "debate_post_with_recipients", kind="Q", payload="")
    assert posted.get("msg_id"), posted
    call, _ = api
    claim = call(
        "debate_worker_claim", topic_id=TOPIC, role="EXECUTOR_2",
        parent_session_id=RECIPIENT, trigger_msg_id=posted["msg_id"],
    )
    assert "error_type" not in claim, claim
    assert claim["worker_session_id"].startswith(RECIPIENT + "-W"), claim
    assert claim["parent_session_id"] == RECIPIENT
    assert claim["trigger_msg_id"] == posted["msg_id"]
    # Existing self-handoff/protocol guards remain unchanged in the full suite.
    # Do not invent an authenticated-rotation surface that F0 does not implement.
