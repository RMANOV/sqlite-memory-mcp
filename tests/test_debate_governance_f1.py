"""C3 phase A = F1 minimal authority chain: intentional per-node RED until A2.

Contract: PA packet v1.3 (DB note d88fe66f, DA ACCEPT e99bad07ccf3) over the
foundation delta de7c9ec07ae5 (P0–P3, F13–F19) and Codex r4 §1.3 (consume order).
Every governance import is inside the fixture or the node (F0 style), so a
missing surface fails ONE node with AttributeError/TypeError, never collection.
Snapshots cover every debate table so a refused call proves zero writes.

Chain under test, all through REAL public wrappers unless a node says raw SQL:
  inventory → bootstrap HUMAN (private manifest) → approve_pin (manifest tool)
  → pin (same manifest re-presented; epoch 0→1) → authorize DECISION by the
  pinned authority → debate_bind_role(authorization_msg_id=…) consumes it in
  one immediate transaction with exactly one finalized spend.
"""

from __future__ import annotations

import inspect
import json
import os
import sqlite3
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

TOPIC = "C3F1"
AUTHOR = "codex-f1exec01"        # EXECUTOR_1
RECIPIENT = "codex-f1exec02"     # EXECUTOR_2 (the usual retire target)
AUTHORITY = "codex-f1adv01"      # ADVOCATE_CODEX (the pinned authority)
HUMAN = "human-f1op01"
WRITERS = ("debate_post", "debate_post_with_recipients")
GOVERNANCE_TRIGGERS = {
    "debate_messages_fts_ai", "debate_messages_fts_ad", "debate_messages_fts_au",
    "debate_messages_provenance_immutable",
    "debate_messages_governance_immutable_update",
    "debate_messages_governance_immutable_delete",
}


@pytest.fixture
def api(tmp_path, monkeypatch):
    import db_utils
    import debate_wake_signal
    import intel_server
    from schema import init_db

    path = str(tmp_path / "f1.db")
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

    gov_dir = tmp_path / "gov"
    gov_dir.mkdir(mode=0o700)
    os.chmod(gov_dir, 0o700)
    return call, path, gov_dir


# ── helpers ────────────────────────────────────────────────────────────────


def _gov():
    import debate_governance
    return debate_governance


def _iso(seconds):
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def _snapshot(path):
    with sqlite3.connect(path) as conn:
        names = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND (name='debates' OR name GLOB 'debate_*') ORDER BY name")]
        return {
            n: sorted((tuple(r) for r in conn.execute(f'SELECT * FROM "{n}"')), key=repr)
            for n in names
        }


def _changed(before, after):
    return {n for n in set(before) | set(after) if before.get(n) != after.get(n)}


def _rows(path, sql, params=()):
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _spends(path):
    return _rows(path, "SELECT * FROM debate_authorization_spends ORDER BY spent_at, target_key")


def _governance_record(path):
    row = _rows(path, "SELECT metadata_json FROM debates WHERE topic_id = ?", (TOPIC,))[0]
    return json.loads(row["metadata_json"])["governance"]


def _write_manifest(gov_dir, name, manifest, mode=0o600):
    path = gov_dir / name
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh)
    os.chmod(path, mode)
    return str(path), _gov().content_digest("c3-manifest/v1", manifest)


def _roles():
    return [
        {"role": "EXECUTOR_1", "session_id": AUTHOR},
        {"role": "EXECUTOR_2", "session_id": RECIPIENT},
        {"role": "ADVOCATE_CODEX", "session_id": AUTHORITY},
    ]


def _init_active(api, topic=TOPIC):
    call, _, _ = api
    out = call(
        "debate_init", topic_id=topic, title="C3 F1 synthetic chain",
        roles_json=json.dumps(_roles()), created_by_role="EXECUTOR_1",
        metadata_json=json.dumps({
            # priority_lane requires priority_reason (topic_priority_reason_required);
            # the A1 run proved every node fails at init without it.
            "priority_lane": "P2", "priority_reason": "synthetic F1 chain test",
            "unrelated": {"keep": [1, "two"]},
        }),
    )
    assert "error_type" not in out, out
    state = call(
        "debate_state", topic_id=topic, role="EXECUTOR_1", new_state="ACTIVE",
        reason="synthetic test", author_session_id=AUTHOR,
    )
    assert state.get("new_state") == "ACTIVE", state


def _inventory(api, topic=TOPIC):
    call, _, _ = api
    out = call("debate_governance_inventory", topic_id=topic)
    assert "error_type" not in out, out
    return out


def _binding(inv, role, session_id):
    return next(b for b in inv["bindings"] if b["role"] == role and b["session_id"] == session_id)


def _bootstrap_manifest(api, *, human=HUMAN, fingerprint=None, expires=None,
                        name="bootstrap.json", mode=0o600, gov_dir=None, topic=TOPIC):
    _, _, default_dir = api
    inv = _inventory(api, topic)
    manifest = {
        "schema": "governance-bootstrap/v1", "topic_id": topic,
        "expected_topic_fingerprint": fingerprint or inv["topic_fingerprint"],
        "human_session_id": human, "expires_at": expires or _iso(3600),
        "nonce": "0011223344556677",
    }
    return _write_manifest(gov_dir or default_dir, name, manifest, mode)


def _bootstrap(api, path, digest):
    call, _, _ = api
    return call("debate_governance_bootstrap_human", manifest_path=path,
                expected_manifest_sha256=digest)


def _approve_manifest(api, *, human=HUMAN, nonce="1122334455667788", name=None,
                      expires=None, topic=TOPIC):
    _, _, gov_dir = api
    inv = _inventory(api, topic)
    authority = _binding(inv, "ADVOCATE_CODEX", AUTHORITY)
    manifest = {
        "schema": "governance-approve/v1", "topic_id": topic,
        "authority_role": "ADVOCATE_CODEX", "authority_session_id": AUTHORITY,
        "authority_generation": authority["generation"],
        "expected_authority_fingerprint": authority["fingerprint"],
        "expected_authority_epoch": inv["authority_epoch"],
        "human_session_id": human, "expires_at": expires or _iso(3600), "nonce": nonce,
    }
    return _write_manifest(gov_dir, name or f"approve-{inv['authority_epoch']}-{nonce}.json",
                           manifest)


def _approve(api, path, digest, author=HUMAN):
    call, _, _ = api
    return call("debate_governance_approve_pin", manifest_path=path,
                expected_manifest_sha256=digest, author_session_id=author)


def _pin(api, approval_msg_id, path, digest, author=HUMAN, topic=TOPIC):
    call, _, _ = api
    return call("debate_governance_pin", topic_id=topic, approval_msg_id=approval_msg_id,
                author_session_id=author, manifest_path=path, expected_manifest_sha256=digest)


def _chain(api, topic=TOPIC):
    """bootstrap → approve_pin → pin; returns the ids and the approve manifest."""
    path, digest = _bootstrap_manifest(api, topic=topic, name=f"bootstrap-{topic}.json")
    boot = _bootstrap(api, path, digest)
    assert boot.get("status") == "applied", boot
    apath, adigest = _approve_manifest(api, topic=topic)
    approval = _approve(api, apath, adigest)
    assert approval.get("msg_id"), approval
    pinned = _pin(api, approval["msg_id"], apath, adigest, topic=topic)
    assert "error_type" not in pinned, pinned
    return {"bootstrap": boot, "approval": approval["msg_id"], "pin": pinned,
            "approve_path": apath, "approve_digest": adigest}


def _grant_payload(api, *, target_role="EXECUTOR_2", target_session=RECIPIENT,
                   action="retire_binding", claims="hold", expires=None, topic=TOPIC,
                   fingerprint=None, nonce="8877665544332211"):
    if fingerprint is None:
        fingerprint = _binding(_inventory(api, topic), target_role, target_session)["fingerprint"]
    return {
        "schema": "governance/v1", "type": "authorize", "action": action,
        "topic_id": topic, "target_role": target_role, "target_session_id": target_session,
        "target_fingerprint": fingerprint,
        "effect": {"state": "retired" if action == "retire_binding" else "diagnostic",
                   "claims": claims},
        "expires_at": expires or _iso(3600), "nonce": nonce,
    }


def _authorize(api, payload, *, author=AUTHORITY, writer="debate_post_with_recipients",
               kind="DECISION", role="ADVOCATE_CODEX", topic=TOPIC):
    call, _, _ = api
    kwargs = {"addressed_to_csv": "EXECUTOR_1"} if writer == "debate_post_with_recipients" else {}
    return call(
        writer, topic_id=topic, role=role, priority="H", kind=kind,
        body="synthetic authorization", payload_json=_gov().canonical_json(payload),
        body_mode="structured", author_session_id=author, **kwargs,
    )


def _grant(api, **kw):
    # the grant is posted ON the topic it names (a pinned authority exists there)
    out = _authorize(api, _grant_payload(api, **kw), topic=kw.get("topic", TOPIC))
    assert out.get("msg_id"), out
    return out["msg_id"]


def _bind(api, *, role="EXECUTOR_2", session=RECIPIENT, state="retired", author=AUTHORITY,
          grant=None, topic=TOPIC, **extra):
    call, _, _ = api
    kwargs = {"authorization_msg_id": grant} if grant else {}
    kwargs.update(extra)
    return call("debate_bind_role", topic_id=topic, role=role, session_id=session,
                state=state, reason="synthetic F1 effect", author_session_id=author, **kwargs)


def _read(api, msg_id, topic=TOPIC):
    call, _, _ = api
    out = call("debate_read", topic_id=topic, role="EXECUTOR_1", limit=500)
    return next(m for m in out["messages"] if m["msg_id"] == msg_id)


def _signal(api, msg_id, *, session=AUTHOR, role="EXECUTOR_1", topic=TOPIC):
    call, _, _ = api
    out = call("debate_signal_check", session_id=session, role=role, topic_id=topic, limit=500)
    return next(m for m in out["messages"] if m["msg_id"] == msg_id)


def _stored(path, msg_id):
    return _rows(path, "SELECT * FROM debate_messages WHERE msg_id = ?", (msg_id,))[0]


# ── P0 inventory ────────────────────────────────────────────────────────────


def test_p0_inventory_reports_unpinned_topic_with_fingerprints(api):
    _, path, _ = api
    _init_active(api)
    before = _snapshot(path)
    inv = _inventory(api)
    assert inv["topic_id"] == TOPIC and inv["topic_state"] == "ACTIVE"
    assert inv["mode"] == "legacy" and inv["authority_epoch"] == 0
    assert inv["candidate_role"] == "ADVOCATE_CODEX" and inv["authority"] is None
    assert inv["active_human_owners"] == []
    assert len(inv["topic_fingerprint"]) == 64
    roles = {(b["role"], b["session_id"], b["state"]) for b in inv["bindings"]}
    assert {("EXECUTOR_1", AUTHOR, "active"), ("EXECUTOR_2", RECIPIENT, "active"),
            ("ADVOCATE_CODEX", AUTHORITY, "active")} <= roles
    for b in inv["bindings"]:
        assert len(b["fingerprint"]) == 64 and b["generation"] >= 1
        assert b["active_claims"] == 0
    assert inv["pin_record_backed"] is True
    assert _snapshot(path) == before


# ── P1 bootstrap ────────────────────────────────────────────────────────────


def test_p1_bootstrap_applies_human_binding_receipt_and_spend(api):
    _, path, _ = api
    _init_active(api)
    mpath, digest = _bootstrap_manifest(api)
    out = _bootstrap(api, mpath, digest)
    assert out["status"] == "applied" and out["manifest_sha256"] == digest, out
    assert out["binding"]["role"] == "HUMAN" and out["binding"]["session_id"] == HUMAN
    assert out["binding"]["state"] == "active"
    row = _stored(path, out["receipt_msg_id"])
    assert row["kind"] == "DECISION" and row["role"] == "HUMAN"
    assert row["governance_schema"] == "governance/v1"
    assert row["author_session_id"] == HUMAN and row["provenance_class"] == "parent"
    payload = json.loads(row["payload_json"])
    assert payload["schema"] == "governance/v1" and payload["type"] == "bootstrap_human"
    assert payload["manifest_sha256"] == digest
    assert payload["issuer"]["role"] == "HUMAN" and payload["issuer"]["session_id"] == HUMAN
    assert payload["approval_source"] == "operator_manifest_cooperative"
    spends = _spends(path)
    assert len(spends) == 1
    assert spends[0]["action"] == "bootstrap_human"
    assert spends[0]["manifest_sha256"] == digest and len(spends[0]["target_key"]) == 64
    assert spends[0]["authorization_msg_id"] == out["receipt_msg_id"]
    inv = _inventory(api)
    assert inv["active_human_owners"] == [HUMAN]
    declared = {r["role"] for r in json.loads(
        _rows(path, "SELECT roles_json FROM debates WHERE topic_id = ?", (TOPIC,))[0]["roles_json"])}
    assert "HUMAN" in declared


def test_p1_bootstrap_exact_retry_is_idempotent(api):
    _, path, _ = api
    _init_active(api)
    mpath, digest = _bootstrap_manifest(api)
    first = _bootstrap(api, mpath, digest)
    assert first["status"] == "applied", first
    before = _snapshot(path)
    again = _bootstrap(api, mpath, digest)
    assert again["status"] == "already_bootstrapped", again
    assert again["receipt_msg_id"] == first["receipt_msg_id"]
    assert again["binding"] == first["binding"]
    assert _snapshot(path) == before


def test_p1_bootstrap_after_image_change_is_refused(api):
    _, path, _ = api
    _init_active(api)
    mpath, digest = _bootstrap_manifest(api)
    assert _bootstrap(api, mpath, digest)["status"] == "applied"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE debate_role_bindings SET reason = 'tampered' "
            "WHERE topic_id = ? AND role = 'HUMAN' AND session_id = ?", (TOPIC, HUMAN))
    before = _snapshot(path)
    out = _bootstrap(api, mpath, digest)
    assert out["error_type"] == "bootstrap_after_image_changed", out
    assert _snapshot(path) == before


def test_p1_second_human_is_refused_while_an_active_human_exists(api):
    _, path, _ = api
    _init_active(api)
    mpath, digest = _bootstrap_manifest(api)
    assert _bootstrap(api, mpath, digest)["status"] == "applied"
    other, odigest = _bootstrap_manifest(api, human="human-f1op02", name="second.json")
    before = _snapshot(path)
    out = _bootstrap(api, other, odigest)
    assert out["error_type"] == "bootstrap_human_exists", out
    assert _snapshot(path) == before


@pytest.mark.parametrize("writer", WRITERS)
def test_p1_generic_public_bootstrap_form_is_refused(api, writer):
    _, path, _ = api
    _init_active(api)
    before = _snapshot(path)
    payload = {
        "schema": "governance/v1", "type": "bootstrap_human", "topic_id": TOPIC,
        "human_session_id": HUMAN, "manifest_sha256": "a" * 64,
        "expires_at": _iso(3600), "nonce": "0011223344556677",
    }
    out = _authorize(api, payload, author=AUTHOR, writer=writer, role="EXECUTOR_1")
    # Contract (packet v1.3 C2 + F0): bootstrap_human is a reserved, tool-only
    # DECISION type; the public validator refuses it as an invalid payload
    # before any authority lookup.  Green at A1 because F0 raises exactly this.
    assert out.get("error_type") == "governance_payload_invalid", out
    # DA W42 §3(b): the refusal must name the reserved type, not merely be
    # "some invalid payload" (F0 raises the same type for unrelated defects).
    assert out["details"]["type"] == "bootstrap_human", out
    assert _snapshot(path) == before


# ── F19 private manifest loader ─────────────────────────────────────────────


@pytest.mark.parametrize("case", [
    "symlink", "not_regular", "file_mode", "parent_mode", "digest_mismatch",
    "expired", "expiry_too_far",
])
def test_f19_private_manifest_loader_refuses(api, tmp_path, case):
    _, path, gov_dir = api
    _init_active(api)
    mpath, digest = _bootstrap_manifest(api)
    expected, reason = "private_manifest_invalid", None
    if case == "symlink":
        link = gov_dir / "link.json"
        os.symlink(mpath, link)
        mpath, reason = str(link), "symlink"
    elif case == "not_regular":
        (gov_dir / "dir.json").mkdir(mode=0o700)
        mpath, reason = str(gov_dir / "dir.json"), "not_regular"
    elif case == "file_mode":
        os.chmod(mpath, 0o644)
        reason = "mode"
    elif case == "parent_mode":
        loose = tmp_path / "loose"
        loose.mkdir(mode=0o755)
        os.chmod(loose, 0o755)
        mpath, digest = _bootstrap_manifest(api, gov_dir=loose)
        reason = "parent_mode"
    elif case == "digest_mismatch":
        digest, expected = "0" * 64, "manifest_digest_mismatch"
    elif case == "expired":
        mpath, digest = _bootstrap_manifest(api, expires=_iso(-3600), name="old.json")
        expected = "manifest_expired"
    else:
        mpath, digest = _bootstrap_manifest(api, expires=_iso(48 * 3600), name="far.json")
        expected = "governance_expiry_too_far"
    before = _snapshot(path)
    out = _bootstrap(api, mpath, digest)
    assert out.get("error_type") == expected, out
    if reason:
        assert out["details"]["reason"] == reason, out
    assert _snapshot(path) == before


def test_f19_owner_check_compares_st_uid_with_geteuid(api, monkeypatch):
    _, path, _ = api
    _init_active(api)
    mpath, digest = _bootstrap_manifest(api)
    real = os.geteuid()
    monkeypatch.setattr(os, "geteuid", lambda: real + 1)
    before = _snapshot(path)
    out = _bootstrap(api, mpath, digest)
    assert out.get("error_type") == "private_manifest_invalid", out
    assert out["details"]["reason"] == "owner"
    assert _snapshot(path) == before


def test_f19_topic_fingerprint_drift_is_refused(api):
    _, path, _ = api
    _init_active(api)
    mpath, digest = _bootstrap_manifest(api, fingerprint="f" * 64)
    before = _snapshot(path)
    out = _bootstrap(api, mpath, digest)
    assert out.get("error_type") == "bootstrap_topic_changed", out
    assert _snapshot(path) == before


def test_f19_win32_platform_gate_is_typed_and_precedes_any_open(api, monkeypatch):
    _, path, gov_dir = api
    _init_active(api)
    mpath, digest = _bootstrap_manifest(api)
    link = gov_dir / "link.json"          # a symlink would be refused by the loader,
    os.symlink(mpath, link)               # so the platform gate must fire first
    monkeypatch.setattr(sys, "platform", "win32")
    before = _snapshot(path)
    out = _bootstrap(api, str(link), digest)
    assert out.get("error_type") == "governance_platform_gated", out
    assert _snapshot(path) == before


# ── P2 approve_pin + pin ────────────────────────────────────────────────────


def test_p2_approve_pin_row_is_one_canonical_string_for_every_reader(api):
    _, path, _ = api
    _init_active(api)
    mpath, digest = _bootstrap_manifest(api)
    assert _bootstrap(api, mpath, digest)["status"] == "applied"
    apath, adigest = _approve_manifest(api)
    out = _approve(api, apath, adigest)
    assert out.get("msg_id") and out["manifest_sha256"] == adigest, out
    row = _stored(path, out["msg_id"])
    assert row["kind"] == "DECISION" and row["role"] == "HUMAN"
    assert row["body_mode"] == "structured" and row["governance_schema"] == "governance/v1"
    payload = json.loads(row["payload_json"])
    assert payload["type"] == "approve_pin" and payload["manifest_sha256"] == adigest
    assert payload["authority_role"] == "ADVOCATE_CODEX"
    assert payload["authority_session_id"] == AUTHORITY
    issuer = payload["issuer"]
    assert issuer["role"] == "HUMAN" and issuer["session_id"] == HUMAN
    assert issuer["topic_id"] == TOPIC and len(issuer["binding_fingerprint"]) == 64
    assert issuer["binding_generation"] >= 1 and issuer["authority_epoch"] is None
    assert _gov().canonical_json(payload) == row["payload_json"]
    read = _read(api, out["msg_id"])
    assert read["payload_json"] == row["payload_json"] and read["body_mode"] == "structured"
    signal = _signal(api, out["msg_id"], session=AUTHORITY, role="ADVOCATE_CODEX")
    assert signal["payload_json"] == row["payload_json"]
    spends = _spends(path)
    mint = [s for s in spends if s["action"] == "approve_pin"]
    assert len(mint) == 1 and mint[0]["manifest_sha256"] == adigest
    assert mint[0]["authorization_msg_id"] == out["msg_id"] and len(mint[0]["target_key"]) == 64


def test_p2_approve_pin_by_a_non_human_caller_is_forbidden(api):
    _, path, _ = api
    _init_active(api)
    mpath, digest = _bootstrap_manifest(api)
    assert _bootstrap(api, mpath, digest)["status"] == "applied"
    apath, adigest = _approve_manifest(api, human=AUTHORITY)
    before = _snapshot(path)
    out = _approve(api, apath, adigest, author=AUTHORITY)
    assert out.get("error_type") == "governance_actor_forbidden", out
    assert _snapshot(path) == before


def test_p2_approve_manifest_is_single_use(api):
    _, path, _ = api
    _init_active(api)
    mpath, digest = _bootstrap_manifest(api)
    assert _bootstrap(api, mpath, digest)["status"] == "applied"
    apath, adigest = _approve_manifest(api)
    assert _approve(api, apath, adigest).get("msg_id")
    before = _snapshot(path)
    out = _approve(api, apath, adigest)
    assert out.get("error_type") == "governance_manifest_spent", out
    assert _snapshot(path) == before


def test_p2_reader_of_the_decision_row_cannot_replay_approve_pin(api):
    """DA C2 node: what a row reader learns (raw ids from the ledger and from
    debate_binding_list) is not enough to replay the HUMAN's approval."""
    call, path, _ = api
    _init_active(api)
    chain = _chain(api)
    approval_row = _read(api, chain["approval"])
    bindings = call("debate_binding_list", topic_id=TOPIC)
    assert any(b["session_id"] == HUMAN for b in bindings["bindings"])
    assert "author_session_id" not in approval_row          # readers never get the column
    before = _snapshot(path)
    # (a) same manifest again, presented as the HUMAN → spent
    replay = _approve(api, chain["approve_path"], chain["approve_digest"])
    assert replay.get("error_type") == "governance_manifest_spent", replay
    # (b) hand-built approve_pin DECISION as HUMAN through the public writer, in
    #     the exact F0 input shape with the REAL digest → refused: on a pinned
    #     topic the public approve_pin form is never minted (v1.3 C2), the F0
    #     tail names it governance_action_not_implemented.
    stored = json.loads(_stored(path, chain["approval"])["payload_json"])
    payload = {
        "schema": "governance/v1", "type": "approve_pin", "topic_id": TOPIC,
        "authority_role": stored["authority_role"],
        "authority_session_id": stored["authority_session_id"],
        "authority_generation": stored["authority_generation"],
        "expected_authority_epoch": stored["expected_authority_epoch"],
        "manifest_sha256": chain["approve_digest"],
        "expires_at": _iso(3600), "nonce": "0a0a0a0a0a0a0a0a",
    }
    forged = _authorize(api, payload, author=HUMAN, role="HUMAN")
    assert forged.get("error_type") == "governance_action_not_implemented", forged
    # (c) pin again after the legitimate pin → consumed
    again = _pin(api, chain["approval"], chain["approve_path"], chain["approve_digest"])
    assert again.get("error_type") == "authorization_consumed", again
    assert _snapshot(path) == before


def test_p2_pin_increments_epoch_and_records_one_consume_spend(api):
    _, path, _ = api
    _init_active(api)
    assert _governance_record(path)["authority_epoch"] == 0
    chain = _chain(api)
    record = _governance_record(path)
    assert record["mode"] == "authority" and record["authority_epoch"] == 1
    assert record["authority_role"] == "ADVOCATE_CODEX"
    assert record["authority_session_id"] == AUTHORITY
    assert record["authority_generation"] >= 1
    assert record["approval_msg_id"] == chain["approval"]
    assert record["candidate_role"] == "ADVOCATE_CODEX"
    assert record["pinned_by"] == HUMAN and record["pinned_at"]
    spends = _spends(path)
    assert [s["action"] for s in spends].count("pin") == 1
    pin = next(s for s in spends if s["action"] == "pin")
    assert pin["authorization_msg_id"] == chain["approval"]
    assert pin["target_key"] == "single" and pin["manifest_sha256"] is None
    assert len(spends) == 3     # bootstrap_human + approve_pin mint + pin consume
    inv = _inventory(api)
    assert inv["mode"] == "authority" and inv["authority_epoch"] == 1
    assert inv["authority"] == {
        "role": "ADVOCATE_CODEX", "session_id": AUTHORITY,
        "generation": record["authority_generation"],
    }
    assert inv["pin_record_backed"] is True
    other = json.loads(_rows(path, "SELECT metadata_json FROM debates WHERE topic_id = ?",
                             (TOPIC,))[0]["metadata_json"])
    assert other["unrelated"] == {"keep": [1, "two"]}


def test_p2_pin_by_another_session_is_forbidden(api):
    _, path, _ = api
    _init_active(api)
    mpath, digest = _bootstrap_manifest(api)
    assert _bootstrap(api, mpath, digest)["status"] == "applied"
    apath, adigest = _approve_manifest(api)
    approval = _approve(api, apath, adigest)["msg_id"]
    before = _snapshot(path)
    out = _pin(api, approval, apath, adigest, author=AUTHOR)
    assert out.get("error_type") == "governance_actor_forbidden", out
    assert _snapshot(path) == before


def test_p2_pin_requires_the_approved_manifest_digest(api):
    _, path, _ = api
    _init_active(api)
    mpath, digest = _bootstrap_manifest(api)
    assert _bootstrap(api, mpath, digest)["status"] == "applied"
    apath, adigest = _approve_manifest(api)
    approval = _approve(api, apath, adigest)["msg_id"]
    other, odigest = _approve_manifest(api, nonce="99aa99aa99aa99aa")
    before = _snapshot(path)
    out = _pin(api, approval, other, odigest)
    assert out.get("error_type") == "manifest_digest_mismatch", out
    assert _snapshot(path) == before


@pytest.mark.parametrize("case", [
    "authority_retired", "authority_rotated", "human_retired", "human_rotated",
])
def test_p2_approve_to_pin_window_is_cas_guarded(api, case):
    """EXECUTOR M3 / DA C3: the authority or the approving HUMAN changes
    between approve_pin and pin.  Authority changes → the approval's CAS
    target no longer exists (authorization_target_changed); HUMAN changes →
    the approval's issuer binding is no longer the ACTIVE row at the stamped
    generation/fingerprint (authorization_issuer_stale).  Zero spends, epoch 0."""
    call, path, _ = api
    _init_active(api)
    mpath, digest = _bootstrap_manifest(api)
    assert _bootstrap(api, mpath, digest)["status"] == "applied"
    apath, adigest = _approve_manifest(api)
    approval = _approve(api, apath, adigest)["msg_id"]
    now = _iso(0)
    if case == "authority_retired":
        with sqlite3.connect(path) as conn:
            conn.execute(
                "UPDATE debate_role_bindings SET state = 'retired', retired_at = ?, "
                "updated_at = ? WHERE topic_id = ? AND role = 'ADVOCATE_CODEX' "
                "AND session_id = ?", (now, now, TOPIC, AUTHORITY))
        expected = "authorization_target_changed"
    elif case == "authority_rotated":
        swap = call("debate_bind_role", topic_id=TOPIC, role="ADVOCATE_CODEX",
                    session_id="codex-f1adv02", reason="rotate authority",
                    replace_active=True)
        assert swap.get("state") == "active", swap
        expected = "authorization_target_changed"
    elif case == "human_retired":
        with sqlite3.connect(path) as conn:
            conn.execute(
                "UPDATE debate_role_bindings SET state = 'retired', retired_at = ?, "
                "updated_at = ? WHERE topic_id = ? AND role = 'HUMAN' AND session_id = ?",
                (now, now, TOPIC, HUMAN))
        expected = "authorization_issuer_stale"
    else:
        swap = call("debate_bind_role", topic_id=TOPIC, role="HUMAN",
                    session_id="human-f1op02", reason="rotate human",
                    replace_active=True)
        assert swap.get("state") == "active", swap
        expected = "authorization_issuer_stale"
    before = _snapshot(path)
    out = _pin(api, approval, apath, adigest)
    assert out.get("error_type") == expected, out
    assert _snapshot(path) == before
    assert _governance_record(path)["authority_epoch"] == 0
    assert [s["action"] for s in _spends(path)] == ["bootstrap_human", "approve_pin"]


def test_p2_pin_fault_between_record_and_spend_rolls_back(api, monkeypatch):
    import debate
    _, path, _ = api
    _init_active(api)
    mpath, digest = _bootstrap_manifest(api)
    assert _bootstrap(api, mpath, digest)["status"] == "applied"
    apath, adigest = _approve_manifest(api)
    approval = _approve(api, apath, adigest)["msg_id"]
    before = _snapshot(path)

    def boom(*args, **kwargs):
        raise RuntimeError("injected after the metadata update")

    monkeypatch.setattr(debate, "_record_authorization_spend", boom)
    out = _pin(api, approval, apath, adigest)
    assert out.get("error_type") == "internal_error", out
    assert _governance_record(path)["authority_epoch"] == 0
    assert _snapshot(path) == before


def test_p2_pin_record_backed_detects_a_raw_metadata_edit(api):
    _, path, _ = api
    _init_active(api)
    _chain(api)
    assert _inventory(api)["pin_record_backed"] is True
    with sqlite3.connect(path) as conn:
        row = conn.execute("SELECT metadata_json FROM debates WHERE topic_id = ?", (TOPIC,)).fetchone()
        meta = json.loads(row[0])
        meta["governance"]["authority_epoch"] = 7
        conn.execute("UPDATE debates SET metadata_json = ? WHERE topic_id = ?",
                     (json.dumps(meta), TOPIC))
    assert _inventory(api)["pin_record_backed"] is False


# ── P3 authorize + consume ──────────────────────────────────────────────────


def test_p3_authorize_grant_is_one_canonical_string_for_every_reader(api):
    _, path, _ = api
    _init_active(api)
    _chain(api)
    grant = _grant(api)
    row = _stored(path, grant)
    assert row["governance_schema"] == "governance/v1" and row["body_mode"] == "structured"
    assert row["author_session_id"] == AUTHORITY and row["provenance_class"] == "parent"
    payload = json.loads(row["payload_json"])
    assert payload["type"] == "authorize" and payload["action"] == "retire_binding"
    issuer = payload["issuer"]
    assert issuer["session_id"] == AUTHORITY and issuer["role"] == "ADVOCATE_CODEX"
    assert issuer["authority_epoch"] == 1 and len(issuer["binding_fingerprint"]) == 64
    assert issuer["binding_generation"] == _governance_record(path)["authority_generation"]
    assert _gov().canonical_json(payload) == row["payload_json"]
    assert _read(api, grant)["payload_json"] == row["payload_json"]
    assert _signal(api, grant)["payload_json"] == row["payload_json"]


def test_p3_bind_with_authorization_retires_target_with_exactly_one_spend(api):
    _, path, _ = api
    _init_active(api)
    _chain(api)
    grant = _grant(api)
    before = _snapshot(path)
    out = _bind(api, grant=grant)
    assert "error_type" not in out, out
    assert out["state"] == "retired" and out["ownership_gap_override"] is True
    assert out["authorization_msg_id"] == grant and out["spend"]["target_key"] == "single"
    after = _snapshot(path)
    assert _changed(before, after) == {"debate_role_bindings", "debate_authorization_spends"}
    rows = _rows(path, "SELECT role, session_id, state FROM debate_role_bindings "
                       "WHERE topic_id = ? ORDER BY role, session_id", (TOPIC,))
    assert {(r["role"], r["session_id"], r["state"]) for r in rows} >= {
        ("EXECUTOR_2", RECIPIENT, "retired"), ("EXECUTOR_1", AUTHOR, "active"),
        ("ADVOCATE_CODEX", AUTHORITY, "active"), ("HUMAN", HUMAN, "active"),
    }
    spends = [s for s in _spends(path) if s["authorization_msg_id"] == grant]
    assert len(spends) == 1 and spends[0]["action"] == "retire_binding"
    assert spends[0]["target_key"] == "single" and spends[0]["manifest_sha256"] is None
    assert json.loads(spends[0]["before_json"])["state"] == "active"
    assert json.loads(spends[0]["after_json"])["state"] == "retired"


def test_p3_bind_without_authorization_is_authorization_required(api):
    _, path, _ = api
    _init_active(api)
    _chain(api)
    before = _snapshot(path)
    out = _bind(api)
    assert out.get("error_type") == "authorization_required", out
    assert "debate_governance_inventory" in json.dumps(out.get("details", {}))
    assert _snapshot(path) == before


def test_p3_alias_conductor_override_msg_id_is_accepted_with_a_deprecation_marker(api):
    _, path, _ = api
    _init_active(api)
    _chain(api)
    grant = _grant(api)
    out = _bind(api, conductor_override_msg_id=grant)
    assert "error_type" not in out, out
    assert out["state"] == "retired" and out["authorization_msg_id"] == grant
    assert out["deprecated_argument"] == "conductor_override_msg_id"


def test_p3_alias_conflict_is_refused_without_rows(api):
    _, path, _ = api
    _init_active(api)
    _chain(api)
    grant = _grant(api)
    other = _grant(api, nonce="1234567890abcdef")
    before = _snapshot(path)
    out = _bind(api, grant=grant, conductor_override_msg_id=other)
    assert out.get("error_type") == "override_argument_conflict", out
    assert _snapshot(path) == before


@pytest.mark.parametrize("case", [
    "action", "target_session", "foreign_topic", "expired", "hold_with_active_claim",
])
def test_f13_grant_mismatches_apply_nothing(api, monkeypatch, case):
    call, path, _ = api
    _init_active(api)
    _chain(api)
    expected = {
        "action": "authorization_action_mismatch",
        "target_session": "authorization_scope_mismatch",
        "foreign_topic": "authorization_not_found",
        "expired": "authorization_expired",
        "hold_with_active_claim": "active_claims_present",
    }[case]
    kwargs = {}
    if case == "action":
        grant = _grant(api, action="diagnostic_uncover", claims="hold")
    elif case == "target_session":
        grant = _grant(api, target_role="EXECUTOR_1", target_session=AUTHOR)
    elif case == "foreign_topic":
        # a REAL grant issued by C3F1B's own pinned authority, consumed on TOPIC
        _init_active(api, topic="C3F1B")
        _chain(api, topic="C3F1B")
        grant = _grant(api, topic="C3F1B")
    elif case == "expired":
        grant = _grant(api)
        gov = _gov()
        later = datetime.now(timezone.utc) + timedelta(days=2)
        monkeypatch.setattr(gov, "utc_now", lambda: later)
    else:
        trigger = call("debate_post_with_recipients", topic_id=TOPIC, role="EXECUTOR_1",
                       priority="M", kind="Q", body="claimable work", payload_json="",
                       author_session_id=AUTHOR, addressed_to_csv="EXECUTOR_2")
        claim = call("debate_worker_claim", topic_id=TOPIC, role="EXECUTOR_2",
                     parent_session_id=RECIPIENT, trigger_msg_id=trigger["msg_id"])
        assert claim.get("worker_session_id"), claim
        grant = _grant(api, claims="hold")
    before = _snapshot(path)
    out = _bind(api, grant=grant, **kwargs)
    assert out.get("error_type") == expected, out
    assert _snapshot(path) == before


def test_f14_replay_is_consumed_even_after_the_target_was_retired(api):
    _, path, _ = api
    _init_active(api)
    _chain(api)
    grant = _grant(api)
    assert _bind(api, grant=grant).get("state") == "retired"
    before = _snapshot(path)
    out = _bind(api, grant=grant)
    assert out.get("error_type") == "authorization_consumed", out
    assert _snapshot(path) == before
    assert len([s for s in _spends(path) if s["authorization_msg_id"] == grant]) == 1


def test_f15_repin_bumps_epoch_and_stales_grants_of_the_old_epoch(api):
    _, path, _ = api
    _init_active(api)
    _chain(api)
    stale = _grant(api)
    apath, adigest = _approve_manifest(api, nonce="abcdef0123456789")
    approval = _approve(api, apath, adigest)["msg_id"]
    assert "error_type" not in _pin(api, approval, apath, adigest)
    assert _governance_record(path)["authority_epoch"] == 2
    before = _snapshot(path)
    out = _bind(api, grant=stale)
    assert out.get("error_type") == "authorization_issuer_stale", out
    assert _snapshot(path) == before
    fresh = _grant(api, nonce="fedcba9876543210")
    assert json.loads(_stored(path, fresh)["payload_json"])["issuer"]["authority_epoch"] == 2
    assert _bind(api, grant=fresh).get("state") == "retired"


def test_f17_two_connections_race_for_one_grant_one_spend(api):
    import db_utils
    import debate
    _, path, _ = api
    _init_active(api)
    _chain(api)
    grant = _grant(api)
    barrier = threading.Barrier(2)
    outcomes = []

    def worker():
        barrier.wait()
        try:
            with db_utils.get_conn_immediate(path) as conn:
                out = debate.bind_role_session(
                    conn, topic_id=TOPIC, role="EXECUTOR_2", session_id=RECIPIENT,
                    state="retired", reason="race", author_session_id=AUTHORITY,
                    authorization_msg_id=grant,
                )
            outcomes.append(out["state"])
        except debate.DebateError as exc:
            outcomes.append(exc.error_type)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert sorted(outcomes) == ["authorization_consumed", "retired"], outcomes
    assert len([s for s in _spends(path) if s["authorization_msg_id"] == grant]) == 1


def test_f17_spend_insert_fault_rolls_back_the_effect(api, monkeypatch):
    import debate
    _, path, _ = api
    _init_active(api)
    _chain(api)
    grant = _grant(api)
    before = _snapshot(path)

    def boom(*args, **kwargs):
        raise RuntimeError("injected after the effect")

    monkeypatch.setattr(debate, "_record_authorization_spend", boom)
    out = _bind(api, grant=grant)
    assert out.get("error_type") == "internal_error", out
    assert _snapshot(path) == before
    state = _rows(path, "SELECT state FROM debate_role_bindings WHERE topic_id = ? "
                        "AND role = 'EXECUTOR_2' AND session_id = ?", (TOPIC, RECIPIENT))
    assert state == [{"state": "active"}]


# ── F18 immutability, classification, migration ─────────────────────────────


def test_f18_spend_rows_are_append_only(api):
    _, path, _ = api
    _init_active(api)
    _chain(api)
    grant = _grant(api)
    assert _bind(api, grant=grant).get("state") == "retired"
    before = _snapshot(path)
    with sqlite3.connect(path) as conn:
        for sql in (
            "UPDATE debate_authorization_spends SET target_key = 'x' WHERE authorization_msg_id = ?",
            "DELETE FROM debate_authorization_spends WHERE authorization_msg_id = ?",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(sql, (grant,))
    assert _snapshot(path) == before


def test_f18_governance_rows_are_immutable_by_column_not_by_payload_shape(api):
    _, path, _ = api
    _init_active(api)
    _chain(api)
    grant = _grant(api)
    before = _snapshot(path)
    with sqlite3.connect(path) as conn:
        for sql in (
            "UPDATE debate_messages SET payload_json = '{}' WHERE msg_id = ?",
            "UPDATE debate_messages SET body = 'edited' WHERE msg_id = ?",
            "UPDATE debate_messages SET governance_schema = NULL WHERE msg_id = ?",
            "UPDATE debate_messages SET kind = 'STATUS' WHERE msg_id = ?",
            "DELETE FROM debate_messages WHERE msg_id = ?",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(sql, (grant,))
    assert _snapshot(path) == before


def test_f18_nested_governance_quote_is_deletable_but_a_grant_is_not(api):
    _, path, _ = api
    _init_active(api)
    _chain(api)
    grant = _grant(api)
    quoted = json.dumps({"quoted": json.loads(_stored(path, grant)["payload_json"])},
                        sort_keys=True, separators=(",", ":"))
    now = _iso(0)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO debate_messages (msg_id, topic_id, role, ts, priority, kind, body, "
            "body_mode, payload_json, created_at) VALUES (?, ?, 'EXECUTOR_1', ?, 'M', "
            "'STATUS', 'quotes a grant', 'structured', ?, ?)",
            ("feedfacecafe", TOPIC, now, quoted, now))
        assert conn.execute("SELECT governance_schema FROM debate_messages WHERE msg_id = ?",
                            ("feedfacecafe",)).fetchone()[0] is None
        conn.execute("DELETE FROM debate_messages WHERE msg_id = ?", ("feedfacecafe",))
        assert conn.execute("SELECT COUNT(*) FROM debate_messages WHERE msg_id = ?",
                            ("feedfacecafe",)).fetchone()[0] == 0
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM debate_messages WHERE msg_id = ?", (grant,))


@pytest.mark.parametrize("writer", WRITERS)
def test_f18_public_status_with_a_complete_governance_payload_is_refused_not_stored(api, writer):
    """Classification contract: only DECISION rows produced by the validator
    (approve_pin / bootstrap_human server-posted, authorize public) carry
    governance_schema; a STATUS carrying the complete stored form is refused."""
    _, path, _ = api
    _init_active(api)
    _chain(api)
    grant = _grant(api)
    payload = json.loads(_stored(path, grant)["payload_json"])
    before = _snapshot(path)
    out = _authorize(api, payload, author=AUTHOR, writer=writer, role="EXECUTOR_1",
                     kind="STATUS")
    # The complete stored form carries "issuer", which only the server may
    # stamp: the validator refuses it first, whatever the kind (F0 order).
    assert out.get("error_type") == "governance_server_field_supplied", out
    assert _snapshot(path) == before
    assert _rows(path, "SELECT COUNT(*) AS c FROM debate_messages WHERE governance_schema "
                       "IS NOT NULL AND kind != 'DECISION'")[0]["c"] == 0


def test_f18_repeat_init_db_keeps_governance_rows_and_triggers(api):
    from schema import init_db
    _, path, _ = api
    _init_active(api)
    _chain(api)
    grant = _grant(api)
    before = _snapshot(path)
    init_db(path)
    init_db(path)
    assert _snapshot(path) == before
    assert _stored(path, grant)["governance_schema"] == "governance/v1"
    with sqlite3.connect(path) as conn:
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='debate_messages'")}
    assert GOVERNANCE_TRIGGERS <= names


def test_f18_migration_adds_the_column_before_the_triggers_on_an_existing_v1_db(tmp_path):
    """DA e99bad07ccf3 (2): a v1 DB built from today's DDL (no governance_schema)
    must gain the column through the ALTER migration BEFORE the immutability
    triggers that reference old.governance_schema are created; no rebuild."""
    from schema import init_db
    path = str(tmp_path / "v1_no_column.db")
    kinds = ("'Q','A','STATUS','DECISION','PING','WATERMARK','STATE','COMPACTION',"
             "'CLAIM','CHALLENGE','EVIDENCE','REBUT','CONCEDE','VERIFY','DISSENT','ESCALATE'")
    with sqlite3.connect(path) as conn:
        conn.executescript(
            "CREATE TABLE debates(topic_id TEXT PRIMARY KEY,title TEXT NOT NULL,"
            "state TEXT NOT NULL,created_at TEXT NOT NULL,created_by_role TEXT NOT NULL,"
            "resolve_by TEXT,archived_at TEXT,roles_json TEXT NOT NULL,metadata_json TEXT);"
            "CREATE TABLE debate_messages(msg_id TEXT PRIMARY KEY,topic_id TEXT NOT NULL "
            "REFERENCES debates(topic_id),role TEXT NOT NULL,ts TEXT NOT NULL,"
            "priority TEXT NOT NULL,kind TEXT NOT NULL CHECK(kind IN (" + kinds + ")),"
            "standing INTEGER,vehicle TEXT,reply_to TEXT REFERENCES debate_messages(msg_id),"
            "body TEXT NOT NULL,protocol_version TEXT,round_no INTEGER,body_mode TEXT,"
            "payload_json TEXT,author_session_id TEXT DEFAULT NULL,"
            "provenance_class TEXT NOT NULL DEFAULT 'legacy',created_at TEXT NOT NULL);"
            "INSERT INTO debates VALUES('C3F1_OLD','history','ACTIVE',"
            "'2026-01-01T00:00:00Z','OLD',NULL,NULL,'[]',NULL);"
            "INSERT INTO debate_messages(msg_id,topic_id,role,ts,priority,kind,body,"
            "created_at) VALUES('abcdef123456','C3F1_OLD','OLD','2026-01-01T00:00:00Z',"
            "'M','STATUS','historical row','2026-01-01T00:00:00Z');"
        )
        conn.row_factory = sqlite3.Row
        old = dict(conn.execute("SELECT * FROM debate_messages").fetchone())
    init_db(path)
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        columns = [r["name"] for r in conn.execute("PRAGMA table_info('debate_messages')")]
        assert "governance_schema" in columns
        names = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='debate_messages'")}
        assert GOVERNANCE_TRIGGERS <= names
        new = dict(conn.execute("SELECT * FROM debate_messages").fetchone())
    assert new.pop("governance_schema") is None
    assert new == old
    init_db(path)   # idempotent: no error, same shape


# ── M-A2 / M-A3 / D-A2 ──────────────────────────────────────────────────────


def test_m_a2_internal_parameters_are_not_reachable_from_public_inputs(api):
    import debate_ops
    import intel_server
    _, path, _ = api
    _init_active(api)
    for name in WRITERS:
        params = inspect.signature(getattr(intel_server, name)).parameters
        assert not {"internal_governance", "internal_unattributed", "governance_schema"} & set(params)
    before = _snapshot(path)
    for name in WRITERS:
        for key in ("internal_governance", "governance_schema"):
            kwargs = {"addressed_to_csv": "EXECUTOR_2"} if name.endswith("recipients") else {}
            kwargs[key] = {"type": "authorize"}
            out = json.loads(getattr(intel_server, name)(
                topic_id=TOPIC, role="EXECUTOR_1", priority="M", kind="STATUS",
                body="probe", author_session_id=AUTHOR, **kwargs,
            ))
            assert out.get("error_type") == "internal_error", out
            # DA W42 §3(a): green for the RIGHT reason — the boundary mapped the
            # unknown-keyword TypeError, not some other crash.
            assert "unexpected keyword argument" in out.get("error", ""), out
            assert key in out.get("error", ""), out
    assert _snapshot(path) == before
    parser = debate_ops.build_parser()
    flags = set()
    for action in parser._actions:
        for choice in getattr(action, "choices", {}) .values() if getattr(action, "choices", None) else ():
            for sub in getattr(choice, "_actions", []):
                flags.update(sub.option_strings)
    assert not {"--internal-governance", "--governance-schema", "--internal-unattributed"} & flags


def test_m_a3_retired_human_binding_can_no_longer_approve(api):
    _, path, _ = api
    _init_active(api)
    _chain(api)
    grant = _grant(api, target_role="HUMAN", target_session=HUMAN)
    out = _bind(api, role="HUMAN", session=HUMAN, grant=grant)
    assert out.get("state") == "retired", out
    apath, adigest = _approve_manifest(api, nonce="0f0f0f0f0f0f0f0f")
    before = _snapshot(path)
    refused = _approve(api, apath, adigest)
    assert refused.get("error_type") == "governance_actor_forbidden", refused
    assert _snapshot(path) == before


def test_m_a3_retired_authority_can_neither_consume_nor_authorize(api):
    _, path, _ = api
    _init_active(api)
    _chain(api)
    pending = _grant(api)                                   # for EXECUTOR_2, unconsumed
    self_grant = _grant(api, target_role="ADVOCATE_CODEX", target_session=AUTHORITY,
                        nonce="1a1a1a1a1a1a1a1a")
    out = _bind(api, role="ADVOCATE_CODEX", session=AUTHORITY, grant=self_grant)
    assert out.get("state") == "retired", out
    before = _snapshot(path)
    consume = _bind(api, grant=pending)
    assert consume.get("error_type") == "governance_actor_forbidden", consume
    fresh = _authorize(api, _grant_payload(api, nonce="2b2b2b2b2b2b2b2b"))
    assert fresh.get("error_type") == "ROLE_UNAVAILABLE", fresh      # A0 auth-first
    assert _snapshot(path) == before


def test_d_a2_far_expiry_is_refused_at_post_and_at_consume(api):
    _, path, _ = api
    _init_active(api)
    _chain(api)
    before = _snapshot(path)
    out = _authorize(api, _grant_payload(api, expires=_iso(48 * 3600)))
    assert out.get("error_type") == "governance_expiry_too_far", out
    assert _snapshot(path) == before
    # plant a stamped grant with a far expiry by raw SQL (a SQL adversary), then consume
    real = _grant(api)
    payload = json.loads(_stored(path, real)["payload_json"])
    payload["expires_at"] = _iso(48 * 3600)
    payload["nonce"] = "3c3c3c3c3c3c3c3c"
    now = _iso(0)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO debate_messages (msg_id, topic_id, role, ts, priority, kind, body, "
            "body_mode, payload_json, author_session_id, provenance_class, governance_schema, "
            "created_at) VALUES (?, ?, 'ADVOCATE_CODEX', ?, 'H', 'DECISION', 'planted', "
            "'structured', ?, ?, 'parent', 'governance/v1', ?)",
            ("badc0ffee000", TOPIC, now, _gov().canonical_json(payload), AUTHORITY, now))
    before = _snapshot(path)
    out = _bind(api, grant="badc0ffee000")
    assert out.get("error_type") == "governance_expiry_too_far", out
    assert _snapshot(path) == before
