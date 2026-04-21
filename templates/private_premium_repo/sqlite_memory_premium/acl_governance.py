"""Placeholder premium ACL and governance tools for the public template."""

from __future__ import annotations

import json

from . import host_api
from .app import premium_mcp
from .runtime_state import denied_response, require_feature
from .schema import ensure_private_schema


def _template_response(feature_id: str, tool_name: str) -> str:
    return json.dumps(
        {
            "status": "template_only",
            "feature_id": feature_id,
            "tool_name": tool_name,
            "note": (
                "Replace this placeholder with the real private premium implementation."
            ),
        },
        ensure_ascii=False,
        sort_keys=True,
    )


@premium_mcp.tool()
def premium_acl_upsert_role(
    role_name: str,
    permissions_json: str,
    description: str = "",
    scope_kind: str = "global",
    scope_ref: str = "",
) -> str:
    """Template placeholder for premium RBAC role definition."""
    verdict = require_feature(
        "acl_rbac",
        tool_name="sqlite-premium-template.premium_acl_upsert_role",
        payload={"role_name": role_name, "scope_kind": scope_kind},
    )
    if not verdict.get("allowed"):
        return denied_response("acl_rbac", verdict)
    with host_api.get_conn() as conn:
        ensure_private_schema(conn)
    return _template_response("acl_rbac", "premium_acl_upsert_role")


@premium_mcp.tool()
def premium_acl_assign_role(
    principal_type: str,
    principal_id: str,
    role_name: str,
    scope_kind: str = "global",
    scope_ref: str = "",
    granted_by: str = "system",
    expires_at: str = "",
) -> str:
    """Template placeholder for premium RBAC role assignment."""
    verdict = require_feature(
        "acl_rbac",
        tool_name="sqlite-premium-template.premium_acl_assign_role",
        payload={"role_name": role_name, "principal_type": principal_type},
    )
    if not verdict.get("allowed"):
        return denied_response("acl_rbac", verdict)
    with host_api.get_conn() as conn:
        ensure_private_schema(conn)
    return _template_response("acl_rbac", "premium_acl_assign_role")


@premium_mcp.tool()
def premium_acl_check_access(
    principal_type: str,
    principal_id: str,
    permission: str,
    scope_kind: str = "global",
    scope_ref: str = "",
) -> str:
    """Template placeholder for premium RBAC access evaluation."""
    verdict = require_feature(
        "acl_rbac",
        tool_name="sqlite-premium-template.premium_acl_check_access",
        payload={"principal_type": principal_type, "permission": permission},
    )
    if not verdict.get("allowed"):
        return denied_response("acl_rbac", verdict)
    with host_api.get_conn() as conn:
        ensure_private_schema(conn)
    return _template_response("acl_rbac", "premium_acl_check_access")


@premium_mcp.tool()
def premium_governance_record_decision(
    title: str,
    decision: str,
    subject_kind: str,
    subject_id: str = "",
    rationale: str = "",
    evidence_json: str = "[]",
    risk_level: str = "medium",
    scope_kind: str = "global",
    scope_ref: str = "",
    created_by: str = "system",
) -> str:
    """Template placeholder for premium governance decision recording."""
    verdict = require_feature(
        "governance_audit",
        tool_name="sqlite-premium-template.premium_governance_record_decision",
        payload={"title": title, "subject_kind": subject_kind},
    )
    if not verdict.get("allowed"):
        return denied_response("governance_audit", verdict)
    with host_api.get_conn() as conn:
        ensure_private_schema(conn)
    return _template_response(
        "governance_audit",
        "premium_governance_record_decision",
    )


@premium_mcp.tool()
def premium_governance_audit_digest(
    limit: int = 10,
    risk_level: str = "",
    scope_kind: str = "",
    scope_ref: str = "",
) -> str:
    """Template placeholder for premium governance audit summaries."""
    verdict = require_feature(
        "governance_audit",
        tool_name="sqlite-premium-template.premium_governance_audit_digest",
        payload={"limit": limit, "risk_level": risk_level or None},
    )
    if not verdict.get("allowed"):
        return denied_response("governance_audit", verdict)
    with host_api.get_conn() as conn:
        ensure_private_schema(conn)
    return _template_response("governance_audit", "premium_governance_audit_digest")
