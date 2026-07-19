from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from trust_boundary import (
    Attestation,
    BoundaryPolicy,
    audit_decoupling,
    evaluate_boundary,
)


def _attestation(
    name: str,
    origin: str,
    *,
    generator: str = "generator",
    verifier: str = "verifier",
    verifier_origin: str | None = None,
    verified: bool = True,
) -> Attestation:
    return Attestation(
        name,
        frozenset({origin}),
        generator,
        verifier,
        frozenset({verifier_origin or f"audit-{origin}"}),
        verified,
    )


def test_decoupling_requires_distinct_actor_and_origin():
    assert audit_decoupling(_attestation("a", "oa")).passed is True
    assert (
        audit_decoupling(_attestation("a", "oa", verifier="generator")).passed is False
    )
    assert (
        audit_decoupling(_attestation("a", "oa", verifier_origin="oa")).passed is False
    )


def test_small_sample_is_report_only_even_with_attestations():
    decision = evaluate_boundary(
        [_attestation("a", "oa"), _attestation("b", "ob")],
        sample_size=2,
    )

    assert decision.outward is False
    assert decision.report_only is True
    assert decision.reason == "insufficient_sample"


def test_independent_verified_attestations_can_cross_boundary():
    decision = evaluate_boundary(
        [_attestation("a", "oa"), _attestation("b", "ob")],
        sample_size=10,
    )

    assert decision.outward is True
    assert decision.report_only is False
    assert decision.independent_attestations == 2


def test_overlap_unverified_and_coupled_evidence_do_not_count():
    decision = evaluate_boundary(
        [
            _attestation("a", "shared"),
            _attestation("b", "shared"),
            _attestation("c", "oc", verified=False),
            _attestation("d", "od", verifier="generator"),
        ],
        sample_size=10,
    )

    assert decision.outward is False
    assert decision.independent_attestations == 1


def test_high_stakes_override_holds_without_authorization():
    rows = [
        _attestation("a", "oa"),
        _attestation("b", "ob"),
        _attestation("c", "oc"),
    ]

    held = evaluate_boundary(rows, sample_size=10, high_stakes=True)
    allowed = evaluate_boundary(
        rows,
        sample_size=10,
        high_stakes=True,
        high_stakes_authorized=True,
    )

    assert held.reason == "high_stakes_hold"
    assert held.outward is False
    assert allowed.outward is True


def test_high_stakes_uses_stricter_independence_threshold():
    policy = BoundaryPolicy(
        minimum_sample=1,
        required_independent_attestations=1,
        high_stakes_independent_attestations=3,
        high_stakes_requires_authorization=False,
    )
    decision = evaluate_boundary(
        [_attestation("a", "oa"), _attestation("b", "ob")],
        sample_size=2,
        high_stakes=True,
        policy=policy,
    )

    assert decision.outward is False
    assert decision.reason == "insufficient_independence"


@pytest.mark.parametrize("field", ["origins", "verifier_origins"])
def test_attestation_rejects_blank_origin_values(field):
    values = {
        "attestation_id": "a",
        "origins": frozenset({"origin"}),
        "generator_id": "generator",
        "verifier_id": "verifier",
        "verifier_origins": frozenset({"audit-origin"}),
        "verified": True,
    }
    values[field] = frozenset({"  "})

    with pytest.raises(ValueError, match="origins"):
        Attestation(**values)


def test_restored_malformed_attestation_fails_closed_at_boundary():
    malformed = object.__new__(Attestation)
    object.__setattr__(malformed, "attestation_id", "legacy")
    object.__setattr__(malformed, "origins", frozenset({""}))
    object.__setattr__(malformed, "generator_id", "generator")
    object.__setattr__(malformed, "verifier_id", "verifier")
    object.__setattr__(malformed, "verifier_origins", frozenset({"audit"}))
    object.__setattr__(malformed, "verified", True)

    decision = evaluate_boundary([malformed], sample_size=10)

    assert decision.outward is False
    assert decision.report_only is True
    assert decision.reason == "invalid_attestation_origins"
    assert decision.independent_attestations == 0
