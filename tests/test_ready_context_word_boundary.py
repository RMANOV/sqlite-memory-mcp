"""Regression suite for the ready_context classifier defects.

Three defects are pinned here:

1. Substring matching. Every classifier was ``any(marker in text)`` over the
   whole task body, so ``pin`` matched "mapping"/"opinion"/"shaping",
   ``reading`` matched "proofreading", ``sync`` matched "asynchronous" and
   ``bridge`` matched "cambridge". On the live corpus ``pin`` hit 293 texts of
   which only 19 were the standalone word.
2. Intent markers were inferred from description/notes. A task whose *body*
   merely discussed pinning or a superseded design was read as an explicit
   operator instruction, so ``reason_primary=explicit_user_correction`` fired
   on 389 tasks of which only 46 were really ``section='today'``.
3. ``_ready_state`` tested the text-inferred ``cleanup_candidate`` code before
   ``status == 'in_progress'`` / ``section == 'today'``, so a keyword in a body
   paragraph outranked the strongest liveness signal available. Measured: 0 of
   3 in_progress tasks reached ``ready_now``.

Four regressions that the first pass at those three fixes introduced are
pinned in section 5:

4. Word boundaries turned every inflection into a false *negative* — "two
   blockers remain" stopped matching ``blocker`` at all, so the blocked state
   and its blocker list were lost, not downgraded.
5. ``reopened`` (past tense) fell outside ``\\breopen\\b``, so a closed task
   whose notes said "reopened by user" left the cleanup-review path entirely
   and became ``excluded``.
6. Scoping *every* intent marker to the title made pinned tasks disappear:
   ``surface_until=`` written in ``description`` — the field this server's tool
   contract calls "the default primary body" — no longer held a someday or
   reading row on the surface.
7. The liveness reorder was wider than the defect: it let ``in_progress`` beat
   ``blocked`` too, emptying ``prime_context``'s ``blocked_or_waiting`` bucket.

Everything here is hermetic: the DB round-trip uses a fresh ``tmp_path``
SQLite file with a minimal tasks table. No production database is opened.

Run: pytest tests/test_ready_context_word_boundary.py -v
"""

import os
import re
import sqlite3
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import smart_retrieval  # noqa: E402
from smart_retrieval import (  # noqa: E402
    _ready_has_explicit_surface,
    _ready_is_cleanup_candidate,
    _ready_is_reading,
    build_ready_record,
    prime_context,
    ready_context,
    suggested_ready,
)

TODAY = date(2026, 5, 24)


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


def _record(task):
    return build_ready_record(task, today=TODAY)


# ── 1. Word-boundary matching ─────────────────────────────────────────────


def test_patterns_are_compiled_once_at_module_level():
    # The fix must not recompile per call: the patterns are module constants.
    for name in (
        "_SURFACE_MARKERS",
        "_CLEANUP_MARKERS",
        "_READING_MARKERS",
        "_BRIDGE_MARKERS",
        "_MACHINE_MARKERS",
    ):
        assert isinstance(getattr(smart_retrieval, name), re.Pattern), name
    for _category, pattern in smart_retrieval._BLOCKER_TERMS:
        assert isinstance(pattern, re.Pattern)


def test_pin_does_not_match_mapping_opinion_shaping():
    # The 274-false-match case: "pin" as a substring of ordinary words.
    for title in (
        "Mapping Studio installer delivery",
        "Collect operator opinion on the tray",
        "Shaping the retrieval contract",
        "Typing indicator jitter",
    ):
        task = _task("t", title)
        assert not _ready_has_explicit_surface(task), title
        assert "explicit_user_correction" not in _record(task)["reason_codes"], title


def test_pin_still_matches_as_a_standalone_word_in_title():
    # The 19 true matches must survive the fix.
    for title in (
        "pin this to the tray",
        "Task is pinned for review",
        "surface_until=2026-05-24 keep visible",
        "curated-reading queue",
    ):
        task = _task("t", title)
        assert _ready_has_explicit_surface(task), title
        assert "explicit_user_correction" in _record(task)["reason_codes"], title


def test_reading_does_not_match_proofreading_or_threading():
    for title in ("Proofreading pass on the README", "Threading model rewrite"):
        assert not _ready_is_reading(_task("t", title)), title
    # True positives still classify as readings.
    assert _ready_is_reading(_task("t", "Critical reading about Beads"))
    assert _ready_is_reading(_task("t", "note", project="critical-readings"))


def test_sync_and_bridge_do_not_match_inside_longer_words():
    # "unsynced" is deliberately absent here: it is a real inflection of the
    # sync marker, not incidental prose, and is pinned as a positive in
    # test_marker_inflections_are_not_lost_by_word_boundaries.
    quiet = _task(
        "quiet",
        "Cambridge asynchronous notes",
        description="the queue was bridged overnight",
    )
    codes = _record(quiet)["reason_codes"]
    assert "machine_anomaly_open" not in codes
    assert "bridge_sync_caution" not in codes

    # Standalone words still fire, so the real signal is not lost.
    loud = _task(
        "loud",
        "Bridge sync status repair",
        notes="bridge sync updated_at churn after import wave",
    )
    loud_codes = _record(loud)["reason_codes"]
    assert "machine_anomaly_open" in loud_codes
    assert "bridge_sync_caution" in loud_codes


def test_blocker_terms_respect_word_boundaries():
    # "permission" is a blocker term; "permissionless" is not that signal.
    assert _record(_task("t", "Permissionless rollout design"))["blockers"] == []
    assert _record(_task("t", "Waiting on permission from operator"))["blockers"]


# ── 2. Intent markers are scoped to title + section ───────────────────────


def test_surface_marker_in_body_is_not_an_explicit_user_instruction():
    body_only = _task(
        "body",
        "Retrieval scoring rework",
        description="we should pin the weights once the corpus settles",
        notes="pinned dependency versions are listed below",
    )
    assert not _ready_has_explicit_surface(body_only)
    record = _record(body_only)
    assert "explicit_user_correction" not in record["reason_codes"]
    assert record["reason_primary"] != "explicit_user_correction"


def test_surface_marker_in_title_or_section_still_counts():
    assert _ready_has_explicit_surface(_task("t", "pin to tray"))
    # section='today' is the operator's own declaration and keeps its code.
    assert (
        "explicit_user_correction"
        in _record(_task("t", "Ordinary work", section="today"))["reason_codes"]
    )


def test_cleanup_markers_in_body_do_not_mark_a_task_for_cleanup():
    body_only = _task(
        "body",
        "Entity merge pass",
        description="drop the duplicate rows the superseded importer produced",
        notes="cleanup_candidate detection is what this task implements",
    )
    assert not _ready_is_cleanup_candidate(body_only)
    record = _record(body_only)
    assert "cleanup_candidate" not in record["reason_codes"]
    assert record["ready_state"] != "cleanup_candidate"


def test_cleanup_marker_in_title_still_counts():
    titled = _task("titled", "Superseded by the v2 importer")
    assert _ready_is_cleanup_candidate(titled)
    assert _record(titled)["ready_state"] == "cleanup_candidate"


# ── 3. Explicit operator state outranks text-inferred cleanup ─────────────


def test_in_progress_task_is_not_demoted_by_a_cleanup_keyword():
    # Marker is in the TITLE, so the cleanup reason genuinely fires — this
    # pins the ordering fix, not merely the scoping fix.
    task = _task("live", "Superseded importer rewrite", status="in_progress")
    record = _record(task)

    assert "cleanup_candidate" in record["reason_codes"]  # signal preserved
    assert record["ready_state"] == "ready_now"  # liveness wins


def test_section_today_task_is_not_demoted_by_a_cleanup_keyword():
    task = _task("today", "Duplicate detection rollout", section="today")
    record = _record(task)

    assert "cleanup_candidate" in record["reason_codes"]
    assert record["ready_state"] == "ready_now"
    assert record["reason_primary"] == "explicit_user_correction"


def test_explicit_waiting_section_still_wins_over_liveness_and_cleanup():
    parked = _task(
        "parked",
        "Duplicate cleanup follow-up",
        status="in_progress",
        section="waiting",
    )
    assert _record(parked)["ready_state"] == "waiting"


def test_closed_status_gate_is_untouched_by_the_reorder():
    closed = _task("closed", "Superseded importer", status="done")
    assert _record(closed)["ready_state"] == "cleanup_candidate"
    quiet_closed = _task("quiet-closed", "Ordinary finished work", status="done")
    assert _record(quiet_closed)["ready_state"] == "excluded"


def test_reading_gate_still_precedes_the_liveness_check():
    # A reading note in section='today' must stay out of the default surface.
    rows = [
        _task(
            "reading-due",
            "OODA reading about agent memory",
            section="today",
            project="readings",
        )
    ]
    assert ready_context(rows, today=TODAY) == []
    assert (
        ready_context(rows, include_readings=True, today=TODAY)[0]["ready_state"]
        == "ready_now"
    )


# ── 4. Regressions introduced by the first pass at fixes 1-3 ──────────────


# Each row: (field, text, predicate over the built record, the inflected form).
# Every case is a form the word-boundary anchor alone silently dropped.
_INFLECTION_CASES = (
    (
        "notes",
        "two blockers remain on the notary step",
        lambda r: any(b["category"] == "blocked_by" for b in r["blockers"]),
        "blockers",
    ),
    (
        "notes",
        "review the file permissions with ops",
        lambda r: any(b["category"] == "missing_input" for b in r["blockers"]),
        "permissions",
    ),
    (
        "notes",
        "the queue has been syncing all night",
        lambda r: "machine_anomaly_open" in r["reason_codes"],
        "syncing",
    ),
    (
        "notes",
        "the queue was unsynced overnight",
        lambda r: "machine_anomaly_open" in r["reason_codes"],
        "unsynced",
    ),
    (
        "title",
        "Machines in the lab keep overheating",
        lambda r: "machine_anomaly_open" in r["reason_codes"],
        "machines",
    ),
    (
        "notes",
        "two deadlines land this week",
        lambda r: "active_delivery_pressure" in r["reason_codes"],
        "deadlines",
    ),
    (
        "notes",
        "external commitments to the notary are open",
        lambda r: "external_commitment_risk" in r["reason_codes"],
        "commitments",
    ),
    (
        "notes",
        "staleness across the whole pack",
        lambda r: "stale_but_unresolved" in r["reason_codes"],
        "staleness",
    ),
    (
        "notes",
        "bridge rows were imported twice",
        lambda r: "bridge_sync_caution" in r["reason_codes"],
        "imported",
    ),
    (
        # Cleanup stays intent-scoped, so this one is asserted from the title.
        "title",
        "Duplicates from the v2 export",
        lambda r: "cleanup_candidate" in r["reason_codes"],
        "duplicates",
    ),
)


def test_marker_inflections_are_not_lost_by_word_boundaries():
    # Regression 4. `\bblocker\b` cannot match "blockers", so the whole signal
    # vanished instead of being downgraded. The module already knew the shape
    # of this problem: _READING_MARKERS enumerates `reading` AND `readings`.
    for field, text, predicate, form in _INFLECTION_CASES:
        # priority=critical keeps reason_codes non-empty, so the empty-list
        # sentinel (`reason_codes or ["stale_but_unresolved"]`) can never make
        # one of these assertions vacuously true.
        task = _task("infl", "Notary step", priority="critical")
        task[field] = text
        assert predicate(_record(task)), f"{form!r} lost in {field}={text!r}"

    # The measured case, end to end: the blocker list and the blocked state
    # both came back empty before the fix.
    measured = _task(
        "measured",
        "Notary step",
        notes="two blockers remain on the notary step",
    )
    record = _record(measured)
    assert record["blockers"] == [
        {"category": "blocked_by", "detail": "matched task text"}
    ]
    assert record["ready_state"] == "blocked"

    # Widening must not re-open the collisions the boundary anchor closed.
    for title in (
        "Important announcement about the tray",  # import + "ance"/"ant"
        "Importance of the retrieval contract",
        "Permissionless rollout design",
        "Cambridge bridged overnight",  # `bridge` gains no inflections
    ):
        quiet = _record(_task("quiet", title, notes="bridge context"))
        assert "bridge_sync_caution" not in quiet["reason_codes"], title
        assert quiet["blockers"] == [], title

    # The accepted forms stay enumerated in one greppable module constant, so
    # widening a marker can never leak into another by regex accident.
    assert isinstance(smart_retrieval._MARKER_INFLECTIONS, dict)


def test_reopened_past_tense_keeps_a_closed_task_in_the_review_path():
    # Regression 5. "reopened by user" is far likelier operator prose than the
    # bare token, and losing it drops the row out of ready_context entirely.
    closed = _task(
        "closed-reopened",
        "Notary step",
        status="done",
        notes="reopened by user after confusion",
    )
    record = _record(closed)

    assert "reopen_requested_by_user" in record["reason_codes"]
    assert record["ready_state"] == "cleanup_candidate"
    # It must actually reach the surface, not merely carry a reason code.
    assert [row["id"] for row in suggested_ready([closed], today=TODAY)] == [
        "closed-reopened"
    ]

    # The structured token keeps working, and a quiet closed task stays out.
    token = _task(
        "closed-token",
        "Notary step",
        status="done",
        notes="reopen_requested_by_user after sync confusion",
    )
    assert _record(token)["ready_state"] == "cleanup_candidate"
    quiet_closed = _task("quiet-closed", "Finished work", status="done")
    assert _record(quiet_closed)["ready_state"] == "excluded"


def test_surface_until_in_description_still_pins_someday_and_reading_rows():
    # Regression 6. Scoping intent markers to the title made pinned rows
    # DISAPPEAR (excluded), not merely lose a reason code — and `description`
    # is where this server's tool contract tells callers to write task content.
    someday = _task(
        "someday-pinned",
        "Notary automation",
        section="someday",
        description="surface_until=2026-06-01",
    )
    assert _ready_has_explicit_surface(someday)
    assert _record(someday)["ready_state"] == "suggested_ready"

    reading = _task(
        "reading-pinned",
        "Beads chapter notes",
        project="readings",
        description="surface_until=2026-05-24",
    )
    assert _ready_has_explicit_surface(reading)
    assert _record(reading)["ready_state"] == "suggested_ready"
    # Readings are excluded from the default tray unless explicitly surfaced.
    assert [row["id"] for row in suggested_ready([reading], today=TODAY)] == [
        "reading-pinned"
    ]

    # The prose tier stays title-scoped: musing about pinning is not an order.
    prose = _task(
        "prose",
        "Retrieval scoring rework",
        section="someday",
        description="we should pin the weights once the corpus settles",
        notes="pinned dependency versions are listed below",
    )
    assert not _ready_has_explicit_surface(prose)
    assert _record(prose)["ready_state"] == "excluded"


def test_blocked_stays_reachable_for_in_progress_tasks():
    # Regression 7. The reorder was claimed as "liveness beats the text-inferred
    # cleanup label" but also made `blocked` unreachable while in_progress,
    # emptying prime_context's blocked_or_waiting bucket.
    task = _task(
        "live-blocked",
        "Installer signing work",
        status="in_progress",
        notes="needs input from the notary service",
    )
    record = _record(task)

    assert record["blockers"]
    assert "blocked_by_open_item" in record["reason_codes"]
    assert record["ready_state"] == "blocked"
    assert "Resolve blocker" in record["next_action"]

    pack = prime_context([task], today=TODAY)
    assert [r["id"] for r in pack["blocked_or_waiting"]] == ["live-blocked"]

    # section='today' is equally not a licence to ignore a blocker.
    today_blocked = _task(
        "today-blocked",
        "Installer signing work",
        section="today",
        notes="waiting on the notary service",
    )
    assert _record(today_blocked)["ready_state"] in {"blocked", "waiting"}

    # The narrow win the reorder was actually for still holds: with nothing
    # blocking, liveness outranks the text-inferred cleanup label.
    live = _task("live", "Superseded importer rewrite", status="in_progress")
    live_record = _record(live)
    assert "cleanup_candidate" in live_record["reason_codes"]
    assert live_record["ready_state"] == "ready_now"


# ── 5. End-to-end over a temp SQLite DB ───────────────────────────────────

_SCHEMA = """
CREATE TABLE tasks (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    description TEXT DEFAULT NULL,
    status      TEXT NOT NULL DEFAULT 'not_started',
    priority    TEXT DEFAULT 'medium',
    section     TEXT DEFAULT 'inbox',
    due_date    TEXT DEFAULT NULL,
    project     TEXT DEFAULT NULL,
    parent_id   TEXT DEFAULT NULL,
    notes       TEXT DEFAULT NULL,
    reminder_at TEXT DEFAULT NULL,
    type        TEXT NOT NULL DEFAULT 'task',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
"""


def _temp_conn(tmp_path):
    """Fresh throwaway DB file. Never the production memory.db."""
    conn = sqlite3.connect(str(tmp_path / "ready_context_test.db"))
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


def test_ready_context_over_temp_db_rows(tmp_path):
    conn = _temp_conn(tmp_path)
    rows = [
        # 1: the "mapping" collision — must NOT read as an operator instruction.
        ("mapping", "Mapping Studio installer delivery", "inbox", "not_started", ""),
        # 2: in_progress with a cleanup word in the body — must stay live.
        ("live", "Retrieval rework", "inbox", "in_progress", "superseded approach"),
        # 3: section=today with a cleanup word in the body — must stay ready.
        ("today", "Tray polish", "today", "not_started", "removes duplicate rows"),
        # 4: genuine explicit surface in the title.
        ("pinned", "pin to tray until Friday", "inbox", "not_started", ""),
    ]
    conn.executemany(
        "INSERT INTO tasks (id, title, section, status, description,"
        " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [(*r, "2026-05-20T10:00:00+00:00", "2026-05-20T10:00:00+00:00") for r in rows],
    )
    conn.commit()

    tasks = [dict(r) for r in conn.execute("SELECT * FROM tasks ORDER BY id")]
    records = {r["id"]: r for r in ready_context(tasks, today=TODAY)}
    conn.close()

    assert set(records) == {"mapping", "live", "today", "pinned"}

    assert "explicit_user_correction" not in records["mapping"]["reason_codes"]
    assert records["mapping"]["ready_state"] == "suggested_ready"

    assert records["live"]["ready_state"] == "ready_now"
    assert "cleanup_candidate" not in records["live"]["reason_codes"]

    assert records["today"]["ready_state"] == "ready_now"
    assert records["today"]["reason_primary"] == "explicit_user_correction"

    assert "explicit_user_correction" in records["pinned"]["reason_codes"]

    # Nothing in this set is a cleanup candidate any more.
    assert [
        r["id"] for r in records.values() if r["ready_state"] == "cleanup_candidate"
    ] == []
