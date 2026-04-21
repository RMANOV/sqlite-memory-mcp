#!/usr/bin/env python3
"""Thin MCP server exposing only entity management tools.

Shares the same SQLite database as the main sqlite-kb server.
Exists because Claude Code 2.x has a tool-count limit per MCP server
(~9 tools visible out of 50), so entity tools are split into a separate server.
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
    setup_logger,
    now_iso as _now_iso,
    TaskDAO,
)
from premium_runtime import maybe_mount_premium_extensions
from schema import error as _error

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
        now = _now_iso()

        TaskDAO.link_entity(
            conn, task_id, entity_id, link_type="manual", created_at=now
        )

        return json.dumps(
            {
                "task_id": task_id,
                "entity_name": entity_name,
                "entity_id": entity_id,
                "link_type": "manual",
                "created_at": now,
            }
        )


# ═══════════════════════════════════════════════════════════════════════════
# Tool 2: unlink_task_entity
# ═══════════════════════════════════════════════════════════════════════════


@mcp.tool()
def unlink_task_entity(task_id: str, entity_name: str) -> str:
    """Remove a link between a task and a knowledge graph entity."""
    with _get_conn() as conn:
        entity_id = _get_entity_id(conn, entity_name)
        if not entity_id:
            return _error(f"Entity '{entity_name}' not found")

        removed = TaskDAO.unlink_entity(conn, task_id, entity_id)

        return json.dumps({"removed": removed > 0})


# ═══════════════════════════════════════════════════════════════════════════
# Tool 3: get_task_links
# ═══════════════════════════════════════════════════════════════════════════


@mcp.tool()
def get_task_links(task_id: str) -> str:
    """Get all knowledge graph entities linked to a task."""
    with _get_conn() as conn:
        links = TaskDAO.get_task_links(conn, task_id)
        return json.dumps({"task_id": task_id, "links": links})


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
        return json.dumps({"entity_name": entity_name, "tasks": tasks})


# ═══════════════════════════════════════════════════════════════════════════
# Tool 5: suggest_task_links
# ═══════════════════════════════════════════════════════════════════════════


@mcp.tool()
def suggest_task_links(task_id: str, limit: int = 5) -> str:
    """Suggest knowledge graph entities that may be related to a task.

    Uses FTS5 for candidate retrieval + Jaccard similarity for ranking.
    Does NOT auto-create links — returns suggestions for human/Claude review.
    """
    with _get_conn() as conn:
        task = TaskDAO.get_by_id(conn, task_id, "title, description")
        if not task:
            return _error(f"Task {task_id} not found")

        search_text = f"{task['title'] or ''} {task['description'] or ''}"
        task_tokens = _tokenize(search_text)
        if not task_tokens:
            return json.dumps({"task_id": task_id, "suggestions": []})

        fts_q = _fts_query(search_text)
        if not fts_q:
            return json.dumps({"task_id": task_id, "suggestions": []})

        candidates = conn.execute(
            "SELECT rowid, name, entity_type, rank "
            "FROM memory_fts WHERE memory_fts MATCH ? "
            "ORDER BY rank LIMIT 50",
            (fts_q,),
        ).fetchall()

        linked_ids = TaskDAO.get_linked_entity_ids(conn, task_id)

        scored = []
        for c in candidates:
            if c["rowid"] in linked_ids:
                continue

            obs = conn.execute(
                "SELECT content FROM observations WHERE entity_id = ?",
                (c["rowid"],),
            ).fetchall()
            obs_text = " ".join(o["content"] for o in obs)
            entity_tokens = _tokenize(f"{c['name']} {obs_text}")

            if not entity_tokens:
                continue

            t_tok = set(list(task_tokens)[:500])
            e_tok = set(list(entity_tokens)[:500])
            intersection = t_tok & e_tok
            union = t_tok | e_tok
            jaccard = len(intersection) / len(union) if union else 0.0

            norm_rank = min(1.0, abs(c["rank"]) / 20.0)
            combined = 0.6 * norm_rank + 0.4 * jaccard

            scored.append(
                {
                    "entity_name": c["name"],
                    "entity_type": c["entity_type"],
                    "score": round(combined, 4),
                    "shared_keywords": sorted(intersection)[:10],
                }
            )

        scored.sort(key=lambda x: x["score"], reverse=True)

        return json.dumps({"task_id": task_id, "suggestions": scored[:limit]})


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

        for src in sources:
            src_obs = conn.execute(
                "SELECT content FROM observations WHERE entity_id = ?",
                (src["id"],),
            ).fetchall()
            src_text = " ".join(o["content"] for o in src_obs)
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

            for cand in candidates:
                cand_id = cand["rowid"]
                if cand_id == src["id"]:
                    continue

                pair_key = (min(src["id"], cand_id), max(src["id"], cand_id))
                if pair_key in seen_pairs:
                    continue
                seen_pairs.add(pair_key)

                cand_obs = conn.execute(
                    "SELECT content FROM observations WHERE entity_id = ?",
                    (cand_id,),
                ).fetchall()
                cand_text = " ".join(o["content"] for o in cand_obs)
                cand_tokens = _tokenize(f"{cand['name']} {cand_text}")

                if not cand_tokens:
                    continue

                s_tok = set(list(src_tokens)[:500])
                c_tok = set(list(cand_tokens)[:500])
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

        return json.dumps({"overlaps": overlaps[:limit]})


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
            return json.dumps(preview)

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
                    created_at=link["created_at"],
                )

        # 4. Delete source entity (CASCADE cleans orphan observations/relations/links)
        conn.execute("DELETE FROM entities WHERE id = ?", (src_id,))

        # 5. Rebuild FTS5 for target + clean source
        _fts_sync(conn, tgt_id)
        conn.execute("DELETE FROM memory_fts WHERE rowid = ?", (src_id,))

        preview["merged"] = True
        preview["dry_run"] = False
        return json.dumps(preview)


# ── Entry point ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    maybe_mount_premium_extensions(mcp, server_name="sqlite-entity")
    mcp.run(transport="stdio")
