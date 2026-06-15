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


def test_suggested_ready_keeps_provenance_metadata():
    rows = [_task("work", "Concrete work", priority="high")]

    suggested = suggested_ready(rows, today=date(2026, 5, 24))

    assert suggested[0]["_ready_provenance"]["source_id"] == "work"
    assert (
        suggested[0]["_ready_provenance"]["rule_version"]
        == READY_CONTEXT_CONTRACT_VERSION
    )


def test_closed_reopen_confusion_can_surface_as_cleanup_candidate():
    rows = [
        _task(
            "closed-confused",
            "Closed task",
            status="done",
            notes="reopen_requested_by_user after sync confusion",
        )
    ]

    record = ready_context(rows, today=date(2026, 5, 24))[0]
    suggested = suggested_ready(rows, today=date(2026, 5, 24))

    assert record["ready_state"] == "cleanup_candidate"
    assert suggested[0]["_ready_state"] == "cleanup_candidate"


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


def test_due_today_reading_tasks_stay_out_of_default_suggested_ready():
    rows = [
        _task(
            "reading-due",
            "OODA reading about agent memory",
            priority="critical",
            section="today",
            due_date="2026-05-24",
            project="readings",
        ),
        _task("work", "Concrete work", priority="high"),
    ]

    default_ids = [row["id"] for row in suggested_ready(rows, today=date(2026, 5, 24))]
    reading_ids = [
        row["id"]
        for row in suggested_ready(rows, include_readings=True, today=date(2026, 5, 24))
    ]

    assert default_ids == ["work"]
    assert "reading-due" in reading_ids


def test_explicitly_surfaced_reading_can_enter_default_suggested_ready():
    rows = [
        _task(
            "reading-surfaced",
            "Critical reading surface_until=2026-05-24",
            priority="critical",
            section="today",
            project="readings",
        ),
        _task("work", "Concrete work", priority="high"),
    ]

    default_ids = [row["id"] for row in suggested_ready(rows, today=date(2026, 5, 24))]

    assert "reading-surfaced" in default_ids


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


# ── B3 (contract v1) additive-field + cold-start coverage ──────────────────


def test_contract_version_is_v1():
    # Change 5: explicit, backcompat-safe v0 -> v1 bump is pinned by the suite.
    assert READY_CONTEXT_CONTRACT_VERSION == "ready_context.v1"


def test_ready_record_carries_additive_audit_fields():
    # Change 1 + 2: today_used echoes the injected date; reason_primary is the
    # deterministic winner derived from the existing reason_codes order.
    fixed = date(2026, 5, 24)
    record = build_ready_record(
        _task(
            "mapping",
            "Mapping Studio installer delivery",
            priority="critical",
            section="today",
            due_date="2026-05-24",
            project="mapping-studio",
        ),
        today=fixed,
    )

    assert record["today_used"] == "2026-05-24"
    assert record["reason_primary"] in record["reason_codes"]
    # section=today => explicit_user_correction is the highest-precedence code.
    assert record["reason_primary"] == "explicit_user_correction"
    assert record["provenance"]["rule_version"] == "ready_context.v1"


def test_reason_primary_is_deterministic_across_calls():
    # Determinism proof: identical input -> identical winning reason every time.
    # The bridge/sync text triggers both machine_anomaly_open (rank 8) and
    # bridge_sync_caution (rank 10); the higher-precedence code wins, every run.
    row = _task(
        "bridge",
        "Bridge sync status repair",
        priority="high",
        notes="bridge sync updated_at churn after import wave",
    )
    record = build_ready_record(row, today=date(2026, 5, 24))
    assert "machine_anomaly_open" in record["reason_codes"]
    assert "bridge_sync_caution" in record["reason_codes"]
    primaries = {
        build_ready_record(row, today=date(2026, 5, 24))["reason_primary"]
        for _ in range(5)
    }
    assert primaries == {"machine_anomaly_open"}


def test_reason_primary_prefers_blocker_over_lower_precedence_codes():
    # A critical+blocked task surfaces the blocker as the primary reason, not
    # the lower-precedence critical_priority code.
    record = build_ready_record(
        _task(
            "smart-tab",
            "Smart-tab threshold task",
            priority="critical",
            description="acceptance rule is under-specified",
        ),
        today=date(2026, 5, 24),
    )

    assert "blocked_by_open_item" in record["reason_codes"]
    assert "critical_priority" in record["reason_codes"]
    assert record["reason_primary"] == "blocked_by_open_item"


def test_sort_position_is_assigned_after_final_sort():
    # Change 2: sort_position is a 0-based rank stamped after the sort, and it
    # matches the deterministic order of the returned records.
    rows = [
        _task("blocked", "Blocked work", notes="blocked by user decision"),
        _task("ready", "Ready work", priority="critical", section="today"),
    ]

    records = ready_context(rows, today=date(2026, 5, 24))

    assert [r["sort_position"] for r in records] == list(range(len(records)))
    # ready_now sorts ahead of blocked, so 'ready' gets position 0.
    assert records[0]["id"] == "ready"
    assert records[0]["sort_position"] == 0


def test_cold_start_ready_context_is_empty_shape():
    # Change 4: ready_context([]) returns a {count:0, items:[]}-shaped result.
    records = ready_context([], today=date(2026, 5, 24))

    assert records == []
    assert len(records) == 0


def test_cold_start_prime_returns_mandate_with_empty_items():
    # Change 3 + 4: prime-on-empty yields the mandate/guidance with empty item
    # lists and fabricates nothing. today is injected for determinism.
    pack = prime_context([], today=date(2026, 5, 24))

    assert pack["contract_version"] == "ready_context.v1"
    assert pack["mandate"]
    assert pack["guidance"]
    assert pack["current_mandate"] == pack["mandate"]
    assert pack["today_used"] == "2026-05-24"
    assert pack["items_empty"] is True
    for key in (
        "top_ready_items",
        "blocked_or_waiting",
        "cleanup_candidates",
        "explicit_exclusions",
        "risk_or_escalation_items",
        "evidence_refs",
    ):
        assert pack[key] == []


def test_suggested_ready_exposes_additive_fields():
    # The suggested-tab projection mirrors the new audit fields under the
    # established _ready_ prefix without dropping prior metadata.
    rows = [_task("work", "Concrete work", priority="high")]

    suggested = suggested_ready(rows, today=date(2026, 5, 24))

    assert suggested[0]["_ready_today_used"] == "2026-05-24"
    assert suggested[0]["_ready_reason_primary"]
    assert suggested[0]["_ready_sort_position"] == 0
    # Backcompat: prior projected fields remain intact.
    assert suggested[0]["_ready_provenance"]["rule_version"] == "ready_context.v1"
