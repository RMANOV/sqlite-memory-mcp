"""Thin compatibility layer over the public sqlite-memory host runtime."""

from __future__ import annotations

from importlib import import_module
from typing import Any


def _load_required_module(name: str) -> Any:
    try:
        return import_module(name)
    except ImportError as exc:
        raise RuntimeError(
            f"{name} is not importable. Run this package through the "
            "sqlite-memory-mcp host runtime or add the host repo/package to "
            "PYTHONPATH."
        ) from exc


_host_db_utils = _load_required_module("db_utils")
_host_premium_runtime = _load_required_module("premium_runtime")
_host_contract = _load_required_module("premium_contract")

get_conn = _host_db_utils.get_conn
now_iso = _host_db_utils.now_iso
record_memory_event = _host_db_utils.record_memory_event
setup_logger = _host_db_utils.setup_logger
MACHINE_ID = _host_db_utils.MACHINE_ID

evaluate_feature_gate = _host_premium_runtime.evaluate_feature_gate

PREMIUM_RUNTIME_CONTRACT_VERSION = _host_contract.PREMIUM_RUNTIME_CONTRACT_VERSION
PremiumMountContext = _host_contract.PremiumMountContext
PremiumRegistrationResult = _host_contract.PremiumRegistrationResult
