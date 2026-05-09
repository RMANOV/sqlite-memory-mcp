"""Tests for gbrain_export — sqlite-memory-mcp ↔ GBrain bridge.

Covers: directory layout, slug safety, folder routing by entity_type,
YAML frontmatter content, observations bullet rendering, relations
wikilink rendering with relative paths, project_filter scoping, return
counters, and graceful fallback for unknown entity_types.
"""

from __future__ import annotations

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from db_utils import now_iso
from gbrain_export import _slugify, _folder_for, export_to_gbrain_brain_repo
from schema import init_db


@pytest.fixture
def conn(tmp_path):
    db_path = str(tmp_path / "gbrain_export.db")
    init_db(db_path)
    c = sqlite3.connect(db_path, isolation_level=None)
    c.row_factory = sqlite3.Row
    yield c
    c.close()


def _add_entity(conn, name, entity_type="topic", project=None) -> int:
    cur = conn.execute(
        "INSERT INTO entities (name, entity_type, project, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (name, entity_type, project, now_iso(), now_iso()),
    )
    return cur.lastrowid


def _add_observation(conn, eid, content):
    conn.execute(
        "INSERT INTO observations (entity_id, content, created_at) VALUES (?, ?, ?)",
        (eid, content, now_iso()),
    )


def _add_relation(conn, from_id, to_id, relation_type):
    conn.execute(
        "INSERT INTO relations (from_id, to_id, relation_type, created_at) "
        "VALUES (?, ?, ?, ?)",
        (from_id, to_id, relation_type, now_iso()),
    )


# ── slug + folder helpers ──────────────────────────────────────────────


def test_slugify_replaces_spaces_and_slashes():
    assert _slugify("Hello World") == "Hello-World"
    assert _slugify("a/b\\c") == "a-b-c"
    assert _slugify("  trimmed  ") == "trimmed"
    assert _slugify("") == "entity"


def test_slugify_preserves_unicode_letters():
    assert _slugify("Имe") == "Имe"
    assert _slugify("José García") == "José-García"


def test_folder_for_known_types():
    assert _folder_for("person") == "people"
    assert _folder_for("PERSON") == "people"
    assert _folder_for("company") == "companies"
    assert _folder_for("Organization") == "companies"


def test_folder_for_unknown_types_falls_back_to_topics():
    assert _folder_for("framework") == "topics"
    assert _folder_for(None) == "topics"
    assert _folder_for("") == "topics"


# ── export structure ───────────────────────────────────────────────────


def test_export_creates_three_subfolders(conn, tmp_path):
    out = tmp_path / "brain"
    counts = export_to_gbrain_brain_repo(conn, str(out))
    for sub in ("people", "companies", "topics"):
        assert (out / sub).is_dir()
    assert counts["entities_written"] == 0
    assert counts["files_written"] == 0


def test_export_routes_entity_types_to_correct_folders(conn, tmp_path):
    _add_entity(conn, "Alice Engineer", entity_type="person")
    _add_entity(conn, "Acme Corp", entity_type="company")
    _add_entity(conn, "Reciprocal Rank Fusion", entity_type="concept")

    out = tmp_path / "brain"
    export_to_gbrain_brain_repo(conn, str(out))

    assert (out / "people" / "Alice-Engineer.md").is_file()
    assert (out / "companies" / "Acme-Corp.md").is_file()
    assert (out / "topics" / "Reciprocal-Rank-Fusion.md").is_file()


def test_export_writes_yaml_frontmatter_with_metadata(conn, tmp_path):
    _add_entity(conn, "Test Person", entity_type="person", project="alpha")
    out = tmp_path / "brain"
    export_to_gbrain_brain_repo(conn, str(out))
    content = (out / "people" / "Test-Person.md").read_text()
    assert content.startswith("---\n")
    assert "name: Test Person" in content
    assert "entity_type: person" in content
    assert "project: alpha" in content


def test_export_emits_observations_as_bullets(conn, tmp_path):
    eid = _add_entity(conn, "Observed Entity", entity_type="topic")
    _add_observation(conn, eid, "first fact")
    _add_observation(conn, eid, "second fact with details")

    out = tmp_path / "brain"
    export_to_gbrain_brain_repo(conn, str(out))
    content = (out / "topics" / "Observed-Entity.md").read_text()

    assert "## Observations" in content
    assert "- first fact" in content
    assert "- second fact with details" in content


def test_export_renders_relations_as_wikilinks_relative_paths(conn, tmp_path):
    alice = _add_entity(conn, "Alice", entity_type="person")
    bob = _add_entity(conn, "Bob", entity_type="person")
    acme = _add_entity(conn, "Acme", entity_type="company")
    _add_relation(conn, alice, bob, "knows")
    _add_relation(conn, alice, acme, "works_at")

    out = tmp_path / "brain"
    export_to_gbrain_brain_repo(conn, str(out))
    alice_md = (out / "people" / "Alice.md").read_text()

    assert "## Relations" in alice_md
    assert "- knows: [Bob](./Bob.md)" in alice_md
    assert "- works_at: [Acme](../companies/Acme.md)" in alice_md


def test_export_project_filter_scopes_correctly(conn, tmp_path):
    _add_entity(conn, "InAlpha", entity_type="topic", project="alpha")
    _add_entity(conn, "InBeta", entity_type="topic", project="beta")

    out = tmp_path / "brain"
    counts = export_to_gbrain_brain_repo(conn, str(out), project_filter="alpha")

    assert counts["entities_written"] == 1
    assert (out / "topics" / "InAlpha.md").is_file()
    assert not (out / "topics" / "InBeta.md").exists()


def test_export_counters_match_actual_writes(conn, tmp_path):
    e1 = _add_entity(conn, "E1", entity_type="topic")
    e2 = _add_entity(conn, "E2", entity_type="topic")
    _add_observation(conn, e1, "obs1")
    _add_observation(conn, e1, "obs2")
    _add_observation(conn, e2, "obs3")
    _add_relation(conn, e1, e2, "depends_on")

    out = tmp_path / "brain"
    counts = export_to_gbrain_brain_repo(conn, str(out))

    assert counts["entities_written"] == 2
    assert counts["observations_written"] == 3
    assert counts["relations_written"] == 1
    assert counts["files_written"] == 2


def test_export_skips_relations_with_dangling_target(conn, tmp_path):
    """Relations whose target was filtered out should be silently dropped."""
    a = _add_entity(conn, "Alpha", entity_type="topic", project="x")
    b = _add_entity(conn, "Beta", entity_type="topic", project="y")
    _add_relation(conn, a, b, "mentions")

    out = tmp_path / "brain"
    counts = export_to_gbrain_brain_repo(conn, str(out), project_filter="x")

    assert counts["entities_written"] == 1
    assert counts["relations_written"] == 0
    alpha_md = (out / "topics" / "Alpha.md").read_text()
    assert "## Relations" not in alpha_md


def test_export_handles_unicode_entity_names(conn, tmp_path):
    _add_entity(conn, "Иван Иванов", entity_type="person")
    _add_entity(conn, "公司名", entity_type="company")

    out = tmp_path / "brain"
    export_to_gbrain_brain_repo(conn, str(out))

    assert (out / "people" / "Иван-Иванов.md").is_file()
    assert (out / "companies" / "公司名.md").is_file()


def test_export_yaml_frontmatter_quotes_special_chars(conn, tmp_path):
    """Names with colons or quotes must round-trip through YAML safely."""
    _add_entity(conn, 'Tricky: name "with quotes"', entity_type="topic")
    out = tmp_path / "brain"
    export_to_gbrain_brain_repo(conn, str(out))
    files = list((out / "topics").glob("*.md"))
    assert len(files) == 1
    content = files[0].read_text()
    # name should be quoted because it contains both : and "
    assert 'name: "Tricky: name \\"with quotes\\""' in content


def test_export_idempotent_overwrite(conn, tmp_path):
    """Re-running export overwrites cleanly without piling up files."""
    _add_entity(conn, "Once", entity_type="topic")
    out = tmp_path / "brain"
    export_to_gbrain_brain_repo(conn, str(out))
    export_to_gbrain_brain_repo(conn, str(out))
    files = list((out / "topics").glob("*.md"))
    assert len(files) == 1


def test_export_no_entities_no_files(conn, tmp_path):
    """Empty DB still creates the three folders but writes zero files."""
    out = tmp_path / "brain"
    counts = export_to_gbrain_brain_repo(conn, str(out))
    assert counts == {
        "entities_written": 0,
        "relations_written": 0,
        "observations_written": 0,
        "files_written": 0,
    }
    assert sum(1 for _ in out.rglob("*.md")) == 0
