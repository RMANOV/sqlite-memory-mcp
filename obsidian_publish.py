"""Governed one-way markdown publication (Obsidian Feature #1).

ADVOCATE-PASSED locked spec (debate OBSIDIAN_FEATURES_20260529, msg 5344ee6a6458):

  Name        : "Governed one-way markdown publication" (NOT "sync").
  Invariant   : sqlite-memory is the SOLE canonical store. This module writes
                markdown OUT only. There is NO vault->memory code path. Vault
                edits can NEVER mutate memory — proven structurally by opening
                the DB connection READ-ONLY (mode=ro) so a write is physically
                impossible from the emit path.
  Eligibility : DEFAULT-DENY ALLOWLIST. A record is emitted ONLY when it
                positively matches a published/live governance signal:
                  * promoted facts  -> canonical_facts rows that were promoted
                    from a candidate claim (source_claim_id IS NOT NULL) and
                    are still LIVE (valid_to IS NULL AND superseded_by IS NULL),
                    excluding seeded/system fact_scopes (e.g. predicate_vocabulary).
                  * approved entities -> entities.visibility == 'public'
                    (private / pending_public / NULL -> DENIED).
                  * approved observations -> belong to a public entity.
                  * approved relations -> BOTH endpoint entities are public.
                  * approved notes/tasks -> tasks.visibility == 'public'.
                Anything candidate / unreviewed / rejected / private / draft /
                pending / ambiguous / missing-state -> NEVER emitted.
  Idempotent  : re-running with unchanged source produces byte-identical files
                (deterministic ordering, stable frontmatter key order,
                content_hash excludes volatile fields, generated_at derived from
                the source row's updated_at, write skipped when hash unchanged).
  Provenance  : YAML frontmatter on every file (source_id, source_kind,
                provenance, governance_state, content_hash, generator_version,
                generated_at, ...). Every published note is traceable to its
                governed origin.
  Revoke      : when a source row is revoked/demoted/superseded/un-published in
                memory, the emitter writes a TOMBSTONE (governance_state=revoked,
                retains source_id / provenance / content_hash / generator_version,
                body replaced with a short revocation marker). Hard delete is a
                later optional GC, not the v1 default.
  Conflict    : canonical wins. A hand-edited vault file is OVERWRITTEN on the
                next publish (the edit is transient) and the overwrite is logged.
  Surface     : v1 is CLI / function only (bin/obsidian-publish). NO MCP tool.

This module is a pure, read-only consumer of the existing DAO/schema. It adds
no tables and no migrations. Rollback = delete this module + the CLI shim.
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import re
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# db_utils is the single source of truth for the DB path + timestamp helpers.
try:
    from db_utils import DB_PATH, now_iso
except Exception:  # pragma: no cover - allow standalone import in odd layouts
    DB_PATH = os.environ.get(
        "SQLITE_MEMORY_DB", os.path.expanduser("~/.claude/memory/memory.db")
    )

    def now_iso() -> str:
        from datetime import datetime, timezone

        return datetime.now(timezone.utc).isoformat()


log = logging.getLogger("obsidian-publish")

# Bumped whenever the emitted markdown layout/semantics change. Part of every
# file's frontmatter + content_hash so output is reproducible per generator.
GENERATOR_VERSION = "1.0.0"

# Vault root: where governed markdown is published. Overridable via env for the
# CLI; tests always pass an explicit tmp_path so production is never touched.
DEFAULT_VAULT_PATH = os.environ.get(
    "OBSIDIAN_VAULT_PATH",
    os.path.expanduser("~/.claude/memory/obsidian_vault"),
)

# Subdirectories under the vault root, one per source kind. Stable + flat so
# Obsidian can navigate and filenames stay deterministic.
SUBDIR_FACTS = "facts"
SUBDIR_ENTITIES = "entities"
SUBDIR_RELATIONS = "relations"
SUBDIR_NOTES = "notes"

# fact_scopes that are seeded/system internals, NOT promoted user knowledge.
# Default-deny: these never leave memory even though they live in canonical_facts.
EXCLUDED_FACT_SCOPES = frozenset({"predicate_vocabulary"})

# The single explicit "approved/published" governance signal for
# entities/observations/relations/notes. private / pending_public / NULL -> denied.
PUBLISHED_VISIBILITY = "public"

_REVOKED_BODY = "> [!warning] Revoked\n> This published note was revoked in the canonical store and is retained as a tombstone for audit. Its content has been withdrawn."

_GOV_STATE_PUBLISHED = "published"
_GOV_STATE_REVOKED = "revoked"
_STALE_TMP_SECONDS = 3600

# Filename slug charset allowlist (path-traversal / weird-char defense). The
# canonical, collision-free identity is the trailing immutable id; the slug is
# only a human-readable prefix.
_SLUG_RE = re.compile(r"[^a-z0-9]+")


# ── value objects ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PublishRecord:
    """One governed record selected for publication (or tombstoning)."""

    source_kind: str  # fact | entity | relation | note
    source_id: str  # immutable DB id (fact_id / entity id / relation id / task id)
    title: str
    body: str
    provenance: str
    governance_state: str  # published | revoked
    approved_at: str | None
    updated_at: str | None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class EmitResult:
    """Outcome of an emit run."""

    written: list[str] = field(default_factory=list)  # files written (new/changed)
    skipped: list[str] = field(default_factory=list)  # unchanged -> no rewrite
    tombstoned: list[str] = field(default_factory=list)  # revoked -> tombstone file
    overwrites: list[str] = field(default_factory=list)  # canonical overwrote hand-edit

    def as_dict(self) -> dict[str, Any]:
        return {
            "written": sorted(self.written),
            "skipped": sorted(self.skipped),
            "tombstoned": sorted(self.tombstoned),
            "overwrites": sorted(self.overwrites),
            "counts": {
                "written": len(self.written),
                "skipped": len(self.skipped),
                "tombstoned": len(self.tombstoned),
                "overwrites": len(self.overwrites),
            },
        }


# ── read-only DB access (canonical-source invariant, structural) ────────────


def open_readonly(db_path: str) -> sqlite3.Connection:
    """Open the memory DB strictly READ-ONLY.

    Using SQLite's ``mode=ro`` URI makes it physically impossible for this
    module to mutate memory — any INSERT/UPDATE/DELETE raises
    ``sqlite3.OperationalError`` ("attempt to write a readonly database").
    This is the structural proof behind the "vault edits cannot mutate memory"
    invariant: there is no write path, by construction.
    """
    resolved = Path(db_path).resolve()
    uri = f"file:{resolved}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


# ── eligibility selectors (DEFAULT-DENY ALLOWLIST) ──────────────────────────


def select_published_facts(conn: sqlite3.Connection) -> list[PublishRecord]:
    """Promoted + LIVE canonical facts (default-deny allowlist).

    Eligible iff: promoted from a claim (source_claim_id IS NOT NULL), still
    live (valid_to IS NULL AND superseded_by_fact_id IS NULL), and not a
    seeded/system scope. Seeded vocabulary has source_claim_id NULL and is
    therefore excluded twice over.
    """
    if not _table_exists(conn, "canonical_facts"):
        return []
    placeholders = ",".join("?" for _ in EXCLUDED_FACT_SCOPES) or "''"
    rows = conn.execute(
        f"""
        SELECT fact_id, subject, predicate, object_text, object_type, fact_scope,
               provenance_summary, confidence, validation_mode, source_claim_id,
               valid_from, created_at, updated_at
        FROM canonical_facts
        WHERE source_claim_id IS NOT NULL
          AND valid_to IS NULL
          AND superseded_by_fact_id IS NULL
          AND fact_scope NOT IN ({placeholders})
        ORDER BY fact_id
        """,
        tuple(sorted(EXCLUDED_FACT_SCOPES)),
    ).fetchall()
    out: list[PublishRecord] = []
    for r in rows:
        title = f"{r['subject']} {r['predicate']} {r['object_text']}".strip()
        body_lines = [
            f"**Subject:** {r['subject']}",
            f"**Predicate:** {r['predicate']}",
            f"**Object:** {r['object_text']}",
            f"**Scope:** {r['fact_scope']}",
            f"**Confidence:** {r['confidence']}",
            f"**Validation mode:** {r['validation_mode']}",
        ]
        out.append(
            PublishRecord(
                source_kind="fact",
                source_id=str(r["fact_id"]),
                title=title,
                body="\n\n".join(body_lines),
                provenance=str(r["provenance_summary"]),
                governance_state=_GOV_STATE_PUBLISHED,
                approved_at=r["valid_from"] or r["created_at"],
                updated_at=r["updated_at"],
                extra={
                    "fact_scope": r["fact_scope"],
                    "validation_mode": r["validation_mode"],
                    "confidence": r["confidence"],
                    "source_claim_id": r["source_claim_id"],
                },
            )
        )
    return out


def select_published_entities(conn: sqlite3.Connection) -> list[PublishRecord]:
    """Approved entities (visibility == 'public') + their observations."""
    if not _table_exists(conn, "entities"):
        return []
    rows = conn.execute(
        """
        SELECT id, name, entity_type, project, visibility, origin,
               created_at, updated_at
        FROM entities
        WHERE visibility = ?
        ORDER BY id
        """,
        (PUBLISHED_VISIBILITY,),
    ).fetchall()
    out: list[PublishRecord] = []
    for r in rows:
        obs = conn.execute(
            "SELECT content FROM observations WHERE entity_id = ? ORDER BY id",
            (r["id"],),
        ).fetchall()
        obs_texts = [o["content"] for o in obs]
        body_lines = [
            f"**Type:** {r['entity_type']}",
            f"**Project:** {r['project'] or '(none)'}",
        ]
        if obs_texts:
            body_lines.append("## Observations")
            body_lines.extend(f"- {text}" for text in obs_texts)
        out.append(
            PublishRecord(
                source_kind="entity",
                source_id=str(r["id"]),
                title=str(r["name"]),
                body="\n\n".join(body_lines),
                provenance=f"entity origin={r['origin']} visibility={r['visibility']}",
                governance_state=_GOV_STATE_PUBLISHED,
                approved_at=r["updated_at"],
                updated_at=r["updated_at"],
                extra={
                    "entity_type": r["entity_type"],
                    "project": r["project"],
                    "observation_count": len(obs_texts),
                },
            )
        )
    return out


def select_published_relations(conn: sqlite3.Connection) -> list[PublishRecord]:
    """Approved relations: BOTH endpoint entities are public (default-deny)."""
    if not _table_exists(conn, "relations") or not _table_exists(conn, "entities"):
        return []
    rows = conn.execute(
        """
        SELECT rel.id        AS rel_id,
               rel.relation_type AS relation_type,
               rel.created_at AS created_at,
               ef.name        AS from_name,
               et.name        AS to_name
        FROM relations rel
        JOIN entities ef ON ef.id = rel.from_id
        JOIN entities et ON et.id = rel.to_id
        WHERE ef.visibility = ? AND et.visibility = ?
        ORDER BY rel.id
        """,
        (PUBLISHED_VISIBILITY, PUBLISHED_VISIBILITY),
    ).fetchall()
    out: list[PublishRecord] = []
    for r in rows:
        title = f"{r['from_name']} {r['relation_type']} {r['to_name']}"
        body = (
            f"**From:** [[{_slug(r['from_name'])}]] ({r['from_name']})\n\n"
            f"**Relation:** {r['relation_type']}\n\n"
            f"**To:** [[{_slug(r['to_name'])}]] ({r['to_name']})"
        )
        out.append(
            PublishRecord(
                source_kind="relation",
                source_id=str(r["rel_id"]),
                title=title,
                body=body,
                provenance="relation between two approved (public) entities",
                governance_state=_GOV_STATE_PUBLISHED,
                approved_at=r["created_at"],
                updated_at=r["created_at"],
                extra={"relation_type": r["relation_type"]},
            )
        )
    return out


def select_published_notes(conn: sqlite3.Connection) -> list[PublishRecord]:
    """Approved notes/tasks: tasks.visibility == 'public' (explicit publish)."""
    if not _table_exists(conn, "tasks"):
        return []
    rows = conn.execute(
        """
        SELECT id, title, description, notes, type, status, project,
               visibility, created_at, updated_at
        FROM tasks
        WHERE visibility = ?
        ORDER BY id
        """,
        (PUBLISHED_VISIBILITY,),
    ).fetchall()
    out: list[PublishRecord] = []
    for r in rows:
        body_lines = [
            f"**Type:** {r['type']}",
            f"**Status:** {r['status']}",
            f"**Project:** {r['project'] or '(none)'}",
        ]
        if r["description"]:
            body_lines.append("## Description")
            body_lines.append(str(r["description"]))
        if r["notes"]:
            body_lines.append("## Notes")
            body_lines.append(str(r["notes"]))
        out.append(
            PublishRecord(
                source_kind="note",
                source_id=str(r["id"]),
                title=str(r["title"]),
                body="\n\n".join(body_lines),
                provenance=f"task type={r['type']} status={r['status']} visibility={r['visibility']}",
                governance_state=_GOV_STATE_PUBLISHED,
                approved_at=r["updated_at"],
                updated_at=r["updated_at"],
                extra={
                    "type": r["type"],
                    "status": r["status"],
                    "project": r["project"],
                },
            )
        )
    return out


def select_all_published(conn: sqlite3.Connection) -> list[PublishRecord]:
    """Full default-deny allowlist across every supported source kind."""
    records: list[PublishRecord] = []
    records.extend(select_published_facts(conn))
    records.extend(select_published_entities(conn))
    records.extend(select_published_relations(conn))
    records.extend(select_published_notes(conn))
    return records


# ── markdown rendering (deterministic) ──────────────────────────────────────


def _slug(text: str) -> str:
    """Path-safe, lowercase slug. Never produces traversal or separators."""
    s = _SLUG_RE.sub("-", (text or "").strip().lower()).strip("-")
    return s or "untitled"


_SUBDIRS = {
    "fact": SUBDIR_FACTS,
    "entity": SUBDIR_ENTITIES,
    "relation": SUBDIR_RELATIONS,
    "note": SUBDIR_NOTES,
}


def relpath_for(rec: PublishRecord) -> str:
    """Deterministic vault-relative path keyed on the IMMUTABLE id.

    Filename = "<slug-of-title>--<source_id>.md". The trailing id is the stable
    identity (so a rename of the title still maps to the same file and never
    orphans), the slug is just a readable prefix.
    """
    subdir = _SUBDIRS.get(rec.source_kind, "misc")
    fname = f"{_slug(rec.title)}--{_slug(rec.source_id)}.md"
    return f"{subdir}/{fname}"


# Frontmatter key order is FIXED for deterministic, byte-identical output.
# NOTE: content_hash is excluded from the hash input (it is the hash itself) and
# generated_at is derived from the source row's updated_at (deterministic), so a
# re-emit of unchanged data is byte-identical even into a fresh directory.
_FRONTMATTER_KEYS = (
    "source_kind",
    "source_id",
    "governance_state",
    "provenance",
    "approved_at",
    "generated_at",
    "generator_version",
    "content_hash",
)


def _yaml_scalar(value: Any) -> str:
    """Render a scalar as a safe single-line YAML value (always quoted str)."""
    if value is None:
        return '""'
    s = str(value).replace("\\", "\\\\").replace('"', '\\"')
    s = s.replace("\n", " ").replace("\r", " ")
    return f'"{s}"'


def _render_body_region(rec: PublishRecord, *, tombstone: bool) -> str:
    """Render the markdown body region (everything after the frontmatter).

    This is the integrity-protected payload: ``content_hash`` is computed over
    exactly this string, so any later hand-edit of the body makes the file
    fail its own self-check (rehash != embedded hash). The body is the only
    user-visible content, so this is also the meaningful diff surface.
    """
    title = rec.title
    body = _REVOKED_BODY if tombstone else rec.body
    lines = [f"# {title}", "", body, ""]  # trailing newline for clean diffs
    return "\n".join(lines)


def compute_content_hash(body_region: str) -> str:
    """sha256 over the rendered BODY REGION.

    Defining the hash over the body region (not the frontmatter, which carries
    the hash itself + the deterministic generated_at) makes every emitted file
    SELF-VERIFYING: re-hash the body region and compare to the embedded
    ``content_hash`` to detect a vault-side hand-edit without consulting memory.
    Includes the generator version prefix so a layout change forces a rewrite.
    """
    h = hashlib.sha256()
    h.update(GENERATOR_VERSION.encode("utf-8"))
    h.update(b"\x1f")
    h.update(body_region.encode("utf-8"))
    return h.hexdigest()


def render_markdown(rec: PublishRecord, *, tombstone: bool = False) -> str:
    """Render a record to deterministic markdown with provenance frontmatter."""
    gov_state = _GOV_STATE_REVOKED if tombstone else rec.governance_state
    body_region = _render_body_region(rec, tombstone=tombstone)
    content_hash = compute_content_hash(body_region)

    # generated_at is derived from the source's updated_at (deterministic) so
    # re-emit is byte-identical. Falls back to a fixed sentinel, never wall-clock.
    generated_at = rec.updated_at or rec.approved_at or "1970-01-01T00:00:00+00:00"

    fm_values = {
        "source_kind": rec.source_kind,
        "source_id": rec.source_id,
        "governance_state": gov_state,
        "provenance": rec.provenance,
        "approved_at": rec.approved_at,
        "generated_at": generated_at,
        "generator_version": GENERATOR_VERSION,
        "content_hash": content_hash,
    }
    fm_lines = ["---"]
    for key in _FRONTMATTER_KEYS:
        fm_lines.append(f"{key}: {_yaml_scalar(fm_values[key])}")
    fm_lines.append("---")
    fm_lines.append("")  # blank line between frontmatter and body
    frontmatter = "\n".join(fm_lines) + "\n"
    # body_region already ends with a newline; frontmatter + body_region is the
    # full file. The body region after the closing "---\n\n" is exactly what
    # compute_content_hash() covers, so the file self-verifies.
    return frontmatter + body_region


# ── file I/O (atomic, conflict-logging) ─────────────────────────────────────


_HASH_RE = re.compile(r'^content_hash:\s*"([0-9a-f]{64})"\s*$', re.MULTILINE)
_GOVSTATE_RE = re.compile(r'^governance_state:\s*"([a-z_]+)"\s*$', re.MULTILINE)


def _read_existing(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _embedded_hash(text: str | None) -> str | None:
    if not text:
        return None
    m = _HASH_RE.search(text)
    return m.group(1) if m else None


def _split_body_region(text: str | None) -> str | None:
    """Return the body region of an emitted file (after the closing '---').

    Files we emit start with a YAML frontmatter block delimited by '---' lines,
    followed by a blank line, then the body. The body region is exactly what
    compute_content_hash() covers. Returns None when the structure is unknown.
    """
    if not text or not text.startswith("---\n"):
        return None
    rest = text[len("---\n") :]
    end = rest.find("\n---\n")
    if end == -1:
        return None
    after = rest[end + len("\n---\n") :]
    # render_markdown writes one blank line between frontmatter and body.
    if after.startswith("\n"):
        after = after[1:]
    return after


def _file_is_tampered(text: str | None) -> bool:
    """True when an emitted file's body no longer matches its embedded hash.

    A pristine generation of ours self-verifies (rehash(body) == embedded). A
    vault-side hand-edit of the body breaks the match without touching memory —
    that is the only real 'conflict'. A legitimate source update is detected
    separately (the file simply differs from the fresh render).
    """
    embedded = _embedded_hash(text)
    body = _split_body_region(text)
    if embedded is None or body is None:
        return True  # unparseable / externally rewritten -> treat as tampered
    return compute_content_hash(body) != embedded


def _atomic_write(path: Path, content: str) -> None:
    """Write atomically (tmp + os.replace), matching repo conventions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


def _cleanup_stale_tmp_files(vault_root: Path) -> int:
    """Best-effort cleanup of old emitter temps without racing active emits."""
    cutoff = time.time() - _STALE_TMP_SECONDS
    removed = 0
    for subdir in (SUBDIR_FACTS, SUBDIR_ENTITIES, SUBDIR_RELATIONS, SUBDIR_NOTES):
        managed_dir = vault_root / subdir
        if not managed_dir.is_dir():
            continue
        for tmp_path in managed_dir.glob("*.md.tmp"):
            try:
                if tmp_path.stat().st_mtime <= cutoff:
                    tmp_path.unlink()
                    removed += 1
            except OSError:
                continue
    return removed


def _resolve_under_root(vault_root: Path, relpath: str) -> Path:
    """Resolve a vault-relative path, rejecting any escape outside the root."""
    root = vault_root.resolve()
    target = (root / relpath).resolve()
    if target != root and root not in target.parents:
        raise ValueError(f"vault path escapes root: {relpath!r}")
    return target


# ── emitter (one-way, idempotent) ───────────────────────────────────────────


def emit(
    *,
    db_path: str | None = None,
    vault_path: str | None = None,
    prune_revoked: bool = True,
) -> EmitResult:
    """Publish governed memory to the vault as deterministic markdown.

    READ-ONLY on memory (canonical-source invariant). Writes markdown OUT only.

    - Eligible (default-deny allowlist) records are rendered + written.
    - Unchanged files (matching content_hash) are SKIPPED -> byte-identical.
    - Hand-edited files are OVERWRITTEN by canonical content (logged).
    - When ``prune_revoked`` is set, previously-published files whose source is
      no longer eligible are converted to TOMBSTONES (governance_state=revoked).
    """
    db = db_path or DB_PATH
    vault_root = Path(vault_path or DEFAULT_VAULT_PATH)
    result = EmitResult()
    _cleanup_stale_tmp_files(vault_root)

    conn = open_readonly(db)
    try:
        records = select_all_published(conn)
    finally:
        conn.close()

    live_relpaths: set[str] = set()
    for rec in records:
        relpath = relpath_for(rec)
        live_relpaths.add(relpath)
        target = _resolve_under_root(vault_root, relpath)
        fresh = render_markdown(rec, tombstone=False)
        existing = _read_existing(target)

        if existing == fresh:
            result.skipped.append(relpath)
            continue

        if existing is not None:
            # File present but differs from the fresh canonical render.
            # Two distinct causes, distinguished by the file's SELF-CHECK:
            #   * tampered (body != its own embedded hash) -> a vault-side
            #     hand-edit. Canonical wins; the edit is transient -> LOG it.
            #   * pristine-but-stale (self-check passes) -> a legitimate update
            #     in memory since the last emit. Rewrite quietly (not a conflict).
            if _file_is_tampered(existing):
                result.overwrites.append(relpath)
                log.warning(
                    "obsidian-publish: canonical overwrite of hand-edited vault "
                    "file %s (vault edits are transient; memory is authoritative)",
                    relpath,
                )
        _atomic_write(target, fresh)
        result.written.append(relpath)

    if prune_revoked:
        _tombstone_orphans(vault_root, live_relpaths, result)

    log.info("obsidian-publish: %s", result.as_dict()["counts"])
    return result


def _iter_published_files(vault_root: Path) -> list[Path]:
    """All markdown files under the managed subdirs (our own generated output)."""
    found: list[Path] = []
    for subdir in (SUBDIR_FACTS, SUBDIR_ENTITIES, SUBDIR_RELATIONS, SUBDIR_NOTES):
        d = vault_root / subdir
        if d.is_dir():
            found.extend(sorted(p for p in d.glob("*.md")))
    return found


def _tombstone_orphans(
    vault_root: Path, live_relpaths: set[str], result: EmitResult
) -> None:
    """Convert previously-published files with no live source into tombstones.

    Revoke / demote / supersede / un-publish in memory drops the source from the
    allowlist; its vault file is retained as an auditable tombstone rather than
    silently deleted (hard delete = later optional GC).
    """
    root = vault_root.resolve()
    for path in _iter_published_files(vault_root):
        rel = path.resolve().relative_to(root).as_posix()
        if rel in live_relpaths:
            continue
        existing = _read_existing(path)
        if existing is None:
            continue
        # Already a tombstone? leave it byte-identical (idempotent).
        m = _GOVSTATE_RE.search(existing)
        if m and m.group(1) == _GOV_STATE_REVOKED:
            result.skipped.append(rel)
            continue
        tomb = _make_tombstone_from_existing(path, existing)
        if tomb is None:
            continue
        if tomb == existing:
            result.skipped.append(rel)
            continue
        _atomic_write(path, tomb)
        result.tombstoned.append(rel)
        log.info("obsidian-publish: tombstoned revoked source -> %s", rel)


_FM_FIELD_RE = re.compile(r'^([a-z_]+):\s*"(.*)"\s*$', re.MULTILINE)
_GOVSTATE_LINE_RE = re.compile(r'^governance_state:\s*"[a-z_]+"\s*$', re.MULTILINE)


def _make_tombstone_from_existing(path: Path, existing: str) -> str | None:
    """Build a tombstone by transforming the existing file IN PLACE.

    Per the locked spec (QS2), a tombstone must RETAIN the original
    source_id / provenance / content_hash / generator_version (and the rest of
    the audit frontmatter). We therefore preserve the existing frontmatter
    VERBATIM and change only two things:
      * flip ``governance_state`` -> ``"revoked"``
      * replace the body region with the short revoked marker

    Keeping the original ``content_hash`` is the intended audit value (it points
    at the content that WAS published); the file no longer self-verifies, but
    ``_tombstone_orphans`` matches tombstones by ``governance_state`` and skips
    them before any tamper check, so this is consistent and idempotent.
    """
    fields = {m.group(1): m.group(2) for m in _FM_FIELD_RE.finditer(existing)}
    if not fields.get("source_id") or not fields.get("source_kind"):
        return None
    body = _split_body_region(existing)
    if body is None:
        return None
    frontmatter = existing[: len(existing) - len(body)]
    # Retain title H1 if present, else fall back to source_id.
    title_match = re.search(r"^# (.+)$", body, re.MULTILINE)
    title = title_match.group(1).strip() if title_match else fields["source_id"]

    new_frontmatter = _GOVSTATE_LINE_RE.sub(
        f"governance_state: {_yaml_scalar(_GOV_STATE_REVOKED)}",
        frontmatter,
        count=1,
    )
    new_body = "\n".join([f"# {title}", "", _REVOKED_BODY, ""])
    return new_frontmatter + new_body


# ── CLI (v1 surface — NO MCP tool) ──────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="obsidian-publish",
        description="Governed one-way markdown publication: emit APPROVED "
        "sqlite-memory content to an Obsidian-readable vault. Read-only on "
        "memory; writes markdown OUT only.",
    )
    parser.add_argument(
        "--db", default=None, help="memory DB path (default: db_utils.DB_PATH)"
    )
    parser.add_argument(
        "--vault",
        default=None,
        help=f"vault output dir (default: {DEFAULT_VAULT_PATH})",
    )
    parser.add_argument(
        "--no-prune",
        action="store_true",
        help="do not tombstone files whose source is no longer eligible",
    )
    parser.add_argument(
        "--quiet", action="store_true", help="suppress the per-run summary line"
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    result = emit(
        db_path=args.db,
        vault_path=args.vault,
        prune_revoked=not args.no_prune,
    )
    if not args.quiet:
        import json

        print(json.dumps(result.as_dict(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
