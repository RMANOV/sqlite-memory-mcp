import os
import sqlite3
import sys
from contextlib import contextmanager

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import premium_runtime
from premium_contract import PREMIUM_RUNTIME_CONTRACT_VERSION
from schema import init_db


_PREMIUM_ENV_VARS = (
    "SQLITE_MEMORY_PREMIUM_ENTITLEMENT_JSON",
    "SQLITE_MEMORY_PREMIUM_ENTITLEMENT_PATH",
    "SQLITE_MEMORY_PREMIUM_ENTITLEMENT_URL",
    "SQLITE_MEMORY_PREMIUM_ARTIFACT_MANIFEST_JSON",
    "SQLITE_MEMORY_PREMIUM_ARTIFACT_MANIFEST_PATH",
    "SQLITE_MEMORY_PREMIUM_ARTIFACT_MANIFEST_URL",
    "SQLITE_MEMORY_PREMIUM_POLICY_JSON",
    "SQLITE_MEMORY_PREMIUM_POLICY_PATH",
    "SQLITE_MEMORY_PREMIUM_POLICY_URL",
    "SQLITE_MEMORY_PREMIUM_PUBLIC_KEY",
    "SQLITE_MEMORY_PREMIUM_ARTIFACT_PUBLIC_KEY",
    "SQLITE_MEMORY_PREMIUM_POLICY_PUBLIC_KEY",
    "SQLITE_MEMORY_PREMIUM_REMOTE_HEADERS_JSON",
    "SQLITE_MEMORY_PREMIUM_REMOTE_HEADERS_PATH",
    "SQLITE_MEMORY_PREMIUM_ENTRYPOINT",
    "SQLITE_MEMORY_PREMIUM_INSTALLATION_SALT",
    "SQLITE_MEMORY_OWNER_APPROVAL",
    "SQLITE_MEMORY_DEBATE_GATE_ENABLED",
    "SQLITE_MEMORY_DEBATE_GATE_DISABLED",
)


@pytest.fixture(autouse=True)
def _isolate_premium_env(monkeypatch):
    for name in _PREMIUM_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


@contextmanager
def _conn_ctx(db_path: str):
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("BEGIN")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


class DummyMCP:
    def __init__(self):
        self.mounted = []
        self.loaded_server_name = None

    def mount(self, target):
        self.mounted.append(target)


def test_init_db_creates_premium_tables(tmp_path):
    db_path = str(tmp_path / "memory.db")
    init_db(db_path)

    conn = sqlite3.connect(db_path)
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    finally:
        conn.close()

    assert "premium_gate_audit" in tables
    assert "premium_revocations" in tables
    assert "premium_artifact_manifests" in tables
    assert "premium_control_plane_cache" in tables


def test_evaluate_feature_gate_denies_without_entitlement_and_audits(tmp_path):
    db_path = str(tmp_path / "memory.db")
    init_db(db_path)
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("BEGIN")
    try:
        verdict = premium_runtime.evaluate_feature_gate(
            conn,
            feature_id="private_extension_runtime",
            server_name="sqlite-kb",
            tool_name="sqlite-kb.premium_runtime",
            actor_id="sqlite-kb",
            payload={"test_case": "missing_entitlement"},
        )
        conn.execute("COMMIT")
    finally:
        conn.close()

    assert verdict["allowed"] is False
    assert verdict["reason"] == "entitlement_missing"

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        audit = conn.execute(
            "SELECT decision, reason FROM premium_gate_audit ORDER BY checked_at DESC LIMIT 1"
        ).fetchone()
        event = conn.execute(
            "SELECT event_type FROM memory_events ORDER BY logical_clock DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()

    assert audit["decision"] == "denied"
    assert audit["reason"] == "entitlement_missing"
    assert event["event_type"] == "premium_gate_denied"


def test_debate_protocol_creation_gate_is_default_off(tmp_path):
    db_path = str(tmp_path / "memory.db")
    init_db(db_path)

    with _conn_ctx(db_path) as conn:
        verdict = premium_runtime.evaluate_debate_protocol_creation_gate(
            conn,
            payload={"test_case": "default_off"},
        )

    assert verdict["allowed"] is True
    assert verdict["decision"] == "skipped"
    assert verdict["reason"] == "debate_protocol_gate_default_off"


def test_debate_protocol_creation_gate_denies_clear_missing_entitlement(
    tmp_path, monkeypatch
):
    db_path = str(tmp_path / "memory.db")
    init_db(db_path)
    monkeypatch.setattr(
        premium_runtime,
        "load_premium_config",
        lambda: {
            **premium_runtime._DEFAULT_CONFIG,
            "debate_protocol_gate_enabled": True,
            "control_plane_required": False,
        },
    )

    with _conn_ctx(db_path) as conn:
        verdict = premium_runtime.evaluate_debate_protocol_creation_gate(
            conn,
            payload={"test_case": "missing_entitlement"},
        )

    assert verdict["allowed"] is False
    assert verdict["reason"] == "entitlement_missing"


def test_debate_protocol_creation_gate_disabled_env_overrides_enabled_config(
    tmp_path, monkeypatch
):
    db_path = str(tmp_path / "memory.db")
    init_db(db_path)
    monkeypatch.setenv("SQLITE_MEMORY_DEBATE_GATE_DISABLED", "1")
    monkeypatch.setattr(
        premium_runtime,
        "load_premium_config",
        lambda: {
            **premium_runtime._DEFAULT_CONFIG,
            "debate_protocol_gate_enabled": True,
        },
    )
    monkeypatch.setattr(
        premium_runtime,
        "_load_entitlement",
        lambda config: pytest.fail("disabled debate gate should not load entitlement"),
    )

    with _conn_ctx(db_path) as conn:
        verdict = premium_runtime.evaluate_debate_protocol_creation_gate(conn)

    assert verdict["allowed"] is True
    assert verdict["decision"] == "skipped"
    assert verdict["reason"] == "debate_protocol_gate_disabled"


def test_debate_protocol_creation_gate_fail_opens_entitlement_runtime_errors(
    tmp_path, monkeypatch
):
    db_path = str(tmp_path / "memory.db")
    init_db(db_path)
    monkeypatch.setattr(
        premium_runtime,
        "load_premium_config",
        lambda: {
            **premium_runtime._DEFAULT_CONFIG,
            "debate_protocol_gate_enabled": True,
            "control_plane_required": False,
        },
    )
    monkeypatch.setattr(
        premium_runtime,
        "_load_entitlement",
        lambda config: (_ for _ in ()).throw(RuntimeError("loader unavailable")),
    )

    with _conn_ctx(db_path) as conn:
        verdict = premium_runtime.evaluate_debate_protocol_creation_gate(
            conn,
            payload={"test_case": "runtime_error"},
        )

    assert verdict["allowed"] is True
    assert verdict["fail_open"] is True
    assert verdict["reason"].startswith(
        "debate_protocol_gate_fail_open:entitlement_load_failed:"
    )

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT decision, reason FROM premium_gate_audit "
            "WHERE feature_id = 'debate_protocol'"
        ).fetchall()
    finally:
        conn.close()

    assert any(
        row["decision"] == "allowed"
        and row["reason"].startswith("debate_protocol_gate_fail_open:")
        for row in rows
    )


def test_debate_protocol_creation_gate_fail_opens_when_evaluator_raises(
    tmp_path, monkeypatch
):
    # Distinct from the by-reason branch above: here evaluate_feature_gate itself
    # raises, exercising the wrapper's anti-lockout except. The recorded reason
    # carries only the exception class, with NO entitlement_load_failed infix.
    db_path = str(tmp_path / "memory.db")
    init_db(db_path)
    monkeypatch.setattr(
        premium_runtime,
        "load_premium_config",
        lambda: {
            **premium_runtime._DEFAULT_CONFIG,
            "debate_protocol_gate_enabled": True,
            "control_plane_required": False,
        },
    )

    def _boom(*args, **kwargs):
        raise RuntimeError("evaluator exploded")

    monkeypatch.setattr(premium_runtime, "evaluate_feature_gate", _boom)

    with _conn_ctx(db_path) as conn:
        verdict = premium_runtime.evaluate_debate_protocol_creation_gate(
            conn,
            payload={"test_case": "evaluator_raises"},
        )

    assert verdict["allowed"] is True
    assert verdict["fail_open"] is True
    assert verdict["reason"] == "debate_protocol_gate_fail_open:RuntimeError"

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT decision, reason FROM premium_gate_audit "
            "WHERE feature_id = 'debate_protocol'"
        ).fetchall()
    finally:
        conn.close()

    # Distinct runtime-error audit row, and never confused with entitlement_valid.
    assert any(
        row["decision"] == "allowed"
        and row["reason"] == "debate_protocol_gate_fail_open:RuntimeError"
        for row in rows
    )
    assert not any(row["reason"] == "entitlement_valid" for row in rows)


def test_debate_protocol_creation_gate_allows_with_valid_entitlement(
    tmp_path, monkeypatch
):
    # (b) ENABLED + valid entitlement => allowed via the normal entitlement path,
    # audited as entitlement_valid (NOT a fail-open row).
    db_path = str(tmp_path / "memory.db")
    init_db(db_path)
    entitlement = {
        "entitlement_id": "ent-debate-unit",
        "customer_id": "cust-debate-unit",
        "features": ["debate_protocol"],
        "machine_ids": [premium_runtime.MACHINE_ID],
        "signature": {"alg": "ed25519", "value": "unused"},
    }
    monkeypatch.setattr(
        premium_runtime,
        "load_premium_config",
        lambda: {
            **premium_runtime._DEFAULT_CONFIG,
            "debate_protocol_gate_enabled": True,
            "control_plane_required": False,
        },
    )
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

    with _conn_ctx(db_path) as conn:
        verdict = premium_runtime.evaluate_debate_protocol_creation_gate(
            conn,
            payload={"test_case": "valid_entitlement"},
        )

    assert verdict["allowed"] is True
    assert verdict["reason"] == "entitlement_valid"
    assert not verdict.get("fail_open")


def test_evaluate_feature_gate_honors_local_revocation(tmp_path, monkeypatch):
    db_path = str(tmp_path / "memory.db")
    init_db(db_path)
    entitlement = {
        "entitlement_id": "ent-1",
        "customer_id": "cust-1",
        "features": ["private_extension_runtime"],
        "machine_ids": [premium_runtime.MACHINE_ID],
        "owner_approval_sha256": premium_runtime._hash_text("approve-now"),
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
    monkeypatch.setenv("SQLITE_MEMORY_OWNER_APPROVAL", "approve-now")

    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("BEGIN")
    try:
        premium_runtime.revoke_entitlement(
            conn,
            entitlement_id="ent-1",
            customer_id="cust-1",
            reason="manual revoke",
            revoked_by="tester",
        )
        verdict = premium_runtime.evaluate_feature_gate(
            conn,
            feature_id="private_extension_runtime",
            server_name="sqlite-kb",
            tool_name="sqlite-kb.premium_runtime",
        )
        conn.execute("COMMIT")
    finally:
        conn.close()

    assert verdict["allowed"] is False
    assert verdict["reason"] == "entitlement_revoked"


def test_evaluate_feature_gate_requires_machine_binding_by_default(
    tmp_path, monkeypatch
):
    db_path = str(tmp_path / "memory.db")
    init_db(db_path)
    entitlement = {
        "entitlement_id": "ent-no-machine",
        "customer_id": "cust-no-machine",
        "features": ["acl_rbac"],
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
    monkeypatch.setattr(
        premium_runtime,
        "load_premium_config",
        lambda: {**premium_runtime._DEFAULT_CONFIG, "control_plane_required": False},
    )

    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("BEGIN")
    try:
        verdict = premium_runtime.evaluate_feature_gate(
            conn,
            feature_id="acl_rbac",
            server_name="sqlite-kb",
            tool_name="sqlite-kb.acl_rbac",
        )
        conn.execute("COMMIT")
    finally:
        conn.close()

    assert verdict["allowed"] is False
    assert verdict["reason"] == "machine_binding_missing"


def test_evaluate_feature_gate_expands_packs_and_dependencies(tmp_path, monkeypatch):
    db_path = str(tmp_path / "memory.db")
    init_db(db_path)
    entitlement = {
        "entitlement_id": "ent-pack-1",
        "customer_id": "cust-pack-1",
        "packs": ["briefing_suite"],
        "machine_ids": [premium_runtime.MACHINE_ID],
        "owner_approval_sha256": premium_runtime._hash_text("approve-pack"),
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
    monkeypatch.setattr(
        premium_runtime,
        "load_premium_config",
        lambda: {**premium_runtime._DEFAULT_CONFIG, "control_plane_required": False},
    )
    monkeypatch.setenv("SQLITE_MEMORY_OWNER_APPROVAL", "approve-pack")

    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("BEGIN")
    try:
        briefing = premium_runtime.evaluate_feature_gate(
            conn,
            feature_id="instant_briefing",
            server_name="sqlite-kb",
            tool_name="sqlite-kb.instant_briefing",
        )
        runtime = premium_runtime.evaluate_feature_gate(
            conn,
            feature_id="private_extension_runtime",
            server_name="sqlite-kb",
            tool_name="sqlite-kb.premium_runtime",
        )
        denied = premium_runtime.evaluate_feature_gate(
            conn,
            feature_id="client_memory_twin",
            server_name="sqlite-kb",
            tool_name="sqlite-kb.client_memory_twin",
        )
        conn.execute("COMMIT")
    finally:
        conn.close()

    assert briefing["allowed"] is True
    assert briefing["selection_mode"] == "packs"
    assert briefing["selected_packs"] == ["briefing_suite"]
    assert "partner_digest" in briefing["effective_features"]
    assert "advanced_ranking" in briefing["effective_features"]
    assert runtime["allowed"] is False
    assert runtime["reason"] == "artifact_manifest_required"
    assert denied["allowed"] is False
    assert denied["reason"] == "feature_not_entitled"


def test_protected_operator_surface_pack_expands_password_view_dependency(
    tmp_path, monkeypatch
):
    db_path = str(tmp_path / "memory.db")
    init_db(db_path)
    entitlement = {
        "entitlement_id": "ent-pack-protected",
        "customer_id": "cust-pack-protected",
        "packs": ["protected_operator_surface"],
        "machine_ids": [premium_runtime.MACHINE_ID],
        "owner_approval_sha256": premium_runtime._hash_text("approve-protected"),
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
    monkeypatch.setattr(
        premium_runtime,
        "load_premium_config",
        lambda: {**premium_runtime._DEFAULT_CONFIG, "control_plane_required": False},
    )
    monkeypatch.setenv("SQLITE_MEMORY_OWNER_APPROVAL", "approve-protected")

    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("BEGIN")
    try:
        protected = premium_runtime.evaluate_feature_gate(
            conn,
            feature_id="password_protected_views",
            server_name="sqlite-kb",
            tool_name="sqlite-kb.password_protected_views",
        )
        custom = premium_runtime.evaluate_feature_gate(
            conn,
            feature_id="custom_design_tab",
            server_name="sqlite-kb",
            tool_name="sqlite-kb.custom_design_tab",
        )
        conn.execute("COMMIT")
    finally:
        conn.close()

    assert protected["allowed"] is True
    assert custom["allowed"] is True
    assert protected["selected_packs"] == ["protected_operator_surface"]
    assert "password_protected_views" in protected["effective_features"]
    assert "custom_design_tab" in protected["effective_features"]


def test_evaluate_feature_gate_denies_when_manifest_required_and_missing(
    tmp_path, monkeypatch
):
    db_path = str(tmp_path / "memory.db")
    init_db(db_path)
    entitlement = {
        "entitlement_id": "ent-required-manifest",
        "customer_id": "cust-required-manifest",
        "features": ["private_extension_runtime"],
        "machine_ids": [premium_runtime.MACHINE_ID],
        "owner_approval_sha256": premium_runtime._hash_text("approve-manifest"),
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
    monkeypatch.setattr(
        premium_runtime,
        "load_premium_config",
        lambda: {
            **premium_runtime._DEFAULT_CONFIG,
            "control_plane_required": False,
            "require_artifact_manifest": True,
        },
    )
    monkeypatch.setenv("SQLITE_MEMORY_OWNER_APPROVAL", "approve-manifest")

    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("BEGIN")
    try:
        verdict = premium_runtime.evaluate_feature_gate(
            conn,
            feature_id="private_extension_runtime",
            server_name="sqlite-kb",
            tool_name="sqlite-kb.premium_runtime",
        )
        conn.execute("COMMIT")
    finally:
        conn.close()

    assert verdict["allowed"] is False
    assert verdict["reason"] == "artifact_manifest_required"


def test_evaluate_feature_gate_accepts_manifest_and_control_policy(
    tmp_path, monkeypatch
):
    db_path = str(tmp_path / "memory.db")
    init_db(db_path)
    premium_file = tmp_path / "premium_plugin_verified.py"
    premium_file.write_text(
        "def register_premium_extensions(mcp, *, server_name=None, mount_context=None):\n"
        "    return {'mounted': True}\n",
        encoding="utf-8",
    )
    entrypoint_info = premium_runtime._entrypoint_runtime_info(str(premium_file))
    entitlement = {
        "entitlement_id": "ent-artifact-ok",
        "customer_id": "cust-artifact-ok",
        "features": ["private_extension_runtime"],
        "machine_ids": [premium_runtime.MACHINE_ID],
        "owner_approval_sha256": premium_runtime._hash_text("approve-verified"),
        "signature": {"alg": "ed25519", "value": "unused"},
    }
    manifest = {
        "manifest_id": "manifest-1",
        "customer_id": "cust-artifact-ok",
        "extension_name": "sqlite-memory-mcp-premium",
        "contract_version": PREMIUM_RUNTIME_CONTRACT_VERSION,
        "entrypoint_ref": entrypoint_info["entrypoint_ref"],
        "entrypoint_sha256": entrypoint_info["entrypoint_sha256"],
        "protection_phase": 3,
        "minimum_host_version": "3.5.0",
        "signature": {"alg": "ed25519", "value": "unused"},
    }
    policy = {
        "policy_id": "policy-1",
        "customer_id": "cust-artifact-ok",
        "allowed_manifest_ids": ["manifest-1"],
        "allowed_features": ["private_extension_runtime"],
        "allowed_entrypoint_hashes": [entrypoint_info["entrypoint_sha256"]],
        "minimum_protection_phase": 2,
        "require_artifact_manifest": True,
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
    monkeypatch.setattr(
        premium_runtime,
        "_verify_signed_payload",
        lambda payload, public_key_value: (True, "signature_valid"),
    )
    monkeypatch.setattr(
        premium_runtime,
        "_load_artifact_manifest",
        lambda config: (manifest, "test:artifact"),
    )
    monkeypatch.setattr(
        premium_runtime,
        "_load_control_plane_document",
        lambda config: (policy, "test:policy"),
    )
    monkeypatch.setattr(
        premium_runtime,
        "load_premium_config",
        lambda: {
            **premium_runtime._DEFAULT_CONFIG,
            "require_artifact_manifest": True,
            "control_plane_required": True,
        },
    )
    monkeypatch.setenv("SQLITE_MEMORY_OWNER_APPROVAL", "approve-verified")

    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("BEGIN")
    try:
        verdict = premium_runtime.evaluate_feature_gate(
            conn,
            feature_id="private_extension_runtime",
            server_name="sqlite-kb",
            tool_name="sqlite-kb.premium_runtime",
            payload={"entrypoint": str(premium_file)},
        )
        conn.execute("COMMIT")
    finally:
        conn.close()

    assert verdict["allowed"] is True
    assert verdict["manifest_id"] == "manifest-1"
    assert verdict["control_plane_status"] == "live"
    assert verdict["protection_phase"] == 3
    assert verdict["installation_fingerprint"].startswith("sha256:")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        manifest_row = conn.execute(
            "SELECT manifest_id, protection_phase FROM premium_artifact_manifests"
        ).fetchone()
        policy_row = conn.execute(
            "SELECT policy_id FROM premium_control_plane_cache"
        ).fetchone()
    finally:
        conn.close()

    assert manifest_row["manifest_id"] == "manifest-1"
    assert manifest_row["protection_phase"] == 3
    assert policy_row["policy_id"] == "policy-1"


def test_manifest_required_policy_loads_manifest_for_non_runtime_feature(
    tmp_path, monkeypatch
):
    db_path = str(tmp_path / "memory.db")
    init_db(db_path)
    entitlement = {
        "entitlement_id": "ent-acl-manifest",
        "customer_id": "cust-acl-manifest",
        "features": ["acl_rbac"],
        "machine_ids": [premium_runtime.MACHINE_ID],
        "signature": {"alg": "ed25519", "value": "unused"},
    }
    manifest = {
        "manifest_id": "manifest-acl",
        "customer_id": "cust-acl-manifest",
        "extension_name": "sqlite-memory-mcp-premium",
        "contract_version": PREMIUM_RUNTIME_CONTRACT_VERSION,
        "protection_phase": 2,
        "minimum_host_version": "3.5.0",
        "signature": {"alg": "ed25519", "value": "unused"},
    }
    policy = {
        "policy_id": "policy-acl",
        "customer_id": "cust-acl-manifest",
        "allowed_manifest_ids": ["manifest-acl"],
        "allowed_features": ["acl_rbac"],
        "minimum_protection_phase": 2,
        "require_artifact_manifest": True,
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
    monkeypatch.setattr(
        premium_runtime,
        "_verify_signed_payload",
        lambda payload, public_key_value: (True, "signature_valid"),
    )
    monkeypatch.setattr(
        premium_runtime,
        "_load_artifact_manifest",
        lambda config: (manifest, "test:artifact"),
    )
    monkeypatch.setattr(
        premium_runtime,
        "_load_control_plane_document",
        lambda config: (policy, "test:policy"),
    )
    monkeypatch.setattr(
        premium_runtime,
        "load_premium_config",
        lambda: {
            **premium_runtime._DEFAULT_CONFIG,
            "control_plane_required": True,
        },
    )

    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("BEGIN")
    try:
        verdict = premium_runtime.evaluate_feature_gate(
            conn,
            feature_id="acl_rbac",
            server_name="sqlite-kb",
            tool_name="sqlite-kb.acl_rbac",
        )
        conn.execute("COMMIT")
    finally:
        conn.close()

    assert verdict["allowed"] is True
    assert verdict["manifest_id"] == "manifest-acl"
    assert verdict["control_plane_status"] == "live"
    assert verdict["protection_phase"] == 2


def test_evaluate_feature_gate_uses_cached_control_policy(tmp_path, monkeypatch):
    db_path = str(tmp_path / "memory.db")
    init_db(db_path)
    entitlement = {
        "entitlement_id": "ent-cache-policy",
        "customer_id": "cust-cache-policy",
        "packs": ["briefing_suite"],
        "machine_ids": [premium_runtime.MACHINE_ID],
        "signature": {"alg": "ed25519", "value": "unused"},
    }
    policy = {
        "policy_id": "policy-cache",
        "customer_id": "cust-cache-policy",
        "allowed_features": ["instant_briefing", "private_extension_runtime"],
        "cache_ttl_seconds": 3600,
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
    monkeypatch.setattr(
        premium_runtime,
        "_verify_signed_payload",
        lambda payload, public_key_value: (True, "signature_valid"),
    )
    monkeypatch.setattr(
        premium_runtime,
        "_load_control_plane_document",
        lambda config: (_ for _ in ()).throw(RuntimeError("offline")),
    )
    monkeypatch.setattr(
        premium_runtime,
        "load_premium_config",
        lambda: {
            **premium_runtime._DEFAULT_CONFIG,
            "control_plane_required": True,
            "allow_cached_control_plane": True,
        },
    )

    with sqlite3.connect(db_path, isolation_level=None) as seed_conn:
        seed_conn.row_factory = sqlite3.Row
        seed_conn.execute("BEGIN")
        premium_runtime._cache_control_plane_policy(
            seed_conn,
            policy=policy,
            source_ref="test:cache",
            config={
                **premium_runtime._DEFAULT_CONFIG,
                "control_plane_cache_ttl_seconds": 3600,
            },
        )
        seed_conn.execute("COMMIT")

    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("BEGIN")
    try:
        verdict = premium_runtime.evaluate_feature_gate(
            conn,
            feature_id="instant_briefing",
            server_name="sqlite-kb",
            tool_name="sqlite-kb.instant_briefing",
        )
        conn.execute("COMMIT")
    finally:
        conn.close()

    assert verdict["allowed"] is True
    assert verdict["control_plane_status"] == "cached"


def test_valid_control_plane_cache_skips_live_fetch(tmp_path, monkeypatch):
    db_path = str(tmp_path / "memory.db")
    init_db(db_path)
    policy = {
        "policy_id": "policy-cache-first",
        "customer_id": "cust-cache-first",
        "cache_ttl_seconds": 3600,
        "signature": {"alg": "ed25519", "value": "unused"},
    }
    config = {
        **premium_runtime._DEFAULT_CONFIG,
        "allow_cached_control_plane": True,
    }
    monkeypatch.setattr(
        premium_runtime,
        "_verify_signed_payload",
        lambda payload, public_key_value: (True, "signature_valid"),
    )
    live_calls = []
    monkeypatch.setattr(
        premium_runtime,
        "_load_control_plane_document",
        lambda cfg: live_calls.append(cfg) or ({"policy_id": "live"}, "live"),
    )

    with sqlite3.connect(db_path, isolation_level=None) as conn:
        conn.row_factory = sqlite3.Row
        premium_runtime._cache_control_plane_policy(
            conn,
            policy=policy,
            source_ref="test:cache",
            config=config,
        )
        resolution = premium_runtime._resolve_control_plane_policy(
            conn,
            config=config,
            customer_id="cust-cache-first",
        )

    assert resolution["status"] == "cached"
    assert resolution["policy"]["policy_id"] == "policy-cache-first"
    assert live_calls == []


def test_load_entitlement_supports_remote_url_and_headers(monkeypatch):
    config = {
        **premium_runtime._DEFAULT_CONFIG,
        "entitlement_url_env_var": "SQLITE_MEMORY_PREMIUM_ENTITLEMENT_URL",
        "remote_headers_inline_env_var": "SQLITE_MEMORY_PREMIUM_REMOTE_HEADERS_JSON",
    }
    monkeypatch.setenv(
        "SQLITE_MEMORY_PREMIUM_ENTITLEMENT_URL",
        "https://issuer.example/v1/runtime/customers/cust-1/entitlement",
    )
    monkeypatch.setenv(
        "SQLITE_MEMORY_PREMIUM_REMOTE_HEADERS_JSON",
        '{"Authorization":"Bearer runtime-token","X-Customer":"cust-1"}',
    )

    captured = {}

    def _fake_remote(url, *, timeout_seconds, headers=None):
        captured["url"] = url
        captured["timeout_seconds"] = timeout_seconds
        captured["headers"] = dict(headers or {})
        return {"entitlement_id": "ent-remote"}

    monkeypatch.setattr(premium_runtime, "_load_remote_json", _fake_remote)

    payload, source_ref = premium_runtime._load_entitlement(config)

    assert payload["entitlement_id"] == "ent-remote"
    assert (
        source_ref == "https://issuer.example/v1/runtime/customers/cust-1/entitlement"
    )
    assert captured["headers"]["Authorization"] == "Bearer runtime-token"
    assert captured["headers"]["X-Customer"] == "cust-1"


def test_load_artifact_manifest_supports_remote_url(monkeypatch):
    config = {
        **premium_runtime._DEFAULT_CONFIG,
        "artifact_manifest_url_env_var": "SQLITE_MEMORY_PREMIUM_ARTIFACT_MANIFEST_URL",
    }
    monkeypatch.setenv(
        "SQLITE_MEMORY_PREMIUM_ARTIFACT_MANIFEST_URL",
        "https://issuer.example/v1/runtime/customers/cust-1/artifact-manifest",
    )
    monkeypatch.setattr(
        premium_runtime,
        "_load_remote_json",
        lambda url, *, timeout_seconds, headers=None: {
            "manifest_id": "manifest-remote"
        },
    )

    payload, source_ref = premium_runtime._load_artifact_manifest(config)

    assert payload["manifest_id"] == "manifest-remote"
    assert (
        source_ref
        == "https://issuer.example/v1/runtime/customers/cust-1/artifact-manifest"
    )


def test_maybe_mount_premium_extensions_refuses_import_when_gate_denies(
    tmp_path, monkeypatch
):
    db_path = str(tmp_path / "memory.db")
    init_db(db_path)
    premium_file = tmp_path / "premium_plugin.py"
    premium_file.write_text(
        "raise RuntimeError('should not import when gate denies')\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SQLITE_MEMORY_PREMIUM_ENTRYPOINT", str(premium_file))
    monkeypatch.setattr(premium_runtime, "_get_conn", lambda: _conn_ctx(db_path))
    monkeypatch.setattr(
        premium_runtime,
        "evaluate_feature_gate",
        lambda conn, **kwargs: {
            "allowed": False,
            "decision": "denied",
            "reason": "entitlement_missing",
            "feature_id": "private_extension_runtime",
        },
    )

    result = premium_runtime.maybe_mount_premium_extensions(
        DummyMCP(), server_name="sqlite-kb"
    )

    assert result["status"] == "denied"
    assert result["reason"] == "entitlement_missing"


def test_maybe_mount_premium_extensions_loads_private_module_when_allowed(
    tmp_path, monkeypatch
):
    db_path = str(tmp_path / "memory.db")
    init_db(db_path)
    premium_file = tmp_path / "premium_plugin.py"
    premium_file.write_text(
        "def register(mcp, server_name=None):\n"
        "    mcp.loaded_server_name = server_name\n"
        "    return {'loaded': server_name}\n",
        encoding="utf-8",
    )
    mcp = DummyMCP()
    monkeypatch.setenv("SQLITE_MEMORY_PREMIUM_ENTRYPOINT", str(premium_file))
    monkeypatch.setattr(premium_runtime, "_get_conn", lambda: _conn_ctx(db_path))
    monkeypatch.setattr(
        premium_runtime,
        "evaluate_feature_gate",
        lambda conn, **kwargs: {
            "allowed": True,
            "decision": "allowed",
            "reason": "entitlement_valid",
            "feature_id": "private_extension_runtime",
            "entitlement_id": "ent-1",
            "customer_id": "cust-1",
            "manifest_id": "manifest-load",
            "protection_phase": 2,
            "installation_fingerprint": "sha256:test-load",
        },
    )

    result = premium_runtime.maybe_mount_premium_extensions(
        mcp, server_name="sqlite-kb"
    )

    assert result["status"] == "loaded"
    assert mcp.loaded_server_name == "sqlite-kb"

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        audit = conn.execute(
            "SELECT decision, reason FROM premium_gate_audit ORDER BY checked_at DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()

    assert audit["decision"] == "load_success"
    assert audit["reason"] == "premium_extensions_loaded"


def test_maybe_mount_premium_extensions_passes_mount_context(tmp_path, monkeypatch):
    db_path = str(tmp_path / "memory.db")
    init_db(db_path)
    premium_file = tmp_path / "premium_plugin_ctx.py"
    premium_file.write_text(
        "def register_premium_extensions(mcp, *, server_name=None, mount_context=None):\n"
        "    mcp.loaded_server_name = server_name\n"
        "    mcp.contract_version = mount_context.contract_version\n"
        "    mcp.feature_id = mount_context.feature_id\n"
        "    mcp.host_runtime_version = mount_context.host_runtime_version\n"
        "    mcp.installation_fingerprint = mount_context.installation_fingerprint\n"
        "    mcp.manifest_id = mount_context.manifest_id\n"
        "    mcp.protection_phase = mount_context.protection_phase\n"
        "    return {'mounted': True, 'contract_version': mount_context.contract_version}\n",
        encoding="utf-8",
    )
    mcp = DummyMCP()
    monkeypatch.setenv("SQLITE_MEMORY_PREMIUM_ENTRYPOINT", str(premium_file))
    monkeypatch.setattr(premium_runtime, "_get_conn", lambda: _conn_ctx(db_path))
    monkeypatch.setattr(
        premium_runtime,
        "evaluate_feature_gate",
        lambda conn, **kwargs: {
            "allowed": True,
            "decision": "allowed",
            "reason": "entitlement_valid",
            "feature_id": "private_extension_runtime",
            "entitlement_id": "ent-ctx",
            "customer_id": "cust-ctx",
            "manifest_id": "manifest-ctx",
            "protection_phase": 4,
            "installation_fingerprint": "sha256:test-ctx",
        },
    )

    result = premium_runtime.maybe_mount_premium_extensions(
        mcp, server_name="sqlite-unified"
    )

    assert result["status"] == "loaded"
    assert mcp.loaded_server_name == "sqlite-unified"
    assert mcp.contract_version == PREMIUM_RUNTIME_CONTRACT_VERSION
    assert mcp.feature_id == "private_extension_runtime"
    assert mcp.host_runtime_version == premium_runtime.HOST_RUNTIME_VERSION
    assert mcp.installation_fingerprint == "sha256:test-ctx"
    assert mcp.manifest_id == "manifest-ctx"
    assert mcp.protection_phase == 4


def test_maybe_mount_reuses_gate_runtime_context(tmp_path, monkeypatch):
    db_path = str(tmp_path / "memory.db")
    init_db(db_path)
    premium_file = tmp_path / "premium_plugin_reuse.py"
    premium_file.write_text(
        "def register_premium_extensions(mcp, *, mount_context=None, **kwargs):\n"
        "    mcp.runtime_config = mount_context.config\n"
        "    return {'mounted': True}\n",
        encoding="utf-8",
    )
    mcp = DummyMCP()
    monkeypatch.setenv("SQLITE_MEMORY_PREMIUM_ENTRYPOINT", str(premium_file))
    monkeypatch.setattr(premium_runtime, "_get_conn", lambda: _conn_ctx(db_path))
    monkeypatch.setattr(
        premium_runtime,
        "evaluate_feature_gate",
        lambda conn, **kwargs: {
            "allowed": True,
            "decision": "allowed",
            "reason": "entitlement_valid",
            "feature_id": "private_extension_runtime",
            "manifest_id": "manifest-reuse",
            "protection_phase": 3,
            "installation_fingerprint": "sha256:reuse",
            "_runtime_context": {
                "manifest": {"manifest_id": "manifest-reuse"},
                "control_policy": {"policy_id": "policy-reuse"},
            },
        },
    )
    monkeypatch.setattr(
        premium_runtime,
        "_load_artifact_manifest",
        lambda config: pytest.fail("mount must reuse the gate manifest"),
    )
    monkeypatch.setattr(
        premium_runtime,
        "_resolve_control_plane_policy",
        lambda *args, **kwargs: pytest.fail("mount must reuse the gate policy"),
    )

    result = premium_runtime.maybe_mount_premium_extensions(
        mcp, server_name="sqlite-unified"
    )

    assert result["status"] == "loaded"
    assert mcp.runtime_config["_premium_artifact_manifest"]["manifest_id"] == (
        "manifest-reuse"
    )
    assert mcp.runtime_config["_premium_control_policy"]["policy_id"] == (
        "policy-reuse"
    )


# ── Real-crypto invariants (no mocked signatures) ─────────────────────────────


def _ed25519_keypair():
    """Generate an Ed25519 keypair for test signing."""
    import base64
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization

    private_key = Ed25519PrivateKey.generate()
    public_bytes = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    public_key_b64 = base64.b64encode(public_bytes).decode("ascii")
    return private_key, public_key_b64


def _sign_payload(payload: dict, private_key) -> dict:
    """Attach a real Ed25519 signature to a payload (in-place copy)."""
    import base64

    signed = {k: v for k, v in payload.items() if k != "signature"}
    signature_bytes = private_key.sign(
        premium_runtime._canonical_signed_payload(signed)
    )
    signed["signature"] = {
        "alg": "ed25519",
        "value": base64.b64encode(signature_bytes).decode("ascii"),
    }
    return signed


def test_real_ed25519_verify_accepts_genuine_and_rejects_tamper():
    private_key, public_key_b64 = _ed25519_keypair()
    payload = {"policy_id": "real-1", "allowed_features": ["instant_briefing"]}
    signed = _sign_payload(payload, private_key)

    ok, reason = premium_runtime._verify_signed_payload(signed, public_key_b64)
    assert ok is True, reason

    # Tamper: add a permissive feature AFTER signing — verification must fail
    tampered = dict(signed)
    tampered["allowed_features"] = ["instant_briefing", "private_extension_runtime"]
    ok2, reason2 = premium_runtime._verify_signed_payload(tampered, public_key_b64)
    assert ok2 is False
    assert reason2.startswith("signature_invalid"), reason2


def test_entitlement_with_z_format_expiry_is_correctly_denied(tmp_path, monkeypatch):
    """Z-suffix ISO format must be interpreted via epoch, not string compare."""
    db_path = str(tmp_path / "memory.db")
    init_db(db_path)
    # Expires one hour ago, serialised with Z (which sorts above "+" lexically)
    from datetime import datetime, timedelta, timezone

    expired_at = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    entitlement = {
        "entitlement_id": "ent-z-expired",
        "customer_id": "cust-z",
        "features": ["instant_briefing"],
        "machine_ids": [premium_runtime.MACHINE_ID],
        "expires_at": expired_at,
        "owner_approval_sha256": premium_runtime._hash_text("approve-z"),
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
    monkeypatch.setattr(
        premium_runtime,
        "load_premium_config",
        lambda: {**premium_runtime._DEFAULT_CONFIG, "control_plane_required": False},
    )
    monkeypatch.setenv("SQLITE_MEMORY_OWNER_APPROVAL", "approve-z")

    with _conn_ctx(db_path) as conn:
        verdict = premium_runtime.evaluate_feature_gate(
            conn,
            feature_id="instant_briefing",
            server_name="sqlite-kb",
            tool_name="sqlite-kb.instant_briefing",
        )

    assert verdict["allowed"] is False
    assert verdict["reason"] == "entitlement_expired"


def test_cache_rollback_guard_rejects_older_policy(tmp_path):
    """Replay of an older signed policy must not overwrite a newer cache entry."""
    db_path = str(tmp_path / "memory.db")
    init_db(db_path)
    newer = {
        "policy_id": "policy-newer",
        "customer_id": "cust-rb",
        "issued_at": "2026-04-22T12:00:00+00:00",
        "allowed_features": ["instant_briefing"],  # stricter: only 1
        "signature": {"alg": "ed25519", "value": "unused"},
    }
    older = {
        "policy_id": "policy-older",
        "customer_id": "cust-rb",
        "issued_at": "2026-04-20T12:00:00+00:00",  # 2 days earlier
        "allowed_features": [
            "instant_briefing",
            "private_extension_runtime",
            "client_memory_twin",
        ],
        "signature": {"alg": "ed25519", "value": "unused"},
    }

    with _conn_ctx(db_path) as conn:
        wrote_newer = premium_runtime._cache_control_plane_policy(
            conn,
            policy=newer,
            source_ref="test:newer",
            config=premium_runtime._DEFAULT_CONFIG,
        )
        assert wrote_newer is True

        wrote_older = premium_runtime._cache_control_plane_policy(
            conn,
            policy=older,
            source_ref="test:older",
            config=premium_runtime._DEFAULT_CONFIG,
        )
        assert wrote_older is False

        row = conn.execute(
            "SELECT policy_id FROM premium_control_plane_cache "
            "WHERE scope_key = 'customer:cust-rb'"
        ).fetchone()
        assert row["policy_id"] == "policy-newer"


def test_cached_control_policy_tamper_rejected_via_real_signature(
    tmp_path, monkeypatch
):
    """Direct DB tamper of cached policy must be rejected on read (C1)."""
    db_path = str(tmp_path / "memory.db")
    init_db(db_path)
    private_key, public_key_b64 = _ed25519_keypair()

    genuine_policy = _sign_payload(
        {
            "policy_id": "policy-real",
            "customer_id": "cust-tamper",
            "issued_at": "2026-04-22T10:00:00+00:00",
            "cache_ttl_seconds": 3600,
            "allowed_features": ["instant_briefing"],
        },
        private_key,
    )

    import json as _json

    with _conn_ctx(db_path) as conn:
        wrote = premium_runtime._cache_control_plane_policy(
            conn,
            policy=genuine_policy,
            source_ref="test:real",
            config=premium_runtime._DEFAULT_CONFIG,
        )
        assert wrote is True

        # Tamper directly in the DB: flip allowed_features to a permissive set
        tampered = dict(genuine_policy)
        tampered["allowed_features"] = [
            "instant_briefing",
            "private_extension_runtime",
            "client_memory_twin",
        ]
        conn.execute(
            "UPDATE premium_control_plane_cache SET payload_json = ? "
            "WHERE scope_key = 'customer:cust-tamper'",
            (_json.dumps(tampered, ensure_ascii=False, sort_keys=True),),
        )

    monkeypatch.setenv(
        "SQLITE_MEMORY_PREMIUM_POLICY_PUBLIC_KEY",
        public_key_b64,
    )

    with _conn_ctx(db_path) as conn:
        policy = premium_runtime._load_cached_control_plane_policy(
            conn,
            customer_id="cust-tamper",
            config={**premium_runtime._DEFAULT_CONFIG},
        )
    assert policy is None, "tampered cache must not be trusted"


def test_load_remote_json_rejects_plain_http():
    """Plain HTTP URL must fail closed (H2)."""
    import pytest

    with pytest.raises(premium_runtime.PremiumRuntimeError, match="HTTPS"):
        premium_runtime._load_remote_json(
            "http://attacker.example.com/entitlement.json",
            timeout_seconds=5,
        )


def test_no_redirect_handler_refuses_all_3xx_codes():
    """Each HTTP redirect code must raise PremiumRuntimeError — no silent follow."""
    import http.client
    import io
    import pytest
    import urllib.request

    handler = premium_runtime._NoRedirectHandler()
    req = urllib.request.Request("https://pinned.example.com/policy.json")

    for code, method_name in (
        (301, "http_error_301"),
        (302, "http_error_302"),
        (303, "http_error_303"),
        (307, "http_error_307"),
        (308, "http_error_308"),
    ):
        method = getattr(handler, method_name)
        headers = http.client.HTTPMessage()
        headers["Location"] = "https://evil.example.com/pivot"
        with pytest.raises(premium_runtime.PremiumRuntimeError, match="redirect"):
            method(req, io.BytesIO(b""), code, "Found", headers)
