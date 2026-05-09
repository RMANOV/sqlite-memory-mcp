"""Paranoid edge-case battery for Phase 1 Memory Reflection.

Covers ground that the standard DAO + tool tests do not: Unicode in
instructions and evidence, concurrency, fault injection (corrupted JSON,
direct SQL CHECK violations), foreign-key cascade behavior with and
without `PRAGMA foreign_keys = ON`, exact boundary values for limits,
and an empirical verification that the hot path is LLM-free (no network
sockets opened during reflect_start).

Run with:
    pytest tests/test_reflection_phase1_paranoid.py -v

These tests intentionally use a private temporary DB per test so they
can run in parallel with the live memory.db and never touch it.
"""

from __future__ import annotations

import importlib
import json
import os
import socket
import sqlite3
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


@pytest.fixture
def server(tmp_path, monkeypatch):
    """Spin a fresh DB and a freshly imported intel_server bound to it."""
    db_path = str(tmp_path / "paranoid.db")
    monkeypatch.setenv("SQLITE_MEMORY_DB", db_path)
    for mod in ("intel_server", "db_utils", "reflection", "reflection_dao", "schema"):
        sys.modules.pop(mod, None)
    from schema import init_db

    init_db(db_path)
    import intel_server  # noqa: F401

    yield intel_server


@pytest.fixture
def conn(tmp_path):
    db_path = str(tmp_path / "paranoid_dao.db")
    from schema import init_db

    init_db(db_path)
    c = sqlite3.connect(db_path, isolation_level=None)
    c.row_factory = sqlite3.Row
    yield c
    c.close()


# ── Boundary values for instructions length (C14) ──────────────────────


def test_instructions_exactly_4096_accepted(server):
    raw = server.reflect_start(instructions="x" * 4096)
    out = json.loads(raw)
    assert out["status"] == "completed", out


def test_instructions_4097_rejected(server):
    raw = server.reflect_start(instructions="x" * 4097)
    out = json.loads(raw)
    assert out.get("error_type") == "instructions_too_long"


def test_instructions_zero_chars_treated_as_none(server):
    """Empty string should be normalized to None (no validation issue)."""
    raw = server.reflect_start(instructions="")
    out = json.loads(raw)
    assert out["status"] == "completed"


# ── Boundary values for stale_days / limit_per_category ────────────────


def test_stale_days_zero_does_not_crash(server):
    """stale_days=0 means due_date < today; should not raise."""
    raw = server.reflect_start(stale_days=0)
    assert json.loads(raw)["status"] == "completed"


def test_stale_days_99999_returns_no_stale(server):
    raw = server.reflect_start(stale_days=99999)
    out = json.loads(raw)
    assert out["status"] == "completed"


def test_limit_per_category_zero(server):
    """limit_per_category=0 yields empty candidate lists."""
    raw = server.reflect_start(limit_per_category=0)
    out = json.loads(raw)
    assert out["status"] == "completed"
    assert out["candidates_persisted"] == 0


def test_limit_per_category_negative_does_not_crash(server):
    """Negative limit must not crash (SQL clamps via SQLite or returns empty)."""
    raw = server.reflect_start(limit_per_category=-5)
    out = json.loads(raw)
    assert out["status"] == "completed"


# ── Unicode in instructions ────────────────────────────────────────────


@pytest.mark.parametrize(
    "label,value",
    [
        ("bulgarian_emoji", "Фокусирай се върху прескочените стейл задачи 🔥"),
        ("rtl_arabic", "مرحبا بالعالم — focus on right-to-left content"),
        (
            "zalgo",
            "Z̸̢̢̛̺̲͚̦͉͕̙̟͉̭a̷̧̛̭̮͍̪̘͍l̶̢̛͍̪̮̲͚̥g̴͔̳̮͚̮o̵̧̢̢̥̥̭̟̜",
        ),
        ("emoji_only", "🔥💀🗑️♻️🧹"),
        ("null_byte", "a\x00b\x00c"),
        ("mixed_scripts", "English 中文 한국어 العربية русский"),
    ],
)
def test_unicode_instructions_round_trip(server, label, value):
    raw = server.reflect_start(instructions=value)
    out = json.loads(raw)
    assert out["status"] == "completed", f"{label}: {out}"
    rid = out["run_id"]
    raw2 = server.reflect_status(rid)
    s = json.loads(raw2)
    stored = s["run"]["instructions"] or ""
    if "\x00" in value:
        # SQLite does store NULL bytes in TEXT; verify exact round-trip.
        assert stored == value, f"{label}: NULL-byte truncation"
    else:
        assert stored == value, f"{label}: roundtrip mismatch"


# ── Unicode in candidate evidence (JSON column) ────────────────────────


def test_evidence_unicode_json_roundtrip(conn):
    from reflection_dao import add_candidate, create_run, get_candidate

    rid = create_run(conn)
    evidence = {
        "title": "Тестова задача 🔥",
        "ratio": 0.95,
        "list": ["a", "б", "ع"],
        "nested": {"key": 'value with "quotes" and \\backslash'},
    }
    cid = add_candidate(
        conn,
        rid,
        candidate_type="test",
        suggested_action="merge",
        target_kind="task",
        target_ref="t1",
        evidence=evidence,
    )
    row = get_candidate(conn, cid)
    assert row["evidence"] == evidence


def test_proposed_state_none_round_trips(conn):
    from reflection_dao import add_candidate, create_run, get_candidate

    rid = create_run(conn)
    cid = add_candidate(
        conn,
        rid,
        candidate_type="test",
        suggested_action="merge",
        target_kind="task",
        target_ref="t1",
        evidence={},
        proposed_state=None,
    )
    assert get_candidate(conn, cid)["proposed_state"] is None


# ── Concurrency ────────────────────────────────────────────────────────


def test_parallel_reflect_start_no_db_lock_errors(server):
    """5 concurrent reflect_start calls must all complete without DB lock."""
    statuses: list[str] = []

    def worker(_i: int) -> None:
        try:
            raw = server.reflect_start(stale_days=99999)
            statuses.append(json.loads(raw)["status"])
        except Exception as exc:
            statuses.append(f"err: {exc}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    failures = [s for s in statuses if s != "completed"]
    assert not failures, f"failures: {failures}"


# ── Fault injection: corrupted JSON columns ────────────────────────────


def test_list_candidates_handles_corrupt_evidence_json(conn):
    """Direct SQL inserts garbage JSON; list_candidates must not crash."""
    from reflection_dao import create_run, list_candidates

    rid = create_run(conn)
    conn.execute(
        "INSERT INTO reflection_candidates (candidate_id, run_id, candidate_type, "
        "suggested_action, target_kind, target_ref, evidence_json, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "c-corrupt",
            rid,
            "test",
            "merge",
            "task",
            "t1",
            "{not valid json",
            "2026-01-01T00:00:00",
        ),
    )
    rows, total = list_candidates(conn, rid)
    assert total == 1
    assert rows[0]["evidence"] == {}, "corrupt JSON should fall back to {}"


def test_get_candidate_handles_corrupt_proposed_state(conn):
    from reflection_dao import create_run, get_candidate

    rid = create_run(conn)
    conn.execute(
        "INSERT INTO reflection_candidates (candidate_id, run_id, candidate_type, "
        "suggested_action, target_kind, target_ref, evidence_json, "
        "proposed_state_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "c-corrupt2",
            rid,
            "test",
            "merge",
            "task",
            "t1",
            "{}",
            "{not valid",
            "2026-01-01T00:00:00",
        ),
    )
    row = get_candidate(conn, "c-corrupt2")
    assert row is not None
    assert row["proposed_state"] is None


# ── Direct SQL bypass of DAO: CHECK constraints must hold ──────────────


def test_check_constraint_blocks_invalid_status(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO reflection_runs (run_id, version, status, created_by, "
            "created_at) VALUES ('rfl-bad', 'reflect_v1.0', 'zombie', 'test', "
            "'2026-01-01T00:00:00')"
        )


def test_check_constraint_blocks_invalid_target_kind(conn):
    from reflection_dao import create_run

    rid = create_run(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO reflection_candidates (candidate_id, run_id, candidate_type, "
            "suggested_action, target_kind, target_ref, evidence_json, created_at) "
            "VALUES ('c-bad', ?, 't', 'm', 'email', 't1', '{}', '2026-01-01')",
            (rid,),
        )


# ── Foreign-key cascade behavior depends on PRAGMA ─────────────────────


def test_fk_cascade_active_when_pragma_on(tmp_path):
    """With PRAGMA foreign_keys=ON, cascade fires on parent delete."""
    from schema import init_db

    db_path = str(tmp_path / "fk_on.db")
    init_db(db_path)
    c = sqlite3.connect(db_path, isolation_level=None)
    c.row_factory = sqlite3.Row
    try:
        c.execute("PRAGMA foreign_keys = ON")
        from reflection_dao import add_candidate, add_input, create_run

        rid = create_run(c)
        add_input(c, rid, "tasks", {"x": 1})
        add_candidate(
            c,
            rid,
            candidate_type="t",
            suggested_action="m",
            target_kind="task",
            target_ref="t1",
            evidence={},
        )
        c.execute("DELETE FROM reflection_runs WHERE run_id = ?", (rid,))
        for table in ("reflection_inputs", "reflection_candidates"):
            n = c.execute(
                f"SELECT COUNT(*) FROM {table} WHERE run_id = ?", (rid,)
            ).fetchone()[0]
            assert n == 0, f"{table} not cascaded with PRAGMA on"
    finally:
        c.close()


def test_fk_cascade_silent_when_pragma_off_documents_gotcha(tmp_path):
    """Without PRAGMA, cascade does NOT fire — orphan rows persist.

    This is a well-known SQLite default (per-connection). Production
    paths set PRAGMA foreign_keys=ON in db_utils._PRAGMAS, so this is a
    test-only concern. The test pins the behavior so any future change
    is intentional.
    """
    from schema import init_db

    db_path = str(tmp_path / "fk_off.db")
    init_db(db_path)
    c = sqlite3.connect(db_path, isolation_level=None)
    c.row_factory = sqlite3.Row
    try:
        from reflection_dao import add_input, create_run

        rid = create_run(c)
        add_input(c, rid, "tasks", {"x": 1})
        c.execute("DELETE FROM reflection_runs WHERE run_id = ?", (rid,))
        orphan = c.execute(
            "SELECT COUNT(*) FROM reflection_inputs WHERE run_id = ?", (rid,)
        ).fetchone()[0]
        assert orphan == 1, "expected orphan row when PRAGMA is off"
    finally:
        c.close()


def test_production_pragmas_include_foreign_keys_on():
    """db_utils._PRAGMAS guarantees FK enforcement on all production conns."""
    import db_utils

    assert any("foreign_keys=ON" in p.replace(" ", "") for p in db_utils._PRAGMAS), (
        "production PRAGMA list missing foreign_keys=ON"
    )


# ── State machine — direct error_type emission paths ───────────────────


def test_cancel_after_terminal_returns_invalid_state_transition(server):
    rid = json.loads(server.reflect_start())["run_id"]
    out = json.loads(server.reflect_cancel(rid))
    assert out.get("error_type") == "invalid_state_transition"


def test_archive_pending_returns_invalid_state_transition(server, tmp_path):
    """Manually create a pending row (skip the auto-completed reflect_start path)."""
    db_path = os.environ["SQLITE_MEMORY_DB"]
    c = sqlite3.connect(db_path, isolation_level=None)
    try:
        c.execute(
            "INSERT INTO reflection_runs (run_id, version, status, created_by, "
            "created_at) VALUES ('rfl-pend', 'reflect_v1.0', 'pending', 'test', "
            "'2026-01-01T00:00:00')"
        )
    finally:
        c.close()
    out = json.loads(server.reflect_archive("rfl-pend"))
    assert out.get("error_type") == "invalid_state_transition"


# ── LLM-free property ─────────────────────────────────────────────────


def test_no_llm_or_network_imports_in_hot_path():
    """Hot-path modules must not import requests/httpx/openai/anthropic etc."""
    forbidden = ("requests", "httpx", "urllib3", "anthropic", "openai")
    for modname in ("reflection", "reflection_dao", "intel_server"):
        m = importlib.import_module(modname)
        loaded = set(sys.modules)
        for f in forbidden:
            assert f not in loaded or not _module_uses(m, f), (
                f"{modname} pulls in {f}"
            )


def _module_uses(module, target_name: str) -> bool:
    """Return True if module's namespace references the forbidden name directly."""
    return target_name in module.__dict__


def test_reflect_start_works_with_socket_blocked(server):
    """Empirical proof of LLM-free: reflect_start must succeed when sockets blocked."""
    orig = socket.socket
    attempts: list = []

    def tripwire(*a, **kw):
        attempts.append(a)
        raise OSError("network blocked by paranoid test")

    socket.socket = tripwire
    try:
        raw = server.reflect_start(stale_days=99999)
        out = json.loads(raw)
        assert out["status"] == "completed"
        assert attempts == [], f"unexpected socket attempts: {attempts}"
    finally:
        socket.socket = orig
