"""The live-worker census must key on the claim key, not the bare id.

``debate_worker_claims`` is UNIQUE on ``(topic_id, role, worker_session_id)``
and ``debate.claim_worker_session`` mints ``<parent>-W<n>`` from a counter
keyed ``(topic_id, role, parent_session_id)``. The same ``<parent>-W1`` is
therefore minted independently in different topics (and, for a parent holding
two roles, in different roles of one topic) — the id alone is NOT a worker
identity.

``hooks/debate_pump.py::_machine_live_worker_count`` used to union bare id
strings into a ``set[str]``, so N distinct live workers sharing an id counted
as one. That is an UNDER-count, the only direction that can breach
``max_concurrent_workers`` on the path the function exists to protect: a pump
restart, where ``CHILDREN`` is empty and the DB census is the sole floor.

Scope note: impact is bounded, not an incident. ``live_children =
max(len(CHILDREN), baseline_live_workers)`` means the in-process set is a
floor within one pump lifetime, and ``DEBATE_PUMP_MAX_WORKERS_PER_SCAN``
caps any over-spawn per scan. This is a correctness fix.

Every test runs against a constructed temp DB — production memory.db is
never opened.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
HOOKS = REPO / "hooks"
for _path in (str(REPO), str(HOOKS)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import debate_pump  # noqa: E402 - imported after local repo path bootstrap
from schema import init_db  # noqa: E402 - imported after local repo path bootstrap

CREATE_TIME = 1_760_000_000.0

# (topic_id, role, parent_session_id, trigger_msg_id, worker_session_id, pid)
Claim = tuple[str, str, str, str, str, int]


@pytest.fixture()
def census_db(tmp_path, monkeypatch):
    """Real schema, constructed rows, pump pointed at the temp file."""
    db_path = tmp_path / "census.db"
    init_db(str(db_path))  # never call init_db() without a path: default is live
    monkeypatch.setattr(debate_pump, "DB_PATH", str(db_path))
    return db_path


def _seed_claims(db_path: Path, claims: list[Claim]) -> None:
    """Insert active claims plus the real-spawn receipt each one resolves."""
    con = sqlite3.connect(db_path, isolation_level=None)
    try:
        for n, (topic, role, parent, trigger, worker, pid) in enumerate(claims):
            con.execute(
                "INSERT INTO debate_worker_claims "
                "(topic_id, role, parent_session_id, trigger_msg_id, "
                " worker_session_id, state, claimed_at, heartbeat_at) "
                "VALUES (?,?,?,?,?, 'active', "
                "'2026-08-02T00:00:00Z','2026-08-02T00:00:00Z')",
                (topic, role, parent, trigger, worker),
            )
            con.execute(
                "INSERT INTO debate_wake_log "
                "(wake_id, trigger_msg_id, topic_id, recipient, target_role, "
                " target_session_id, target_runtime, action, result, "
                " schema_version, details_json, created_at) "
                "VALUES (?,?,?,?,?,?, 'cc', 'external_agent_spawn', "
                "'real_spawn', 'v1', ?, '2026-08-02T00:00:00Z')",
                (
                    f"w{n}",
                    trigger,
                    topic,
                    role,
                    role,
                    worker,
                    json.dumps({"pid": pid, "create_time": CREATE_TIME}),
                ),
            )
    finally:
        con.close()


def _only_pids_live(monkeypatch, live_pids: set[int]) -> None:
    monkeypatch.setattr(
        debate_pump,
        "_pid_is_live_agent",
        lambda pid, create_time=None: pid in live_pids
        and create_time == CREATE_TIME,
    )


# ── the defect ───────────────────────────────────────────────────────────────


def test_same_worker_id_in_two_topics_counts_as_two_live_workers(
    census_db, monkeypatch
):
    """Two topics, same parent+role → identical id, two real processes."""
    _seed_claims(
        census_db,
        [
            ("T_ALPHA", "EXECUTOR", "cc-parent", "m_alpha", "cc-parent-W1", 4001),
            ("T_BETA", "EXECUTOR", "cc-parent", "m_beta", "cc-parent-W1", 4002),
        ],
    )
    _only_pids_live(monkeypatch, {4001, 4002})

    keys = debate_pump._live_worker_claim_keys("T_ALPHA")
    keys |= debate_pump._live_worker_claim_keys("T_BETA")
    assert keys == {
        ("T_ALPHA", "EXECUTOR", "cc-parent-W1"),
        ("T_BETA", "EXECUTOR", "cc-parent-W1"),
    }

    # The pre-fix union — kept as an explicit witness of the regression.
    assert len({worker for _topic, _role, worker in keys}) == 1

    # And the per-topic helper still speaks bare ids, which is the contract
    # the Windows adapter and recover_stale_worker_claims depend on.
    assert debate_pump._live_worker_session_ids("T_ALPHA") == {"cc-parent-W1"}

    assert debate_pump._machine_live_worker_count(["T_ALPHA", "T_BETA"]) == 2
    assert debate_pump._safe_machine_live_worker_count(["T_ALPHA", "T_BETA"]) == 2


def test_same_worker_id_in_two_roles_of_one_topic_counts_twice(
    census_db, monkeypatch
):
    """Mirrors live data: one parent holding two roles in one topic spawns
    two distinct pids that share an id (UNIQUE is per-role, so both rows
    exist legitimately)."""
    _seed_claims(
        census_db,
        [
            ("T_ONE", "ADVOCATE", "codex-p", "m_adv", "codex-p-W1", 5001),
            ("T_ONE", "EXECUTOR", "codex-p", "m_exec", "codex-p-W1", 5002),
        ],
    )
    _only_pids_live(monkeypatch, {5001, 5002})

    assert debate_pump._live_worker_claim_keys("T_ONE") == {
        ("T_ONE", "ADVOCATE", "codex-p-W1"),
        ("T_ONE", "EXECUTOR", "codex-p-W1"),
    }
    assert debate_pump._machine_live_worker_count(["T_ONE"]) == 2

    # Two roles collapse to one id at the per-topic surface — that is correct
    # for the bare-id contract, and precisely why the census cannot use it.
    assert debate_pump._live_worker_session_ids("T_ONE") == {"codex-p-W1"}


# ── guards: the liveness probe itself is unchanged ──────────────────────────


def test_dead_pid_is_still_excluded(census_db, monkeypatch):
    _seed_claims(
        census_db,
        [
            ("T_ALPHA", "EXECUTOR", "cc-parent", "m_alpha", "cc-parent-W1", 4001),
            ("T_BETA", "EXECUTOR", "cc-parent", "m_beta", "cc-parent-W1", 4002),
        ],
    )
    _only_pids_live(monkeypatch, {4001})  # 4002 exited

    assert debate_pump._machine_live_worker_count(["T_ALPHA", "T_BETA"]) == 1


def test_wrong_create_time_is_still_excluded(census_db, monkeypatch):
    """PID reuse: the receipt's identity must still gate the count."""
    _seed_claims(
        census_db,
        [("T_ALPHA", "EXECUTOR", "cc-parent", "m_alpha", "cc-parent-W1", 4001)],
    )
    monkeypatch.setattr(
        debate_pump,
        "_pid_is_live_agent",
        lambda pid, create_time=None: False,
    )
    assert debate_pump._machine_live_worker_count(["T_ALPHA"]) == 0


def test_completed_claims_are_not_counted(census_db, monkeypatch):
    _seed_claims(
        census_db,
        [("T_ALPHA", "EXECUTOR", "cc-parent", "m_alpha", "cc-parent-W1", 4001)],
    )
    con = sqlite3.connect(census_db, isolation_level=None)
    con.execute("UPDATE debate_worker_claims SET state='completed'")
    con.close()
    _only_pids_live(monkeypatch, {4001})

    assert debate_pump._machine_live_worker_count(["T_ALPHA"]) == 0


# ── the other consumer must keep seeing bare ids ────────────────────────────


def test_stale_claim_recovery_still_receives_bare_session_ids(
    census_db, monkeypatch
):
    """``debate.recover_stale_worker_claims`` matches ``row['worker_session_id']``
    against a ``set[str]``; feeding it tuples would silently retire every live
    worker's claim. The pump projects the claim keys before the call."""
    import db_utils

    import debate

    _seed_claims(
        census_db,
        [("T_ALPHA", "EXECUTOR", "cc-parent", "m_alpha", "cc-parent-W1", 4001)],
    )
    _only_pids_live(monkeypatch, {4001})
    monkeypatch.setattr(debate_pump, "_active_topic_ids", lambda topics: ["T_ALPHA"])

    @contextmanager
    def _fake_conn(*args, **kwargs):
        yield object()

    monkeypatch.setattr(db_utils, "get_conn_immediate", _fake_conn)
    seen: list[set] = []

    def _fake_recover(conn, **kwargs):
        seen.append(kwargs["live_worker_session_ids"])
        return {"retired_count": 0, "completed_count": 0}

    monkeypatch.setattr(debate, "recover_stale_worker_claims", _fake_recover)

    debate_pump._recover_stale_worker_claims(
        topics=["T_ALPHA"], stale_seconds=600, minimum_age_seconds=120
    )

    assert seen == [{"cc-parent-W1"}]
    assert all(isinstance(sid, str) for sid in seen[0])
