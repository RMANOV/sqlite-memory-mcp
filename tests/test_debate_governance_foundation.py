"""C3 F00-F05/F09 foundation contracts; intentional, classified surface RED.

No governance module is imported during collection. Synthetic serializer rows
and old-schema history are data-shape fixtures, never valid authorization.
Publicly issued spend/effect/issuer evidence belongs to the later F1 phase.
"""

from __future__ import annotations

import dataclasses
import hashlib
import importlib
import json
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


def _gov():
    return importlib.import_module("debate_governance")


def _error(gov, error_type, function, *args):
    with pytest.raises(gov.GovernanceError) as caught:
        function(*args)
    assert caught.value.error_type == error_type


def _sized_json(byte_size):
    prefix, suffix = '{"text":"', '"}'
    room = byte_size - len((prefix + suffix).encode("utf-8"))
    raw = prefix + "я" * (room // 2) + "x" * (room % 2) + suffix
    assert len(raw.encode("utf-8")) == byte_size
    assert len(raw) < byte_size
    return raw


def _manifest():
    return {
        "schema": "governance-migration/v1",
        "control_topic_id": "C3F0",
        "nonce": "0011223344556677",
        "targets": [
            {
                "topic_id": "C3F0",
                "role": "EXECUTOR_2",
                "session_id": "codex-f0exec02",
                "expected_generation": 1,
                "expected_fingerprint": "a" * 64,
                "action": "retire_binding",
                "claims": "hold",
            }
        ],
    }


def test_f00_module_import_is_stdlib_only_and_side_effect_free():
    _gov()  # Absence is a scoped ModuleNotFoundError, not collection failure.
    script = (
        "import builtins, importlib, pathlib, socket, sqlite3, sys\n"
        "def forbidden(*a, **kw):\n"
        "    raise AssertionError('import-time external side effect')\n"
        "sqlite3.connect = forbidden\n"
        "socket.socket = forbidden\n"
        "builtins.open = forbidden\n"
        "pathlib.Path.open = forbidden\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "before = set(sys.modules)\n"
        "importlib.import_module('debate_governance')\n"
        "extra = {n.split('.')[0] for n in set(sys.modules) - before}\n"
        "assert extra <= set(sys.stdlib_module_names) | {'debate_governance'}, extra\n"
        "assert not {'debate', 'intel_server', 'schema'} & set(sys.modules)\n"
    )
    run = subprocess.run(
        [sys.executable, "-I", "-S", "-c", script, str(REPO)],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert run.returncode == 0, run.stderr[-2000:]


def test_f01_canonical_json_and_nul_domain_digest():
    gov = _gov()
    value = {"b": 1, "a": "я"}
    canonical = '{"a":"я","b":1}'
    assert gov.canonical_json(value) == canonical
    for domain in ("c3-binding/v1", "c3-topic/v1", "c3-target/v1", "c3-manifest/v1"):
        expected = hashlib.sha256(
            domain.encode("ascii") + bytes([0]) + canonical.encode("utf-8")
        ).hexdigest()
        assert gov.content_digest(domain, value) == expected
        assert gov.content_digest(domain, {"a": "я", "b": 1}) == expected
    assert gov.content_digest("c3-target/v1", value) != gov.content_digest(
        "c3-manifest/v1", value
    )


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")],
                         ids=["nan", "positive_infinity", "negative_infinity"])
def test_f02_canonical_json_refuses_nonfinite(value):
    gov = _gov()
    with pytest.raises(ValueError):
        gov.canonical_json({"bad": value})


@pytest.mark.parametrize(
    ("raw", "error_type"),
    [
        ('{"schema":"governance/v1","schema":"governance/v2"}',
         "governance_duplicate_key"),
        ('{"effect":{"claims":"hold","claims":"retire"}}',
         "governance_duplicate_key"),
        ('{"items":[{"nonce":"a","nonce":"b"}]}', "governance_duplicate_key"),
        ('{"bad":NaN}', "governance_payload_invalid"),
        ('{"bad":Infinity}', "governance_payload_invalid"),
        ('{"bad":-Infinity}', "governance_payload_invalid"),
        ('{"broken":', "governance_payload_invalid"),
        ("[]", "governance_payload_invalid"),
        ("42", "governance_payload_invalid"),
    ],
    ids=["duplicate_root", "duplicate_nested", "duplicate_in_array",
         "nan", "positive_infinity", "negative_infinity", "malformed",
         "array_not_object", "scalar_not_object"],
)
def test_f02_decoder_rejects_ambiguous_or_invalid_objects(raw, error_type):
    gov = _gov()
    _error(gov, error_type, gov.decode_governance_payload, raw)


@pytest.mark.parametrize("byte_size", [65536, 65537], ids=["exact_limit", "over_limit"])
def test_f03_decoder_measures_utf8_bytes_before_schema_validation(byte_size):
    gov = _gov()
    raw = _sized_json(byte_size)
    if byte_size == 65536:
        # Decoder is schema-neutral: otherwise a missing field masks the boundary.
        assert gov.decode_governance_payload(raw) == json.loads(raw)
    else:
        _error(gov, "governance_payload_too_large", gov.decode_governance_payload, raw)


@pytest.mark.parametrize("count", [0, 129], ids=["empty", "too_many_targets"])
def test_f03_manifest_target_count_bound(count):
    gov = _gov()
    manifest = _manifest()
    template = manifest["targets"][0]
    manifest["targets"] = [
        dict(template, session_id=f"codex-f0target{i:03d}") for i in range(count)
    ]
    _error(gov, "manifest_invalid", gov.normalize_migration_manifest, manifest)


def test_f03_manifest_duplicate_identity_is_not_an_extra_capability():
    gov = _gov()
    manifest = _manifest()
    manifest["targets"].append(dict(manifest["targets"][0], claims="retire"))
    _error(gov, "manifest_duplicate_target", gov.normalize_migration_manifest, manifest)


def test_f03_manifest_order_and_claim_policy_digest():
    gov = _gov()
    manifest = _manifest()
    manifest["targets"].append(
        dict(manifest["targets"][0], role="EXECUTOR_3", session_id="codex-f0exec03")
    )
    normalized = gov.normalize_migration_manifest(manifest)
    reversed_manifest = dict(manifest, targets=list(reversed(manifest["targets"])))
    assert gov.normalize_migration_manifest(reversed_manifest) == normalized
    identities = [(t["topic_id"], t["role"], t["session_id"])
                  for t in normalized["targets"]]
    assert identities == sorted(identities)
    digest = gov.content_digest("c3-manifest/v1", normalized)
    assert gov.validate_manifest_digest(reversed_manifest, digest) == normalized
    changed = json.loads(json.dumps(manifest))
    changed["targets"][0]["claims"] = "retire"
    assert gov.content_digest(
        "c3-manifest/v1", gov.normalize_migration_manifest(changed)
    ) != digest
    _error(gov, "manifest_digest_mismatch", gov.validate_manifest_digest, changed, digest)


def test_f04_binding_version_is_frozen_and_fingerprint_covers_all_six_fields():
    gov = _gov()
    value = {
        "topic_id": "C3F0", "role": "EXECUTOR_2", "session_id": "codex-f0exec02",
        "state": "active", "generation": 1, "updated_at": "2026-09-14T00:00:00Z",
    }
    version = gov.BindingVersion(**value)
    assert dataclasses.asdict(version) == value
    with pytest.raises(dataclasses.FrozenInstanceError):
        version.generation = 2
    original = gov.content_digest("c3-binding/v1", dataclasses.asdict(version))
    for field, replacement in (
        ("topic_id", "C3F0_OTHER"), ("role", "EXECUTOR_3"),
        ("session_id", "codex-f0exec03"), ("state", "retired"),
        ("generation", 2), ("updated_at", "2026-09-14T00:00:01Z"),
    ):
        changed = gov.BindingVersion(**dict(value, **{field: replacement}))
        assert gov.content_digest("c3-binding/v1", dataclasses.asdict(changed)) != original


def _old_schema_history(path, shape):
    if shape == "fresh":
        return None
    envelope = ""
    extra_columns = ""
    extra_values = ""
    kinds = "'Q','A','STATUS','DECISION','PING','WATERMARK','STATE','COMPACTION'"
    if shape == "v1_without_provenance":
        kinds += ",'CLAIM','CHALLENGE','EVIDENCE','REBUT','CONCEDE','VERIFY','DISSENT','ESCALATE'"
        envelope = ",protocol_version TEXT,round_no INTEGER,body_mode TEXT,payload_json TEXT"
        extra_columns = ",protocol_version,round_no,body_mode,payload_json"
        extra_values = ",NULL,NULL,NULL,NULL"
    with sqlite3.connect(path) as conn:
        conn.executescript(
            "CREATE TABLE debates(topic_id TEXT PRIMARY KEY,title TEXT NOT NULL,"
            "state TEXT NOT NULL,created_at TEXT NOT NULL,created_by_role TEXT NOT NULL,"
            "resolve_by TEXT,archived_at TEXT,roles_json TEXT NOT NULL,metadata_json TEXT);"
            "CREATE TABLE debate_messages(msg_id TEXT PRIMARY KEY,topic_id TEXT NOT NULL "
            "REFERENCES debates(topic_id),role TEXT NOT NULL,ts TEXT NOT NULL,"
            "priority TEXT NOT NULL,kind TEXT NOT NULL CHECK(kind IN (" + kinds + ")),"
            "standing INTEGER,vehicle TEXT,reply_to TEXT REFERENCES debate_messages(msg_id),"
            "body TEXT NOT NULL" + envelope + ",created_at TEXT NOT NULL);"
            "INSERT INTO debates VALUES('C3F0_OLD','history','ACTIVE',"
            "'2026-01-01T00:00:00Z','OLD',NULL,NULL,'[]',NULL);"
            "INSERT INTO debate_messages(msg_id,topic_id,role,ts,priority,kind,"
            "standing,vehicle,reply_to,body" + extra_columns + ",created_at) "
            "VALUES('abcdef123456','C3F0_OLD','OLD','2026-01-01T00:00:00Z','M',"
            "'STATUS',NULL,'analysis',NULL,'non-authorizing historical row'"
            + extra_values + ",'2026-01-01T00:00:00Z');"
        )
        conn.row_factory = sqlite3.Row
        return dict(conn.execute("SELECT * FROM debate_messages").fetchone())


def _spend_schema_contract(conn):
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' "
        "AND name='debate_authorization_spends'"
    ).fetchone()
    assert row is not None, "F05 surface RED: debate_authorization_spends missing"
    expected = [
        "authorization_msg_id", "target_key", "manifest_sha256", "action",
        "control_topic_id", "target_topic_id", "target_role", "target_session_id",
        "target_fingerprint", "issuer_json", "before_json", "after_json",
        "receipt_json", "spent_at",
    ]
    info = list(conn.execute("PRAGMA table_info(debate_authorization_spends)"))
    assert [r[1] for r in info] == expected
    assert [r[2].upper() for r in info] == ["TEXT"] * len(expected)
    assert [r[3] for r in info] == [1, 1, 0] + [1] * 11
    assert [(r[1], r[5]) for r in info if r[5]] == [
        ("authorization_msg_id", 1), ("target_key", 2)
    ]
    fks = list(conn.execute("PRAGMA foreign_key_list(debate_authorization_spends)"))
    assert any(r[2:5] == ("debate_messages", "authorization_msg_id", "msg_id") for r in fks)
    compact = re.sub(r"\s+", "", row[0]).lower()
    check = (
        "check((manifest_sha256isnullandtarget_key='single')or"
        "(manifest_sha256isnotnullandlength(manifest_sha256)=64andlength(target_key)=64))"
    )
    assert check in compact  # Contract is length-only, NOT a new hex-only SQL CHECK.
    triggers = [r[0].upper() for r in conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='trigger' "
        "AND tbl_name='debate_authorization_spends'"
    )]
    assert any("BEFORE UPDATE" in s and "RAISE(ABORT" in re.sub(r"\s+", "", s)
               for s in triggers)
    assert any("BEFORE DELETE" in s and "RAISE(ABORT" in re.sub(r"\s+", "", s)
               for s in triggers)
    # This checks schema surfaces only. F1 will use a REAL public-issued spend
    # to prove CHECK/immutability/effect behavior, not a fabricated grant row.


@pytest.mark.parametrize("shape", ["fresh", "v1_without_provenance", "pre_v1"])
def test_f05_spend_schema_exists_after_every_upgrade_and_repeat_init(tmp_path, shape):
    from schema import init_db

    path = tmp_path / "foundation.db"
    historical = _old_schema_history(path, shape)
    for _ in range(2):
        init_db(str(path))
        with sqlite3.connect(path) as conn:
            if historical is not None:
                conn.row_factory = sqlite3.Row
                actual = dict(conn.execute(
                    "SELECT * FROM debate_messages WHERE msg_id='abcdef123456'"
                ).fetchone())
                assert {key: actual[key] for key in historical} == historical
                assert actual["author_session_id"] is None
                assert actual["provenance_class"] == "legacy"
                conn.row_factory = None
            assert list(conn.execute("PRAGMA foreign_key_check")) == []
            _spend_schema_contract(conn)


def _stored_format_row():
    # Complete FORMAT EXAMPLE, deliberately not a DB authorization fixture.
    payload = {
        "schema": "governance/v1", "type": "authorize", "action": "retire_binding",
        "topic_id": "C3F0", "target_role": "EXECUTOR_2",
        "target_session_id": "codex-f0exec02", "target_fingerprint": "a" * 64,
        "effect": {"state": "retired", "claims": "hold"},
        "expires_at": "2026-09-15T00:00:00Z", "nonce": "0011223344556677",
        "issuer": {
            "topic_id": "C3F0", "role": "ADVOCATE_CODEX",
            "session_id": "codex-f0adv01", "binding_generation": 1,
            "binding_fingerprint": "b" * 64, "authority_epoch": 1,
        },
    }
    return {
        "msg_id": "abcdef123456", "topic_id": "C3F0", "role": "ADVOCATE_CODEX",
        "kind": "DECISION", "body": "format-only fixture", "protocol_version": None,
        "round_no": None, "body_mode": "structured",
        "payload_json": json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                   separators=(",", ":")),
    }


def test_f09_complete_stored_format_keeps_canonical_string_not_authority_proof():
    gov = _gov()
    row = _stored_format_row()
    before = dict(row)
    out = gov.serialize_debate_message(row)
    assert out["payload_json"] == row["payload_json"]
    assert isinstance(out["payload_json"], str)
    assert out["body_mode"] == "structured"
    assert "protocol_version" not in out and "round_no" not in out
    assert row == before


@pytest.mark.parametrize("payload", [
    None, "{broken", '{"authorizes":{"action":"retire_binding"}}',
    '{"schema":"governance/v2","type":"authorize"}',
    '{"schema":"governance/v1","type":"authorize"}',
], ids=["ordinary", "malformed", "authority_looking_text", "unknown_schema",
        "incomplete_stored_form"])
def test_f09_legacy_or_malformed_rows_are_not_promoted(payload):
    gov = _gov()
    row = dict(_stored_format_row(), payload_json=payload)
    out = gov.serialize_debate_message(row)
    assert not {"protocol_version", "round_no", "body_mode", "payload_json"} & out.keys()
    assert out["msg_id"] == row["msg_id"]


def test_f09_v1_read_shape_is_unchanged():
    gov = _gov()
    row = dict(_stored_format_row(), protocol_version="debate/v1", round_no=1,
               kind="CLAIM", payload_json='{"summary":"existing v1"}')
    assert gov.serialize_debate_message(row) == row
