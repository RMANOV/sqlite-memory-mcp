"""Optional premium task tray loader for the private runtime."""

from __future__ import annotations

import os
from typing import Any

from premium_contract import build_mount_context
from premium_runtime import (
    MACHINE_ID,
    _entrypoint_fingerprint,
    _get_conn,
    _resolve_module_from_entrypoint,
    _write_gate_audit,
    build_mount_runtime_config,
    evaluate_feature_gate,
    load_premium_config,
    logger,
)


def maybe_load_task_tray_extension(*, server_name: str) -> Any | None:
    """Load an optional premium task-tray extension behind the entitlement gate."""
    config = load_premium_config()
    if not config.get("enabled", True):
        return None
    if not config.get("mount_private_extensions", True):
        return None

    entry_env = str(config.get("private_entrypoint_env_var") or "").strip()
    entrypoint = ""
    if entry_env:
        entrypoint = os.environ.get(entry_env, "").strip()
    if not entrypoint:
        return None

    payload = {
        "server_name": server_name,
        "entrypoint_ref": _entrypoint_fingerprint(entrypoint),
        "surface": "task_tray",
    }
    with _get_conn() as conn:
        verdict = evaluate_feature_gate(
            conn,
            feature_id="custom_design_tab",
            server_name=server_name,
            tool_name=f"{server_name}.premium_task_tray",
            actor_id=server_name,
            payload=payload,
        )
        if not verdict.get("allowed"):
            return None

    try:
        module, _attr_name = _resolve_module_from_entrypoint(entrypoint)
        builder = getattr(module, "build_task_tray_extension", None)
        if builder is None or not callable(builder):
            return None
        extension = builder(
            server_name=server_name,
            mount_context=build_mount_context(
                server_name=server_name,
                feature_id="custom_design_tab",
                machine_id=MACHINE_ID,
                config=build_mount_runtime_config(
                    base_config=config,
                    selection={
                        key: value
                        for key, value in verdict.items()
                        if key
                        in {"selection_mode", "selected_packs", "effective_features"}
                    },
                ),
            ),
        )
        with _get_conn() as conn:
            _write_gate_audit(
                conn,
                feature_id="custom_design_tab",
                decision="ui_load_success",
                reason="premium_task_tray_extension_loaded",
                server_name=server_name,
                tool_name=f"{server_name}.premium_task_tray",
                actor_id=server_name,
                payload=payload,
            )
        return extension
    except Exception as exc:
        logger.warning(
            "Premium task tray extension failed for %s: %s",
            server_name,
            exc,
            exc_info=True,
        )
        with _get_conn() as conn:
            _write_gate_audit(
                conn,
                feature_id="custom_design_tab",
                decision="ui_load_failed",
                reason=f"premium_task_tray_extension_failed:{exc.__class__.__name__}",
                server_name=server_name,
                tool_name=f"{server_name}.premium_task_tray",
                actor_id=server_name,
                payload=payload,
            )
        return None
