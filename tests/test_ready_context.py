import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from smart_retrieval import (  # noqa: E402
    READY_CONTEXT_CONTRACT_VERSION,
    build_ready_record,
    prime_context,
    ready_context,
    suggested_ready,
)


def _task(task_id, title, **overrides):
    row = {
        "id": task_id,
        "title": title,
        "description": "",
        "notes": "",
        "status": "not_started",
        "priority": "medium",
        "section": "inbox",
        "due_date": None,
        "project": None,
        "parent_id": None,
        "type": "task",
        "reminder_at": None,
        "created_at": "2026-05-20T10:00:00+00:00",
        "updated_at": "2026-05-20T10:00:00+00:00",
    }
    row.update(overrides)
    return row


def test_ready_record_has_required_contract_fields():
    record = build_ready_record(
        _task(
            "mapping",
            "Mapping Studio installer delivery",
            priority="critical",
            section="today",
            due_date="2026-05-24",
            project="mapping-studio",
        ),
        today=date(2026, 5, 24),
    )

    for key in (
        "id",
        "type",
        "title",
        "status",
        "section",
        "priority",
        "due_date",
        "project",
        "ready_state",
        "blockers",
        "urgency",
        "reason_codes",
        "provenance",
        "next_action",
        "confidence",
        "stale_warning",
    ):
        assert key in record
    assert record["ready_state"] == "ready_now"
    assert record["urgency"] == "high"
    assert "critical_priority" in record["reason_codes"]
    assert "due_or_overdue" in record["reason_codes"]
    assert record["provenance"]["rule_version"] == READY_CONTEXT_CONTRACT_VERSION


def test_suggested_ready_excludes_closed_max_style_rows():
    rows = [
        _task(
            "max",
            "Max safety protocol",
            status="done",
            priority="critical",
            notes="closed unless new symptoms or parent conversation",
        ),
        _task("redis", "Redis follow-up", priority="critical", section="today"),
    ]

    suggested = suggested_ready(rows, today=date(2026, 5, 24))

    assert [row["id"] for row in suggested] == ["redis"]


def test_under_specified_acceptance_rule_stays_visible_as_blocked():
    record = build_ready_record(
        _task(
            "smart-tab",
            "Smart-tab threshold task",
            priority="critical",
            description="acceptance rule is under-specified",
        ),
        today=date(2026, 5, 24),
    )

    assert record["ready_state"] == "blocked"
    assert {"category": "needs_user_decision", "detail": "matched task text"} in record[
        "blockers"
    ]
    assert "blocked_by_open_item" in record["reason_codes"]
    assert "Resolve blocker" in record["next_action"]


def test_bridge_updated_at_churn_does_not_become_clean_recency_signal():
    record = build_ready_record(
        _task(
            "bridge",
            "Bridge sync status repair",
            priority="high",
            notes="bridge sync updated_at churn after import wave",
        ),
        today=date(2026, 5, 24),
    )

    assert "bridge_sync_caution" in record["reason_codes"]
    assert record["stale_warning"]
    assert record["confidence"] == "medium"


def test_readings_do_not_flood_default_suggested_ready():
    rows = [
        _task(
            "reading",
            "Critical reading about Beads",
            type="note",
            priority="critical",
            project="critical-readings",
        ),
        _task("work", "Concrete work", priority="high"),
    ]

    default_ids = [row["id"] for row in suggested_ready(rows, today=date(2026, 5, 24))]
    reading_ids = [
        row["id"]
        for row in suggested_ready(
            rows,
            include_readings=True,
            today=date(2026, 5, 24),
        )
    ]

    assert default_ids == ["work"]
    assert "reading" in reading_ids


def test_prime_context_uses_same_ready_records():
    rows = [
        _task("ready", "Ready work", priority="critical", section="today"),
        _task("blocked", "Blocked work", notes="blocked by user decision"),
        _task("closed", "Closed work", status="done"),
    ]

    pack = prime_context(rows, today=date(2026, 5, 24))
    ready_ids = {record["id"] for record in ready_context(rows, today=date(2026, 5, 24))}

    assert pack["contract_version"] == READY_CONTEXT_CONTRACT_VERSION
    assert {record["id"] for record in pack["top_ready_items"]}.issubset(ready_ids)
    assert pack["blocked_or_waiting"][0]["id"] == "blocked"
    assert pack["explicit_exclusions"][0]["id"] == "closed"
