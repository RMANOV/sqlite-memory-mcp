"""Reverse adapter for the sqlite-memory-mcp ↔ GBrain bridge.

Reads a GBrain-compatible brain repo layout (people/, companies/,
topics/, plus YAML frontmatter + observation bullets + relations as
wikilinks) and imports the contents into a sqlite-memory-mcp KG via the
canonical entity / observation / relation tables.

Idempotent by default: an entity with the same name is skipped (the
existing row keeps its observations and relations). Pass
`skip_if_exists=False` to upsert observations and relations (entities
themselves are still UNIQUE on name, so no duplicate rows).

Pure SQL + filesystem; no LLM, no network.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any

from db_utils import now_iso, fts_sync_entity


_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_OBSERVATIONS_HEADER = re.compile(r"^##\s+Observations\s*$", re.MULTILINE)
_RELATIONS_HEADER = re.compile(r"^##\s+Relations\s*$", re.MULTILINE)
_BULLET_RE = re.compile(r"^-\s+(.*?)\s*$")
_RELATION_LINK_RE = re.compile(
    r"^-\s+([^:]+):\s*\[([^\]]+)\]\(([^)]+)\)\s*$"
)


def _parse_yaml_frontmatter(block: str) -> dict[str, Any]:
    """Minimal YAML key:value parser sufficient for our own frontmatter.

    Handles unquoted scalars, double-quoted scalars with backslash escapes,
    and treats unknown content as raw string. We never write nested or
    multi-line YAML in the export side, so we don't need a full parser.
    """
    out: dict[str, Any] = {}
    for raw_line in block.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip()
        val = val.strip()
        if val.startswith('"') and val.endswith('"') and len(val) >= 2:
            inner = val[1:-1]
            inner = inner.replace('\\"', '"').replace("\\\\", "\\")
            out[key] = inner
        elif val == "":
            out[key] = None
        elif val == "true":
            out[key] = True
        elif val == "false":
            out[key] = False
        else:
            out[key] = val
    return out


def _split_sections(body: str) -> tuple[list[str], list[tuple[str, str]]]:
    """Extract observation lines and relation tuples from a Markdown body.

    Returns (observations, relations) where each relation is
    (predicate, target_entity_name).
    """
    observations: list[str] = []
    relations: list[tuple[str, str]] = []

    obs_match = _OBSERVATIONS_HEADER.search(body)
    rel_match = _RELATIONS_HEADER.search(body)

    if obs_match:
        obs_start = obs_match.end()
        obs_end = rel_match.start() if rel_match else len(body)
        for line in body[obs_start:obs_end].splitlines():
            line = line.rstrip()
            if not line.startswith("- "):
                continue
            if _RELATION_LINK_RE.match(line):
                continue
            m = _BULLET_RE.match(line)
            if m:
                observations.append(m.group(1))

    if rel_match:
        rel_start = rel_match.end()
        for line in body[rel_start:].splitlines():
            line = line.rstrip()
            m = _RELATION_LINK_RE.match(line)
            if m:
                predicate = m.group(1).strip()
                target_name = m.group(2).strip()
                relations.append((predicate, target_name))

    return observations, relations


def _parse_brain_file(path: Path) -> dict[str, Any] | None:
    """Parse one brain .md file. Returns None if the file is malformed."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    fm_match = _FRONTMATTER_RE.match(text)
    if not fm_match:
        return None
    frontmatter = _parse_yaml_frontmatter(fm_match.group(1))
    body = text[fm_match.end():]
    observations, relations = _split_sections(body)
    return {
        "frontmatter": frontmatter,
        "observations": observations,
        "relations": relations,
        "source_file": str(path),
    }


def _entity_id_by_name(conn: sqlite3.Connection, name: str) -> int | None:
    row = conn.execute(
        "SELECT id FROM entities WHERE name = ?", (name,)
    ).fetchone()
    return int(row[0]) if row else None


def import_from_gbrain_brain_repo(
    conn: sqlite3.Connection,
    input_dir: str,
    *,
    project_default: str | None = None,
    skip_if_exists: bool = True,
) -> dict[str, Any]:
    """Import a GBrain brain repo into the sqlite-memory-mcp KG.

    Args:
        conn: open sqlite3 connection to a sqlite-memory-mcp DB.
        input_dir: folder produced by export_to_gbrain_brain_repo (or any
            GBrain-compatible layout). Walks {people,companies,topics}/.
        project_default: project value applied to entities whose
            frontmatter has no `project` field. Default leaves NULL.
        skip_if_exists: when True (default), skip entities that already
            exist (UNIQUE name) without altering them. When False,
            insert any new observations/relations against the existing
            row but do not delete or update existing data.

    Returns counters: entities_created, entities_skipped, observations_inserted,
    relations_inserted, relations_skipped (target unresolved), files_parsed,
    files_skipped (malformed).
    """
    base = Path(input_dir)
    counters = {
        "entities_created": 0,
        "entities_skipped": 0,
        "observations_inserted": 0,
        "relations_inserted": 0,
        "relations_skipped": 0,
        "files_parsed": 0,
        "files_skipped": 0,
    }

    if not base.is_dir():
        return counters

    parsed_files: list[dict[str, Any]] = []
    for sub in ("people", "companies", "topics"):
        sub_dir = base / sub
        if not sub_dir.is_dir():
            continue
        for md_path in sorted(sub_dir.glob("*.md")):
            parsed = _parse_brain_file(md_path)
            if parsed is None:
                counters["files_skipped"] += 1
                continue
            counters["files_parsed"] += 1
            parsed_files.append(parsed)

    name_to_id: dict[str, int] = {}
    for parsed in parsed_files:
        fm = parsed["frontmatter"]
        name = fm.get("name")
        if not isinstance(name, str) or not name.strip():
            counters["files_skipped"] += 1
            continue
        existing_id = _entity_id_by_name(conn, name)
        if existing_id is not None:
            name_to_id[name] = existing_id
            if skip_if_exists:
                counters["entities_skipped"] += 1
                continue
            counters["entities_skipped"] += 1
        else:
            entity_type = fm.get("entity_type") or "topic"
            project = fm.get("project") or project_default
            now = now_iso()
            cur = conn.execute(
                "INSERT INTO entities (name, entity_type, project, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (name, entity_type, project, fm.get("created_at") or now,
                 fm.get("updated_at") or now),
            )
            eid = int(cur.lastrowid)
            name_to_id[name] = eid
            counters["entities_created"] += 1

        eid = name_to_id[name]
        if not skip_if_exists or counters["entities_created"] > 0:
            for obs in parsed["observations"]:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO observations "
                    "(entity_id, content, created_at) VALUES (?, ?, ?)",
                    (eid, obs, now_iso()),
                )
                if cur.rowcount:
                    counters["observations_inserted"] += 1

    for parsed in parsed_files:
        fm = parsed["frontmatter"]
        name = fm.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        from_id = name_to_id.get(name)
        if from_id is None:
            continue
        if skip_if_exists:
            existed_before = _entity_id_by_name(conn, name) == from_id
            row_count = conn.execute(
                "SELECT COUNT(*) FROM relations WHERE from_id = ?",
                (from_id,),
            ).fetchone()[0]
            if existed_before and row_count > 0:
                continue
        for predicate, target_name in parsed["relations"]:
            target_id = name_to_id.get(target_name)
            if target_id is None:
                target_id = _entity_id_by_name(conn, target_name)
            if target_id is None:
                counters["relations_skipped"] += 1
                continue
            cur = conn.execute(
                "INSERT OR IGNORE INTO relations "
                "(from_id, to_id, relation_type, created_at) "
                "VALUES (?, ?, ?, ?)",
                (from_id, target_id, predicate, now_iso()),
            )
            if cur.rowcount:
                counters["relations_inserted"] += 1

    for eid in name_to_id.values():
        try:
            fts_sync_entity(conn, eid)
        except Exception:
            pass

    return counters
