"""Tier A #4 — sqlite-memory-mcp ↔ GBrain bridge: export adapter.

Reads entities / observations / relations from a sqlite-memory-mcp SQLite
store and writes Markdown files in a layout compatible with Garry Tan's
GBrain brain repo (people/, companies/, topics/ + YAML frontmatter +
wikilink relations). Bidirectional bridge starts as one-way export; an
import counterpart can be added later without breaking this surface.

Why this lives in the OSS core: the bridge narrative is integration, not
competition. Same KG observations can flow either way; operators pick
the runtime they prefer for any given deployment.

Public function:

    export_to_gbrain_brain_repo(conn, output_dir, *, project_filter=None)
        → dict[str, int]

The returned dict keys:
    entities_written, relations_written, observations_written, files_written.

No LLM, no network. Pure SQL + filesystem. Deterministic across runs
given the same DB snapshot.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any


# Map entity_type to GBrain-style folder. Anything not listed falls back
# to `topics/` so unknown types never crash the export.
_ENTITY_TYPE_FOLDERS: dict[str, str] = {
    "person": "people",
    "people": "people",
    "user": "people",
    "company": "companies",
    "organization": "companies",
    "org": "companies",
    "team": "companies",
}

_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


def _slugify(name: str) -> str:
    """Cross-platform-safe filename. Preserves Unicode letters/digits."""
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "-", name)
    safe = re.sub(r"\s+", "-", safe.strip())
    safe = re.sub(r"-+", "-", safe).strip("-.")
    if safe.upper().split(".", 1)[0] in _WINDOWS_RESERVED_NAMES:
        safe = f"_{safe}"
    return safe or "entity"


def _folder_for(entity_type: str | None) -> str:
    if not entity_type:
        return "topics"
    return _ENTITY_TYPE_FOLDERS.get(entity_type.lower().strip(), "topics")


def _yaml_escape(value: Any) -> str:
    """Minimal YAML scalar escape for frontmatter values.

    Strings with `:` or starting with reserved chars get quoted; None
    becomes empty; integers/floats render as-is.
    """
    if value is None:
        return ""
    if isinstance(value, (int, float, bool)):
        return str(value).lower() if isinstance(value, bool) else str(value)
    s = str(value)
    if any(c in s for c in ":#&*!|>'\"%@`") or s.startswith(("-", "?", " ")) or s == "":
        escaped = s.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return s


def _render_entity_markdown(
    entity: dict[str, Any],
    observations: list[str],
    out_relations: list[tuple[str, str, str]],
) -> str:
    """Render one entity as Markdown with YAML frontmatter + sections.

    out_relations is a list of (predicate, target_name, target_md_relpath).
    """
    fm_lines = ["---"]
    for key in ("name", "entity_type", "project", "created_at", "updated_at"):
        if key in entity and entity[key] is not None:
            fm_lines.append(f"{key}: {_yaml_escape(entity[key])}")
    fm_lines.append("---")
    fm_lines.append("")
    fm_lines.append(f"# {entity.get('name', 'Untitled')}")
    fm_lines.append("")

    if observations:
        fm_lines.append("## Observations")
        fm_lines.append("")
        for obs in observations:
            cleaned = obs.strip().replace("\n", " ")
            fm_lines.append(f"- {cleaned}")
        fm_lines.append("")

    if out_relations:
        fm_lines.append("## Relations")
        fm_lines.append("")
        for predicate, target_name, target_relpath in out_relations:
            fm_lines.append(f"- {predicate}: [{target_name}]({target_relpath})")
        fm_lines.append("")

    return "\n".join(fm_lines)


def export_to_gbrain_brain_repo(
    conn: sqlite3.Connection,
    output_dir: str,
    *,
    project_filter: str | None = None,
) -> dict[str, int]:
    """Export entities/observations/relations to GBrain-compatible Markdown.

    Args:
        conn: open sqlite3 connection to a sqlite-memory-mcp DB.
        output_dir: folder to write under. Created if missing. Each entity
            becomes one .md file under people/, companies/, or topics/.
        project_filter: if given, restrict to entities with that exact
            project value. Default exports all entities.

    Returns counters: {entities_written, relations_written,
    observations_written, files_written}.
    """
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    for sub in ("people", "companies", "topics"):
        (out / sub).mkdir(exist_ok=True)

    if project_filter is not None:
        entity_rows = conn.execute(
            "SELECT id, name, entity_type, project, created_at, updated_at "
            "FROM entities WHERE project = ? ORDER BY name",
            (project_filter,),
        ).fetchall()
    else:
        entity_rows = conn.execute(
            "SELECT id, name, entity_type, project, created_at, updated_at "
            "FROM entities ORDER BY name"
        ).fetchall()

    entity_meta: dict[int, dict[str, Any]] = {}
    name_to_path: dict[str, str] = {}
    for row in entity_rows:
        d = dict(row) if not isinstance(row, dict) else row
        folder = _folder_for(d.get("entity_type"))
        slug = _slugify(d["name"])
        relpath = f"{folder}/{slug}.md"
        entity_meta[d["id"]] = {
            **d,
            "_folder": folder,
            "_slug": slug,
            "_relpath": relpath,
        }
        name_to_path[d["name"]] = relpath

    obs_counter = 0
    rel_counter = 0
    files_counter = 0

    if entity_meta:
        ids = list(entity_meta.keys())
        ph = ",".join("?" * len(ids))
        obs_rows = conn.execute(
            f"SELECT entity_id, content FROM observations "
            f"WHERE entity_id IN ({ph}) ORDER BY entity_id, id",
            ids,
        ).fetchall()
        obs_by_entity: dict[int, list[str]] = {}
        for r in obs_rows:
            obs_by_entity.setdefault(r["entity_id"], []).append(r["content"])
            obs_counter += 1

        rel_rows = conn.execute(
            f"SELECT from_id, to_id, relation_type FROM relations "
            f"WHERE from_id IN ({ph}) ORDER BY from_id, id",
            ids,
        ).fetchall()
        rels_by_entity: dict[int, list[tuple[str, str, str]]] = {}
        for r in rel_rows:
            target = entity_meta.get(r["to_id"])
            if target is None:
                continue
            from_meta = entity_meta[r["from_id"]]
            from_folder = from_meta["_folder"]
            target_relpath = target["_relpath"]
            if from_folder == target["_folder"]:
                target_relpath = f"./{target['_slug']}.md"
            else:
                target_relpath = f"../{target_relpath}"
            rels_by_entity.setdefault(r["from_id"], []).append(
                (r["relation_type"], target["name"], target_relpath)
            )
            rel_counter += 1
    else:
        obs_by_entity = {}
        rels_by_entity = {}

    for eid, meta in entity_meta.items():
        md = _render_entity_markdown(
            meta,
            obs_by_entity.get(eid, []),
            rels_by_entity.get(eid, []),
        )
        target = out / meta["_folder"] / f"{meta['_slug']}.md"
        target.write_text(md, encoding="utf-8")
        files_counter += 1

    return {
        "entities_written": len(entity_meta),
        "relations_written": rel_counter,
        "observations_written": obs_counter,
        "files_written": files_counter,
    }
