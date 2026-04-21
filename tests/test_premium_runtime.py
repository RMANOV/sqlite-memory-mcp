import os
import sqlite3
import sys
from contextlib import contextmanager

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import premium_runtime
from schema import init_db


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
