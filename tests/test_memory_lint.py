"""Zero-write regression test for the read-only ``bin/memory-lint`` CLI (B4).

Builds a throwaway SQLite DB with the real schema + a handful of seed rows that
trigger every detector, then asserts that running the lint CLI (both its core
``collect_report`` function and the ``bin/memory-lint`` executable as a
subprocess) leaves ``memory_audit_issues`` and ``memory_events`` BYTE-FOR-BYTE
unchanged (row counts + content hash), exits 0, and prints the expected section
headers.
"""

import hashlib
import importlib.util
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from schema import init_db  # noqa: E402

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CLI_PATH = os.path.join(_REPO_ROOT, "bin", "memory-lint")


def _load_cli_module():
    """Import ``bin/memory-lint`` (no .py extension) as a module for its core fn."""
    spec = importlib.util.spec_from_loader(
        "memory_lint_cli",
        importlib.machinery.SourceFileLoader("memory_lint_cli", _CLI_PATH),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _table_fingerprint(db_path: str, table: str) -> tuple[int, str]:
    """Return (row_count, sha256 of all rows) for a table, deterministically."""
    import sqlite3

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        rows = conn.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
        digest = hashlib.sha256(repr(rows).encode("utf-8")).hexdigest()
    finally:
        conn.close()
    return count, digest


@pytest.fixture
def seeded_db(tmp_path):
    """A full-schema DB seeded to trigger every detector + a sentinel event."""
    db_path = str(tmp_path / "memory.db")
    init_db(db_path)

    import sqlite3

    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.execute("PRAGMA foreign_keys=ON")

    old = "2000-01-01T00:00:00+00:00"  # well past any staleness window
    now = "2026-06-16T00:00:00+00:00"

    # Two stale entities (updated long ago, never accessed).
    conn.execute(
        "INSERT INTO entities (id, name, entity_type, project, created_at, updated_at) "
        "VALUES (1, 'StaleSvc', 'service', 'demo', ?, ?)",
        (old, old),
    )
    conn.execute(
        "INSERT INTO entities (id, name, entity_type, project, created_at, updated_at) "
        "VALUES (2, 'OtherSvc', 'service', 'demo', ?, ?)",
        (old, old),
    )

    # Near-duplicate observations on entity 1 (Jaccard >= 0.7).
    conn.execute(
        "INSERT INTO observations (id, entity_id, content, created_at) "
        "VALUES (1, 1, 'the quick brown fox jumps over the lazy dog', ?)",
        (now,),
    )
    conn.execute(
        "INSERT INTO observations (id, entity_id, content, created_at) "
        "VALUES (2, 1, 'the quick brown fox jumps over the lazy cat', ?)",
        (now,),
    )

    # Contradicting claims on entity 1: uses vs replaces, same subject+object.
    conn.execute(
        "INSERT INTO lazy_claims (claim_id, entity_id, observation_id, subject, "
        "predicate, object_text, confidence, status, created_at, updated_at) "
        "VALUES ('c1', 1, 1, 'StaleSvc', 'uses', 'Redis', 0.8, 'candidate', ?, ?)",
        (now, now),
    )
    conn.execute(
        "INSERT INTO lazy_claims (claim_id, entity_id, observation_id, subject, "
        "predicate, object_text, confidence, status, created_at, updated_at) "
        "VALUES ('c2', 1, 1, 'StaleSvc', 'replaces', 'Redis', 0.8, 'candidate', ?, ?)",
        (now, now),
    )

    # One open audit issue (so list_memory_audit_issues returns content).
    conn.execute(
        "INSERT INTO memory_audit_issues (issue_id, issue_type, severity, "
        "subject_kind, subject_ref, details_json, status, first_detected_at, "
        "last_detected_at) "
        "VALUES ('i1', 'claim_missing_evidence', 'high', 'claim', 'c1', "
        "'{\"note\": \"seed\"}', 'open', ?, ?)",
        (now, now),
    )

    # A sentinel memory_events row — MUST remain untouched by the read-only CLI.
    conn.execute(
        "INSERT INTO memory_events (event_id, event_ts, aggregate_kind, "
        "aggregate_id, event_type, tool_name, machine_id, logical_clock) "
        "VALUES ('e1', ?, 'task', 'sentinel', 'seed_event', 'test', 'm1', 1)",
        (now,),
    )
    conn.commit()
    conn.close()
    return db_path


def test_collect_report_is_zero_write(seeded_db):
    """The CLI's core collect_report mutates neither ledger nor event log."""
    cli = _load_cli_module()

    before_issues = _table_fingerprint(seeded_db, "memory_audit_issues")
    before_events = _table_fingerprint(seeded_db, "memory_events")

    conn = cli.open_readonly(seeded_db)
    try:
        # query_only must be ON (belt + suspenders guard).
        assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
        report = cli.collect_report(conn)
    finally:
        conn.close()

    # Every detector found its seeded signal.
    assert report["audit"]["count"] == 1
    assert len(report["near_duplicates"]) == 1
    assert len(report["contradictions"]) == 1
    assert len(report["stale_entities"]) >= 1

    after_issues = _table_fingerprint(seeded_db, "memory_audit_issues")
    after_events = _table_fingerprint(seeded_db, "memory_events")

    assert before_issues == after_issues, "memory_audit_issues changed (count/hash)"
    assert before_events == after_events, "memory_events changed (count/hash)"


def test_readonly_conn_rejects_writes(seeded_db):
    """The connection physically cannot write."""
    import sqlite3

    cli = _load_cli_module()
    conn = cli.open_readonly(seeded_db)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute(
                "INSERT INTO memory_events (event_id, event_ts, aggregate_kind, "
                "aggregate_id, event_type, tool_name) "
                "VALUES ('x', '2026-06-16T00:00:00+00:00', 'task', 'x', 'x', 'x')"
            )
    finally:
        conn.close()


def test_cli_subprocess_zero_write_and_headers(seeded_db):
    """Running the executable end-to-end exits 0, prints headers, writes nothing."""
    before_issues = _table_fingerprint(seeded_db, "memory_audit_issues")
    before_events = _table_fingerprint(seeded_db, "memory_events")

    proc = subprocess.run(
        [sys.executable, _CLI_PATH, seeded_db],
        capture_output=True,
        text=True,
        cwd=_REPO_ROOT,
    )

    assert proc.returncode == 0, f"stderr:\n{proc.stderr}"
    out = proc.stdout
    for header in (
        "AUDIT ISSUES",
        "NEAR-DUPLICATES",
        "CONTRADICTIONS",
        "STALE ENTITIES",
        "SUMMARY",
    ):
        assert header in out, f"missing section header: {header}"

    after_issues = _table_fingerprint(seeded_db, "memory_audit_issues")
    after_events = _table_fingerprint(seeded_db, "memory_events")
    assert before_issues == after_issues
    assert before_events == after_events
