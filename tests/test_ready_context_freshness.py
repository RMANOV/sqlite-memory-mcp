"""Age must not turn abandoned dates into high-confidence instructions."""

from datetime import date

import pytest

from smart_retrieval import (
    build_ready_record,
    prime_context,
    ready_context,
    suggested_ready,
)


TODAY = date(2026, 10, 25)


def task(task_id, **changes):
    row = {
        "id": task_id,
        "title": "Follow up on delivery",
        "type": "task",
        "status": "not_started",
        "priority": "high",
        "section": "inbox",
        "due_date": "2026-09-28",
        "created_at": "2026-09-01T09:00:00+00:00",
        "updated_at": "2026-09-25T10:00:00+00:00",
    }
    row.update(changes)
    return row


@pytest.mark.parametrize(
    "changes", [{}, {"status": "in_progress"}, {"section": "today"}]
)
def test_old_overdue_and_active_rows_require_verification(changes):
    record = build_ready_record(task("old", **changes), today=TODAY)
    assert record["ready_state"] == "suggested_ready"
    assert record["confidence"] == "low"
    assert record["stale_warning"]
    assert record["freshness"]["status"] == "stale"
    assert record["freshness"]["age_days"] == 30
    assert record["freshness"]["requires_verification"] is True
    assert "Verify" in record["next_action"]


def test_recently_confirmed_overdue_task_keeps_current_priority():
    record = build_ready_record(
        task("recent", updated_at="2026-10-24T19:30:00+03:00"), today=TODAY
    )
    assert record["ready_state"] == "ready_now"
    assert record["confidence"] == "high"
    assert record["stale_warning"] == ""
    assert record["freshness"]["status"] == "recent"


@pytest.mark.parametrize("due", [None, "invalid", "2026-99-11"])
def test_missing_or_invalid_due_date_does_not_refresh_old_work(due):
    record = build_ready_record(
        task("old", due_date=due, status="in_progress"), today=TODAY
    )
    assert record["freshness"]["status"] == "stale"
    assert record["ready_state"] == "suggested_ready"


@pytest.mark.parametrize(
    ("updated", "status"), [("2026-10-12", "recent"), ("2026-10-11", "stale")]
)
def test_two_week_freshness_boundary(updated, status):
    record = build_ready_record(task("boundary", updated_at=updated), today=TODAY)
    assert record["freshness"]["status"] == status


def test_current_candidates_precede_stale_without_dropping_them():
    rows = [
        task("stale", priority="critical", section="today"),
        task("recent", priority="medium", due_date=None, updated_at="2026-10-24"),
    ]
    records = ready_context(rows, today=TODAY)
    assert [r["id"] for r in records] == ["recent", "stale"]
    assert [r["sort_position"] for r in records] == [0, 1]
    assert (
        prime_context(rows, limit=1, today=TODAY)["top_ready_items"][0]["id"]
        == "recent"
    )


@pytest.mark.parametrize("updated", [None, "", "not-a-date", "2026-10-24Tinvalid", 123])
def test_unknown_or_malformed_recency_is_not_high_confidence(updated):
    record = build_ready_record(
        task("unknown", updated_at=updated, created_at=None), today=TODAY
    )
    assert record["freshness"]["status"] == "unknown"
    assert record["freshness"]["age_days"] is None
    assert record["stale_warning"]
    assert record["ready_state"] == "suggested_ready"
    assert record["confidence"] == "low"


def test_missing_update_uses_creation_date_but_invalid_update_does_not():
    created = build_ready_record(
        task("new", updated_at=None, created_at="2026-10-25T09:00:00Z"), today=TODAY
    )
    invalid = build_ready_record(
        task("bad", updated_at="broken", created_at="2026-10-25T09:00:00Z"), today=TODAY
    )
    assert created["freshness"]["status"] == "recent"
    assert created["freshness"]["timestamp_field"] == "created_at"
    assert invalid["freshness"]["status"] == "unknown"


def test_future_metadata_timestamp_does_not_establish_freshness():
    record = build_ready_record(task("future", updated_at="2026-11-01"), today=TODAY)
    assert record["freshness"]["status"] == "future_timestamp"
    assert record["stale_warning"]
    assert record["confidence"] == "low"


def test_longstanding_future_schedule_is_not_deprioritized_for_old_metadata():
    record = build_ready_record(task("scheduled", due_date="2026-11-10"), today=TODAY)
    assert record["freshness"]["status"] == "scheduled"
    assert record["freshness"]["requires_verification"] is False
    assert record["stale_warning"] == ""
    assert record["ready_state"] == "suggested_ready"


@pytest.mark.parametrize("changes", [{"status": "in_progress"}, {"section": "today"}])
def test_future_due_date_does_not_refresh_old_active_or_today_claims(changes):
    record = build_ready_record(
        task("old-active", due_date="2026-11-10", **changes), today=TODAY
    )
    assert record["freshness"]["status"] == "stale"
    assert record["freshness"]["requires_verification"] is True
    assert record["ready_state"] == "suggested_ready"
    assert record["confidence"] == "low"


def test_freshness_uses_supplied_historical_date_and_preserves_blockers():
    row = task("history")
    historical = build_ready_record(row, today=date(2026, 9, 28))
    current = build_ready_record(row, today=TODAY)
    assert historical["freshness"]["status"] == "recent"
    assert historical["ready_state"] == "ready_now"
    assert current["freshness"]["status"] == "stale"
    waiting = build_ready_record(task("waiting", section="waiting"), today=TODAY)
    assert waiting["ready_state"] == "waiting"
    assert waiting["blockers"]


def test_suggested_projection_keeps_freshness_and_input_unchanged():
    row = task("old")
    snapshot = dict(row)
    projected = suggested_ready([row], today=TODAY)[0]
    assert projected["_ready_freshness"]["status"] == "stale"
    assert row == snapshot


def test_recent_import_churn_still_requires_verification():
    record = build_ready_record(
        task("import", updated_at="2026-10-25", notes="bridge sync updated_at churn"),
        today=TODAY,
    )
    assert record["freshness"]["requires_verification"] is True
    assert record["ready_state"] != "ready_now"
    assert record["stale_warning"]
