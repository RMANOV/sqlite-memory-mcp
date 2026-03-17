# tests/test_entity_sync.py
"""Tests for entity import/export resilience (Bug 1 + Bug 2 fix verification).

Bug 1: Entity import rolled back by task merge failure in shared transaction.
Bug 2: Remote-only entities lost during Phase 4 export (no entity merge).
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from bridge_sync_worker import _import_remote_entities, _merge_remote_entities


# ── Fixtures ─────────────────────────────────────────────────────────────


import sqlite3
from db_utils import now_iso


_SCHEMA = """
CREATE TABLE entities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    entity_type TEXT NOT NULL,
    project TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_id INTEGER NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE VIRTUAL TABLE memory_fts USING fts5(
    name, entity_type, observations_text,
    tokenize = "unicode61 remove_diacritics 2"
);
"""


@pytest.fixture
def conn(tmp_path):
    db_path = str(tmp_path / "test.db")
    c = sqlite3.connect(db_path, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    c.executescript(_SCHEMA)
    yield c
    c.close()


# ── _import_remote_entities tests ────────────────────────────────────────


class TestImportRemoteEntities:
    def test_imports_new_entities(self, conn):
        """New entities from remote are imported into local DB."""
        entities = [
            {
                "name": "Human Note 2026-03-17",
                "entityType": "note",
                "project": "shared:bridge",
                "observations": [{"content": "Test observation"}],
            }
        ]
        conn.execute("BEGIN")
        count = _import_remote_entities(conn, entities)
        conn.execute("COMMIT")

        assert count == 1
        row = conn.execute(
            "SELECT * FROM entities WHERE name = ?", ("Human Note 2026-03-17",)
        ).fetchone()
        assert row is not None
        assert row["entity_type"] == "note"

    def test_skips_existing_entities(self, conn):
        """Entities already in local DB are skipped."""
        now = now_iso()
        conn.execute(
            "INSERT INTO entities (name, entity_type, project, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("Existing Entity", "concept", "shared:bridge", now, now),
        )
        entities = [
            {"name": "Existing Entity", "entityType": "concept", "observations": []},
            {"name": "New Entity", "entityType": "concept", "observations": []},
        ]
        conn.execute("BEGIN")
        count = _import_remote_entities(conn, entities)
        conn.execute("COMMIT")

        assert count == 1  # Only the new one

    def test_bad_entity_does_not_abort_others(self, conn):
        """One malformed entity does not prevent import of subsequent entities."""
        entities = [
            # Missing "name" key — will raise KeyError
            {"entityType": "bad", "observations": []},
            # Valid entity — should still be imported
            {"name": "Good Entity", "entityType": "concept", "observations": []},
        ]
        conn.execute("BEGIN")
        count = _import_remote_entities(conn, entities)
        conn.execute("COMMIT")

        assert count == 1
        row = conn.execute(
            "SELECT * FROM entities WHERE name = ?", ("Good Entity",)
        ).fetchone()
        assert row is not None

    def test_duplicate_name_in_batch_handled(self, conn):
        """Duplicate entity names within a single batch don't crash."""
        entities = [
            {"name": "Dup Entity", "entityType": "concept", "observations": []},
            {"name": "Dup Entity", "entityType": "concept", "observations": []},
        ]
        conn.execute("BEGIN")
        count = _import_remote_entities(conn, entities)
        conn.execute("COMMIT")

        # First succeeds, second fails silently (UNIQUE constraint) or is skipped
        assert count >= 1

    def test_observations_imported(self, conn):
        """Observations attached to entities are imported."""
        entities = [
            {
                "name": "Entity With Obs",
                "entityType": "note",
                "observations": [
                    {"content": "Obs 1", "createdAt": "2026-03-17T10:00:00"},
                    {"content": "Obs 2"},
                ],
            },
        ]
        conn.execute("BEGIN")
        _import_remote_entities(conn, entities)
        conn.execute("COMMIT")

        eid = conn.execute(
            "SELECT id FROM entities WHERE name = ?", ("Entity With Obs",)
        ).fetchone()["id"]
        obs = conn.execute(
            "SELECT content FROM observations WHERE entity_id = ? ORDER BY id", (eid,)
        ).fetchall()
        assert len(obs) == 2
        assert obs[0]["content"] == "Obs 1"
        assert obs[1]["content"] == "Obs 2"


# ── _merge_remote_entities tests ─────────────────────────────────────────


class TestMergeRemoteEntities:
    def test_preserves_remote_only_entities(self):
        """Remote entities not in local export are appended."""
        local = [{"name": "Local Entity", "entityType": "concept"}]
        existing = {
            "entities": [
                {"name": "Local Entity", "entityType": "concept"},
                {"name": "Remote Only", "entityType": "note"},
            ]
        }
        result = _merge_remote_entities(local, existing)

        names = {e["name"] for e in result}
        assert "Remote Only" in names
        assert "Local Entity" in names
        assert len(result) == 2

    def test_no_duplicates_on_merge(self):
        """Entities present in both local and remote are not duplicated."""
        local = [
            {"name": "Shared Entity", "entityType": "concept"},
            {"name": "Local Only", "entityType": "note"},
        ]
        existing = {
            "entities": [
                {"name": "Shared Entity", "entityType": "concept"},
                {"name": "Remote Only", "entityType": "note"},
            ]
        }
        result = _merge_remote_entities(local, existing)

        names = [e["name"] for e in result]
        assert names.count("Shared Entity") == 1
        assert len(result) == 3

    def test_empty_remote_entities(self):
        """No remote entities — local list unchanged."""
        local = [{"name": "A", "entityType": "x"}]
        existing = {"entities": []}
        result = _merge_remote_entities(local, existing)
        assert len(result) == 1

    def test_missing_entities_key(self):
        """No 'entities' key in existing data — gracefully handled."""
        local = [{"name": "A", "entityType": "x"}]
        existing = {}
        result = _merge_remote_entities(local, existing)
        assert len(result) == 1

    def test_remote_entity_without_name_skipped(self):
        """Remote entity missing 'name' field is skipped by the guard."""
        local = [{"name": "A", "entityType": "x"}]
        existing = {
            "entities": [
                {"entityType": "broken"},  # No name — skipped
                {"name": "Valid Remote", "entityType": "note"},
            ]
        }
        result = _merge_remote_entities(local, existing)
        names = {e["name"] for e in result}
        assert "Valid Remote" in names
        assert "A" in names
        assert len(result) == 2  # broken is skipped by `if re.get("name")` guard

    def test_win_entities_survive_fedora_push(self):
        """Reproduces the exact bug: Win pushes 2 entities, fedora push must keep them."""
        # Fedora's local export (13 entities, none from Win)
        fedora_local = [{"name": f"FedoraEntity-{i}", "entityType": "concept"} for i in range(13)]

        # shared.json as written by Win (15 entities = 13 fedora + 2 new)
        win_shared = {
            "entities": fedora_local + [
                {"name": "Human Note 2026-03-17", "entityType": "note",
                 "observations": [{"content": "Human note content"}]},
                {"name": "MCP Config Scope Fix 2026-03-17", "entityType": "fix",
                 "observations": [{"content": "Config scope fix"}]},
            ]
        }

        result = _merge_remote_entities(fedora_local, win_shared)

        names = {e["name"] for e in result}
        assert "Human Note 2026-03-17" in names
        assert "MCP Config Scope Fix 2026-03-17" in names
        assert len(result) == 15
