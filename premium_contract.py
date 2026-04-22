"""Public contract for separate premium extension repositories.

This module is safe to publish. It defines the stable registration surface
that a private premium repo can target without living inside the OSS core.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, TypedDict

PREMIUM_RUNTIME_CONTRACT_VERSION = "1.1"


@dataclass(slots=True)
class PremiumMountContext:
    """Context passed from OSS core into a private premium extension."""

    contract_version: str
    server_name: str
    feature_id: str
    machine_id: str
    host_runtime_version: str = ""
    installation_fingerprint: str = ""
    manifest_id: str = ""
    protection_phase: int = 1
    config: dict[str, Any] = field(default_factory=dict)


class PremiumRegistrationResult(TypedDict, total=False):
    """Structured result returned by premium registration hooks."""

    mounted: bool
    contract_version: str
    extension_name: str
    host_runtime_version: str
    installation_fingerprint: str
    manifest_id: str
    protection_phase: int
    packs: list[str]
    features: list[str]
    selection_mode: str
    notes: str


class PremiumExtensionRegistrar(Protocol):
    """Callable signature for a private premium registration hook."""

    def __call__(
        self,
        mcp: Any,
        *,
        server_name: str | None = None,
        mount_context: PremiumMountContext | None = None,
    ) -> PremiumRegistrationResult | dict[str, Any] | None: ...


def build_mount_context(
    *,
    server_name: str,
    feature_id: str,
    machine_id: str,
    host_runtime_version: str = "",
    installation_fingerprint: str = "",
    manifest_id: str = "",
    protection_phase: int = 1,
    config: dict[str, Any] | None = None,
) -> PremiumMountContext:
    """Build a standard context object for private premium extensions."""
    return PremiumMountContext(
        contract_version=PREMIUM_RUNTIME_CONTRACT_VERSION,
        server_name=server_name,
        feature_id=feature_id,
        machine_id=machine_id,
        host_runtime_version=host_runtime_version,
        installation_fingerprint=installation_fingerprint,
        manifest_id=manifest_id,
        protection_phase=protection_phase,
        config=dict(config or {}),
    )
