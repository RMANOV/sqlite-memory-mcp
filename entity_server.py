#!/usr/bin/env python3
"""Thin MCP server exposing only entity management tools.

Shares the same SQLite database as the main sqlite-kb server and keeps the entity
surface independently deployable; ``unified_server.py`` also mounts it.
"""

from __future__ import annotations

import json

from fastmcp_compat import FastMCP

from db_utils import (
    get_conn as _get_conn,
    get_entity_id as _get_entity_id,
    fts_query as _fts_query,
    tokenize_for_similarity as _tokenize,
    fts_sync_entity as _fts_sync,
    now_iso,
    record_task_entity_link_tombstone,
    setup_logger,
    TaskDAO,
)
from premium_runtime import maybe_mount_premium_extensions
from schema import error as _error
from link_suggestions import (
    record_link_decision as _record_link_decision,
    suggest_links as _suggest_links,
)

# ── Logging (file-only, NEVER stdout — breaks MCP stdio) ────────────────

logger = setup_logger("sqlite-entity", "entity_server.log")

# ── FastMCP app ──────────────────────────────────────────────────────────

mcp = FastMCP(
    "sqlite-entity",
    instructions=(
        "Entity management tools: task-entity links, overlap detection, entity merging. "
        "Shares DB with sqlite-kb."
    ),
)


def _observations_by_entity(conn, entity_ids) -> dict[int, list[str]]:
    ids = list(dict.fromkeys(int(entity_id) for entity_id in entity_ids))
    if not ids:
        return {}
    grouped: dict[int, list[str]] = {}
    for offset in range(0, len(ids), 500):
        id_batch = ids[offset : offset + 500]
        placeholders = ",".join("?" for _ in id_batch)
        rows = conn.execute(
            "SELECT entity_id,content FROM observations "
            f"WHERE entity_id IN ({placeholders}) ORDER BY entity_id,id",
            id_batch,
        ).fetchall()
        for row in rows:
            grouped.setdefault(int(row["entity_id"]), []).append(row["content"])
    return grouped


# ═══════════════════════════════════════════════════════════════════════════
# Tool 1: link_task_entity
# ═══════════════════════════════════════════════════════════════════════════


@mcp.tool()
def link_task_entity(task_id: str, entity_name: str) -> str:
    """Link a task to a knowledge graph entity.

    Creates a manual link between a task and an entity. If an auto-discovered
    link already exists, it upgrades to manual (manual always wins).
    """
    with _get_conn() as conn:
        if not TaskDAO.exists(conn, task_id):
            return _error(f"Task {task_id} not found")

        entity_id = _get_entity_id(conn, entity_name)
        if not entity_id:
            return _error(f"Entity '{entity_name}' not found")
        result = _record_link_decision(
            conn,
            task_id=task_id,
            entity_id=entity_id,
            decision="accepted",
            decided_by="human",
            decision_source="manual_link",
            accepted_link_type="manual",
        )
        result["link_type"] = "manual"
        link_row = conn.execute(
            "SELECT created_at FROM task_entity_links "
            "WHERE task_id = ? AND entity_id = ?",
            (task_id, entity_id),
        ).fetchone()
        result["created_at"] = link_row["created_at"] if link_row else None
        return json.dumps(result, ensure_ascii=False)


# ═══════════════════════════════════════════════════════════════════════════
# Tool 2: unlink_task_entity
# ═══════════════════════════════════════════════════════════════════════════


@mcp.tool()
def unlink_task_entity(task_id: str, entity_name: str) -> str:
    """Remove a task↔entity link.

    Removing a silent high-confidence link also records an explicit human
    rejection, so that pair stays suppressed and becomes a real evaluation
    label. Other link types preserve the historical unlink behavior.
    """
    with _get_conn() as conn:
        entity_id = _get_entity_id(conn, entity_name)
        if not entity_id:
            return _error(f"Entity '{entity_name}' not found")

        existing = conn.execute(
            "SELECT link_type FROM task_entity_links "
            "WHERE task_id = ? AND entity_id = ?",
            (task_id, entity_id),
        ).fetchone()
        if existing is not None and existing["link_type"] == "auto_high_confidence":
            result = _record_link_decision(
                conn,
                task_id=task_id,
                entity_id=entity_id,
                decision="rejected",
                decided_by="human",
                decision_source="auto_high_confidence_rejected_by_human",
            )
            return json.dumps(
                {
                    "removed": True,
                    "decision_recorded": "rejected",
                    "decision_id": result["decision_id"],
                },
                ensure_ascii=False,
            )
        removed = TaskDAO.unlink_entity(conn, task_id, entity_id)

        return json.dumps({"removed": removed > 0}, ensure_ascii=False)


# ═══════════════════════════════════════════════════════════════════════════
# Tool 3: get_task_links
# ═══════════════════════════════════════════════════════════════════════════


@mcp.tool()
def get_task_links(task_id: str) -> str:
    """Get all knowledge graph entities linked to a task."""
    with _get_conn() as conn:
        links = TaskDAO.get_task_links(conn, task_id)
        return json.dumps({"task_id": task_id, "links": links}, ensure_ascii=False)


# ═══════════════════════════════════════════════════════════════════════════
# Tool 4: get_entity_tasks
# ═══════════════════════════════════════════════════════════════════════════


@mcp.tool()
def get_entity_tasks(entity_name: str) -> str:
    """Get all tasks linked to a knowledge graph entity."""
    with _get_conn() as conn:
        entity_id = _get_entity_id(conn, entity_name)
        if not entity_id:
            return _error(f"Entity '{entity_name}' not found")

        tasks = TaskDAO.get_entity_tasks(conn, entity_id)
        return json.dumps(
            {"entity_name": entity_name, "tasks": tasks}, ensure_ascii=False
        )


# ═══════════════════════════════════════════════════════════════════════════
# Tool 5: suggest_task_links
# ═══════════════════════════════════════════════════════════════════════════


@mcp.tool()
def suggest_task_links(
    task_id: str, limit: int = 5, include_vector: bool = False
) -> str:
    """Suggest knowledge graph entities that may be related to a task.

    Uses one versioned pairwise scorer with exact name/alias, FTS5, project,
    provenance, graph/meta-path, temporal, optional vector, and weak derived
    community signals. Does NOT auto-create links.
    """
    with _get_conn() as conn:
        try:
            result = _suggest_links(
                conn,
                task_id,
                limit=limit,
                include_vector=include_vector,
            )
        except ValueError as exc:
            return _error(str(exc))
        result["accept_tool"] = "link_task_entity"
        result["undo_auto_tool"] = "unlink_task_entity"
        return json.dumps(result, ensure_ascii=False)


# ═══════════════════════════════════════════════════════════════════════════
# Tool 6: find_entity_overlaps
# ═══════════════════════════════════════════════════════════════════════════


@mcp.tool()
def find_entity_overlaps(
    entity_name: str | None = None,
    min_score: float = 0.3,
    limit: int = 20,
) -> str:
    """Find overlapping/duplicate entities in the knowledge graph.

    Uses FTS5 + Jaccard similarity to detect entity pairs with significant
    observation overlap. Pairs with score >= 0.8 get a merge suggestion.
    """
    with _get_conn() as conn:
        if entity_name:
            sources = conn.execute(
                "SELECT id, name, entity_type FROM entities WHERE name = ?",
                (entity_name,),
            ).fetchall()
            if not sources:
                return _error(f"Entity '{entity_name}' not found")
        else:
            sources = conn.execute(
                "SELECT id, name, entity_type FROM entities"
            ).fetchall()

        seen_pairs: set[tuple[int, int]] = set()
        overlaps = []
        observations = _observations_by_entity(
            conn, [source["id"] for source in sources]
        )

        for src in sources:
            src_text = " ".join(observations.get(src["id"], []))
            src_tokens = _tokenize(f"{src['name']} {src_text}")

            if not src_tokens:
                continue

            fts_q = _fts_query(src_text or src["name"])
            if not fts_q:
                continue

            candidates = conn.execute(
                "SELECT rowid, name, entity_type "
                "FROM memory_fts WHERE memory_fts MATCH ? LIMIT 50",
                (fts_q,),
            ).fetchall()
            missing_ids = [
                candidate["rowid"]
                for candidate in candidates
                if candidate["rowid"] not in observations
            ]
            observations.update(_observations_by_entity(conn, missing_ids))
            for missing_id in missing_ids:
                observations.setdefault(missing_id, [])

            for cand in candidates:
                cand_id = cand["rowid"]
                if cand_id == src["id"]:
                    continue

                pair_key = (min(src["id"], cand_id), max(src["id"], cand_id))
                if pair_key in seen_pairs:
                    continue
                seen_pairs.add(pair_key)

                cand_text = " ".join(observations.get(cand_id, []))
                cand_tokens = _tokenize(f"{cand['name']} {cand_text}")

                if not cand_tokens:
                    continue

                # Longest-first, not codepoint order. Plain ``sorted`` puts
                # every ASCII token ahead of every Cyrillic one, so on a
                # Bulgarian corpus the cap fills with digits and Latin noise
                # and the overlap is computed over the wrong half of the text.
                # Same policy as link_suggestions._safe_fts_query and
                # memory_thread_clustering; ``w`` keeps ties deterministic.
                s_tok = set(sorted(src_tokens, key=lambda w: (-len(w), w))[:500])
                c_tok = set(sorted(cand_tokens, key=lambda w: (-len(w), w))[:500])
                intersection = s_tok & c_tok
                union_set = s_tok | c_tok
                jaccard = len(intersection) / len(union_set) if union_set else 0.0

                if jaccard < min_score:
                    continue

                overlaps.append(
                    {
                        "entity_a": src["name"],
                        "entity_b": cand["name"],
                        "score": round(jaccard, 4),
                        "shared_keywords": sorted(intersection)[:10],
                        "suggest_merge": jaccard >= 0.8,
                    }
                )

        overlaps.sort(key=lambda x: x["score"], reverse=True)

        return json.dumps({"overlaps": overlaps[:limit]}, ensure_ascii=False)


# ═══════════════════════════════════════════════════════════════════════════
# Tool 7: merge_entities
# ═══════════════════════════════════════════════════════════════════════════


@mcp.tool()
def merge_entities(source_name: str, target_name: str, dry_run: bool = True) -> str:
    """Merge one entity into another, combining observations, relations, and task links.

    The source entity is absorbed into the target. Use dry_run=True (default) to
    preview what will be moved before committing.

    Args:
        source_name: Entity to merge FROM (will be deleted)
        target_name: Entity to merge INTO (will receive all data)
        dry_run: If True, only show what would happen without making changes
    """
    with _get_conn() as conn:
        source = conn.execute(
            "SELECT id, name FROM entities WHERE name = ?", (source_name,)
        ).fetchone()
        if not source:
            return _error(f"Source entity '{source_name}' not found")

        target = conn.execute(
            "SELECT id, name FROM entities WHERE name = ?", (target_name,)
        ).fetchone()
        if not target:
            return _error(f"Target entity '{target_name}' not found")

        src_id, tgt_id = source["id"], target["id"]

        if src_id == tgt_id:
            return _error("Source and target are the same entity")

        # Count what will be moved
        unique_obs = conn.execute(
            "SELECT COUNT(*) AS cnt FROM observations "
            "WHERE entity_id = ? AND content NOT IN "
            "(SELECT content FROM observations WHERE entity_id = ?)",
            (src_id, tgt_id),
        ).fetchone()["cnt"]

        rel_from = conn.execute(
            "SELECT COUNT(*) AS cnt FROM relations WHERE from_id = ? AND to_id != ?",
            (src_id, tgt_id),
        ).fetchone()["cnt"]

        rel_to = conn.execute(
            "SELECT COUNT(*) AS cnt FROM relations WHERE to_id = ? AND from_id != ?",
            (src_id, tgt_id),
        ).fetchone()["cnt"]

        task_links = conn.execute(
            "SELECT COUNT(*) AS cnt FROM task_entity_links "
            "WHERE entity_id = ? AND task_id NOT IN "
            "(SELECT task_id FROM task_entity_links WHERE entity_id = ?)",
            (src_id, tgt_id),
        ).fetchone()["cnt"]

        preview = {
            "source": source_name,
            "target": target_name,
            "observations_to_move": unique_obs,
            "relations_to_move": rel_from + rel_to,
            "task_links_to_move": task_links,
            "dry_run": dry_run,
        }

        if dry_run:
            return json.dumps(preview, ensure_ascii=False)

        # 1. Move unique observations
        conn.execute(
            "INSERT INTO observations (entity_id, content, created_at) "
            "SELECT ?, content, created_at FROM observations "
            "WHERE entity_id = ? AND content NOT IN "
            "(SELECT content FROM observations WHERE entity_id = ?)",
            (tgt_id, src_id, tgt_id),
        )

        # 2. Reassign relations (from_id) — skip self-loops and dupes
        from_rels = conn.execute(
            "SELECT id, to_id, relation_type FROM relations "
            "WHERE from_id = ? AND to_id != ?",
            (src_id, tgt_id),
        ).fetchall()
        for rel in from_rels:
            existing = conn.execute(
                "SELECT 1 FROM relations "
                "WHERE from_id = ? AND to_id = ? AND relation_type = ?",
                (tgt_id, rel["to_id"], rel["relation_type"]),
            ).fetchone()
            if not existing:
                conn.execute(
                    "UPDATE relations SET from_id = ? WHERE id = ?",
                    (tgt_id, rel["id"]),
                )

        # Reassign relations (to_id)
        to_rels = conn.execute(
            "SELECT id, from_id, relation_type FROM relations "
            "WHERE to_id = ? AND from_id != ?",
            (src_id, tgt_id),
        ).fetchall()
        for rel in to_rels:
            existing = conn.execute(
                "SELECT 1 FROM relations "
                "WHERE from_id = ? AND to_id = ? AND relation_type = ?",
                (rel["from_id"], tgt_id, rel["relation_type"]),
            ).fetchone()
            if not existing:
                conn.execute(
                    "UPDATE relations SET to_id = ? WHERE id = ?",
                    (tgt_id, rel["id"]),
                )

        # 3. Reassign task links
        src_links = conn.execute(
            "SELECT task_id, link_type, score, created_at "
            "FROM task_entity_links WHERE entity_id = ?",
            (src_id,),
        ).fetchall()
        merge_link_at = now_iso()
        for link in src_links:
            # The source entity is deleted below.  Preserve each old exported
            # name as a link tombstone before the FK cascade can erase it.
            record_task_entity_link_tombstone(
                conn,
                task_id=link["task_id"],
                entity_name=source["name"],
                link_type=link["link_type"],
                score=link["score"],
                created_at=link["created_at"],
                deleted_at=merge_link_at,
            )
        tgt_linked_task_ids = {
            r["task_id"]
            for r in conn.execute(
                "SELECT task_id FROM task_entity_links WHERE entity_id = ?", (tgt_id,)
            ).fetchall()
        }
        for link in src_links:
            if link["task_id"] not in tgt_linked_task_ids:
                TaskDAO.link_entity(
                    conn,
                    link["task_id"],
                    tgt_id,
                    link_type=link["link_type"],
                    score=link["score"],
                    created_at=merge_link_at,
                )

        # 4. Delete source entity (CASCADE cleans orphan observations/relations/links)
        conn.execute("DELETE FROM entities WHERE id = ?", (src_id,))

        # 5. Rebuild FTS5 for target + clean source
        _fts_sync(conn, tgt_id)
        conn.execute("DELETE FROM memory_fts WHERE rowid = ?", (src_id,))

        preview["merged"] = True
        preview["dry_run"] = False
        return json.dumps(preview, ensure_ascii=False)


# ── Entry point ──────────────────────────────────────────────────────────
def main() -> None:
    maybe_mount_premium_extensions(mcp, server_name="sqlite-entity")
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
