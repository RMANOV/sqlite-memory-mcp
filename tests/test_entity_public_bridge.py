"""Public entity transport must survive a fresh machine without widening access."""

import json
import sqlite3

import pytest

from db_utils import (
    export_entities_index,
    export_entity_files,
    import_bridge_entities_and_relations,
    import_remote_bridge_data,
    load_remote_entities_for_import,
)
from schema import init_db


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "memory.db"
    init_db(str(path))
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    yield conn
    conn.close()


def entity(name, *, project="shared:bridge", visibility=None, updated="2026-08-01T00:00:00Z"):
    result = {
        "name": name,
        "entityType": "concept",
        "project": project,
        "observations": [{"content": name + " fact", "createdAt": "2026-07-01T00:00:00Z"}],
        "createdAt": "2026-07-01T00:00:00Z",
        "updatedAt": updated,
    }
    if visibility is not None:
        result["visibility"] = visibility
    return result


def test_fresh_import_preserves_index_visibility_and_public_only_entity(db, tmp_path):
    bridge = tmp_path / "bridge"
    (bridge / "entities").mkdir(parents=True)
    shared = entity("Shared published fact")
    private = entity("Private shared fact")
    for identifier, item in [(154, shared), (155, private)]:
        (bridge / "entities" / f"{identifier}.json").write_text(
            json.dumps({"id": identifier, **item}), encoding="utf-8"
        )
    (bridge / "entities_index.json").write_text(json.dumps({"entities": [
        {"id": 154, **shared, "visibility": "public"},
        {"id": 155, **private, "visibility": "private"},
    ]}), encoding="utf-8")
    public_only = entity("Public-only fact", project="research")
    relation = {"from": shared["name"], "to": public_only["name"],
                "relationType": "supports", "createdAt": "2026-07-02T00:00:00Z"}
    payload = {"public_knowledge": {"entities": [shared, public_only]}, "relations": [relation]}

    result = import_remote_bridge_data(db, str(bridge), payload)
    assert result["entities"] == 3
    assert result["relations"] == 1
    rows = {row["name"]: dict(row) for row in db.execute("SELECT * FROM entities")}
    assert rows[shared["name"]]["visibility"] == "public"
    assert rows[private["name"]]["visibility"] == "private"
    assert rows[public_only["name"]]["visibility"] == "public"
    assert rows[public_only["name"]]["project"] == "research"
    assert rows[public_only["name"]]["created_at"] == public_only["createdAt"]
    assert rows[public_only["name"]]["updated_at"] == public_only["updatedAt"]
    assert db.execute("SELECT count(*) FROM observations").fetchone()[0] == 3
    assert import_remote_bridge_data(db, str(bridge), payload)["entities"] == 0
    assert db.execute("SELECT count(*) FROM observations").fetchone()[0] == 3

    _, exported = export_entity_files(db, str(bridge))
    export_entities_index(db, str(bridge), rows=exported)
    reloaded = {row["name"]: row for row in load_remote_entities_for_import(str(bridge), {})}
    assert reloaded[shared["name"]]["visibility"] == "public"
    assert reloaded[private["name"]]["visibility"] == "private"
    assert reloaded[shared["name"]]["observations"] == shared["observations"]


@pytest.mark.parametrize("remote_updated", ["2026-07-31T23:59:59Z", "2026-08-01T00:00:00Z"])
def test_stale_or_tied_public_snapshot_cannot_override_local_private(db, remote_updated):
    local = entity("Privacy choice", visibility="private")
    import_bridge_entities_and_relations(db, [local], [])
    remote = entity("Privacy choice", visibility="public", updated=remote_updated)
    import_bridge_entities_and_relations(db, [remote], [])
    row = db.execute("SELECT visibility, updated_at FROM entities").fetchone()
    assert tuple(row) == ("private", local["updatedAt"])


def test_strictly_newer_visibility_change_and_revocation_round_trip(db):
    local = entity("Published choice", visibility="private")
    import_bridge_entities_and_relations(db, [local], [])
    remote = entity("Published choice", visibility="public", updated="2026-08-02T00:00:00Z")
    import_bridge_entities_and_relations(db, [remote], [])
    assert db.execute("SELECT visibility FROM entities").fetchone()[0] == "public"
    revoked = entity("Published choice", visibility="private", updated="2026-08-03T00:00:00Z")
    import_bridge_entities_and_relations(db, [revoked], [])
    import_bridge_entities_and_relations(db, [remote], [])
    assert db.execute("SELECT visibility FROM entities").fetchone()[0] == "private"


def test_explicit_private_shared_entity_wins_over_stale_public_projection(db, tmp_path):
    private = entity("Revoked publication", visibility="private")
    old_public = entity("Revoked publication", updated="2026-07-01T00:00:00Z")
    payload = {"entities": [private], "public_knowledge": {"entities": [old_public]}}
    import_remote_bridge_data(db, str(tmp_path), payload)
    assert db.execute("SELECT visibility FROM entities").fetchone()[0] == "private"


def test_public_only_legacy_payload_is_imported_without_entity_manifest(db, tmp_path):
    public = entity("Legacy published fact", project="research")
    result = import_remote_bridge_data(db, str(tmp_path), {"public_knowledge": {"entities": [public]}})
    assert result["entities"] == 1
    assert tuple(db.execute("SELECT name, visibility FROM entities").fetchone()) == (public["name"], "public")
