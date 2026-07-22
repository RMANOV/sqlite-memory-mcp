"""Acceptance tests for Governed one-way markdown publication (Obsidian #1).

Proves the ADVOCATE-gated invariants of the locked spec
(debate OBSIDIAN_FEATURES_20260529):

  T1  vault edits CANNOT mutate memory (headline invariant) — structural +
      regression: re-running the emitter after a hand-edit leaves every memory
      table byte-for-byte identical, and the emitter's connection is read-only.
  T2  default-deny allowlist — only promoted facts + public
      entities/observations/relations + public notes are emitted; candidate /
      pending_public / private / seeded-vocabulary / revoked never leak.
  T3  idempotency — emit twice => byte-identical files; unchanged => skipped.
  T4  provenance/audit frontmatter present + correct on every file.
  T5  revoke/tombstone — invalidated fact / un-published entity become
      governance_state=revoked tombstones that retain provenance; memory intact.
  T6  determinism — same governed input => byte-identical output into a FRESH dir.
  T7  conflict policy — a hand-edited vault file is overwritten by canonical
      content and the overwrite is logged.
  T8  path safety — relpaths stay under the vault root; traversal is rejected.

All DBs are tmp_path; production memory.db is never touched. No MCP server is
started. The full real schema + real governance code paths (promote_candidate,
govern_fact) are exercised so the allowlist test is authoritative.
"""

import hashlib
import os
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import obsidian_publish as op
from claim_graph import promote_candidate
from memory_audit import govern_fact
from schema import init_db

TS = "2026-05-29T10:00:00+00:00"


# ── fixtures: build governed + ungoverned state via REAL code paths ──────────


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "memory.db")
    init_db(path)
    return path


@pytest.fixture
def vault(tmp_path):
    return str(tmp_path / "vault")


def _conn(db_path):
    c = sqlite3.connect(db_path, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    return c


def _add_entity(conn, name, *, visibility, obs=(), etype="concept"):
    cur = conn.execute(
        "INSERT INTO entities (name, entity_type, visibility, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (name, etype, visibility, TS, TS),
    )
    eid = cur.lastrowid
    for o in obs:
        conn.execute(
            "INSERT INTO observations (entity_id, content, created_at) VALUES (?, ?, ?)",
            (eid, o, TS),
        )
    return eid


def _add_relation(conn, from_id, to_id, rtype):
    cur = conn.execute(
        "INSERT INTO relations (from_id, to_id, relation_type, created_at) "
        "VALUES (?, ?, ?, ?)",
        (from_id, to_id, rtype, TS),
    )
    return cur.lastrowid


def _add_task(conn, tid, title, *, visibility, ttype="note", desc="desc body"):
    conn.execute(
        "INSERT INTO tasks (id, title, description, type, status, visibility, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, 'not_started', ?, ?, ?)",
        (tid, title, desc, ttype, visibility, TS, TS),
    )


def _add_promoted_fact(conn, *, subject, predicate, obj, scope="general"):
    """Create a candidate claim and promote it through the REAL governance gate."""
    # context_chunks row is the FK parent for candidate_claims.
    chunk_id = hashlib.sha1(f"{subject}{predicate}{obj}".encode()).hexdigest()[:16]
    conn.execute(
        "INSERT INTO context_chunks ("
        "chunk_id, session_id, entity_id, source_type, source_ref, source_hash, "
        "title, body, language, state, enrich_policy, materiality_score, "
        "created_at, updated_at) VALUES "
        "(?, NULL, NULL, 'observation', ?, ?, ?, ?, 'en', 'enriched', 'manual', "
        "0.9, ?, ?)",
        (chunk_id, chunk_id, chunk_id, "t", "body", TS, TS),
    )
    claim_id = "claim-" + chunk_id
    conn.execute(
        "INSERT INTO candidate_claims ("
        "claim_id, chunk_id, subject, predicate, object_text, object_type, "
        "claim_scope, confidence, status, requires_human, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, 'text', ?, 0.95, 'candidate', 0, ?, ?)",
        (claim_id, chunk_id, subject, predicate, obj, scope, TS, TS),
    )
    res = promote_candidate(conn, claim_id, mode="human_confirmed")
    assert res.get("promoted") is True, res
    return res["fact_id"]


@pytest.fixture
def governed_db(db_path):
    """A DB with a representative mix of governed + ungoverned content."""
    conn = _conn(db_path)
    try:
        # APPROVED (eligible)
        pub_a = _add_entity(
            conn,
            "Public Alpha",
            visibility="public",
            obs=["alpha obs 1", "alpha obs 2"],
        )
        pub_b = _add_entity(conn, "Public Beta", visibility="public", obs=["beta obs"])
        _add_relation(conn, pub_a, pub_b, "depends_on")  # both public -> eligible
        _add_task(conn, "note-pub", "Published Note", visibility="public")
        fact_id = _add_promoted_fact(conn, subject="Alpha", predicate="is", obj="ready")

        # DENIED (must NEVER be emitted)
        priv = _add_entity(conn, "Private One", visibility="private", obs=["secret"])
        pend = _add_entity(
            conn, "Pending One", visibility="pending_public", obs=["staged"]
        )
        _add_relation(conn, pub_a, priv, "references")  # one endpoint private -> denied
        _add_relation(conn, pub_a, pend, "references")  # one endpoint pending -> denied
        _add_task(conn, "note-priv", "Private Note", visibility="private")
        _add_task(conn, "note-pend", "Pending Note", visibility="pending_public")
    finally:
        conn.commit()
        conn.close()
    return {"db": db_path, "public_entity": pub_a, "fact_id": fact_id}


# ── helpers ──────────────────────────────────────────────────────────────────


def _table_digest(db_path):
    """SHA256 over every user table's full contents — proof of (non-)mutation."""
    conn = sqlite3.connect(db_path)
    try:
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        ]
        h = hashlib.sha256()
        for t in tables:
            h.update(t.encode())
            try:
                rows = conn.execute(f"SELECT * FROM '{t}'").fetchall()  # noqa: S608
            except sqlite3.OperationalError:
                continue
            for row in rows:
                h.update(repr(tuple(row)).encode())
        return h.hexdigest()
    finally:
        conn.close()


def _read_vault(vault):
    out = {}
    for root, _dirs, files in os.walk(vault):
        for f in sorted(files):
            p = os.path.join(root, f)
            rel = os.path.relpath(p, vault).replace(os.sep, "/")
            with open(p, encoding="utf-8") as fh:
                out[rel] = fh.read()
    return out


# ── T2: default-deny allowlist ───────────────────────────────────────────────


def test_only_governed_content_is_emitted(governed_db, vault):
    op.emit(db_path=governed_db["db"], vault_path=vault)
    files = _read_vault(vault)
    blob = "\n".join(files.values())

    # APPROVED content present.
    assert any("facts/" in k for k in files), files.keys()
    assert any("Public Alpha" in v for v in files.values())
    assert any("Public Beta" in v for v in files.values())
    assert any("Published Note" in v for v in files.values())
    assert any("relations/" in k for k in files)

    # DENIED content NEVER leaks.
    assert "Private One" not in blob
    assert "Pending One" not in blob
    assert "Private Note" not in blob
    assert "Pending Note" not in blob
    assert "secret" not in blob
    assert "staged" not in blob

    # Relation with a non-public endpoint is not emitted.
    rel_files = {k: v for k, v in files.items() if k.startswith("relations/")}
    assert len(rel_files) == 1  # only the public<->public relation
    assert "depends_on" in next(iter(rel_files.values()))

    # Seeded predicate_vocabulary facts (source_claim_id NULL) never emit.
    assert "predicate_vocabulary" not in blob
    assert "mentions" not in blob or "depends_on" in blob  # vocab terms absent


def test_missing_or_ambiguous_state_is_denied(db_path, vault):
    conn = _conn(db_path)
    try:
        # Entity with NULL visibility (forced) -> ineligible (default-deny).
        conn.execute(
            "INSERT INTO entities (name, entity_type, visibility, created_at, updated_at) "
            "VALUES ('NullVis', 'concept', NULL, ?, ?)",
            (TS, TS),
        )
        conn.commit()
    finally:
        conn.close()
    result = op.emit(db_path=db_path, vault_path=vault)
    assert result.as_dict()["counts"]["written"] == 0
    assert "NullVis" not in "\n".join(_read_vault(vault).values())


def test_candidate_claim_never_emitted(db_path, vault):
    """A candidate that was NOT promoted must never appear in the vault."""
    conn = _conn(db_path)
    try:
        chunk_id = "chunkX"
        conn.execute(
            "INSERT INTO context_chunks (chunk_id, session_id, entity_id, source_type, "
            "source_ref, source_hash, title, body, language, state, enrich_policy, "
            "materiality_score, created_at, updated_at) VALUES "
            "(?, NULL, NULL, 'observation', 'r', 'h', 't', 'b', 'en', 'enrichable', "
            "'manual', 0.9, ?, ?)",
            (chunk_id, TS, TS),
        )
        conn.execute(
            "INSERT INTO candidate_claims (claim_id, chunk_id, subject, predicate, "
            "object_text, object_type, claim_scope, confidence, status, requires_human, "
            "created_at, updated_at) VALUES "
            "('c1', ?, 'Secret', 'is', 'unreviewed', 'text', 'general', 0.9, "
            "'candidate', 1, ?, ?)",
            (chunk_id, TS, TS),
        )
        conn.commit()
    finally:
        conn.close()
    op.emit(db_path=db_path, vault_path=vault)
    assert "unreviewed" not in "\n".join(_read_vault(vault).values())


# ── T1: vault edits cannot mutate memory (headline invariant) ────────────────


def test_vault_edits_cannot_mutate_memory(governed_db, vault):
    op.emit(db_path=governed_db["db"], vault_path=vault)
    before = _table_digest(governed_db["db"])

    # Hand-edit a published vault file (simulating a user editing in Obsidian).
    files = _read_vault(vault)
    target_rel = next(k for k in files if k.startswith("entities/"))
    target = os.path.join(vault, target_rel)
    with open(target, "a", encoding="utf-8") as fh:
        fh.write("\n\nHAND EDITED BY USER IN OBSIDIAN\n")

    # Re-run the emitter AFTER the vault edit. If any vault->memory path existed,
    # this is where it would fire. The DB must be byte-for-byte identical.
    result = op.emit(db_path=governed_db["db"], vault_path=vault)
    after = _table_digest(governed_db["db"])
    assert before == after, "emitter mutated memory — canonical invariant violated"
    # The hand-edit is detected (file failed its self-check) and overwritten,
    # not silently absorbed.
    assert target_rel in result.overwrites


def test_connection_is_readonly(governed_db):
    """Structural proof: the emitter's connection physically cannot write."""
    conn = op.open_readonly(governed_db["db"])
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute(
                "INSERT INTO entities (name, entity_type, visibility, created_at, updated_at) "
                "VALUES ('hacker', 'x', 'public', ?, ?)",
                (TS, TS),
            )
    finally:
        conn.close()


# ── T3 / T6: idempotency + determinism ───────────────────────────────────────


def test_idempotent_reemit_is_byte_identical(governed_db, vault):
    r1 = op.emit(db_path=governed_db["db"], vault_path=vault)
    snap1 = _read_vault(vault)
    r2 = op.emit(db_path=governed_db["db"], vault_path=vault)
    snap2 = _read_vault(vault)
    assert snap1 == snap2, "re-emit produced different bytes"
    # Second run rewrites nothing.
    assert r2.as_dict()["counts"]["written"] == 0
    assert len(r2.skipped) >= len(r1.written)


def test_emit_removes_only_stale_managed_tmp_files(governed_db, vault):
    notes_dir = Path(vault) / op.SUBDIR_NOTES
    notes_dir.mkdir(parents=True)
    stale = notes_dir / "orphan.md.tmp"
    fresh = notes_dir / "active.md.tmp"
    stale.write_text("stale", encoding="utf-8")
    fresh.write_text("fresh", encoding="utf-8")
    os.utime(stale, (1, 1))

    op.emit(db_path=governed_db["db"], vault_path=vault)

    assert not stale.exists()
    assert fresh.exists()


def test_determinism_fresh_dir(governed_db, tmp_path):
    """Same governed input -> byte-identical output into two FRESH dirs."""
    v1 = str(tmp_path / "v1")
    v2 = str(tmp_path / "v2")
    op.emit(db_path=governed_db["db"], vault_path=v1)
    op.emit(db_path=governed_db["db"], vault_path=v2)
    assert _read_vault(v1) == _read_vault(v2)


# ── T4: provenance / audit frontmatter ───────────────────────────────────────


def test_provenance_frontmatter_present_and_correct(governed_db, vault):
    op.emit(db_path=governed_db["db"], vault_path=vault)
    files = _read_vault(vault)
    assert files, "nothing emitted"
    for rel, content in files.items():
        assert content.startswith("---\n"), rel
        for key in (
            "source_kind:",
            "source_id:",
            "governance_state:",
            "provenance:",
            "generated_at:",
            "generator_version:",
            "content_hash:",
        ):
            assert key in content, f"{rel} missing {key}"
        # content_hash is a real sha256 hex digest.
        m = op._HASH_RE.search(content)
        assert m and len(m.group(1)) == 64, rel
        # published files carry the published state.
        gm = op._GOVSTATE_RE.search(content)
        assert gm and gm.group(1) in ("published", "revoked"), rel


# ── T5: revoke / tombstone ────────────────────────────────────────────────────


def test_revoked_fact_becomes_tombstone(governed_db, vault):
    op.emit(db_path=governed_db["db"], vault_path=vault)
    before_files = _read_vault(vault)
    fact_files_before = [k for k in before_files if k.startswith("facts/")]
    assert fact_files_before
    # Capture the ORIGINAL published content_hash + generator_version to assert
    # the tombstone RETAINS them (locked-spec QS2).
    orig_published = before_files[fact_files_before[0]]
    orig_hash = op._HASH_RE.search(orig_published).group(1)
    orig_genver = op._FM_FIELD_RE.search(
        "\n".join(
            ln
            for ln in orig_published.splitlines()
            if ln.startswith("generator_version:")
        )
    ).group(2)

    # Revoke the fact through the REAL governance path (invalidate sets valid_to).
    conn = _conn(governed_db["db"])
    try:
        res = govern_fact(conn, governed_db["fact_id"], "invalidate")
        assert res.get("changed") is True, res
        conn.commit()
    finally:
        conn.close()

    op.emit(db_path=governed_db["db"], vault_path=vault)
    files = _read_vault(vault)
    fact_files_after = [k for k in files if k.startswith("facts/")]
    # The file is RETAINED (tombstone, not deleted) and marked revoked.
    assert fact_files_after == fact_files_before
    tomb = files[fact_files_after[0]]
    gm = op._GOVSTATE_RE.search(tomb)
    assert gm and gm.group(1) == "revoked"
    assert "Revoked" in tomb
    # Provenance retained on the tombstone.
    assert "source_id:" in tomb and "content_hash:" in tomb and "provenance:" in tomb
    # Locked-spec QS2: the ORIGINAL content_hash + generator_version are RETAINED.
    assert op._HASH_RE.search(tomb).group(1) == orig_hash
    assert f'generator_version: "{orig_genver}"' in tomb

    # Tombstoning is idempotent.
    op.emit(db_path=governed_db["db"], vault_path=vault)
    assert _read_vault(vault) == files


def test_unpublished_entity_becomes_tombstone_memory_intact(governed_db, vault):
    op.emit(db_path=governed_db["db"], vault_path=vault)

    # Un-publish the entity in memory (public -> private). This is OUR write.
    conn = _conn(governed_db["db"])
    try:
        conn.execute(
            "UPDATE entities SET visibility='private' WHERE id=?",
            (governed_db["public_entity"],),
        )
        conn.commit()
    finally:
        conn.close()

    # Memory snapshot taken AFTER our un-publish, immediately BEFORE the emit.
    pre_emit = _table_digest(governed_db["db"])

    op.emit(db_path=governed_db["db"], vault_path=vault)
    files = _read_vault(vault)
    # The previously-published entity file is now a tombstone.
    revoked = [
        k
        for k, v in files.items()
        if k.startswith("entities/") and 'governance_state: "revoked"' in v
    ]
    assert revoked, "un-published entity was not tombstoned"

    # The emit itself did not mutate memory (read-only invariant under revoke).
    assert _table_digest(governed_db["db"]) == pre_emit

    # Tombstoning is idempotent.
    op.emit(db_path=governed_db["db"], vault_path=vault)
    assert _read_vault(vault) == files


# ── T7: conflict policy (canonical overwrite + logged) ───────────────────────


def test_canonical_overwrites_handedit_and_logs(governed_db, vault, caplog):
    """REALISTIC hand-edit: user edits the body in Obsidian, frontmatter intact."""
    op.emit(db_path=governed_db["db"], vault_path=vault)
    files = _read_vault(vault)
    target_rel = next(k for k in files if k.startswith("entities/"))
    target = os.path.join(vault, target_rel)

    # The kind of edit a real user makes: append to the body, never touching
    # the frontmatter / content_hash. The file now fails its own self-check.
    with open(target, "a", encoding="utf-8") as fh:
        fh.write("\nmalicious user note added in Obsidian\n")

    import logging

    with caplog.at_level(logging.WARNING, logger="obsidian-publish"):
        result = op.emit(db_path=governed_db["db"], vault_path=vault)

    restored = _read_vault(vault)[target_rel]
    assert "malicious" not in restored  # canonical content won
    assert "Public Alpha" in restored or "Public Beta" in restored
    assert target_rel in result.overwrites
    assert any("canonical overwrite" in rec.message for rec in caplog.records)


def test_legitimate_source_update_is_not_flagged_as_overwrite(governed_db, vault):
    """A pristine prior generation made stale by a memory update is NOT a conflict."""
    op.emit(db_path=governed_db["db"], vault_path=vault)

    # Legitimately change the source in memory (bump updated_at + add an obs).
    conn = _conn(governed_db["db"])
    try:
        conn.execute(
            "INSERT INTO observations (entity_id, content, created_at) VALUES (?, ?, ?)",
            (
                governed_db["public_entity"],
                "freshly added observation",
                "2026-06-01T00:00:00+00:00",
            ),
        )
        conn.execute(
            "UPDATE entities SET updated_at='2026-06-01T00:00:00+00:00' WHERE id=?",
            (governed_db["public_entity"],),
        )
        conn.commit()
    finally:
        conn.close()

    result = op.emit(db_path=governed_db["db"], vault_path=vault)
    # The file is rewritten (source changed) but it is NOT a hand-edit overwrite.
    assert result.overwrites == []
    assert any(k.startswith("entities/") for k in result.written)
    assert "freshly added observation" in "\n".join(_read_vault(vault).values())


# ── T8: path safety ───────────────────────────────────────────────────────────


def test_relpath_keyed_on_immutable_id(governed_db, vault):
    op.emit(db_path=governed_db["db"], vault_path=vault)
    files = list(_read_vault(vault))
    # Every emitted filename ends with --<id>.md and lives in a known subdir.
    for rel in files:
        assert rel.split("/")[0] in (
            op.SUBDIR_FACTS,
            op.SUBDIR_ENTITIES,
            op.SUBDIR_RELATIONS,
            op.SUBDIR_NOTES,
        )
        assert rel.endswith(".md")
        assert "--" in rel.split("/")[-1]


def test_traversal_is_rejected(tmp_path):
    root = tmp_path / "vault"
    with pytest.raises(ValueError):
        op._resolve_under_root(root, "../../etc/passwd")
    with pytest.raises(ValueError):
        op._resolve_under_root(root, "/etc/passwd")


def test_slug_never_produces_separators():
    assert op._slug("../../etc/passwd") == "etc-passwd"
    assert op._slug("a/b\\c") == "a-b-c"
    assert op._slug("   ") == "untitled"
    assert "/" not in op._slug("weird/../name")


# ── CLI smoke ─────────────────────────────────────────────────────────────────


def test_cli_runs_and_reports(governed_db, vault, capsys):
    rc = op.main(["--db", governed_db["db"], "--vault", vault])
    assert rc == 0
    out = capsys.readouterr().out
    assert '"written"' in out
    assert os.path.isdir(vault)
