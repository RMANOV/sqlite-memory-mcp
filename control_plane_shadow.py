"""Read-only control-plane modeling primitives.

This is a shadow evaluator: it validates and judges descriptors but contains no
credential, token, command, network, database, or execution integration.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Mapping


@dataclass(frozen=True)
class MissionContract:
    mission_id: str
    objective: str
    constraints: tuple[str, ...]
    success_checks: tuple[str, ...]
    allowed_capabilities: frozenset[str]
    prohibited_capabilities: frozenset[str] = frozenset()


@dataclass(frozen=True)
class ContractValidation:
    valid: bool
    issues: tuple[str, ...]


def validate_contract(contract: MissionContract) -> ContractValidation:
    issues: list[str] = []
    if not contract.mission_id.strip():
        issues.append("missing_mission_id")
    if not contract.objective.strip():
        issues.append("missing_objective")
    if not contract.constraints:
        issues.append("missing_constraints")
    if not contract.success_checks:
        issues.append("missing_success_checks")
    if not contract.allowed_capabilities:
        issues.append("missing_allowed_capabilities")
    if contract.allowed_capabilities & contract.prohibited_capabilities:
        issues.append("capability_policy_overlap")
    if any(not value.strip() for value in contract.constraints):
        issues.append("empty_constraint")
    if any(not value.strip() for value in contract.success_checks):
        issues.append("empty_success_check")
    return ContractValidation(not issues, tuple(issues))


@dataclass(frozen=True)
class StepProposal:
    step_id: str
    description: str
    capability: str
    depends_on: tuple[str, ...] = ()


def decompose_contract(
    contract: MissionContract, proposals: Iterable[StepProposal]
) -> tuple[StepProposal, ...]:
    """Validate a proposed decomposition; never schedule or execute it."""
    validation = validate_contract(contract)
    if not validation.valid:
        raise ValueError(f"invalid mission contract: {','.join(validation.issues)}")
    rows = tuple(proposals)
    identifiers = [row.step_id for row in rows]
    if any(not value.strip() for value in identifiers) or len(set(identifiers)) != len(
        identifiers
    ):
        raise ValueError("step identifiers must be unique and non-empty")
    known = set(identifiers)
    graph: dict[str, tuple[str, ...]] = {}
    for row in rows:
        if not row.description.strip():
            raise ValueError("step description is required")
        if row.capability not in contract.allowed_capabilities:
            raise ValueError(f"capability is not allowed: {row.capability}")
        if any(dependency not in known for dependency in row.depends_on):
            raise ValueError(f"unknown dependency for step: {row.step_id}")
        graph[row.step_id] = row.depends_on

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(step_id: str) -> None:
        if step_id in visiting:
            raise ValueError("step dependency cycle")
        if step_id in visited:
            return
        visiting.add(step_id)
        for dependency in graph[step_id]:
            visit(dependency)
        visiting.remove(step_id)
        visited.add(step_id)

    for identifier in identifiers:
        visit(identifier)
    return rows


class ShadowState(str, Enum):
    DRAFT = "draft"
    VALIDATED = "validated"
    CHALLENGED = "challenged"
    JUDGED = "judged"
    READY = "ready"
    REFUSED = "refused"


_TRANSITIONS: Mapping[tuple[ShadowState, str], ShadowState] = {
    (ShadowState.DRAFT, "validate"): ShadowState.VALIDATED,
    (ShadowState.VALIDATED, "challenge"): ShadowState.CHALLENGED,
    (ShadowState.CHALLENGED, "judge"): ShadowState.JUDGED,
    (ShadowState.JUDGED, "accept"): ShadowState.READY,
    (ShadowState.DRAFT, "refuse"): ShadowState.REFUSED,
    (ShadowState.VALIDATED, "refuse"): ShadowState.REFUSED,
    (ShadowState.CHALLENGED, "refuse"): ShadowState.REFUSED,
    (ShadowState.JUDGED, "refuse"): ShadowState.REFUSED,
}


def advance_state(state: ShadowState, event: str) -> ShadowState:
    try:
        return _TRANSITIONS[(state, event)]
    except KeyError as exc:
        raise ValueError(f"invalid shadow transition: {state.value}/{event}") from exc


@dataclass(frozen=True)
class PolicyRule:
    capability: str
    allowed: bool
    requires_review: bool = True


class PolicyTable:
    """Immutable-by-convention allow-list with default deny."""

    def __init__(self, rules: Iterable[PolicyRule]) -> None:
        self._rules: dict[str, PolicyRule] = {}
        for rule in rules:
            if not rule.capability.strip() or rule.capability in self._rules:
                raise ValueError("policy capabilities must be unique and non-empty")
            self._rules[rule.capability] = rule

    def rule_for(self, capability: str) -> PolicyRule:
        return self._rules.get(capability, PolicyRule(capability, False, True))


@dataclass(frozen=True)
class ActionDescriptor:
    capability: str
    target: str
    mutates: bool = False
    network: bool = False
    accesses_secrets: bool = False


@dataclass(frozen=True)
class FirewallDecision:
    allowed: bool
    reason: str
    requires_review: bool = True


def shadow_firewall(
    action: ActionDescriptor,
    contract: MissionContract,
    policies: PolicyTable,
) -> FirewallDecision:
    """Permit only read-only descriptors explicitly allowed by both policies."""
    if action.mutates or action.network or action.accesses_secrets:
        return FirewallDecision(False, "shadow_boundary")
    if action.capability not in contract.allowed_capabilities:
        return FirewallDecision(False, "outside_contract")
    rule = policies.rule_for(action.capability)
    if not rule.allowed:
        return FirewallDecision(False, "policy_denied", rule.requires_review)
    return FirewallDecision(True, "read_only", rule.requires_review)


@dataclass(frozen=True)
class ProbeFinding:
    code: str
    severity: str


def advocate_probes(
    contract: MissionContract, steps: Iterable[StepProposal]
) -> tuple[ProbeFinding, ...]:
    """Return structural objections for a separate judge to consider."""
    findings: list[ProbeFinding] = []
    validation = validate_contract(contract)
    findings.extend(ProbeFinding(issue, "blocker") for issue in validation.issues)
    rows = tuple(steps)
    used_capabilities = {row.capability for row in rows}
    if not rows:
        findings.append(ProbeFinding("empty_decomposition", "blocker"))
    if contract.allowed_capabilities - used_capabilities:
        findings.append(ProbeFinding("unused_capability_scope", "warning"))
    described_checks = " ".join(row.description.casefold() for row in rows)
    if contract.success_checks and not any(
        check.casefold() in described_checks for check in contract.success_checks
    ):
        findings.append(ProbeFinding("success_checks_not_mapped", "blocker"))
    return tuple(findings)


@dataclass(frozen=True)
class JudgeRun:
    judge_id: str
    planner_id: str
    input_digest: str
    output_digest: str
    checks_passed: bool


@dataclass(frozen=True)
class JudgeDecision:
    accepted: bool
    reason: str


def judge_rerun(first: JudgeRun, rerun: JudgeRun) -> JudgeDecision:
    """Require independent judges and an exact deterministic rerun."""
    identity_values = (
        first.judge_id,
        rerun.judge_id,
        first.planner_id,
        rerun.planner_id,
    )
    if any(not value.strip() for value in identity_values):
        return JudgeDecision(False, "identity_not_independent")
    if not first.input_digest or not first.output_digest or not rerun.input_digest:
        return JudgeDecision(False, "incomplete_rerun")
    if (
        first.planner_id != rerun.planner_id
        or first.judge_id == rerun.judge_id
        or first.judge_id == first.planner_id
        or rerun.judge_id == first.planner_id
    ):
        return JudgeDecision(False, "identity_not_independent")
    if not first.checks_passed or not rerun.checks_passed:
        return JudgeDecision(False, "checks_failed")
    if first.input_digest != rerun.input_digest:
        return JudgeDecision(False, "input_mismatch")
    if first.output_digest != rerun.output_digest:
        return JudgeDecision(False, "rerun_mismatch")
    return JudgeDecision(True, "reproduced")
