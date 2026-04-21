"""Placeholder premium communication-memory tools for the public template."""

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
def premium_register_mailbox(
    mailbox_key: str,
    owner_label: str,
    mailbox_type: str = "email",
    project: str = "",
    client_scope: str = "",
    config_json: str = "{}",
) -> str:
    """Template placeholder for premium mailbox registration."""
    verdict = require_feature(
        "multi_mailbox_ingestion",
        tool_name="sqlite-premium-template.premium_register_mailbox",
        payload={"mailbox_key": mailbox_key, "mailbox_type": mailbox_type},
    )
    if not verdict.get("allowed"):
        return denied_response("multi_mailbox_ingestion", verdict)
    with host_api.get_conn() as conn:
        ensure_private_schema(conn)
    return _template_response("multi_mailbox_ingestion", "premium_register_mailbox")


@premium_mcp.tool()
def premium_ingest_message(
    mailbox_key: str,
    external_thread_ref: str,
    external_message_ref: str,
    subject: str,
    body_text: str,
    sender: str,
    recipients_json: str = "[]",
    direction: str = "inbound",
    client_ref: str = "",
    sent_at: str = "",
    tags_json: str = "[]",
    metadata_json: str = "{}",
    ingest_source: str = "manual",
) -> str:
    """Template placeholder for premium communication ingestion."""
    verdict = require_feature(
        "multi_mailbox_ingestion",
        tool_name="sqlite-premium-template.premium_ingest_message",
        payload={"mailbox_key": mailbox_key, "direction": direction},
    )
    if not verdict.get("allowed"):
        return denied_response("multi_mailbox_ingestion", verdict)
    with host_api.get_conn() as conn:
        ensure_private_schema(conn)
    return _template_response("multi_mailbox_ingestion", "premium_ingest_message")


@premium_mcp.tool()
def premium_thread_digest(
    mailbox_key: str = "",
    client_ref: str = "",
    limit: int = 10,
) -> str:
    """Template placeholder for premium thread digests."""
    verdict = require_feature(
        "multi_mailbox_ingestion",
        tool_name="sqlite-premium-template.premium_thread_digest",
        payload={"mailbox_key": mailbox_key or None, "client_ref": client_ref or None},
    )
    if not verdict.get("allowed"):
        return denied_response("multi_mailbox_ingestion", verdict)
    with host_api.get_conn() as conn:
        ensure_private_schema(conn)
    return _template_response("multi_mailbox_ingestion", "premium_thread_digest")


@premium_mcp.tool()
def premium_followup_queue(
    days_stale: int = 3,
    mailbox_key: str = "",
    client_ref: str = "",
    limit: int = 20,
) -> str:
    """Template placeholder for premium follow-up queues."""
    verdict = require_feature(
        "multi_mailbox_ingestion",
        tool_name="sqlite-premium-template.premium_followup_queue",
        payload={"days_stale": days_stale, "mailbox_key": mailbox_key or None},
    )
    if not verdict.get("allowed"):
        return denied_response("multi_mailbox_ingestion", verdict)
    with host_api.get_conn() as conn:
        ensure_private_schema(conn)
    return _template_response("multi_mailbox_ingestion", "premium_followup_queue")
