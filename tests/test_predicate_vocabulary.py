"""Tests for Tier-A #5 typed predicate vocabulary seed (canonical_facts).

The vocabulary is data-not-code per coordinator 2026-05-09 advice — twelve
predicates seeded into canonical_facts with fact_scope='predicate_vocabulary'
during init_db. Future code reads vocabulary from DB, not from source.
Migration is idempotent (skipped if any vocabulary row already exists).
"""

from __future__ import annotations

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from schema import init_db


_EXPECTED_PREDICATES = {
    "mentions",
    "references",
    "depends_on",
    "related_to",
    "supersedes",
    "implements",
    "contradicts",
    "works_at",
    "attended",
    "invested_in",
    "founded",
    "advises",
}


@pytest.fixture
def db_path(tmp_path):
    p = str(tmp_path / "vocab.db")
    init_db(p)
    return p


def test_vocabulary_seeded_on_first_init(db_path):
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT object_text FROM canonical_facts "
        "WHERE fact_scope = 'predicate_vocabulary'"
    ).fetchall()
    conn.close()
    found = {r[0] for r in rows}
    assert found == _EXPECTED_PREDICATES


def test_vocabulary_rows_have_required_columns(db_path):
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT fact_id, subject, predicate, object_type, confidence, "
        "validation_mode FROM canonical_facts "
        "WHERE fact_scope = 'predicate_vocabulary' LIMIT 1"
    ).fetchall()
    conn.close()
    assert rows, "vocabulary must have at least one row"
    fact_id, subject, predicate, object_type, confidence, validation_mode = rows[0]
    assert fact_id.startswith("pred-")
    assert subject == "predicate"
    assert predicate == "name"
    assert object_type == "text"
    assert confidence == 1.0
    assert validation_mode == "multi_evidence"


def test_vocabulary_seed_idempotent(tmp_path):
    """Running init_db twice must not duplicate vocabulary rows."""
    p = str(tmp_path / "idem.db")
    init_db(p)
    init_db(p)
    init_db(p)
    conn = sqlite3.connect(p)
    count = conn.execute(
        "SELECT COUNT(*) FROM canonical_facts WHERE fact_scope = 'predicate_vocabulary'"
    ).fetchone()[0]
    conn.close()
    assert count == len(_EXPECTED_PREDICATES)


def test_vocabulary_indexed_for_scope_queries(db_path):
    """idx_cf_scope index covers fact_scope lookup."""
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='canonical_facts'"
    ).fetchall()
    conn.close()
    names = {r[0] for r in rows}
    assert "idx_cf_scope" in names


def test_vocabulary_includes_gbrain_predicates(db_path):
    """The five GBrain-style predicates ship in the vocabulary."""
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT object_text FROM canonical_facts "
        "WHERE fact_scope = 'predicate_vocabulary' "
        "AND object_text IN ('works_at', 'attended', 'invested_in', 'founded', 'advises')"
    ).fetchall()
    conn.close()
    found = {r[0] for r in rows}
    assert found == {"works_at", "attended", "invested_in", "founded", "advises"}
