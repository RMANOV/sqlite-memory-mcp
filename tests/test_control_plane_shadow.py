from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from control_plane_shadow import (
    ActionDescriptor,
    JudgeRun,
    MissionContract,
    PolicyRule,
    PolicyTable,
    ShadowState,
    StepProposal,
    advance_state,
    advocate_probes,
    decompose_contract,
    judge_rerun,
    shadow_firewall,
    validate_contract,
)


def _contract() -> MissionContract:
    return MissionContract(
        "mission-1",
        "Inspect the supplied artifact",
        ("read only",),
        ("checksum",),
        frozenset({"inspect"}),
        frozenset({"execute"}),
    )


def test_contract_requires_closed_nonoverlapping_scope():
    empty = MissionContract("", "", (), (), frozenset())
    assert validate_contract(empty).valid is False
    assert "missing_objective" in validate_contract(empty).issues

    overlap = MissionContract(
        "m", "o", ("c",), ("s",), frozenset({"read"}), frozenset({"read"})
    )
    assert "capability_policy_overlap" in validate_contract(overlap).issues


def test_decomposition_validates_capabilities_dependencies_and_cycles():
    steps = (
        StepProposal("read", "read checksum", "inspect"),
        StepProposal("compare", "compare checksum", "inspect", ("read",)),
    )
    assert decompose_contract(_contract(), steps) == steps

    with pytest.raises(ValueError, match="not allowed"):
        decompose_contract(_contract(), (StepProposal("run", "run", "execute"),))
    with pytest.raises(ValueError, match="cycle"):
        decompose_contract(
            _contract(),
            (
                StepProposal("a", "a", "inspect", ("b",)),
                StepProposal("b", "b", "inspect", ("a",)),
            ),
        )


def test_state_machine_has_no_shortcut_to_ready():
    state = ShadowState.DRAFT
    for event in ("validate", "challenge", "judge", "accept"):
        state = advance_state(state, event)
    assert state is ShadowState.READY

    with pytest.raises(ValueError, match="invalid shadow transition"):
        advance_state(ShadowState.DRAFT, "accept")
    with pytest.raises(ValueError, match="invalid shadow transition"):
        advance_state(ShadowState.READY, "validate")


@pytest.mark.parametrize("flag", ["mutates", "network", "accesses_secrets"])
def test_firewall_rejects_all_non_shadow_actions(flag):
    values = {"mutates": False, "network": False, "accesses_secrets": False}
    values[flag] = True
    action = ActionDescriptor("inspect", "artifact", **values)
    table = PolicyTable((PolicyRule("inspect", True),))

    assert shadow_firewall(action, _contract(), table).reason == "shadow_boundary"


def test_firewall_uses_contract_and_policy_default_deny():
    action = ActionDescriptor("inspect", "artifact")
    assert (
        shadow_firewall(
            action, _contract(), PolicyTable((PolicyRule("inspect", True),))
        ).allowed
        is True
    )
    assert shadow_firewall(action, _contract(), PolicyTable(())).allowed is False
    outside = ActionDescriptor("unknown", "artifact")
    assert (
        shadow_firewall(outside, _contract(), PolicyTable(())).reason
        == "outside_contract"
    )


def test_advocate_probes_require_steps_to_map_success_checks():
    findings = advocate_probes(
        _contract(), (StepProposal("read", "inspect bytes", "inspect"),)
    )

    assert any(item.code == "success_checks_not_mapped" for item in findings)
    assert not any(
        item.code == "success_checks_not_mapped"
        for item in advocate_probes(
            _contract(), (StepProposal("read", "inspect checksum", "inspect"),)
        )
    )


def test_judge_requires_independent_reproducible_rerun():
    first = JudgeRun("judge-a", "planner", "input", "output", True)
    rerun = JudgeRun("judge-b", "planner", "input", "output", True)
    assert judge_rerun(first, rerun).accepted is True

    same_judge = JudgeRun("judge-a", "planner", "input", "output", True)
    assert judge_rerun(first, same_judge).reason == "identity_not_independent"
    changed = JudgeRun("judge-b", "planner", "input", "different", True)
    assert judge_rerun(first, changed).reason == "rerun_mismatch"
