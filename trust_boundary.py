"""Fail-closed decisions for moving verified claims across a trust boundary."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from lazy_verification import independent_origin_count


def _valid_origins(origins: object) -> bool:
    if not isinstance(origins, (set, frozenset)):
        return False
    return bool(origins) and all(
        isinstance(origin, str) and bool(origin.strip()) for origin in origins
    )


@dataclass(frozen=True)
class BoundaryPolicy:
    minimum_sample: int = 5
    required_independent_attestations: int = 2
    high_stakes_independent_attestations: int = 3
    high_stakes_requires_authorization: bool = True

    def __post_init__(self) -> None:
        if self.minimum_sample < 1:
            raise ValueError("minimum_sample must be positive")
        if self.required_independent_attestations < 1:
            raise ValueError("required_independent_attestations must be positive")
        if (
            self.high_stakes_independent_attestations
            < self.required_independent_attestations
        ):
            raise ValueError(
                "high-stakes threshold cannot be weaker than the normal threshold"
            )


@dataclass(frozen=True)
class Attestation:
    attestation_id: str
    origins: frozenset[str]
    generator_id: str
    verifier_id: str
    verifier_origins: frozenset[str]
    verified: bool

    def __post_init__(self) -> None:
        if not self.attestation_id.strip() or not _valid_origins(self.origins):
            raise ValueError("attestation identity and origins are required")
        if not self.generator_id.strip() or not self.verifier_id.strip():
            raise ValueError("generator and verifier identities are required")
        if not _valid_origins(self.verifier_origins):
            raise ValueError("verifier origins are required")


@dataclass(frozen=True)
class DecouplingAudit:
    independent_actor: bool
    independent_origin: bool

    @property
    def passed(self) -> bool:
        return self.independent_actor and self.independent_origin


def audit_decoupling(attestation: Attestation) -> DecouplingAudit:
    return DecouplingAudit(
        independent_actor=attestation.generator_id != attestation.verifier_id,
        independent_origin=not bool(attestation.origins & attestation.verifier_origins),
    )


@dataclass(frozen=True)
class BoundaryDecision:
    outward: bool
    report_only: bool
    reason: str
    independent_attestations: int
    accepted_attestations: tuple[str, ...] = ()


def evaluate_boundary(
    attestations: Iterable[Attestation],
    *,
    sample_size: int,
    high_stakes: bool = False,
    high_stakes_authorized: bool = False,
    policy: BoundaryPolicy | None = None,
) -> BoundaryDecision:
    """Return an outward permission bit; all incomplete states stay inside."""
    cfg = policy or BoundaryPolicy()
    if sample_size < 0:
        raise ValueError("sample_size cannot be negative")
    if sample_size < cfg.minimum_sample:
        return BoundaryDecision(False, True, "insufficient_sample", 0)
    if (
        high_stakes
        and cfg.high_stakes_requires_authorization
        and not high_stakes_authorized
    ):
        return BoundaryDecision(False, True, "high_stakes_hold", 0)

    rows = list(attestations)
    # Keep the decision boundary fail-closed even for attestations restored
    # from an older serializer that may have bypassed today's constructor.
    if any(
        not _valid_origins(item.origins)
        or not _valid_origins(item.verifier_origins)
        for item in rows
    ):
        return BoundaryDecision(False, True, "invalid_attestation_origins", 0)

    accepted = [
        item for item in rows if item.verified and audit_decoupling(item).passed
    ]
    independent = (
        independent_origin_count([item.origins for item in accepted]) if accepted else 0
    )
    required = (
        cfg.high_stakes_independent_attestations
        if high_stakes
        else cfg.required_independent_attestations
    )
    if independent < required:
        return BoundaryDecision(
            False,
            True,
            "insufficient_independence",
            independent,
            tuple(item.attestation_id for item in accepted),
        )
    return BoundaryDecision(
        True,
        False,
        "verified",
        independent,
        tuple(item.attestation_id for item in accepted),
    )
