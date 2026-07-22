"""Tests for tools.gbrain_bridge — sqlite-memory-mcp ↔ GBrain bridge.

Covers BOTH directions:
- export: directory layout, slug safety (Cyrillic/CJK/special chars),
  folder routing by entity_type, YAML frontmatter, observation bullets,
  relations as wikilinks with relative paths, project_filter scoping.
- import: YAML frontmatter parsing, observation extraction, relation
  parsing with target resolution, idempotency on existing entities,
  malformed-file skip, project_default fallback.
- roundtrip: export → fresh DB → import → assert KG state restored.
"""

from __future__ import annotations

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from db_utils import now_iso
from tools.gbrain_bridge.gbrain_export import (
    _slugify,
    _folder_for,
    export_to_gbrain_brain_repo,
)
from tools.gbrain_bridge.gbrain_import import import_from_gbrain_brain_repo
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


def test_slugify_rejects_windows_forbidden_and_device_names():
    assert _slugify('Tricky: name "with quotes"') == "Tricky-name-with-quotes"
    assert _slugify("CON") == "_CON"
    assert _slugify("lpt1.txt") == "_lpt1.txt"


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


# ── import side ─────────────────────────────────────────────────────────


def _write_brain_file(
    base,
    sub: str,
    slug: str,
    *,
    name: str,
    entity_type: str,
    observations: list[str] | None = None,
    relations: list[tuple[str, str, str]] | None = None,
    project: str | None = None,
):
    folder = base / sub
    folder.mkdir(parents=True, exist_ok=True)
    lines = ["---", f"name: {name}", f"entity_type: {entity_type}"]
    if project:
        lines.append(f"project: {project}")
    lines.extend(["---", "", f"# {name}", ""])
    if observations:
        lines.extend(["## Observations", ""])
        for obs in observations:
            lines.append(f"- {obs}")
        lines.append("")
    if relations:
        lines.extend(["## Relations", ""])
        for predicate, target, relpath in relations:
            lines.append(f"- {predicate}: [{target}]({relpath})")
        lines.append("")
    (folder / f"{slug}.md").write_text("\n".join(lines), encoding="utf-8")


def test_import_creates_entity_with_observations(conn, tmp_path):
    base = tmp_path / "brain"
    _write_brain_file(
        base,
        "people",
        "Alice",
        name="Alice",
        entity_type="person",
        observations=["loves SQLite", "shipped reflect_v1.0"],
    )
    counts = import_from_gbrain_brain_repo(conn, str(base))
    assert counts["entities_created"] == 1
    assert counts["observations_inserted"] == 2

    eid = conn.execute("SELECT id FROM entities WHERE name = ?", ("Alice",)).fetchone()[
        0
    ]
    obs = [
        r[0]
        for r in conn.execute(
            "SELECT content FROM observations WHERE entity_id = ?", (eid,)
        )
    ]
    assert sorted(obs) == ["loves SQLite", "shipped reflect_v1.0"]


def test_import_resolves_relations_within_input_set(conn, tmp_path):
    base = tmp_path / "brain"
    _write_brain_file(
        base,
        "people",
        "Alice",
        name="Alice",
        entity_type="person",
        relations=[("works_at", "Acme", "../companies/Acme.md")],
    )
    _write_brain_file(base, "companies", "Acme", name="Acme", entity_type="company")
    counts = import_from_gbrain_brain_repo(conn, str(base))
    assert counts["entities_created"] == 2
    assert counts["relations_inserted"] == 1
    assert counts["relations_skipped"] == 0


def test_import_skips_relations_with_missing_target(conn, tmp_path):
    base = tmp_path / "brain"
    _write_brain_file(
        base,
        "people",
        "Alice",
        name="Alice",
        entity_type="person",
        relations=[("knows", "Ghost", "../topics/Ghost.md")],
    )
    counts = import_from_gbrain_brain_repo(conn, str(base))
    assert counts["entities_created"] == 1
    assert counts["relations_inserted"] == 0
    assert counts["relations_skipped"] == 1


def test_import_idempotent_on_existing_entity(conn, tmp_path):
    base = tmp_path / "brain"
    _write_brain_file(
        base,
        "topics",
        "Existing",
        name="Existing",
        entity_type="topic",
        observations=["fact1"],
    )
    counts1 = import_from_gbrain_brain_repo(conn, str(base))
    counts2 = import_from_gbrain_brain_repo(conn, str(base))
    assert counts1["entities_created"] == 1
    assert counts2["entities_created"] == 0
    assert counts2["entities_skipped"] >= 1
    obs_count = conn.execute(
        "SELECT COUNT(*) FROM observations o JOIN entities e ON o.entity_id = e.id "
        "WHERE e.name = 'Existing'"
    ).fetchone()[0]
    assert obs_count == 1, "idempotent run must not duplicate observations"


def test_import_skips_malformed_files(conn, tmp_path):
    base = tmp_path / "brain"
    (base / "topics").mkdir(parents=True)
    (base / "topics" / "no_frontmatter.md").write_text(
        "Just some text", encoding="utf-8"
    )
    (base / "topics" / "bad_yaml.md").write_text(
        "---\nthis isn't kv\n---\n# X\n", encoding="utf-8"
    )
    counts = import_from_gbrain_brain_repo(conn, str(base))
    assert counts["files_skipped"] >= 1
    assert counts["entities_created"] == 0


def test_import_applies_project_default_when_missing(conn, tmp_path):
    base = tmp_path / "brain"
    _write_brain_file(
        base, "topics", "NoProject", name="NoProject", entity_type="topic"
    )
    import_from_gbrain_brain_repo(conn, str(base), project_default="alpha")
    project = conn.execute(
        "SELECT project FROM entities WHERE name = 'NoProject'"
    ).fetchone()[0]
    assert project == "alpha"


def test_import_returns_zero_for_missing_dir(conn, tmp_path):
    counts = import_from_gbrain_brain_repo(conn, str(tmp_path / "does_not_exist"))
    assert counts["entities_created"] == 0
    assert counts["files_parsed"] == 0


# ── roundtrip ───────────────────────────────────────────────────────────


def test_roundtrip_export_then_import_preserves_kg(conn, tmp_path):
    """Export → fresh DB → import → all entities/observations/relations restored."""
    alice = _add_entity(conn, "Alice", entity_type="person")
    bob = _add_entity(conn, "Bob", entity_type="person")
    acme = _add_entity(conn, "Acme", entity_type="company")
    _add_observation(conn, alice, "ships sqlite-memory-mcp")
    _add_observation(conn, alice, "knows convergent evolution")
    _add_observation(conn, bob, "designs MCP servers")
    _add_observation(conn, acme, "based in SF")
    _add_relation(conn, alice, bob, "knows")
    _add_relation(conn, alice, acme, "works_at")
    _add_relation(conn, bob, acme, "works_at")

    out = tmp_path / "brain"
    export_counts = export_to_gbrain_brain_repo(conn, str(out))
    assert export_counts["entities_written"] == 3
    assert export_counts["relations_written"] == 3

    fresh_db = str(tmp_path / "fresh.db")
    init_db(fresh_db)
    fresh = sqlite3.connect(fresh_db, isolation_level=None)
    fresh.row_factory = sqlite3.Row
    try:
        import_counts = import_from_gbrain_brain_repo(fresh, str(out))
        assert import_counts["entities_created"] == 3
        assert import_counts["observations_inserted"] == 4
        assert import_counts["relations_inserted"] == 3
        assert import_counts["relations_skipped"] == 0

        names = {r[0] for r in fresh.execute("SELECT name FROM entities")}
        assert names == {"Alice", "Bob", "Acme"}
        rel_count = fresh.execute("SELECT COUNT(*) FROM relations").fetchone()[0]
        assert rel_count == 3
    finally:
        fresh.close()


def test_roundtrip_preserves_unicode_and_special_chars(conn, tmp_path):
    """Bulgarian + emoji + quotes round-trip cleanly."""
    eid = _add_entity(conn, "Иван 🔥", entity_type="person")
    _add_observation(conn, eid, 'писал е код "the right way"')

    out = tmp_path / "brain"
    export_to_gbrain_brain_repo(conn, str(out))

    fresh_db = str(tmp_path / "fresh_unicode.db")
    init_db(fresh_db)
    fresh = sqlite3.connect(fresh_db, isolation_level=None)
    fresh.row_factory = sqlite3.Row
    try:
        import_from_gbrain_brain_repo(fresh, str(out))
        row = fresh.execute("SELECT name FROM entities").fetchone()
        assert row is not None
        assert row[0] == "Иван 🔥"
        obs = fresh.execute(
            "SELECT content FROM observations WHERE entity_id = "
            "(SELECT id FROM entities WHERE name = ?)",
            ("Иван 🔥",),
        ).fetchone()
        assert obs is not None
        assert obs[0] == 'писал е код "the right way"'
    finally:
        fresh.close()
