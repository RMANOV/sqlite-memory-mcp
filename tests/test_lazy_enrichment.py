"""Tests for lazy_enrichment.py — L2 inline claim extraction + health sweep."""

import os
import sqlite3
import sys
from datetime import UTC, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest

from lazy_enrichment import (
    auto_promote_claim,
    detect_contradictions,
    detect_near_duplicates,
    detect_stale_entities,
    extract_inline_claims,
    promote_ready_claims,
    run_health_sweep,
)


@pytest.fixture
def conn():
    """In-memory SQLite with minimal schema for testing."""
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.executescript(
        """
        CREATE TABLE entities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            entity_type TEXT NOT NULL,
            project TEXT,
            visibility TEXT DEFAULT 'private',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(entity_id, content)
        );
        CREATE TABLE lazy_claims (
            claim_id TEXT PRIMARY KEY,
            entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
            observation_id INTEGER NOT NULL REFERENCES observations(id) ON DELETE CASCADE,
            subject TEXT NOT NULL,
            predicate TEXT NOT NULL,
            object_text TEXT NOT NULL,
            confidence REAL NOT NULL,
            hit_count INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL DEFAULT 'candidate',
            promoted_to_fact_id TEXT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE canonical_facts (
            fact_id TEXT PRIMARY KEY,
            subject TEXT NOT NULL,
            predicate TEXT NOT NULL,
            object_text TEXT NOT NULL,
            object_type TEXT NOT NULL DEFAULT 'text',
            fact_scope TEXT NOT NULL,
            provenance_summary TEXT NOT NULL,
            confidence REAL NOT NULL,
            validation_mode TEXT NOT NULL,
            source_claim_id TEXT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE entity_access_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
            tool_name TEXT NOT NULL,
            accessed_at TEXT NOT NULL
        );
        CREATE TABLE relations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_id INTEGER NOT NULL,
            to_id INTEGER NOT NULL,
            relation_type TEXT NOT NULL
        );
        """
    )
    yield db
    db.close()


def _add_entity(
    conn,
    name,
    entity_type="concept",
    project=None,
    updated_at="2026-03-01T00:00:00+00:00",
):
    conn.execute(
        "INSERT INTO entities (name, entity_type, project, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (name, entity_type, project, updated_at, updated_at),
    )
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def _add_obs(conn, entity_id, content, created_at="2026-03-01T00:00:00+00:00"):
    conn.execute(
        "INSERT INTO observations (entity_id, content, created_at) VALUES (?, ?, ?)",
        (entity_id, content, created_at),
    )
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


class TestInlineExtraction:
    def test_basic_extraction(self, conn):
        eid = _add_entity(conn, "TestProject")
        obs_id = _add_obs(conn, eid, "TestProject uses Python for backend processing.")
        count = extract_inline_claims(
            conn, eid, obs_id, "TestProject uses Python for backend processing."
        )
        assert count >= 1
        claims = conn.execute(
            "SELECT * FROM lazy_claims WHERE entity_id = ?", (eid,)
        ).fetchall()
        assert len(claims) >= 1
        claim = claims[0]
        assert claim["predicate"] == "uses"
        assert claim["status"] == "candidate"

    def test_no_match(self, conn):
        eid = _add_entity(conn, "TestEntity")
        obs_id = _add_obs(conn, eid, "Just a plain observation with no patterns.")
        count = extract_inline_claims(
            conn, eid, obs_id, "Just a plain observation with no patterns."
        )
        assert count == 0

    def test_evidence_accumulation(self, conn):
        eid = _add_entity(conn, "MyApp")
        obs1_id = _add_obs(conn, eid, "MyApp uses Redis for caching.")
        obs2_id = _add_obs(conn, eid, "MyApp uses Redis for session storage.")

        extract_inline_claims(conn, eid, obs1_id, "MyApp uses Redis for caching.")
        claim1 = conn.execute(
            "SELECT confidence FROM lazy_claims WHERE subject = 'MyApp' AND object_text LIKE '%Redis%'"
        ).fetchone()
        conf1 = claim1["confidence"]

        # Second mention of same claim should boost confidence
        extract_inline_claims(conn, eid, obs2_id, "MyApp uses Redis for caching.")
        claim2 = conn.execute(
            "SELECT confidence, hit_count FROM lazy_claims "
            "WHERE subject = 'MyApp' AND object_text LIKE '%Redis for caching%'"
        ).fetchone()
        assert claim2["confidence"] > conf1
        assert claim2["hit_count"] == 2

    def test_repeated_evidence_reaches_auto_promotion_threshold(self, conn):
        eid = _add_entity(conn, "EvidenceApp")
        text = "EvidenceApp uses Redis for caching."

        for index in range(4):
            obs_id = _add_obs(conn, eid, f"evidence observation {index}")
            extract_inline_claims(conn, eid, obs_id, text)

        claim = conn.execute(
            "SELECT confidence, hit_count, status, promoted_to_fact_id "
            "FROM lazy_claims WHERE subject = 'EvidenceApp'"
        ).fetchone()
        assert claim["hit_count"] == 4
        assert claim["confidence"] >= 0.85
        assert claim["status"] == "promoted"
        assert claim["promoted_to_fact_id"]

    def test_rejection_penalty(self, conn):
        eid = _add_entity(conn, "TestTool")
        obs_id = _add_obs(conn, eid, "TestTool uses Docker for deployment.")
        extract_inline_claims(conn, eid, obs_id, "TestTool uses Docker for deployment.")

        # Manually reject the claim
        conn.execute(
            "UPDATE lazy_claims SET status = 'rejected' WHERE entity_id = ?", (eid,)
        )

        # Re-extract: should get penalty
        obs_id2 = _add_obs(conn, eid, "TestTool uses Docker for deployment again.")
        extract_inline_claims(
            conn, eid, obs_id2, "TestTool uses Docker for deployment."
        )
        claim = conn.execute(
            "SELECT confidence FROM lazy_claims WHERE entity_id = ? AND status = 'rejected'",
            (eid,),
        ).fetchone()
        assert claim["confidence"] < 0.6  # base for "uses" is 0.6, penalty applied

    def test_short_match_skipped(self, conn):
        eid = _add_entity(conn, "X")
        obs_id = _add_obs(conn, eid, "X uses Y.")
        count = extract_inline_claims(conn, eid, obs_id, "X uses Y.")
        # "X" and "Y" are single chars, should be skipped
        assert count == 0


class TestAutoPromotion:
    def test_promote_high_confidence(self, conn):
        eid = _add_entity(conn, "StableProject")
        obs_id = _add_obs(conn, eid, "StableProject depends on PostgreSQL.")

        # Insert a claim with high confidence directly
        conn.execute(
            "INSERT INTO lazy_claims (claim_id, entity_id, observation_id, "
            "subject, predicate, object_text, confidence, status, created_at, updated_at) "
            "VALUES ('test-claim', ?, ?, 'StableProject', 'depends_on', 'PostgreSQL', 0.9, "
            "'candidate', '2026-03-01', '2026-03-01')",
            (eid, obs_id),
        )

        fact_id = auto_promote_claim(conn, "test-claim", 0.9)
        assert fact_id is not None
        assert fact_id.startswith("lf-")

        # Check claim status updated
        claim = conn.execute(
            "SELECT * FROM lazy_claims WHERE claim_id = 'test-claim'"
        ).fetchone()
        assert claim["status"] == "promoted"
        assert claim["promoted_to_fact_id"] == fact_id

        # Check canonical fact created
        fact = conn.execute(
            "SELECT * FROM canonical_facts WHERE fact_id = ?", (fact_id,)
        ).fetchone()
        assert fact["subject"] == "StableProject"
        assert fact["validation_mode"] == "auto_lazy"

    def test_no_double_promote(self, conn):
        eid = _add_entity(conn, "AlreadyPromoted")
        obs_id = _add_obs(conn, eid, "AlreadyPromoted uses X.")

        conn.execute(
            "INSERT INTO lazy_claims (claim_id, entity_id, observation_id, "
            "subject, predicate, object_text, confidence, status, created_at, updated_at) "
            "VALUES ('promo-claim', ?, ?, 'AlreadyPromoted', 'uses', 'X', 0.9, "
            "'promoted', '2026-03-01', '2026-03-01')",
            (eid, obs_id),
        )

        result = auto_promote_claim(conn, "promo-claim", 0.9)
        assert result is None


class TestNearDuplicates:
    def test_detect_similar_observations(self, conn):
        eid = _add_entity(conn, "DupEntity")
        _add_obs(
            conn,
            eid,
            "The server uses PostgreSQL database for persistent storage of user data.",
        )
        _add_obs(
            conn,
            eid,
            "The server uses PostgreSQL database for persistent storage of all user data.",
        )

        dups = detect_near_duplicates(conn)
        assert len(dups) >= 1
        assert dups[0]["jaccard"] >= 0.7

    def test_no_false_positive(self, conn):
        eid = _add_entity(conn, "DiffEntity")
        _add_obs(conn, eid, "Python is great for data science and machine learning.")
        _add_obs(conn, eid, "Rust excels at systems programming with memory safety.")

        dups = detect_near_duplicates(conn)
        assert len(dups) == 0

    def test_single_obs_no_dup(self, conn):
        eid = _add_entity(conn, "SingleEntity")
        _add_obs(conn, eid, "Only one observation here.")
        dups = detect_near_duplicates(conn)
        assert len(dups) == 0


class TestContradictions:
    def test_detect_opposing_claims(self, conn):
        eid = _add_entity(conn, "ConflictEntity")
        obs_id = _add_obs(conn, eid, "Test")

        # Insert contradicting claims: "X uses Y" and "X replaces Y"
        conn.execute(
            "INSERT INTO lazy_claims (claim_id, entity_id, observation_id, subject, "
            "predicate, object_text, confidence, status, promoted_to_fact_id, "
            "created_at, updated_at) VALUES "
            "('c1', ?, ?, 'Widget', 'uses', 'Library', 0.7, "
            "'candidate', NULL, '2026-03-01', '2026-03-01')",
            (eid, obs_id),
        )
        conn.execute(
            "INSERT INTO lazy_claims (claim_id, entity_id, observation_id, subject, "
            "predicate, object_text, confidence, status, promoted_to_fact_id, "
            "created_at, updated_at) VALUES "
            "('c2', ?, ?, 'Widget', 'replaces', 'Library', 0.6, "
            "'candidate', NULL, '2026-03-01', '2026-03-01')",
            (eid, obs_id),
        )

        contras = detect_contradictions(conn)
        assert len(contras) >= 1

    def test_no_contradiction_different_objects(self, conn):
        eid = _add_entity(conn, "NoConflict")
        obs_id = _add_obs(conn, eid, "Test")

        conn.execute(
            "INSERT INTO lazy_claims (claim_id, entity_id, observation_id, subject, "
            "predicate, object_text, confidence, status, promoted_to_fact_id, "
            "created_at, updated_at) VALUES "
            "('nc1', ?, ?, 'App', 'uses', 'Redis', 0.7, "
            "'candidate', NULL, '2026-03-01', '2026-03-01')",
            (eid, obs_id),
        )
        conn.execute(
            "INSERT INTO lazy_claims (claim_id, entity_id, observation_id, subject, "
            "predicate, object_text, confidence, status, promoted_to_fact_id, "
            "created_at, updated_at) VALUES "
            "('nc2', ?, ?, 'App', 'replaces', 'Memcached', 0.6, "
            "'candidate', NULL, '2026-03-01', '2026-03-01')",
            (eid, obs_id),
        )

        contras = detect_contradictions(conn)
        assert len(contras) == 0


class TestStaleEntities:
    def test_detect_stale(self, conn):
        old_at = (datetime.now(UTC) - timedelta(days=120)).isoformat()
        _add_entity(conn, "OldEntity", updated_at=old_at)
        stale = detect_stale_entities(conn, staleness_days=90)
        assert any(s["name"] == "OldEntity" for s in stale)

    def test_fresh_entity_not_stale(self, conn):
        fresh_at = (datetime.now(UTC) - timedelta(days=30)).isoformat()
        _add_entity(conn, "FreshEntity", updated_at=fresh_at)
        stale = detect_stale_entities(conn, staleness_days=90)
        assert not any(s["name"] == "FreshEntity" for s in stale)


class TestPromoteReady:
    def test_promote_batch(self, conn):
        eid = _add_entity(conn, "BatchEntity")
        obs_id = _add_obs(conn, eid, "Test observation")

        # Insert claim above threshold
        conn.execute(
            "INSERT INTO lazy_claims (claim_id, entity_id, observation_id, subject, "
            "predicate, object_text, confidence, status, promoted_to_fact_id, "
            "created_at, updated_at) VALUES "
            "('batch1', ?, ?, 'BatchEntity', 'uses', 'FastAPI', "
            "0.9, 'candidate', NULL, '2026-03-01', '2026-03-01')",
            (eid, obs_id),
        )
        # Insert claim below threshold
        conn.execute(
            "INSERT INTO lazy_claims (claim_id, entity_id, observation_id, subject, "
            "predicate, object_text, confidence, status, promoted_to_fact_id, "
            "created_at, updated_at) VALUES "
            "('batch2', ?, ?, 'BatchEntity', 'is', 'framework', "
            "0.3, 'candidate', NULL, '2026-03-01', '2026-03-01')",
            (eid, obs_id),
        )

        promoted = promote_ready_claims(conn)
        assert len(promoted) == 1
        assert promoted[0]["claim_id"] == "batch1"


class TestHealthSweep:
    def test_full_sweep(self, conn):
        eid = _add_entity(conn, "SweepEntity", updated_at="2025-01-01T00:00:00+00:00")
        _add_obs(conn, eid, "SweepEntity uses Python for processing data effectively.")
        _add_obs(
            conn, eid, "SweepEntity uses Python for processing data very effectively."
        )

        report = run_health_sweep(conn)
        assert "summary" in report
        assert "near_duplicates" in report
        assert "contradictions" in report
        assert "stale_entities" in report
        assert "promoted" in report
        assert isinstance(report["summary"]["duplicates_found"], int)
